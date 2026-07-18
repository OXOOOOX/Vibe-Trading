"""Export an immutable W1/W3 portfolio-monitor soak snapshot.

The exporter deliberately copies only an allowlisted subset of the monitoring
status response.  It never reads or serializes environment variables, delivery
target identifiers, chat identifiers, database paths, or HTTP response bodies
from failed requests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


REPORT_SCHEMA_VERSION = 1
REPORT_KIND = "portfolio_monitor_soak_snapshot"
DEFAULT_BASE_URL = "http://127.0.0.1:8899"
STATUS_PATH = "/portfolio/monitoring/status"
MAX_STATUS_BYTES = 5 * 1024 * 1024
REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_STATUSES = {"pass", "fail", "insufficient_evidence"}
STAGE_DEFAULT_PROFILE_COUNTS: dict[str, int | None] = {
    "S1": 1,
    "S2": 3,
    "S3": None,
    "W3": 1,
}
STAGE_REQUIRED_COMPLETE_DAYS = {"S1": 1, "S2": 1, "S3": 3, "W3": 1}


class ReportError(RuntimeError):
    """Raised for a safe, user-facing report generation failure."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _reject_duplicate_keys(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is not allowed: {key}")
        result[key] = value
    return result


def _load_json_bytes(raw: bytes, *, source: str) -> Any:
    try:
        text = raw.decode("utf-8-sig")
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReportError(f"{source} is not valid canonicalizable JSON") from exc


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReportError("JSON value cannot be canonicalized") from exc


def hash_json_file(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReportError(f"cannot read JSON input: {path.name}") from exc
    value = _load_json_bytes(raw, source=path.name)
    canonical = canonical_json_bytes(value)
    return {
        "algorithm": "sha256-canonical-json-v1",
        "canonical_json_sha256": hashlib.sha256(canonical).hexdigest(),
        "canonical_bytes": len(canonical),
    }


def _normalize_base_url(value: str) -> str:
    text = str(value or "").strip()
    if "://" not in text:
        text = f"http://{text}"
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ReportError("--base-url must be an HTTP(S) origin")
    if parsed.username or parsed.password:
        raise ReportError("credentials are not allowed in --base-url")
    if parsed.query or parsed.fragment:
        raise ReportError("query strings and fragments are not allowed in --base-url")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
    )


