from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.portfolio_monitor_soak_report import (
    REPORT_KIND,
    ReportError,
    atomic_write_json,
    build_report,
    canonical_json_bytes,
    hash_json_file,
)


def _status(*, with_coverage: bool = True) -> dict:
    value = {
        "enabled_by_config": True,
        "effective_mode": "shadow",
        "runtime": {
            "mode": "shadow",
            "mode_valid": True,
            "running": True,
            "leader": True,
            "fencing_token": 4,
            "owner_id": "owner-secret-not-for-report",
            "last_error": None,
            "calendar": {
                "mode": "exchange_calendar",
                "market_date": "2026-07-15",
                "is_trading_day": True,
                "session": "closed",
                "open": False,
            },
            "deliver_readiness": {
                "ready": False,
                "blocked_reasons": ["shadow_soak_not_approved"],
                "allowlist": ["profile-secret"],
                "resolved_profile_ids": ["profile-secret"],
                "test_target_id": "chat-secret",
                "daily_limit": 5,
                "max_profiles": 1,
                "soak_approved": False,
                "callback_ready": True,
                "uncertain_deliveries": 0,
            },
            "api_key": "env-secret-value",
        },
        "profiles": 1,
        "active_profiles": 1,
        "events": 2,
        "pending_deliveries": 0,
        "uncertain_deliveries": 0,
        "shadow_suppressed_deliveries": 2,
        "blocked_profiles": 0,
        "database_path": "C:/private/monitoring.sqlite3",
        "database_size_bytes": 1_000,
        "database_max_bytes": 1_000_000,
        "database_utilization": 0.001,
        "delivery_status_counts": {"shadow_suppressed": 2},
        "observation_status_counts": {"verified": 100},
        "profile_health": [
            {
                "symbol": "588870.SH",
                "status": "active",
                "last_quote_check_at": "2026-07-15T07:00:00Z",
                "last_success_at": "2026-07-15T07:00:00Z",
                "next_quote_run_at": "2026-07-15T07:05:00Z",
                "blocked_reasons": [],
                "input_outdated": False,
                "delivery_target_id": "target-secret",
            }
        ],
        "runtime_health": {
            "window_hours": 24,
            "tick_count": 500,
            "error_tick_count": 0,
            "events_created": 2,
            "duplicate_event_count": 0,
            "event_attempt_count": 2,
            "duplicate_event_rate": 0.0,
            "duration_ms": {"p50": 20, "p95": 40, "p99": 50, "max": 1000},
            "schedule_lag_ms": {"p50": 5, "p95": 10, "p99": 20, "max": 30},
            "closed_session_backlog": {
                "due_profile_ticks": 2,
                "lag_ms": {"p50": 10, "p95": 20, "p99": 30, "max": 40},
            },
            "bar_lag_ms": {"p50": 100, "p95": 200, "p99": 300, "max": 400},
            "database_growth_bytes": 100,
            "counters": {
                "duplicate_observation_count": {"value": 0, "updated_at": "2026-07-15T07:00:00Z"},
                "unknown_secret_counter": {"value": 123, "updated_at": "secret"},
            },
        },
        "maintenance": {
            "maintenance_id": "maintenance-1",
            "kind": "daily",
            "status": "completed",
            "started_at": "2026-07-15T01:00:00Z",
            "finished_at": "2026-07-15T01:01:00Z",
            "error": None,
            "details": {
                "backup_path": "C:/private/backups/monitoring.sqlite3",
                "database_size_before_bytes": 900,
                "database_size_after_bytes": 1_000,
                "runtime_ticks_pruned": 0,
            },
        },
    }
    if with_coverage:
        value["soak_coverage"] = {
            "complete_trading_days": 1,
            "consecutive_complete_trading_days": 1,
            "independent_feishu_outbound_count": 0,
            "single_leader_incident_count": 0,
            "calendar_evaluation_violation_count": 0,
            "due_profile_outcome_mismatch_count": 0,
            "data_gate_event_violation_count": 0,
            "stale_data_trigger_count": 0,
            "eligible_evaluation_count": 100,
            "actionable_evaluation_count": 97,
            "unknown_reason_code_count": 0,
            "shortest_check_interval_ms": 60_000,
            "leader_lease_ms": 90_000,
            "schedule_plus_duration_ms": {"p50": 2_000, "p95": 10_000, "p99": 20_000, "max": 25_000},
        }
    return value


