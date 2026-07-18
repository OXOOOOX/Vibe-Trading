"""Application service for monitor planning and lifecycle operations."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.portfolio.state import load_state, normalize_symbol
from src.usage import UsageRecorder, UsageStore, bind_usage_recorder

from .planner import MonitoringPlanner
from .models import PlanValidationError
from .evidence import AutonomousEvidenceCollector
from .report_catalog import MonitorReportCatalog
from .report_planner import ReportDrivenMonitoringPlanner
from .store import MonitoringStore


_REFRESHABLE_BLOCK_REASONS = {
    "verified_quote_missing",
    "raw_price_basis_unavailable",
    "quote_provenance_missing",
    "verified_price_missing",
}


def _needs_source_refresh(blocked_reasons: list[str]) -> bool:
    return any(
        reason in _REFRESHABLE_BLOCK_REASONS or reason.startswith("quote_not_actionable:")
        for reason in blocked_reasons
    )


def _holding_hash(holding: dict[str, Any]) -> str:
    selected = {
        key: holding.get(key)
        for key in ("symbol", "code", "name", "quantity", "cost_price", "updated_at")
    }
    return hashlib.sha256(json.dumps(selected, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


class MonitoringService:
    def __init__(
        self,
        *,
        store: MonitoringStore | None = None,
        planner: MonitoringPlanner | None = None,
        report_catalog: MonitorReportCatalog | None = None,
        report_planner: ReportDrivenMonitoringPlanner | None = None,
        planner_executor: ThreadPoolExecutor | None = None,
        evidence_collector: AutonomousEvidenceCollector | None = None,
        deep_report_service: Any | None = None,
        auto_deep_report_submitter: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        usage_store: UsageStore | None = None,
    ) -> None:
        custom_store = store is not None
        self.store = store or MonitoringStore()
        self.usage_store = usage_store or UsageStore(
            self.store.path.with_name("sessions.db") if custom_store else None
        )
        self.planner = planner or MonitoringPlanner()
        self.report_catalog = report_catalog or MonitorReportCatalog(
            store=self.store,
            deep_report_service=deep_report_service,
        )
        self.report_planner = report_planner or ReportDrivenMonitoringPlanner(
            market_planner=self.planner
        )
        self.evidence_collector = evidence_collector or AutonomousEvidenceCollector()
        self.auto_deep_report_submitter = auto_deep_report_submitter
        self._planner_executor = planner_executor or ThreadPoolExecutor(
            max_workers=max(1, min(4, int(os.getenv("VIBE_TRADING_MONITOR_PLANNER_WORKERS", "2")))),
            thread_name_prefix="monitor-planner",
        )
        self._planner_futures: dict[str, Future[Any]] = {}
        self._planner_lock = threading.Lock()
        self._autopilot_lock = threading.Lock()
        self._last_autopilot_tick = 0.0
        self._evidence_probe_buckets: dict[str, str] = {}
        for job_id in self.store.recover_planner_jobs():
            job = self.store.get_planner_job(job_id)
            if (
                job
                and str(job.get("activation_mode") or "manual") == "autonomous"
                and not all(
                    self._autopilot_authorized(str(symbol))
                    for symbol in job.get("requested_symbols") or []
                )
            ):
                try:
                    self.store.cancel_planner_job(job_id)
                except RuntimeError:
                    pass
                trigger_id = str(job.get("autopilot_trigger_id") or "")
                if trigger_id:
                    self.store.update_autopilot_trigger(
                        trigger_id,
                        status="cancelled",
                        error="autopilot_symbol_not_selected",
                    )
                continue
            self._submit_planner_job(job_id)

    @staticmethod
    def _holding(symbol: str) -> dict[str, Any] | None:
        target = normalize_symbol(symbol).upper()
        return next(
            (
                item for item in load_state().holdings
                if normalize_symbol(str(item.get("symbol") or item.get("code") or "")).upper() == target
            ),
            None,
        )

    def _autopilot_authorized(
        self,
        symbol: str,
        *,
        config: dict[str, Any] | None = None,
    ) -> bool:
        normalized = normalize_symbol(symbol).upper()
        current = config or self.store.get_autopilot_config()
        if not current.get("enabled"):
            return False
        if normalized not in set(current.get("selected_symbols") or []):
            return False
        if not normalized.endswith((".SH", ".SZ", ".BJ")):
            return False
        holding = self._holding(normalized)
        return bool(holding and (self._valid_number(holding.get("quantity")) or 0) > 0)

    @staticmethod
    def _auto_deep_report_enabled() -> bool:
        deep_enabled = os.getenv("VIBE_TRADING_DEEP_REPORT_ENABLED", "0").strip().lower()
        auto_enabled = os.getenv(
            "VIBE_TRADING_MONITOR_AUTO_DEEP_REPORT_ENABLED",
            "0",
        ).strip().lower()
        return (
            deep_enabled not in {"0", "false", "no", "off"}
            and auto_enabled in {"1", "true", "yes", "on"}
        )

    def _maybe_queue_auto_deep_report(
        self,
        *,
        autonomous: bool,
        job_id: str,
        symbol: str,
        holding: dict[str, Any],
        selected: dict[str, Any],
        research_reasons: list[str],
        research_date: str,
        trigger_type: str,
    ) -> dict[str, Any] | None:
        """Queue one deduplicated full Deep Report without blocking monitor planning."""
        if (
            not autonomous
            or not research_reasons
            or not self._auto_deep_report_enabled()
            or self.auto_deep_report_submitter is None
        ):
            return None
        result = self.auto_deep_report_submitter(
            {
                "job_id": job_id,
                "symbol": symbol,
                "security_name": str(holding.get("name") or symbol),
                "research_reasons": list(dict.fromkeys(research_reasons)),
                "research_date": research_date,
                "trigger_type": trigger_type or "monitor_research_required",
            }
        )
        child_session_id = str(result.get("session_id") or "")
        if child_session_id and str(result.get("status") or "") != "reused":
            self.usage_store.link_scope(
                "monitor_job",
                job_id,
                "session",
                child_session_id,
                relationship="auto_deep_report",
                child_attempt_id=str(result.get("attempt_id") or "") or None,
            )
        return result

    @staticmethod
    def _profile_is_autopilot(profile: dict[str, Any]) -> bool:
        if str((profile.get("display_plan") or {}).get("created_by") or "") == "autopilot":
            return True
        active_version = profile.get("active_plan_version")
        plans = profile.get("plans") or []
        if active_version is not None:
            return any(
                candidate.get("version") == active_version
                and str(candidate.get("created_by") or "") == "autopilot"
                for candidate in plans
            )
        return any(
            candidate.get("status") == "pending_review"
            and str(candidate.get("created_by") or "") == "autopilot"
            for candidate in plans
        )

    def _stop_autopilot_symbols(
        self,
        symbols: set[str],
        *,
        close_profiles: bool,
        reason: str,
        delivery_mode: str,
    ) -> None:
        if not symbols:
            return
        self.store.cancel_autopilot_symbols(symbols, reason=reason)
        if not close_profiles:
            return
        for profile in self.store.list_profiles():
            if (
                str(profile.get("symbol") or "") in symbols
                and str(profile.get("status") or "") in {"active", "paused", "pending_review"}
                and self._profile_is_autopilot(profile)
            ):
                self.store.close_autopilot_profile(
                    str(profile["profile_id"]),
                    delivery_mode=delivery_mode,
                    reason=reason,
                )

    @staticmethod
    def _valid_number(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    def _enrich_session_metrics(self, profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fill missing session open/high/low from verified cache without rewriting history."""

        cache: dict[tuple[str, str, str], dict[str, float | None] | None] = {}
        market_store = self.planner.market_service.store
        for profile in profiles:
            quote = profile.get("last_quote")
            if not isinstance(quote, dict):
                continue
            if all(
                self._valid_number(quote.get(field)) is not None
                for field in ("session_open", "session_high", "session_low")
            ):
                continue
            symbol = str(profile.get("symbol") or "").upper()
            interval = str(quote.get("interval") or "")
            session_date = str(quote.get("session_date") or "")
            if not symbol or interval not in {"1m", "5m"} or not session_date:
                continue
            key = (symbol, interval, session_date)
            if key not in cache:
                try:
                    bars = market_store.query_bars(
                        symbol=symbol,
                        interval=interval,
                        adjustment="raw",
                        view="consensus",
                        limit=300,
                    )
                except Exception:
                    cache[key] = None
                    continue
                candidates = [
                    bar
                    for bar in bars
                    if str(bar.get("status") or "") == "verified"
                    and str(bar.get("session_date") or "") == session_date
                ]
                opening_bar = min(
                    candidates,
                    key=lambda bar: str(bar.get("bar_time") or ""),
                    default=None,
                )
                session_open = (
                    self._valid_number(opening_bar.get("open"))
                    or self._valid_number(opening_bar.get("close"))
                    if opening_bar
                    else None
                )
                highs = [
                    value
                    for bar in candidates
                    if (value := self._valid_number(bar.get("high") or bar.get("close")))
                    is not None
                ]
                lows = [
                    value
                    for bar in candidates
                    if (value := self._valid_number(bar.get("low") or bar.get("close")))
                    is not None
                ]
                cache[key] = {
                    "session_open": session_open,
                    "session_high": max(highs, default=None),
                    "session_low": min(lows, default=None),
                }
            metrics = cache[key]
            if metrics is not None:
                for field, value in metrics.items():
                    if self._valid_number(quote.get(field)) is None and value is not None:
                        quote[field] = value
        return profiles

    def list_profiles(self) -> list[dict[str, Any]]:
        return self._enrich_session_metrics(self.store.list_profiles())

    def get_profile(self, profile_id: str) -> dict[str, Any] | None:
        profile = self.store.get_profile(profile_id)
        if not profile:
            return None
        return self._enrich_session_metrics([profile])[0]

    def list_report_candidates(self, symbol: str) -> dict[str, Any]:
        normalized = normalize_symbol(symbol).upper()
        if not normalized:
            raise ValueError("symbol is required")
        return {
            "symbol": normalized,
            "candidates": self.report_catalog.list_candidates(normalized),
        }

    def create_planner_job(
        self,
        symbols: list[str],
        *,
        report_refs: dict[str, str] | None = None,
        research_policy: str = "if_needed",
        delivery_target_id: str | None = None,
        force_fresh: bool = True,
        activation_mode: str = "manual",
        trigger_type: str | None = None,
        evidence_fingerprint: str | None = None,
        autopilot_trigger_id: str | None = None,
    ) -> dict[str, Any]:
        if research_policy != "if_needed":
            raise ValueError("research_policy must be if_needed")
        if activation_mode not in {"manual", "autonomous"}:
            raise ValueError("activation_mode must be manual or autonomous")
        normalized = sorted(
            {
                normalize_symbol(symbol).upper()
                for symbol in symbols
                if normalize_symbol(symbol)
            }
        )
        if not normalized:
            raise ValueError("at least one holding symbol is required")
        if activation_mode == "autonomous":
            config = self.store.get_autopilot_config()
            if not config["enabled"]:
                raise ValueError("autonomous monitoring must be enabled first")
            unauthorized = [
                symbol for symbol in normalized
                if not self._autopilot_authorized(symbol, config=config)
            ]
            if unauthorized:
                raise ValueError(
                    "autonomous monitoring only accepts selected current holdings: "
                    f"{', '.join(unauthorized)}"
                )
        holding_symbols = {
            normalize_symbol(str(item.get("symbol") or item.get("code") or "")).upper()
            for item in load_state().holdings
        }
        unknown = [symbol for symbol in normalized if symbol not in holding_symbols]
        if unknown:
            raise ValueError(
                f"initial monitoring only accepts current holdings: {', '.join(unknown)}"
            )
        if delivery_target_id:
            target = self.store.get_target(delivery_target_id)
            if not target or target["status"] != "active":
                raise ValueError("delivery target is not active")
        normalized_refs = {
            normalize_symbol(symbol).upper(): str(report_ref)
            for symbol, report_ref in (report_refs or {}).items()
            if normalize_symbol(symbol) and str(report_ref).strip()
        }
        unexpected_refs = sorted(set(normalized_refs) - set(normalized))
        if unexpected_refs:
            raise ValueError("report_refs contains a symbol outside the requested holdings")
        job = self.store.create_planner_job(
            symbols=normalized,
            report_refs=normalized_refs,
            research_policy=research_policy,
            delivery_target_id=delivery_target_id,
            force_fresh=force_fresh,
            activation_mode=activation_mode,
            trigger_type=trigger_type,
            evidence_fingerprint=evidence_fingerprint,
            autopilot_trigger_id=autopilot_trigger_id,
        )
        self._submit_planner_job(str(job["job_id"]))
        return job

    def cancel_planner_job(self, job_id: str) -> dict[str, Any]:
        return self.store.cancel_planner_job(job_id)

    def retry_planner_job_item(self, job_id: str, symbol: str) -> dict[str, Any]:
        normalized = normalize_symbol(symbol).upper()
        existing = self.store.get_planner_job(job_id)
        if not existing:
            raise KeyError(job_id)
        if (
            str(existing.get("activation_mode") or "manual") == "autonomous"
            and not self._autopilot_authorized(normalized)
        ):
            raise ValueError("symbol is not selected for autonomous monitoring")
        result = self.store.retry_planner_item(job_id, normalized)
        self._submit_planner_job(job_id)
        return result

    def get_autopilot_config(self) -> dict[str, Any]:
        return self.store.get_autopilot_config()

    def set_autopilot_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        previous = self.store.get_autopilot_config()
        if "selected_symbols" in payload:
            requested = {
                normalize_symbol(str(symbol)).upper()
                for symbol in (payload.get("selected_symbols") or [])
                if normalize_symbol(str(symbol))
            }
            current_holdings = {
                normalize_symbol(str(item.get("symbol") or item.get("code") or "")).upper()
                for item in load_state().holdings
                if (self._valid_number(item.get("quantity")) or 0) > 0
            }
            unavailable = sorted(requested - current_holdings)
            if unavailable:
                raise ValueError(
                    "autonomous monitoring only accepts current positive holdings: "
                    f"{', '.join(unavailable)}"
                )
        config = self.store.set_autopilot_config(payload)
        previous_symbols = set(previous.get("selected_symbols") or [])
        selected_symbols = set(config.get("selected_symbols") or [])
        removed_symbols = previous_symbols - selected_symbols
        added_symbols = selected_symbols - previous_symbols
        self._stop_autopilot_symbols(
            removed_symbols,
            close_profiles=True,
            reason="selection_removed",
            delivery_mode=str(config.get("runtime_mode") or "shadow"),
        )
        if not config["enabled"]:
            self._stop_autopilot_symbols(
                previous_symbols | selected_symbols,
                close_profiles=False,
                reason="autopilot_disabled",
                delivery_mode=str(config.get("runtime_mode") or "shadow"),
            )
        elif "holdings_changed" not in set(config.get("trigger_types") or []):
            for symbol in sorted(added_symbols):
                if not self._autopilot_authorized(symbol, config=config):
                    continue
                holding = self._holding(symbol)
                assert holding is not None
                holding_fingerprint = _holding_hash(holding)
                self.store.enqueue_autopilot_trigger(
                    symbol=symbol,
                    trigger_type="holdings_changed",
                    dedupe_key=f"{holding_fingerprint}:selection:{config['revision']}",
                    payload={
                        "holding_hash": holding_fingerprint,
                        "selection_added": True,
                    },
                )
        if config["enabled"]:
            self.autopilot_tick(force=True)
        return config

    def list_autopilot_runs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        runs = self.store.list_autopilot_triggers(limit=limit)
        for run in runs:
            job_id = str(run.get("planner_job_id") or "")
            if not job_id:
                run["blocked_reasons"] = []
                run["validation_errors"] = []
                run["detail_error"] = None
                continue
            job = self.store.get_planner_job(job_id)
            item = next(
                (
                    candidate
                    for candidate in (job or {}).get("items", [])
                    if str(candidate.get("symbol") or "") == str(run.get("symbol") or "")
                ),
                None,
            )
            run["blocked_reasons"] = list((item or {}).get("blocked_reasons") or [])
            run["validation_errors"] = list((item or {}).get("validation_errors") or [])
            run["detail_error"] = (item or {}).get("error")
        return runs

    def list_recommendations(
        self,
        *,
        symbol: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.store.list_recommendations(symbol=symbol, status=status, limit=limit)

    def acknowledge_recommendation(self, recommendation_id: str, feedback_status: str) -> dict[str, Any]:
        return self.store.acknowledge_recommendation(recommendation_id, feedback_status)

    def enqueue_autopilot_event(
        self,
        *,
        trigger_type: str,
        symbol: str,
        fingerprint: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        config = self.store.get_autopilot_config()
        if not config["enabled"] or trigger_type not in set(config["trigger_types"]):
            return None
        normalized_symbol = normalize_symbol(symbol).upper()
        if not self._autopilot_authorized(normalized_symbol, config=config):
            return None
        event_payload = dict(payload or {})
        holding = self._holding(normalized_symbol)
        holding_name = str((holding or {}).get("name") or "").strip()
        if holding_name and not event_payload.get("holding_name"):
            event_payload["holding_name"] = holding_name
        trigger, _created = self.store.enqueue_autopilot_trigger(
            symbol=normalized_symbol,
            trigger_type=trigger_type,
            dedupe_key=fingerprint,
            payload=event_payload,
        )
        self.autopilot_tick(force=True)
        return trigger

    def autopilot_tick(self, *, force: bool = False) -> dict[str, Any]:
        """Discover durable triggers and submit at most one job per symbol."""

        with self._autopilot_lock:
            now_monotonic = time.monotonic()
            if not force and now_monotonic - self._last_autopilot_tick < 30:
                return {"status": "throttled"}
            self._last_autopilot_tick = now_monotonic
            config = self.store.get_autopilot_config()
            if not config["enabled"]:
                return {"status": "disabled"}
            selected_symbols = set(config.get("selected_symbols") or [])
            if not selected_symbols:
                queued_symbols = {
                    str(item["symbol"])
                    for status in ("queued", "running")
                    for item in self.store.list_autopilot_triggers(status=status, limit=500)
                }
                self._stop_autopilot_symbols(
                    queued_symbols,
                    close_profiles=False,
                    reason="selection_removed",
                    delivery_mode=str(config.get("runtime_mode") or "shadow"),
                )
                for profile in self.store.list_profiles():
                    if (
                        str(profile.get("status") or "") in {"active", "paused", "pending_review"}
                        and self._profile_is_autopilot(profile)
                    ):
                        self.store.close_autopilot_profile(
                            str(profile["profile_id"]),
                            delivery_mode=str(config.get("runtime_mode") or "shadow"),
                            reason="selection_removed",
                        )
                return {"status": "no_selected_symbols", "covered_symbols": []}
            holdings = [
                item
                for item in load_state().holdings
                if normalize_symbol(
                    str(item.get("symbol") or item.get("code") or "")
                ).upper() in selected_symbols
                and normalize_symbol(str(item.get("symbol") or item.get("code") or "")).upper().endswith(
                    (".SH", ".SZ", ".BJ")
                )
                and (self._valid_number(item.get("quantity")) or 0) > 0
            ]
            current_symbols = {
                normalize_symbol(str(item.get("symbol") or item.get("code") or "")).upper()
                for item in holdings
            }
            for profile in self.store.list_profiles():
                if (
                    profile.get("symbol") not in current_symbols
                    and profile.get("status") in {"active", "paused", "pending_review"}
                    and self._profile_is_autopilot(profile)
                ):
                    reason = (
                        "selection_removed"
                        if profile.get("symbol") not in selected_symbols
                        else "holding_removed"
                    )
                    self.store.close_autopilot_profile(
                        str(profile["profile_id"]),
                        delivery_mode=str(config.get("runtime_mode") or "shadow"),
                        reason=reason,
                    )
            stale_trigger_symbols = {
                str(item["symbol"])
                for status in ("queued", "running")
                for item in self.store.list_autopilot_triggers(status=status, limit=500)
                if str(item["symbol"]) not in current_symbols
            }
            self._stop_autopilot_symbols(
                stale_trigger_symbols,
                close_profiles=False,
                reason="autopilot_symbol_unavailable",
                delivery_mode=str(config.get("runtime_mode") or "shadow"),
            )
            created = 0
            local_now = datetime.now(ZoneInfo("Asia/Shanghai"))
            for holding in holdings:
                symbol = normalize_symbol(str(holding.get("symbol") or holding.get("code") or "")).upper()
                holding_name = str(holding.get("name") or "").strip()
                identity_payload = {"holding_name": holding_name} if holding_name else {}
                holding_fingerprint = _holding_hash(holding)
                profile = self.store.get_profile_by_symbol(symbol)
                active_plan = next(
                    (
                        plan for plan in (profile or {}).get("plans", [])
                        if plan.get("status") == "active"
                    ),
                    None,
                )
                selected, _reasons = self.report_catalog.choose_candidate(symbol, None)
                report_hash = (
                    hashlib.sha256(str(selected.get("body") or "").encode("utf-8")).hexdigest()
                    if selected is not None
                    else ""
                )
                planning_trigger_covered = False
                if (
                    "holdings_changed" in set(config["trigger_types"])
                    and (
                        profile is None
                        or profile.get("input_snapshot_hash") != holding_fingerprint
                        or (
                            profile.get("status") == "closed"
                            and self._profile_is_autopilot(profile)
                        )
                    )
                ):
                    holding_dedupe = (
                        f"{holding_fingerprint}:selection:{config['revision']}"
                        if profile is not None and profile.get("status") == "closed"
                        else holding_fingerprint
                    )
                    holding_trigger, was_created = self.store.enqueue_autopilot_trigger(
                        symbol=symbol,
                        trigger_type="holdings_changed",
                        dedupe_key=holding_dedupe,
                        payload={
                            **identity_payload,
                            "holding_hash": holding_fingerprint,
                            "report_ref": (selected or {}).get("report_ref"),
                            "report_hash": report_hash or None,
                        },
                    )
                    created += int(was_created)
                    trigger_payload = holding_trigger.get("payload") or {}
                    holding_covers_current_report = (
                        not report_hash
                        or str(trigger_payload.get("report_hash") or "") == report_hash
                    )
                    planning_trigger_covered = (
                        str(holding_trigger.get("status") or "")
                        not in {"failed", "cancelled"}
                        and holding_covers_current_report
                    )
                if (
                    not planning_trigger_covered
                    and selected is not None
                    and "report_ready" in set(config["trigger_types"])
                ):
                    active_hash = str(
                        (((active_plan or {}).get("plan") or {}).get("analysis_ref") or {}).get("body_sha256")
                        or ""
                    )
                    if report_hash != active_hash:
                        report_trigger, was_created = self.store.enqueue_autopilot_trigger(
                            symbol=symbol,
                            trigger_type="report_ready",
                            dedupe_key=report_hash,
                            payload={
                                **identity_payload,
                                "report_ref": selected.get("report_ref"),
                                "report_hash": report_hash,
                            },
                        )
                        created += int(was_created)
                        planning_trigger_covered = str(
                            report_trigger.get("status") or ""
                        ) not in {"failed", "cancelled"}
                if (
                    not planning_trigger_covered
                    and config["daily_close_enabled"]
                    and "scheduled_close" in set(config["trigger_types"])
                    and local_now.hour * 60 + local_now.minute >= 15 * 60 + 5
                    and local_now.weekday() < 5
                ):
                    close_key = local_now.date().isoformat()
                    close_trigger, was_created = self.store.enqueue_autopilot_trigger(
                        symbol=symbol,
                        trigger_type="scheduled_close",
                        dedupe_key=close_key,
                        payload={**identity_payload, "market_date": close_key},
                    )
                    created += int(was_created)
                    planning_trigger_covered = str(
                        close_trigger.get("status") or ""
                    ) not in {"failed", "cancelled"}

                # Probe the read-only evidence set once per half-hour.  A new
                # planner run is enqueued only when the deterministic payload
                # fingerprint actually changes; this is a re-plan, not an
                # unconditional deep-research call.
                bucket = f"{local_now.date().isoformat()}-{local_now.hour:02d}-{local_now.minute // 30}"
                if (
                    not planning_trigger_covered
                    and active_plan is not None
                    and "material_evidence_changed" in set(config["trigger_types"])
                    and self._evidence_probe_buckets.get(symbol) != bucket
                ):
                    self._evidence_probe_buckets[symbol] = bucket
                    selected_for_probe = selected
                    if selected_for_probe is not None:
                        try:
                            market_evidence, market_blocked = self.report_planner.market_evidence(holding)
                            if not market_blocked:
                                snapshot_for_probe = self.report_catalog.freeze(selected_for_probe)
                                bundle = self.evidence_collector.collect(
                                    symbol=symbol,
                                    holding=holding,
                                    report_snapshot=snapshot_for_probe,
                                    market_evidence=market_evidence,
                                )
                                latest_bundle = self.store.get_latest_evidence_bundle(symbol)
                                latest_fingerprint = str(
                                    (latest_bundle or {}).get("evidence_fingerprint") or ""
                                )
                                current_fingerprint = str(bundle["evidence_fingerprint"])
                                self.store.save_evidence_bundle(symbol=symbol, bundle=bundle)
                                if latest_fingerprint and current_fingerprint != latest_fingerprint:
                                    _trigger, was_created = self.store.enqueue_autopilot_trigger(
                                        symbol=symbol,
                                        trigger_type="material_evidence_changed",
                                        dedupe_key=current_fingerprint,
                                        payload={
                                            **identity_payload,
                                            "evidence_fingerprint": current_fingerprint,
                                        },
                                    )
                                    created += int(was_created)
                        except Exception:
                            # A failed evidence probe must not interrupt price
                            # monitoring or close the current active plan.
                            pass

            running_symbols = {
                str(item["symbol"])
                for item in self.store.list_autopilot_triggers(status="running", limit=500)
            }
            submitted = 0
            for trigger in reversed(self.store.list_autopilot_triggers(status="queued", limit=500)):
                symbol = str(trigger["symbol"])
                if (
                    symbol in running_symbols
                    or symbol not in current_symbols
                    or not self._autopilot_authorized(symbol, config=config)
                ):
                    continue
                report_ref = str((trigger.get("payload") or {}).get("report_ref") or "")
                try:
                    job = self.create_planner_job(
                        [symbol],
                        report_refs={symbol: report_ref} if report_ref else None,
                        research_policy="if_needed",
                        delivery_target_id=config.get("delivery_target_id"),
                        force_fresh=True,
                        activation_mode="autonomous",
                        trigger_type=str(trigger["trigger_type"]),
                        evidence_fingerprint=trigger.get("evidence_fingerprint"),
                        autopilot_trigger_id=str(trigger["trigger_id"]),
                    )
                except Exception as exc:
                    self.store.update_autopilot_trigger(
                        str(trigger["trigger_id"]),
                        status="failed",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    continue
                self.store.update_autopilot_trigger(
                    str(trigger["trigger_id"]),
                    status="running",
                    planner_job_id=str(job["job_id"]),
                )
                running_symbols.add(symbol)
                submitted += 1
            return {
                "status": "running",
                "covered_symbols": sorted(current_symbols),
                "created_triggers": created,
                "submitted_jobs": submitted,
            }

    def _submit_planner_job(self, job_id: str) -> None:
        self.usage_store.start_scope("monitor_job", job_id)
        with self._planner_lock:
            current = self._planner_futures.get(job_id)
            if current is not None and not current.done():
                return
            future = self._planner_executor.submit(self._run_planner_job, job_id)
            self._planner_futures[job_id] = future

            def clear(done: Future[Any], *, key: str = job_id) -> None:
                with self._planner_lock:
                    if self._planner_futures.get(key) is done:
                        self._planner_futures.pop(key, None)

            future.add_done_callback(clear)

    def _run_planner_job(self, job_id: str) -> None:
        job = self.store.get_planner_job(job_id)
        if not job or job["status"] == "cancelled":
            return
        if (
            str(job.get("activation_mode") or "manual") == "autonomous"
            and not all(
                self._autopilot_authorized(str(symbol))
                for symbol in job.get("requested_symbols") or []
            )
        ):
            try:
                self.store.cancel_planner_job(job_id)
            except RuntimeError:
                pass
            trigger_id = str(job.get("autopilot_trigger_id") or "")
            if trigger_id:
                self.store.update_autopilot_trigger(
                    trigger_id,
                    status="cancelled",
                    error="autopilot_symbol_not_selected",
                )
            return
        self.store.update_planner_job_status(job_id, "planning")
        for item in job["items"]:
            if item["status"] != "queued" or self.store.planner_job_cancel_requested(job_id):
                continue
            self._run_recorded_planner_item(job, item)
        latest = self.store.get_planner_job(job_id)
        if not latest or latest["status"] == "cancelled":
            return
        statuses = [str(item["status"]) for item in latest["items"]]
        if any(status == "ready" for status in statuses):
            terminal = "ready"
        elif statuses and all(status == "blocked" for status in statuses):
            terminal = "blocked"
        elif statuses and all(status == "cancelled" for status in statuses):
            terminal = "cancelled"
        else:
            terminal = "failed"
        self.store.update_planner_job_status(job_id, terminal)
        trigger_id = str(latest.get("autopilot_trigger_id") or "") if latest else ""
        if trigger_id:
            self.store.update_autopilot_trigger(
                trigger_id,
                status="completed" if terminal == "ready" else terminal,
                planner_job_id=job_id,
                evidence_fingerprint=latest.get("evidence_fingerprint"),
                error=None if terminal == "ready" else f"planner_job_{terminal}",
            )

    def _run_recorded_planner_item(
        self,
        job: dict[str, Any],
        item: dict[str, Any],
    ) -> None:
        """Bind every planner stage to one durable monitor-job attempt."""

        job_id = str(job["job_id"])
        symbol = str(item["symbol"])
        attempt = max(1, int(item.get("attempt") or 1))
        attempt_id = f"{symbol}:{attempt}"
        recorder = UsageRecorder(
            self.usage_store,
            "monitor_job",
            job_id,
            attempt_id=attempt_id,
        )
        tool_event_id = recorder.start_tool(
            f"planner-item:{symbol}:{attempt}",
            "monitor_planner_item",
            {
                "symbol": symbol,
                "activation_mode": str(job.get("activation_mode") or "manual"),
                "trigger_type": str(job.get("trigger_type") or "manual"),
            },
            event_id=f"tool:monitor_job:{job_id}:{attempt_id}:planner-item",
            category="compute",
            metadata={"symbol": symbol, "attempt": attempt},
        )
        started = time.monotonic()
        try:
            with bind_usage_recorder(recorder, tool_event_id):
                self._run_planner_item(job, item)
        except BaseException:
            recorder.finish_tool(
                tool_event_id,
                status="error",
                elapsed_ms=int((time.monotonic() - started) * 1000),
                metadata={"outcome": "unhandled_error"},
            )
            raise

        latest = self.store.get_planner_job(job_id)
        latest_item = next(
            (
                candidate
                for candidate in (latest or {}).get("items", [])
                if str(candidate.get("symbol")) == symbol
            ),
            {},
        )
        outcome = str(latest_item.get("status") or "unknown")
        status = "cancelled" if outcome == "cancelled" else "error" if outcome == "failed" else "ok"
        recorder.finish_tool(
            tool_event_id,
            status=status,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            metadata={"outcome": outcome},
        )

    @staticmethod
    def _usage_period_bounds(period: str) -> tuple[str, str]:
        shanghai = ZoneInfo("Asia/Shanghai")
        now = datetime.now(timezone.utc)
        local_now = now.astimezone(shanghai)
        days = {"today": 1, "7d": 7, "30d": 30}.get(period)
        if days is None:
            raise ValueError("period must be today, 7d, or 30d")
        local_start = datetime.combine(
            local_now.date() - timedelta(days=days - 1),
            datetime.min.time(),
            tzinfo=shanghai,
        )
        started_at = local_start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        completed_at = now.isoformat().replace("+00:00", "Z")
        return started_at, completed_at

    def get_usage_summary(self, period: str = "today") -> dict[str, Any]:
        started_at, completed_at = self._usage_period_bounds(period)
        summary = self.usage_store.get_type_summary(
            "monitor_job",
            started_at=started_at,
            completed_at=completed_at,
        )
        recent_jobs = []
        for scope in summary.pop("recent_scopes", []):
            job = self.store.get_planner_job(str(scope["scope_id"]))
            if not job:
                continue
            job_usage = self.usage_store.get_summary("monitor_job", str(scope["scope_id"]))
            recent_jobs.append(
                {
                    **scope,
                    "job_id": str(job["job_id"]),
                    "status": str(job["status"]),
                    "requested_symbols": list(job.get("requested_symbols") or []),
                    "activation_mode": str(job.get("activation_mode") or "manual"),
                    "trigger_type": job.get("trigger_type"),
                    "created_at": job.get("created_at"),
                    "completed_at": job.get("completed_at"),
                    "usage": job_usage["session"],
                    "linked_scopes": job_usage.get("linked_scopes", []),
                }
            )
        return {**summary, "period": period, "recent_jobs": recent_jobs}

    def get_job_usage(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_planner_job(job_id)
        if not job:
            raise KeyError(job_id)
        return {
            **self.usage_store.get_summary("monitor_job", job_id),
            "job": {
                "job_id": job_id,
                "status": job.get("status"),
                "requested_symbols": list(job.get("requested_symbols") or []),
                "activation_mode": str(job.get("activation_mode") or "manual"),
                "trigger_type": job.get("trigger_type"),
                "created_at": job.get("created_at"),
                "completed_at": job.get("completed_at"),
            },
        }

    def list_usage_events(
        self,
        period: str = "today",
        **filters: Any,
    ) -> dict[str, Any]:
        started_at, completed_at = self._usage_period_bounds(period)
        return self.usage_store.list_type_events(
            "monitor_job",
            started_at=started_at,
            completed_at=completed_at,
            **filters,
        )

    def list_job_usage_events(self, job_id: str, **filters: Any) -> dict[str, Any]:
        if not self.store.get_planner_job(job_id):
            raise KeyError(job_id)
        return self.usage_store.list_events("monitor_job", job_id, **filters)

    def _run_planner_item(self, job: dict[str, Any], item: dict[str, Any]) -> None:
        job_id = str(job["job_id"])
        symbol = str(item["symbol"])
        autonomous = str(job.get("activation_mode") or "manual") == "autonomous"
        if autonomous and not self._autopilot_authorized(symbol):
            self.store.update_planner_item(job_id, symbol, status="cancelled")
            return
        holding = self._holding(symbol)
        if holding is None:
            self.store.update_planner_item(
                job_id,
                symbol,
                status="blocked",
                blocked_reasons=["holding_not_found"],
            )
            return
        selected, research_reasons = self.report_catalog.choose_candidate(
            symbol,
            str(item.get("report_ref") or "") or None,
        )
        now = datetime.now(timezone.utc)
        if selected is None:
            selected = {
                "report_ref": f"evidence-gap:{job_id}:{symbol}",
                "report_type": "single_stock_research",
                "symbol": symbol,
                "title": f"{symbol} monitoring evidence gap",
                "source_id": job_id,
                "source_message_id": None,
                "artifact_id": None,
                "revision": 1,
                "body": (
                    f"# {symbol} monitoring evidence gap\n\n"
                    "## Status\n\nNo current valid report was available. "
                    "This placeholder cannot create rules and only records why fresh research was requested.\n"
                ),
                "quality_status": "data_limited",
                "generated_at": now.isoformat(),
                "data_as_of": now.isoformat(),
                "metadata": {"research_reasons": research_reasons},
            }
        snapshot = self.report_catalog.freeze(selected)
        research_required = bool(research_reasons)
        research_date = now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        auto_deep_report: dict[str, Any] | None = None
        if research_required:
            try:
                auto_deep_report = self._maybe_queue_auto_deep_report(
                    autonomous=autonomous,
                    job_id=job_id,
                    symbol=symbol,
                    holding=holding,
                    selected=selected,
                    research_reasons=research_reasons,
                    research_date=research_date,
                    trigger_type=str(job.get("trigger_type") or ""),
                )
            except Exception as exc:
                # The compact monitoring plan still proceeds. A Deep Report
                # queue failure is visible in progress but never disables the
                # existing monitoring safety path.
                auto_deep_report = {
                    "status": "queue_failed",
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                }
        if research_required and self.store.automatic_research_used(
            symbol,
            research_date,
            excluding_job_id=job_id,
        ):
            self.store.update_planner_item(
                job_id,
                symbol,
                status="blocked",
                report_ref=snapshot["report_ref"],
                report_snapshot_id=snapshot["snapshot_id"],
                blocked_reasons=[*research_reasons, "automatic_research_daily_limit"],
            )
            return
        self.store.update_planner_item(
            job_id,
            symbol,
            status="researching" if research_required else "planning",
            report_ref=snapshot["report_ref"],
            report_snapshot_id=snapshot["snapshot_id"],
            research_date=research_date if research_required else None,
            blocked_reasons=research_reasons,
            progress={
                "stage": "report_frozen",
                "source_snapshot_id": snapshot["snapshot_id"],
                "auto_deep_report": auto_deep_report,
            },
        )

        def on_stage(stage: str, progress: dict[str, Any]) -> None:
            if self.store.planner_job_cancel_requested(job_id):
                raise InterruptedError("planner job cancelled")
            if autonomous and not self._autopilot_authorized(symbol):
                raise InterruptedError("autopilot symbol is no longer selected")
            if (
                stage == "researching"
                and not research_required
                and self.store.automatic_research_used(
                    symbol,
                    research_date,
                    excluding_job_id=job_id,
                )
            ):
                raise PlanValidationError("automatic_research_daily_limit")
            self.store.update_planner_job_status(job_id, stage)
            self.store.update_planner_item(
                job_id,
                symbol,
                status=stage,
                report_ref=snapshot["report_ref"],
                report_snapshot_id=snapshot["snapshot_id"],
                research_date=research_date if research_required else None,
                blocked_reasons=research_reasons,
                progress={"stage": stage, **progress},
            )

        try:
            if bool(job.get("force_fresh", True)):
                if self.store.planner_job_cancel_requested(job_id):
                    raise InterruptedError("planner job cancelled")
                self.store.update_planner_item(
                    job_id,
                    symbol,
                    status="researching" if research_required else "planning",
                    report_ref=snapshot["report_ref"],
                    report_snapshot_id=snapshot["snapshot_id"],
                    research_date=research_date if research_required else None,
                    blocked_reasons=research_reasons,
                    progress={"stage": "market_refresh", "force_fresh": True},
                )
                self.planner.market_service.refresh_sync(
                    symbols=[symbol],
                    profile="portfolio_monitor_report_planning",
                    items=[("1m", "raw"), ("5m", "raw"), ("1D", "raw")],
                    force=True,
                    read_only=True,
                    deadline=time.monotonic() + 30,
                )
            if self.store.planner_job_cancel_requested(job_id):
                raise InterruptedError("planner job cancelled")
            if autonomous and not self._autopilot_authorized(symbol):
                raise InterruptedError("autopilot symbol is no longer selected")
            evidence_bundle: dict[str, Any] | None = None
            if autonomous:
                market_evidence, evidence_blocked = self.report_planner.market_evidence(holding)
                if evidence_blocked:
                    raise PlanValidationError(", ".join(evidence_blocked))
                evidence_bundle = self.evidence_collector.collect(
                    symbol=symbol,
                    holding=holding,
                    report_snapshot=snapshot,
                    market_evidence=market_evidence,
                )
                self.store.save_evidence_bundle(
                    symbol=symbol,
                    bundle=evidence_bundle,
                    job_id=job_id,
                    trigger_id=str(job.get("autopilot_trigger_id") or "") or None,
                )
                if job.get("autopilot_trigger_id"):
                    self.store.update_autopilot_trigger(
                        str(job["autopilot_trigger_id"]),
                        status="running",
                        planner_job_id=job_id,
                        evidence_fingerprint=str(evidence_bundle["evidence_fingerprint"]),
                    )
            plan, manifest, research_candidate = self.report_planner.build(
                job_id=job_id,
                holding=holding,
                report_snapshot=snapshot,
                research_required=research_required,
                autonomous=autonomous,
                supplemental_evidence=evidence_bundle,
                should_cancel=lambda: self.store.planner_job_cancel_requested(job_id)
                or (autonomous and not self._autopilot_authorized(symbol)),
                on_stage=on_stage,
            )
            research_snapshot_id: str | None = None
            if research_candidate is not None:
                if not research_required and self.store.automatic_research_used(
                    symbol,
                    research_date,
                    excluding_job_id=job_id,
                ):
                    raise PlanValidationError("automatic_research_daily_limit")
                research_snapshot = self.report_catalog.freeze(research_candidate)
                research_snapshot_id = str(research_snapshot["snapshot_id"])
                plan, manifest = self.report_planner.finalize_research_snapshot(
                    plan,
                    manifest,
                    research_snapshot,
                )
            if self.store.planner_job_cancel_requested(job_id):
                raise InterruptedError("planner job cancelled")
            if autonomous and not self._autopilot_authorized(symbol):
                raise InterruptedError("autopilot symbol is no longer selected")
            market = symbol.rsplit(".", 1)[-1] if "." in symbol else "UNKNOWN"
            code = symbol.split(".")[0]
            instrument_type = (
                "etf"
                if code.startswith(("15", "16", "50", "51", "52", "56", "58"))
                else "company_equity"
            )
            if autonomous:
                previous_profile = self.store.get_profile_by_symbol(symbol)
                previous_version = (previous_profile or {}).get("active_plan_version")
                previous_plan = next(
                    (
                        item.get("plan") or {}
                        for item in (previous_profile or {}).get("plans", [])
                        if item.get("version") == previous_version
                    ),
                    {},
                )
                previous_fingerprints = {
                    str(item.get("scenario_fingerprint"))
                    for item in previous_plan.get("watch_scenarios") or []
                    if item.get("scenario_fingerprint")
                }
                current_fingerprints = {
                    str(item.get("scenario_fingerprint"))
                    for item in plan.get("watch_scenarios") or []
                    if item.get("scenario_fingerprint")
                }
                manifest = {
                    **manifest,
                    "autonomous_activation": {
                        "activated_by": "autopilot",
                        "planner_job_id": job_id,
                        "trigger_id": job.get("autopilot_trigger_id"),
                        "trigger_type": str(job.get("trigger_type") or "report_ready"),
                        "evidence_fingerprint": (evidence_bundle or {}).get("evidence_fingerprint"),
                        "previous_plan_version": previous_version,
                        "scenario_diff": {
                            "unchanged": sorted(previous_fingerprints & current_fingerprints),
                            "added": sorted(current_fingerprints - previous_fingerprints),
                            "superseded": sorted(previous_fingerprints - current_fingerprints),
                        },
                        "trade_execution": "forbidden",
                    },
                }
                if previous_profile and previous_profile.get("status") == "closed":
                    previous_owner = next(
                        (
                            str(candidate.get("created_by") or "")
                            for candidate in previous_profile.get("plans") or []
                            if candidate.get("version") == previous_profile.get("active_plan_version")
                        ),
                        "",
                    )
                    if previous_owner != "autopilot":
                        raise PlanValidationError(
                            "closed manual monitor cannot be replaced by autopilot"
                        )
                    if not self._autopilot_authorized(symbol):
                        raise InterruptedError("autopilot symbol is no longer selected")
                    self.store.reopen_autopilot_profile(str(previous_profile["profile_id"]))
            profile_id, version = self.store.save_draft(
                symbol=symbol,
                market=market,
                instrument_type=instrument_type,
                plan=plan,
                evidence_manifest=manifest,
                input_snapshot_hash=_holding_hash(holding),
                delivery_target_id=job.get("delivery_target_id"),
                model_id=self.report_planner.model_id,
                created_by="autopilot" if autonomous else "monitor_planner",
            )
            activated = False
            if autonomous:
                if not self._autopilot_authorized(symbol):
                    raise InterruptedError("autopilot symbol is no longer selected")
                self.store.save_condition_coverage(
                    profile_id=profile_id,
                    plan_version=version,
                    plan=plan,
                )
                self.store.activate_autonomous(
                    profile_id,
                    version,
                    trigger_type=str(job.get("trigger_type") or "report_ready"),
                    evidence_fingerprint=(evidence_bundle or {}).get("evidence_fingerprint"),
                )
                activated = True
            self.store.update_planner_item(
                job_id,
                symbol,
                status="ready",
                report_ref=snapshot["report_ref"],
                report_snapshot_id=snapshot["snapshot_id"],
                research_snapshot_id=research_snapshot_id,
                research_date=research_date if research_required or research_snapshot_id else None,
                profile_id=profile_id,
                plan_version=version,
                progress={
                    "stage": "ready",
                    "requires_manual_activation": not autonomous,
                    "activated": activated,
                    "activation_mode": "autonomous" if autonomous else "manual",
                },
            )
        except InterruptedError:
            self.store.update_planner_item(job_id, symbol, status="cancelled")
        except PlanValidationError as exc:
            if autonomous and not self._autopilot_authorized(symbol):
                profile = self.store.get_profile_by_symbol(symbol)
                if (
                    profile
                    and profile.get("status") in {"active", "paused", "pending_review"}
                    and any(
                        str(candidate.get("created_by") or "") == "autopilot"
                        for candidate in profile.get("plans") or []
                        if candidate.get("status") in {"active", "pending_review"}
                    )
                ):
                    self.store.close_autopilot_profile(
                        str(profile["profile_id"]),
                        delivery_mode=str(
                            self.store.get_autopilot_config().get("runtime_mode") or "shadow"
                        ),
                        reason="selection_removed",
                    )
            self.store.update_planner_item(
                job_id,
                symbol,
                status="blocked",
                report_ref=snapshot["report_ref"],
                report_snapshot_id=snapshot["snapshot_id"],
                research_date=research_date if research_required else None,
                blocked_reasons=[*research_reasons, "planner_validation_failed"],
                validation_errors=[str(exc)],
                error=str(exc),
            )
        except Exception as exc:  # per-symbol failure isolation
            self.store.update_planner_item(
                job_id,
                symbol,
                status="failed",
                report_ref=snapshot["report_ref"],
                report_snapshot_id=snapshot["snapshot_id"],
                research_date=research_date if research_required else None,
                error=f"{type(exc).__name__}: {exc}",
            )

    def create_draft_batch(
        self,
        symbols: list[str],
        delivery_target_id: str | None = None,
        *,
        force_fresh: bool = False,
        allow_single_source: bool = False,
    ) -> dict[str, Any]:
        normalized = sorted({normalize_symbol(symbol).upper() for symbol in symbols if normalize_symbol(symbol)})
        if not normalized:
            raise ValueError("at least one holding symbol is required")
        holding_symbols = {
            normalize_symbol(str(item.get("symbol") or item.get("code") or "")).upper()
            for item in load_state().holdings
        }
        unknown = [symbol for symbol in normalized if symbol not in holding_symbols]
        if unknown:
            raise ValueError(f"initial monitoring only accepts current holdings: {', '.join(unknown)}")
        if delivery_target_id:
            target = next((item for item in self.store.list_targets() if item["target_id"] == delivery_target_id and item["status"] == "active"), None)
            if not target:
                raise ValueError("delivery target is not active")
        refresh_blocked_reason: str | None = None
        refresh_attempted = force_fresh or allow_single_source

        def refresh_sources() -> str | None:
            try:
                refresh = self.planner.market_service.refresh_sync(
                    symbols=normalized,
                    profile="portfolio_monitor_planning",
                    items=[("1m", "raw"), ("5m", "raw"), ("1D", "raw")],
                    force=True,
                    read_only=True,
                    deadline=time.monotonic() + 30,
                )
                refresh_status = str(refresh.get("status") or "unknown")
                if refresh_status not in {"completed"}:
                    return f"fresh_refresh_{refresh_status}"
            except Exception:
                return "fresh_refresh_failed"
            return None

        if refresh_attempted:
            refresh_blocked_reason = refresh_sources()
        batch_id = self.store.create_batch(normalized, delivery_target_id)
        for symbol in normalized:
            holding = self._holding(symbol)
            assert holding is not None
            market = symbol.rsplit(".", 1)[-1] if "." in symbol else "UNKNOWN"
            code = symbol.split(".")[0]
            instrument_type = "etf" if code.startswith(("15", "16", "50", "51", "52", "56", "58")) else "company_equity"
            try:
                plan, evidence, blocked = self.planner.build(
                    holding,
                    allow_single_source=allow_single_source,
                )
                if blocked and not refresh_attempted and _needs_source_refresh(blocked):
                    refresh_attempted = True
                    refresh_blocked_reason = refresh_sources()
                    plan, evidence, blocked = self.planner.build(
                        holding,
                        allow_single_source=allow_single_source,
                    )
                if blocked or plan is None:
                    if refresh_blocked_reason and refresh_blocked_reason not in blocked:
                        blocked = [*blocked, refresh_blocked_reason]
                    profile_id = self.store.save_blocked_profile(
                        symbol=symbol, market=market, instrument_type=instrument_type, blocked_reasons=blocked
                    )
                    self.store.finish_batch_item(
                        batch_id, symbol, status="blocked", profile_id=profile_id, blocked_reasons=blocked
                    )
                    continue
                profile_id, version = self.store.save_draft(
                    symbol=symbol,
                    market=market,
                    instrument_type=instrument_type,
                    plan=plan,
                    evidence_manifest=evidence,
                    input_snapshot_hash=_holding_hash(holding),
                    delivery_target_id=delivery_target_id,
                    model_id=self.planner.model_id,
                )
                self.store.finish_batch_item(
                    batch_id, symbol, status="ready", profile_id=profile_id, plan_version=version
                )
            except Exception as exc:  # item isolation is part of the batch contract
                self.store.finish_batch_item(
                    batch_id, symbol, status="failed", error=f"{type(exc).__name__}: {exc}"
                )
        self.store.finish_batch(batch_id)
        result = self.store.get_batch(batch_id)
        assert result is not None
        return result

    def reanalyze(
        self,
        profile_id: str,
        *,
        allow_single_source: bool = False,
    ) -> dict[str, Any]:
        profile = self.store.get_profile(profile_id)
        if not profile:
            raise KeyError(profile_id)
        result = self.create_draft_batch(
            [profile["symbol"]],
            profile.get("delivery_target_id"),
            force_fresh=True,
            allow_single_source=allow_single_source,
        )
        return result

    def reopen(
        self,
        profile_id: str,
        delivery_target_id: str | None = None,
        *,
        allow_single_source: bool = False,
    ) -> dict[str, Any]:
        """Explicitly recheck a closed holding and create a new review draft when safe."""

        profile = self.store.get_profile(profile_id)
        if not profile:
            raise KeyError(profile_id)
        if profile["status"] != "closed":
            raise ValueError("only a closed monitor can be reopened")
        if not self._holding(profile["symbol"]):
            raise ValueError("reopening monitoring only accepts current holdings")
        target_id = delivery_target_id or profile.get("delivery_target_id")
        target = next(
            (
                item for item in self.store.list_targets()
                if item["target_id"] == target_id and item["status"] == "active"
            ),
            None,
        )
        if not target:
            raise ValueError("an active Feishu delivery target is required to reopen")
        self.store.reopen(profile_id, delivery_target_id=str(target_id))
        return self.create_draft_batch(
            [profile["symbol"]],
            str(target_id),
            force_fresh=True,
            allow_single_source=allow_single_source,
        )

    def status(self, runtime_status: dict[str, Any] | None = None) -> dict[str, Any]:
        runtime = runtime_status or {}
        configured = os.getenv("VIBE_TRADING_MONITORING_ENABLED", "0").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        effective_mode = str(runtime.get("mode") or ("shadow" if configured else "off"))
        return {
            "enabled_by_config": configured,
            "effective_mode": effective_mode,
            "runtime": runtime,
            "capabilities": {
                "market_rules": "available",
                "market_schedules": "cn_mainland_only_us_pending",
                "feishu_delivery": (
                    "enabled" if effective_mode == "deliver" else "suppressed_in_shadow_or_off"
                ),
                "news_semantic_monitor": "disabled_pending_data_contract",
                "fundamental_scorecards": "disabled_pending_calibration",
                "automatic_trading": "forbidden",
            },
            **self.store.metrics(),
        }