def fetch_status(base_url: str, *, timeout_seconds: float = 10.0) -> tuple[str, dict[str, Any]]:
    normalized = _normalize_base_url(base_url)
    url = f"{normalized}{STATUS_PATH}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "vibe-trading-soak-report/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=max(0.1, timeout_seconds)) as response:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > MAX_STATUS_BYTES:
                raise ReportError("monitoring status response is too large")
            raw = response.read(MAX_STATUS_BYTES + 1)
    except ReportError:
        raise
    except urllib.error.HTTPError as exc:
        raise ReportError(f"monitoring status request failed with HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ReportError("monitoring status request failed") from exc
    if len(raw) > MAX_STATUS_BYTES:
        raise ReportError("monitoring status response is too large")
    value = _load_json_bytes(raw, source="monitoring status response")
    if not isinstance(value, dict):
        raise ReportError("monitoring status response must be a JSON object")
    return normalized, value


def git_metadata(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    def run(*args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        return completed.stdout.strip()

    try:
        commit = run("rev-parse", "HEAD")
        dirty = bool(run("status", "--porcelain=v1", "--untracked-files=normal"))
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "commit": None, "dirty": None}
    return {"available": True, "commit": commit, "dirty": dirty}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    return value


def _integer(value: Any) -> int | None:
    number = _number(value)
    if number is None or float(number) != int(number):
        return None
    return int(number)


def _distribution(value: Any) -> dict[str, float | int | None]:
    source = _mapping(value)
    return {key: _number(source.get(key)) for key in ("p50", "p95", "p99", "max")}


def _sanitize_blocked_reasons(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({str(item)[:160] for item in value if isinstance(item, str) and item})


def _sanitize_targets(status: dict[str, Any]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for raw in status.get("profile_health") or []:
        if not isinstance(raw, dict):
            continue
        symbol = str(raw.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        targets.append(
            {
                "symbol": symbol[:40],
                "status": str(raw.get("status") or "unknown")[:40],
                "last_quote_check_at": raw.get("last_quote_check_at"),
                "last_success_at": raw.get("last_success_at"),
                "next_quote_run_at": raw.get("next_quote_run_at"),
                "blocked_reasons": _sanitize_blocked_reasons(raw.get("blocked_reasons")),
                "input_outdated": bool(raw.get("input_outdated")),
            }
        )
    return sorted(targets, key=lambda item: item["symbol"])


def _sanitize_tick(value: Any) -> dict[str, Any] | None:
    tick = _mapping(value)
    if not tick:
        return None
    return {
        "mode": tick.get("mode"),
        "decision": tick.get("decision"),
        "started_at": tick.get("started_at"),
        "finished_at": tick.get("finished_at"),
        "duration_ms": _number(tick.get("duration_ms")),
        "due_profiles": _integer(tick.get("due_profiles")),
        "evaluated_profiles": _integer(tick.get("evaluated_profiles")),
        "blocked_profiles": _integer(tick.get("blocked_profiles")),
        "events_created": _integer(tick.get("events_created")),
        "duplicate_events": _integer(tick.get("duplicate_events")),
        "shadow_suppressed": _integer(tick.get("shadow_suppressed")),
        "schedule_lag_ms": _number(tick.get("schedule_lag_ms")),
        "closed_session_due_profiles": _integer(tick.get("closed_session_due_profiles")),
        "closed_session_backlog_lag_ms": _number(
            tick.get("closed_session_backlog_lag_ms")
        ),
        "bar_lag_ms": _number(tick.get("bar_lag_ms")),
        "error_present": bool(tick.get("error")),
    }


def _sanitize_runtime(runtime: dict[str, Any]) -> dict[str, Any]:
    calendar = _mapping(runtime.get("calendar"))
    return {
        "mode": runtime.get("mode"),
        "mode_valid": runtime.get("mode_valid"),
        "mode_reason": runtime.get("mode_reason"),
        "running": runtime.get("running"),
        "leader": runtime.get("leader"),
        "fencing_token": _integer(runtime.get("fencing_token")),
        "current_tick_started_at": runtime.get("current_tick_started_at"),
        "last_error_present": bool(runtime.get("last_error")),
        "calendar": {
            "mode": calendar.get("mode"),
            "market_date": calendar.get("market_date"),
            "is_trading_day": calendar.get("is_trading_day"),
            "session": calendar.get("session"),
            "open": calendar.get("open"),
            "checked_at": calendar.get("checked_at"),
            "error_present": bool(calendar.get("error")),
        },
        "last_tick": _sanitize_tick(runtime.get("last_tick")),
    }


def _sanitize_counters(value: Any) -> dict[str, dict[str, Any]]:
    allowed = {
        "data_blocked_rule_count",
        "duplicate_event_count",
        "duplicate_observation_count",
        "shadow_suppressed_delivery_count",
    }
    result: dict[str, dict[str, Any]] = {}
    for name, raw in _mapping(value).items():
        if name not in allowed:
            continue
        item = _mapping(raw)
        result[name] = {
            "value": _integer(item.get("value")),
            "updated_at": item.get("updated_at"),
        }
    return result


def _sanitize_runtime_health(value: Any) -> dict[str, Any]:
    health = _mapping(value)
    closed = _mapping(health.get("closed_session_backlog"))
    return {
        "window_hours": _number(health.get("window_hours")),
        "tick_count": _integer(health.get("tick_count")),
        "error_tick_count": _integer(health.get("error_tick_count")),
        "events_created": _integer(health.get("events_created")),
        "duplicate_event_count": _integer(health.get("duplicate_event_count")),
        "event_attempt_count": _integer(health.get("event_attempt_count")),
        "duplicate_event_rate": _number(health.get("duplicate_event_rate")),
        "duration_ms": _distribution(health.get("duration_ms")),
        "schedule_lag_ms": _distribution(health.get("schedule_lag_ms")),
        "closed_session_backlog": {
            "due_profile_ticks": _integer(closed.get("due_profile_ticks")),
            "lag_ms": _distribution(closed.get("lag_ms")),
        },
        "bar_lag_ms": _distribution(health.get("bar_lag_ms")),
        "database_growth_bytes": _integer(health.get("database_growth_bytes")),
        "latest_tick": _sanitize_tick(health.get("latest_tick")),
        "counters": _sanitize_counters(health.get("counters")),
    }


def _sanitize_readiness(status: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    raw = runtime.get("deliver_readiness")
    if not isinstance(raw, dict):
        raw = status.get("deliver_readiness")
    readiness = _mapping(raw)
    if not readiness:
        return {"available": False}
    allowlist = readiness.get("allowlist")
    resolved = readiness.get("resolved_profile_ids")
    return {
        "available": True,
        "ready": readiness.get("ready"),
        "blocked_reasons": _sanitize_blocked_reasons(readiness.get("blocked_reasons")),
        "allowlist_count": len(allowlist) if isinstance(allowlist, list) else None,
        "resolved_profile_count": len(resolved) if isinstance(resolved, list) else None,
        "private_test_target_configured": bool(readiness.get("test_target_id")),
        "daily_limit": _integer(readiness.get("daily_limit")),
        "max_profiles": _integer(readiness.get("max_profiles")),
        "soak_approved": readiness.get("soak_approved"),
        "callback_ready": readiness.get("callback_ready"),
        "uncertain_deliveries": _integer(readiness.get("uncertain_deliveries")),
    }


def _sanitize_maintenance(value: Any) -> dict[str, Any]:
    maintenance = _mapping(value)
    if not maintenance:
        return {"available": False}
    details = _mapping(maintenance.get("details"))
    numeric_detail_keys = (
        "database_size_before_bytes",
        "database_size_after_bytes",
        "observations_pruned",
        "runtime_ticks_pruned",
        "expired_backups_pruned",
        "observation_retention_days",
        "metric_retention_days",
        "backup_keep_days",
    )
    return {
        "available": True,
        "maintenance_id": maintenance.get("maintenance_id"),
        "kind": maintenance.get("kind"),
        "status": maintenance.get("status"),
        "started_at": maintenance.get("started_at"),
        "finished_at": maintenance.get("finished_at"),
        "error_present": bool(maintenance.get("error")),
        "backup_created": bool(details.get("backup_path")),
        "details": {key: _integer(details.get(key)) for key in numeric_detail_keys},
    }


def _storage_snapshot(status: dict[str, Any], health: dict[str, Any]) -> dict[str, Any]:
    size = _integer(status.get("database_size_bytes"))
    maximum = _integer(status.get("database_max_bytes"))
    utilization = _number(status.get("database_utilization"))
    growth = _integer(health.get("database_growth_bytes"))
    window_hours = _number(health.get("window_hours"))
    projected_bytes: int | None = None
    projected_utilization: float | None = None
    if (
        size is not None
        and maximum is not None
        and maximum > 0
        and growth is not None
        and window_hours is not None
        and window_hours >= 24
    ):
        daily_growth = max(0.0, float(growth) * 24.0 / float(window_hours))
        projected_bytes = int(math.ceil(size + daily_growth * 30.0))
        projected_utilization = round(projected_bytes / maximum, 6)
    return {
        "profiles": _integer(status.get("profiles")),
        "active_profiles": _integer(status.get("active_profiles")),
        "events": _integer(status.get("events")),
        "pending_deliveries": _integer(status.get("pending_deliveries")),
        "uncertain_deliveries": _integer(status.get("uncertain_deliveries")),
        "shadow_suppressed_deliveries": _integer(
            status.get("shadow_suppressed_deliveries")
        ),
        "blocked_profiles": _integer(status.get("blocked_profiles")),
        "database_size_bytes": size,
        "database_max_bytes": maximum,
        "database_utilization": utilization,
        "database_growth_bytes": growth,
        "database_projected_30d_bytes": projected_bytes,
        "database_projected_30d_utilization": projected_utilization,
        "delivery_status_counts": {
            str(key): _integer(value)
            for key, value in _mapping(status.get("delivery_status_counts")).items()
            if _integer(value) is not None
        },
        "observation_status_counts": {
            str(key): _integer(value)
            for key, value in _mapping(status.get("observation_status_counts")).items()
            if _integer(value) is not None
        },
    }


def _sanitize_soak_evidence(value: Any) -> dict[str, Any]:
    """Allowlist the optional independent/coverage evidence contract.

    Current deployments do not yet expose this block.  Missing fields therefore
    stay ``insufficient_evidence`` instead of being guessed from a status
    snapshot.
    """

    evidence = _mapping(value)
    integer_keys = (
        "complete_trading_days",
        "consecutive_complete_trading_days",
        "independent_feishu_outbound_count",
        "single_leader_incident_count",
        "calendar_evaluation_violation_count",
        "due_profile_outcome_mismatch_count",
        "data_gate_event_violation_count",
        "stale_data_trigger_count",
        "eligible_evaluation_count",
        "actionable_evaluation_count",
        "unknown_reason_code_count",
        "shortest_check_interval_ms",
        "leader_lease_ms",
        "missing_delivery_receipt_count",
        "duplicate_remote_message_count",
        "daily_limit_violation_count",
        "non_allowlisted_delivery_count",
    )
    return {
        "available": bool(evidence),
        **{key: _integer(evidence.get(key)) for key in integer_keys},
        "schedule_plus_duration_ms": _distribution(
            evidence.get("schedule_plus_duration_ms")
        ),
    }


def _gate(
    gate_id: str,
    *,
    criterion: str,
    status: str,
    observed: Any = None,
    reason_code: str | None = None,
) -> dict[str, Any]:
    if status not in GATE_STATUSES:
        raise ValueError(f"invalid gate status: {status}")
    result = {
        "gate_id": gate_id,
        "criterion": criterion,
        "status": status,
        "observed": observed,
    }
    if reason_code:
        result["reason_code"] = reason_code
    return result


def _zero_gate(gate_id: str, criterion: str, value: int | None) -> dict[str, Any]:
    if value is None:
        return _gate(
            gate_id,
            criterion=criterion,
            status="insufficient_evidence",
            reason_code="metric_not_available",
        )
    return _gate(
        gate_id,
        criterion=criterion,
        status="pass" if value == 0 else "fail",
        observed=value,
        reason_code=None if value == 0 else "non_zero_violation_count",
    )


def _hash_unchanged_gate(
    label: str,
    current: dict[str, Any] | None,
    baseline: dict[str, Any] | None,
) -> dict[str, Any]:
    current_hash = _mapping(current).get("canonical_json_sha256")
    baseline_hash = _mapping(baseline).get("canonical_json_sha256")
    if not current_hash or not baseline_hash:
        return _gate(
            f"{label}_unchanged",
            criterion=f"{label} canonical JSON hash is unchanged from the baseline report",
            status="insufficient_evidence",
            observed={"current_present": bool(current_hash), "baseline_present": bool(baseline_hash)},
            reason_code="before_after_hash_pair_required",
        )
    matches = current_hash == baseline_hash
    return _gate(
        f"{label}_unchanged",
        criterion=f"{label} canonical JSON hash is unchanged from the baseline report",
        status="pass" if matches else "fail",
        observed={"matches_baseline": matches},
        reason_code=None if matches else "canonical_hash_changed",
    )


def evaluate_gates(
    *,
    stage: str,
    expected_profile_count: int | None,
    effective_mode: Any,
    runtime: dict[str, Any],
    health: dict[str, Any],
    storage: dict[str, Any],
    readiness: dict[str, Any],
    maintenance: dict[str, Any],
    evidence: dict[str, Any],
    input_hashes: dict[str, Any],
    baseline_hashes: dict[str, Any],
) -> list[dict[str, Any]]:
    gates: list[dict[str, Any]] = []
    required_mode = "deliver" if stage == "W3" else "shadow"
    gates.append(
        _gate(
            "effective_mode",
            criterion=f"effective monitoring mode is {required_mode}",
            status="pass" if effective_mode == required_mode else "fail",
            observed=effective_mode,
            reason_code=None if effective_mode == required_mode else "unexpected_effective_mode",
        )
    )

    running = runtime.get("running")
    leader = runtime.get("leader")
    if isinstance(running, bool) and isinstance(leader, bool):
        gates.append(
            _gate(
                "current_runtime_leader",
                criterion="runtime is running and this instance currently holds the leader lease",
                status="pass" if running and leader else "fail",
                observed={"running": running, "leader": leader},
                reason_code=None if running and leader else "runtime_not_active_leader",
            )
        )
    else:
        gates.append(
            _gate(
                "current_runtime_leader",
                criterion="runtime is running and this instance currently holds the leader lease",
                status="insufficient_evidence",
                reason_code="runtime_leader_state_not_available",
            )
        )

    active_profiles = storage.get("active_profiles")
    if expected_profile_count is None:
        gates.append(
            _gate(
                "active_profile_count",
                criterion="active profile count equals the stage's measured safe limit",
                status="insufficient_evidence",
                observed=active_profiles,
                reason_code="expected_profile_count_required_for_stage",
            )
        )
    elif active_profiles is None:
        gates.append(
            _gate(
                "active_profile_count",
                criterion=f"active profile count equals {expected_profile_count}",
                status="insufficient_evidence",
                reason_code="active_profile_count_not_available",
            )
        )
    else:
        matches = active_profiles == expected_profile_count
        gates.append(
            _gate(
                "active_profile_count",
                criterion=f"active profile count equals {expected_profile_count}",
                status="pass" if matches else "fail",
                observed=active_profiles,
                reason_code=None if matches else "unexpected_active_profile_count",
            )
        )

    required_days = STAGE_REQUIRED_COMPLETE_DAYS[stage]
    complete_days = evidence.get("complete_trading_days")
    consecutive_days = evidence.get("consecutive_complete_trading_days")
    observed_days = consecutive_days if stage == "S3" else complete_days
    if observed_days is None or observed_days < required_days:
        gates.append(
            _gate(
                "complete_trading_day_coverage",
                criterion=(
                    f"at least {required_days} "
                    f"{'consecutive ' if stage == 'S3' else ''}complete trading day(s) are covered"
                ),
                status="insufficient_evidence",
                observed={
                    "complete_trading_days": complete_days,
                    "consecutive_complete_trading_days": consecutive_days,
                },
                reason_code="complete_trading_day_coverage_not_proven",
            )
        )
    else:
        gates.append(
            _gate(
                "complete_trading_day_coverage",
                criterion=(
                    f"at least {required_days} "
                    f"{'consecutive ' if stage == 'S3' else ''}complete trading day(s) are covered"
                ),
                status="pass",
                observed=observed_days,
            )
        )

    tick_count = health.get("tick_count")
    error_ticks = health.get("error_tick_count")
    if tick_count is None or tick_count <= 0 or error_ticks is None:
        gates.append(
            _gate(
                "error_ticks_zero",
                criterion="runtime error tick count is zero over a non-empty observation window",
                status="insufficient_evidence",
                observed={"tick_count": tick_count, "error_tick_count": error_ticks},
                reason_code="non_empty_runtime_window_required",
            )
        )
    else:
        gates.append(
            _gate(
                "error_ticks_zero",
                criterion="runtime error tick count is zero over a non-empty observation window",
                status="pass" if error_ticks == 0 else "fail",
                observed={"tick_count": tick_count, "error_tick_count": error_ticks},
                reason_code=None if error_ticks == 0 else "runtime_error_ticks_observed",
            )
        )
    gates.append(
        _zero_gate(
            "duplicate_confirmed_events_zero",
            "duplicate confirmed event count is zero",
            health.get("duplicate_event_count"),
        )
    )
    gates.append(
        _zero_gate(
            "pending_deliveries_zero",
            "pending delivery count is zero",
            storage.get("pending_deliveries"),
        )
    )
    gates.append(
        _zero_gate(
            "uncertain_deliveries_zero",
            "unexplained delivery_uncertain count is zero",
            storage.get("uncertain_deliveries"),
        )
    )
    gates.append(
        _zero_gate(
            "single_leader_incidents_zero",
            "independent full-window dual-leader incident count is zero",
            evidence.get("single_leader_incident_count"),
        )
    )
    gates.append(
        _zero_gate(
            "calendar_evaluation_violations_zero",
            "closed, lunch, and non-trading sessions never evaluate market rules",
            evidence.get("calendar_evaluation_violation_count"),
        )
    )
    gates.append(
        _zero_gate(
            "due_profile_accounting_mismatches_zero",
            "every due supported profile has exactly one evaluated or blocked outcome",
            evidence.get("due_profile_outcome_mismatch_count"),
        )
    )
    gates.append(
        _zero_gate(
            "data_gate_event_violations_zero",
            "non-actionable or stale data never creates a trading event",
            evidence.get("data_gate_event_violation_count"),
        )
    )
    gates.append(
        _zero_gate(
            "stale_data_triggers_zero",
            "data beyond the freshness limit triggers zero trading events",
            evidence.get("stale_data_trigger_count"),
        )
    )

    if stage != "W3":
        gates.append(
            _zero_gate(
                "independent_feishu_outbound_zero",
                "independently observed Feishu outbound count is zero in shadow mode",
                evidence.get("independent_feishu_outbound_count"),
            )
        )

    gates.append(
        _hash_unchanged_gate(
            "portfolio_state",
            _mapping(input_hashes.get("portfolio_state")),
            _mapping(baseline_hashes.get("portfolio_state")),
        )
    )
    gates.append(
        _hash_unchanged_gate(
            "daily_run_input",
            _mapping(input_hashes.get("daily_run_input")),
            _mapping(baseline_hashes.get("daily_run_input")),
        )
    )

    combined = _mapping(evidence.get("schedule_plus_duration_ms"))
    p95 = _number(combined.get("p95"))
    p99 = _number(combined.get("p99"))
    shortest_interval = _number(evidence.get("shortest_check_interval_ms"))
    if p95 is None or p99 is None or shortest_interval is None or shortest_interval <= 0:
        gates.append(
            _gate(
                "schedule_plus_duration_budget",
                criterion="P95 is at most 20% and P99 at most 50% of the shortest check interval",
                status="insufficient_evidence",
                observed={"p95_ms": p95, "p99_ms": p99, "shortest_interval_ms": shortest_interval},
                reason_code="combined_latency_distribution_not_available",
            )
        )
    else:
        passes = p95 <= shortest_interval * 0.20 and p99 <= shortest_interval * 0.50
        gates.append(
            _gate(
                "schedule_plus_duration_budget",
                criterion="P95 is at most 20% and P99 at most 50% of the shortest check interval",
                status="pass" if passes else "fail",
                observed={"p95_ms": p95, "p99_ms": p99, "shortest_interval_ms": shortest_interval},
                reason_code=None if passes else "runtime_latency_budget_exceeded",
            )
        )

    duration_max = _number(_mapping(health.get("duration_ms")).get("max"))
    lease_ms = _number(evidence.get("leader_lease_ms"))
    if duration_max is None or lease_ms is None or lease_ms <= 0:
        gates.append(
            _gate(
                "duration_below_half_lease",
                criterion="maximum tick duration is below 50% of the leader lease",
                status="insufficient_evidence",
                observed={"duration_max_ms": duration_max, "leader_lease_ms": lease_ms},
                reason_code="leader_lease_or_duration_not_available",
            )
        )
    else:
        passes = duration_max < lease_ms * 0.50
        gates.append(
            _gate(
                "duration_below_half_lease",
                criterion="maximum tick duration is below 50% of the leader lease",
                status="pass" if passes else "fail",
                observed={"duration_max_ms": duration_max, "leader_lease_ms": lease_ms},
                reason_code=None if passes else "tick_duration_too_close_to_lease",
            )
        )

    eligible = evidence.get("eligible_evaluation_count")
    actionable = evidence.get("actionable_evaluation_count")
    unknown_reasons = evidence.get("unknown_reason_code_count")
    if eligible is None or actionable is None or eligible <= 0 or unknown_reasons is None:
        gates.append(
            _gate(
                "actionable_data_availability",
                criterion="actionable availability is at least 95% and every unavailable result has a reason code",
                status="insufficient_evidence",
                observed={"eligible": eligible, "actionable": actionable, "unknown_reason_codes": unknown_reasons},
                reason_code="eligible_actionable_counts_not_available",
            )
        )
    else:
        rate = actionable / eligible
        passes = rate >= 0.95 and unknown_reasons == 0
        gates.append(
            _gate(
                "actionable_data_availability",
                criterion="actionable availability is at least 95% and every unavailable result has a reason code",
                status="pass" if passes else "fail",
                observed={
                    "eligible": eligible,
                    "actionable": actionable,
                    "availability_rate": round(rate, 6),
                    "unknown_reason_codes": unknown_reasons,
                },
                reason_code=None if passes else "actionable_availability_gate_failed",
            )
        )

    utilization = _number(storage.get("database_utilization"))
    if utilization is None:
        gates.append(
            _gate(
                "database_current_utilization",
                criterion="current monitoring database utilization is below 80%",
                status="insufficient_evidence",
                reason_code="database_utilization_not_available",
            )
        )
    else:
        gates.append(
            _gate(
                "database_current_utilization",
                criterion="current monitoring database utilization is below 80%",
                status="pass" if utilization < 0.80 else "fail",
                observed=utilization,
                reason_code=None if utilization < 0.80 else "database_capacity_emergency_threshold_reached",
            )
        )
    projected = _number(storage.get("database_projected_30d_utilization"))
    if projected is None:
        gates.append(
            _gate(
                "database_projected_30d_utilization",
                criterion="30-day projected database utilization is below 50%",
                status="insufficient_evidence",
                reason_code="full_day_database_growth_not_available",
            )
        )
    else:
        gates.append(
            _gate(
                "database_projected_30d_utilization",
                criterion="30-day projected database utilization is below 50%",
                status="pass" if projected < 0.50 else "fail",
                observed=projected,
                reason_code=None if projected < 0.50 else "database_growth_projection_exceeds_gate",
            )
        )

    maintenance_available = maintenance.get("available") is True
    maintenance_completed = maintenance.get("status") == "completed"
    backup_created = maintenance.get("backup_created") is True
    if not maintenance_available:
        gates.append(
            _gate(
                "maintenance_backup_success",
                criterion="latest maintenance completed and created an online backup",
                status="insufficient_evidence",
                reason_code="maintenance_record_not_available",
            )
        )
    else:
        passes = maintenance_completed and backup_created and not maintenance.get("error_present")
        gates.append(
            _gate(
                "maintenance_backup_success",
                criterion="latest maintenance completed and created an online backup",
                status="pass" if passes else "fail",
                observed={
                    "status": maintenance.get("status"),
                    "backup_created": backup_created,
                    "error_present": maintenance.get("error_present"),
                },
                reason_code=None if passes else "maintenance_or_backup_failed",
            )
        )

    if stage == "W3":
        ready = readiness.get("ready") if readiness.get("available") else None
        if not isinstance(ready, bool):
            gates.append(
                _gate(
                    "deliver_readiness",
                    criterion="deliver readiness is explicitly ready",
                    status="insufficient_evidence",
                    reason_code="deliver_readiness_not_available",
                )
            )
        else:
            gates.append(
                _gate(
                    "deliver_readiness",
                    criterion="deliver readiness is explicitly ready",
                    status="pass" if ready else "fail",
                    observed={
                        "ready": ready,
                        "blocked_reasons": readiness.get("blocked_reasons", []),
                    },
                    reason_code=None if ready else "deliver_readiness_failed",
                )
            )
        gates.extend(
            [
                _zero_gate(
                    "missing_delivery_receipts_zero",
                    "every accepted canary delivery has a persisted provider receipt",
                    evidence.get("missing_delivery_receipt_count"),
                ),
                _zero_gate(
                    "duplicate_remote_messages_zero",
                    "duplicate visible remote messages are zero",
                    evidence.get("duplicate_remote_message_count"),
                ),
                _zero_gate(
                    "daily_limit_violations_zero",
                    "daily delivery limit violations are zero",
                    evidence.get("daily_limit_violation_count"),
                ),
                _zero_gate(
                    "non_allowlisted_deliveries_zero",
                    "non-allowlisted profile deliveries are zero",
                    evidence.get("non_allowlisted_delivery_count"),
                ),
            ]
        )
    return gates


def _load_baseline(path: Path | None) -> tuple[dict[str, Any], dict[str, Any]]:
    if path is None:
        return {}, {"available": False}
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReportError(f"cannot read baseline report: {path.name}") from exc
    value = _load_json_bytes(raw, source=path.name)
    if not isinstance(value, dict) or value.get("report_kind") != REPORT_KIND:
        raise ReportError("--baseline-report must be a portfolio monitor soak report")
    canonical = canonical_json_bytes(value)
    hashes = _mapping(value.get("input_hashes"))
    return hashes, {
        "available": True,
        "report_sha256": hashlib.sha256(canonical).hexdigest(),
        "captured_at_utc": value.get("captured_at_utc"),
    }


def build_report(
    status: dict[str, Any],
    *,
    source_base_url: str,
    stage: str,
    expected_profile_count: int | None,
    input_hashes: dict[str, Any] | None = None,
    baseline_hashes: dict[str, Any] | None = None,
    baseline_reference: dict[str, Any] | None = None,
    git: dict[str, Any] | None = None,
    captured_at_utc: str | None = None,
) -> dict[str, Any]:
    if stage not in STAGE_DEFAULT_PROFILE_COUNTS:
        raise ReportError(f"unsupported soak stage: {stage}")
    runtime_raw = _mapping(status.get("runtime"))
    runtime = _sanitize_runtime(runtime_raw)
    health = _sanitize_runtime_health(status.get("runtime_health"))
    readiness = _sanitize_readiness(status, runtime_raw)
    maintenance = _sanitize_maintenance(status.get("maintenance"))
    storage = _storage_snapshot(status, health)
    evidence = _sanitize_soak_evidence(status.get("soak_coverage"))
    hashes = input_hashes or {}
    baseline = baseline_hashes or {}
    gates = evaluate_gates(
        stage=stage,
        expected_profile_count=expected_profile_count,
        effective_mode=status.get("effective_mode"),
        runtime=runtime,
        health=health,
        storage=storage,
        readiness=readiness,
        maintenance=maintenance,
        evidence=evidence,
        input_hashes=hashes,
        baseline_hashes=baseline,
    )
    counts = {
        gate_status: sum(1 for gate in gates if gate["status"] == gate_status)
        for gate_status in ("pass", "fail", "insufficient_evidence")
    }
    if counts["fail"]:
        overall = "fail"
    elif counts["insufficient_evidence"]:
        overall = "insufficient_evidence"
    else:
        overall = "pass"
    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "report_kind": REPORT_KIND,
        "captured_at_utc": captured_at_utc or utc_now(),
        "source": {
            "base_url": source_base_url,
            "status_endpoint": STATUS_PATH,
        },
        "git": git or git_metadata(),
        "stage": {
            "name": stage,
            "expected_profile_count": expected_profile_count,
            "required_complete_trading_days": STAGE_REQUIRED_COMPLETE_DAYS[stage],
        },
        "effective_mode": status.get("effective_mode"),
        "targets": _sanitize_targets(status),
        "runtime": runtime,
        "runtime_health": health,
        "deliver_readiness": readiness,
        "maintenance": maintenance,
        "storage": storage,
        "soak_coverage": evidence,
        "input_hashes": hashes,
        "baseline_report": baseline_reference or {"available": False},
        "gate_summary": {"overall_status": overall, **counts},
        "gates": gates,
    }


def atomic_write_json(path: Path, value: Any, *, force: bool = False) -> None:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not force:
        raise ReportError(f"output already exists: {destination.name}; use --force to replace it")
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if force:
            os.replace(temporary, destination)
        else:
            try:
                os.link(temporary, destination)
            except FileExistsError as exc:
                raise ReportError(
                    f"output already exists: {destination.name}; use --force to replace it"
                ) from exc
            temporary.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export an immutable portfolio-monitor W1/W3 soak snapshot JSON report."
    )
    parser.add_argument("--output", type=Path, required=True, help="Destination JSON path")
    parser.add_argument("--force", action="store_true", help="Atomically replace an existing report")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout-seconds", type=_positive_float, default=10.0)
    parser.add_argument("--stage", choices=tuple(STAGE_DEFAULT_PROFILE_COUNTS), default="S1")
    parser.add_argument(
        "--expected-profile-count",
        type=_positive_int,
        help="Expected exact active profile count; required to prove the S3 capacity gate",
    )
    parser.add_argument("--portfolio-state", type=Path)
    parser.add_argument("--daily-run-input", type=Path)
    parser.add_argument(
        "--baseline-report",
        type=Path,
        help="Earlier immutable report whose input hashes are the before-state",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        output = args.output.expanduser().resolve()
        if output.exists() and not args.force:
            raise ReportError(f"output already exists: {output.name}; use --force to replace it")
        normalized_url, status = fetch_status(
            args.base_url,
            timeout_seconds=args.timeout_seconds,
        )
        input_hashes: dict[str, Any] = {}
        if args.portfolio_state:
            input_hashes["portfolio_state"] = hash_json_file(args.portfolio_state)
        if args.daily_run_input:
            input_hashes["daily_run_input"] = hash_json_file(args.daily_run_input)
        baseline_hashes, baseline_reference = _load_baseline(args.baseline_report)
        expected_profiles = args.expected_profile_count
        if expected_profiles is None:
            expected_profiles = STAGE_DEFAULT_PROFILE_COUNTS[args.stage]
        report = build_report(
            status,
            source_base_url=normalized_url,
            stage=args.stage,
            expected_profile_count=expected_profiles,
            input_hashes=input_hashes,
            baseline_hashes=baseline_hashes,
            baseline_reference=baseline_reference,
        )
        atomic_write_json(output, report, force=args.force)
    except ReportError as exc:
        print(f"soak report error: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {report['gate_summary']['overall_status']} soak report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