def _hash(value: dict) -> dict:
    canonical = canonical_json_bytes(value)
    return {
        "algorithm": "sha256-canonical-json-v1",
        "canonical_json_sha256": hashlib.sha256(canonical).hexdigest(),
        "canonical_bytes": len(canonical),
    }


def test_canonical_hash_ignores_json_key_order_and_whitespace(tmp_path: Path) -> None:
    left = tmp_path / "left.json"
    right = tmp_path / "right.json"
    left.write_text('{"b": 2, "a": [1, {"x": "中"}]}', encoding="utf-8")
    right.write_text('{\n  "a": [1,{"x":"中"}],\n  "b":2\n}', encoding="utf-8")

    assert hash_json_file(left) == hash_json_file(right)


@pytest.mark.parametrize("content", ['{"a":1,"a":2}', '{"a":NaN}'])
def test_hash_rejects_ambiguous_or_non_finite_json(tmp_path: Path, content: str) -> None:
    source = tmp_path / "invalid.json"
    source.write_text(content, encoding="utf-8")

    with pytest.raises(ReportError):
        hash_json_file(source)


def test_incomplete_trading_day_never_passes_and_secrets_are_not_serialized() -> None:
    current = {
        "portfolio_state": _hash({"holdings": []}),
        "daily_run_input": _hash({"date": "2026-07-15"}),
    }
    report = build_report(
        _status(with_coverage=False),
        source_base_url="http://127.0.0.1:8899",
        stage="S1",
        expected_profile_count=1,
        input_hashes=current,
        baseline_hashes=current,
        git={"available": True, "commit": "a" * 40, "dirty": True},
        captured_at_utc="2026-07-15T08:00:00Z",
    )

    coverage_gate = next(
        gate for gate in report["gates"] if gate["gate_id"] == "complete_trading_day_coverage"
    )
    assert coverage_gate["status"] == "insufficient_evidence"
    assert report["gate_summary"]["overall_status"] == "insufficient_evidence"
    serialized = json.dumps(report, ensure_ascii=False)
    for secret in (
        "env-secret-value",
        "owner-secret-not-for-report",
        "profile-secret",
        "chat-secret",
        "target-secret",
        "C:/private/monitoring.sqlite3",
        "C:/private/backups/monitoring.sqlite3",
    ):
        assert secret not in serialized


def test_complete_w1_evidence_can_pass_all_gates() -> None:
    current = {
        "portfolio_state": _hash({"holdings": []}),
        "daily_run_input": _hash({"date": "2026-07-15"}),
    }
    report = build_report(
        _status(),
        source_base_url="http://127.0.0.1:8899",
        stage="S1",
        expected_profile_count=1,
        input_hashes=current,
        baseline_hashes=current,
        git={"available": True, "commit": "a" * 40, "dirty": False},
    )

    assert report["gate_summary"] == {
        "overall_status": "pass",
        "pass": len(report["gates"]),
        "fail": 0,
        "insufficient_evidence": 0,
    }


def test_observed_violation_makes_report_fail() -> None:
    status = _status()
    status["runtime_health"]["error_tick_count"] = 1
    current = {
        "portfolio_state": _hash({}),
        "daily_run_input": _hash({}),
    }

    report = build_report(
        status,
        source_base_url="http://127.0.0.1:8899",
        stage="S1",
        expected_profile_count=1,
        input_hashes=current,
        baseline_hashes=current,
    )

    assert report["gate_summary"]["overall_status"] == "fail"
    assert next(g for g in report["gates"] if g["gate_id"] == "error_ticks_zero")["status"] == "fail"


def test_atomic_report_write_refuses_overwrite_unless_forced(tmp_path: Path) -> None:
    destination = tmp_path / "immutable.json"
    atomic_write_json(destination, {"report_kind": REPORT_KIND, "value": 1})

    with pytest.raises(ReportError):
        atomic_write_json(destination, {"report_kind": REPORT_KIND, "value": 2})
    assert json.loads(destination.read_text(encoding="utf-8"))["value"] == 1

    atomic_write_json(destination, {"report_kind": REPORT_KIND, "value": 2}, force=True)
    assert json.loads(destination.read_text(encoding="utf-8"))["value"] == 2
    assert not list(tmp_path.glob("*.tmp"))
