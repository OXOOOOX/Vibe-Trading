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

from src.portfolio.analysis_methods import METHOD_REGISTRY_VERSION
from src.portfolio.state import load_state, normalize_symbol
from src.reports.execution_policy import (
    MONITOR_STRUCTURAL_REFRESH_MAX_ITERATIONS,
    MONITOR_STRUCTURAL_REFRESH_MAX_TOTAL_TOKENS,
)
from src.usage import UsageRecorder, UsageStore, bind_usage_recorder

from .planner import MonitoringPlanner
from .decisions import DecisionEngine, validate_risk_preference
from .price_volume import PriceVolumeAnalyzer, price_volume_interpretation
from .models import DEFAULT_PRICE_VOLUME_POLICY, PlanValidationError
from .evidence import AutonomousEvidenceCollector
from .report_catalog import MonitorReportCatalog
from .report_planner import (
    DeterministicMarketRepairError,
    ReportDrivenMonitoringPlanner,
)
from .store import MonitoringStore


_REFRESHABLE_BLOCK_REASONS = {
    "verified_quote_missing",
    "raw_price_basis_unavailable",
    "quote_provenance_missing",
    "verified_price_missing",
}

_AUTOPILOT_FAILURE_CIRCUIT_THRESHOLD = 2
_AUTOPILOT_INFRASTRUCTURE_RETRY_LIMIT = 1
_HARD_STRUCTURAL_BLOCK_REASONS = {
    "price_series_discontinuity_unverified",
    "adjustment_factor_unverified",
    "insufficient_post_event_history",
}
_LEGACY_LEVEL_METHODS = {
    "range_upper_with_noise_buffer",
    "symmetric_target_extension",
    "current_to_stop_midpoint",
    "range_lower_with_noise_buffer",
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
        report_library_service: Any | None = None,
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
            report_library_service=report_library_service,
        )
        self.report_planner = report_planner or ReportDrivenMonitoringPlanner(
            market_planner=self.planner
        )
        self.evidence_collector = evidence_collector or AutonomousEvidenceCollector()
        self.auto_deep_report_submitter = auto_deep_report_submitter
        self.price_volume_analyzer = PriceVolumeAnalyzer()
        self.decision_engine = DecisionEngine()
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

    def _maybe_queue_structural_report_refresh(
        self,
        *,
        autonomous: bool,
        job_id: str,
        symbol: str,
        holding: dict[str, Any],
        selected: dict[str, Any],
        research_date: str,
        trigger_type: str,
        bundle_status: str,
    ) -> dict[str, Any] | None:
        """Request one bounded revision when a Deep Report has no usable structure.

        The generated revision is marked with a dedicated generation source.  If
        that revision is still not usable, the marker prevents monitor polling
        from recursively creating another Deep Report revision.
        """
        metadata = (
            dict(selected.get("metadata") or {})
            if isinstance(selected.get("metadata"), dict)
            else {}
        )
        report_type = str(selected.get("report_type") or "")
        report_ref = str(selected.get("report_ref") or "")
        parent_report_id = str(
            metadata.get("report_id")
            or selected.get("source_id")
            or (report_ref.removeprefix("deep-report:") if report_ref.startswith("deep-report:") else "")
        ).strip()
        if (
            not autonomous
            or bundle_status not in {"not_recommended", "data_insufficient"}
            or report_type
            not in {"equity_deep_research", "etf_deep_research", "index_deep_research"}
            or not parent_report_id
            or not self._auto_deep_report_enabled()
            or self.auto_deep_report_submitter is None
        ):
            return None
        if str(metadata.get("generation_source") or "") == (
            "portfolio_monitor_structural_refresh"
        ):
            return {
                "status": "refresh_already_attempted",
                "parent_report_id": parent_report_id,
                "deduplicated": True,
            }
        result = self.auto_deep_report_submitter(
            {
                "job_id": job_id,
                "symbol": symbol,
                "security_name": str(holding.get("name") or symbol),
                "research_reasons": [
                    f"structural_monitoring_{bundle_status}",
                    "no_qualified_structural_monitoring_points",
                ],
                "research_date": research_date,
                "trigger_type": trigger_type or "structural_report_refresh",
                "structural_refresh": True,
                "parent_report_id": parent_report_id,
                "source_bundle_sha256": str(
                    selected.get("monitoring_bundle_sha256") or ""
                ),
            }
        )
        child_session_id = str(result.get("session_id") or "")
        if child_session_id and str(result.get("status") or "") != "reused":
            self.usage_store.link_scope(
                "monitor_job",
                job_id,
                "session",
                child_session_id,
                relationship="structural_report_refresh",
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
            price_volume = quote.get("price_volume")
            if (
                isinstance(price_volume, dict)
                and not isinstance(price_volume.get("interpretation"), dict)
            ):
                regime = str(price_volume.get("regime") or "") or None
                volume_state = str(price_volume.get("volume_state") or "")
                confidence = (
                    "high"
                    if price_volume.get("status") == "ready"
                    and volume_state == "expanded"
                    and regime != "high_volume_stall"
                    else "medium"
                    if price_volume.get("status") == "ready"
                    else "low"
                )
                price_volume["interpretation"] = price_volume_interpretation(
                    regime,
                    accelerated_decline=bool(price_volume.get("accelerated_decline")),
                    confidence=confidence,
                )
            plan = (profile.get("display_plan") or {}).get("plan") or {}
            market = str(profile.get("market") or "").upper()
            backfill_state = self.store.price_volume_backfills.get(
                str(profile.get("symbol") or "").upper()
            )
            if backfill_state is not None:
                quote["price_volume_backfill"] = dict(backfill_state)
            policy = plan.get("price_volume_policy")
            if (
                market in {"SH", "SZ", "BJ"}
                and isinstance(policy, dict)
                and bool(policy.get("enabled", True))
                and (
                    not isinstance(price_volume, dict)
                    or not price_volume.get("analysis_scope")
                    or "source_signature_mismatch"
                    in (price_volume.get("reason_codes") or [])
                )
            ):
                refreshed_price_volume, evidence_time = self.price_volume_analyzer.analyze(
                    market_store=market_store,
                    symbol=str(profile.get("symbol") or ""),
                    now_utc=datetime.now(timezone.utc),
                    policy={**DEFAULT_PRICE_VOLUME_POLICY, **policy},
                    allow_single_source=plan.get("data_mode") == "single_source",
                )
                quote["price_volume"] = refreshed_price_volume
                quote["price_volume_bar_time"] = evidence_time
                price_volume = refreshed_price_volume
            historical = quote.get("historical_price_volume")
            if (
                market in {"SH", "SZ", "BJ"}
                and (not isinstance(price_volume, dict) or price_volume.get("status") != "ready")
                and not isinstance(historical, dict)
            ):
                if isinstance(policy, dict) and bool(policy.get("enabled", True)):
                    historical = self.price_volume_analyzer.analyze_historical(
                        market_store=market_store,
                        symbol=str(profile.get("symbol") or ""),
                        now_utc=datetime.now(timezone.utc),
                        policy={**DEFAULT_PRICE_VOLUME_POLICY, **policy},
                        allow_single_source=plan.get("data_mode") == "single_source",
                    )
                    if historical is not None:
                        quote["historical_price_volume"] = historical
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
        if not delivery_target_id:
            delivery_target_id = self.store.get_delivery_settings().get("effective_target_id")
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
                run["build_state"] = self._autopilot_build_state(run, None, None)
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
            run["build_state"] = self._autopilot_build_state(run, job, item)
            if str(run.get("status") or "") in {"blocked", "failed"}:
                circuit = self._autopilot_circuit_state(
                    str(run.get("symbol") or ""), run, item
                )
                self_repair = run["build_state"]["self_repair"]
                self_repair.update(
                    circuit_open=bool(circuit["open"]),
                    failure_count=int(circuit["failure_count"]),
                    circuit_reason=str(circuit.get("reason") or ""),
                    token_spend_allowed=False,
                    next_retry_conditions=[
                        "daily_tail_changed",
                        "volume_signature_changed",
                        "adjustment_factor_changed",
                        "report_evidence_changed",
                        "holding_changed",
                        "method_version_changed",
                        "explicit_user_retry",
                    ],
                )
        return runs

    @staticmethod
    def _target_blocker_code(reason: str) -> str | None:
        stable = {
            "price_series_discontinuity_unverified",
            "adjustment_factor_unverified",
            "insufficient_post_event_history",
            "volume_unit_conflict",
            "no_qualified_level",
            "ai_selection_invalid",
            "recovery_circuit_open",
        }
        normalized = str(reason or "").strip()
        if normalized in stable:
            return normalized
        if "autopilot_recovery_circuit_open" in normalized:
            return "recovery_circuit_open"
        if "volume" in normalized and ("conflict" in normalized or "unit" in normalized):
            return "volume_unit_conflict"
        if normalized in {
            "deterministic_market_repair_insufficient_daily_history",
            "deterministic_market_repair_requires_verified_market",
        }:
            return "insufficient_post_event_history"
        if normalized in {
            "deterministic_market_repair_no_reproducible_range",
            "deterministic_market_repair_no_plan",
            "report_has_no_qualified_monitoring_points",
        }:
            return "no_qualified_level"
        if any(token in normalized for token in (
            "metric is not allowed",
            "candidate",
            "selection",
            "mapped source conditions",
        )):
            return "ai_selection_invalid"
        return None

    def list_monitoring_targets(self) -> list[dict[str, Any]]:
        """Return one durable UI card for every monitoring-scope target.

        Profiles, build jobs, and configured symbols intentionally overlap.  A
        single aggregate keeps newly selected holdings visible before a plan is
        created and retains superseded archives without duplicating the carousel.
        """

        config = self.get_autopilot_config()
        profiles = self.list_profiles()
        runs = self.list_autopilot_runs(limit=500)
        holdings = {
            normalize_symbol(str(item.get("symbol") or item.get("code") or "")).upper(): item
            for item in load_state().holdings
            if normalize_symbol(str(item.get("symbol") or item.get("code") or ""))
        }
        selected = {
            normalize_symbol(str(symbol)).upper()
            for symbol in config.get("selected_symbols") or []
            if normalize_symbol(str(symbol))
        }

        profiles_by_symbol: dict[str, list[dict[str, Any]]] = {}
        for profile in profiles:
            symbol = normalize_symbol(str(profile.get("symbol") or "")).upper()
            if symbol:
                profiles_by_symbol.setdefault(symbol, []).append(profile)
        latest_run_by_symbol: dict[str, dict[str, Any]] = {}
        for run in runs:
            symbol = normalize_symbol(str(run.get("symbol") or "")).upper()
            if symbol and symbol not in latest_run_by_symbol:
                latest_run_by_symbol[symbol] = run

        symbols = selected | set(profiles_by_symbol) | set(latest_run_by_symbol)
        cards: list[dict[str, Any]] = []
        for symbol in sorted(symbols):
            symbol_profiles = profiles_by_symbol.get(symbol, [])
            profile = next(
                (candidate for candidate in symbol_profiles if candidate.get("status") != "closed"),
                symbol_profiles[0] if symbol_profiles else None,
            )
            run = latest_run_by_symbol.get(symbol)
            build_state = dict((run or {}).get("build_state") or {})
            progress: dict[str, Any] = {}
            if run and run.get("planner_job_id"):
                job = self.store.get_planner_job(str(run["planner_job_id"]))
                item = next(
                    (
                        candidate for candidate in (job or {}).get("items", [])
                        if normalize_symbol(str(candidate.get("symbol") or "")).upper() == symbol
                    ),
                    None,
                )
                progress = dict((item or {}).get("progress") or {})

            display_plan = dict((profile or {}).get("display_plan") or {})
            manifest = dict(display_plan.get("evidence_manifest") or {})
            market_evidence = dict(manifest.get("market_evidence") or manifest)
            snapshot = dict(market_evidence.get("level_snapshot") or {})
            continuity = progress.get("continuity") or market_evidence.get("continuity") or snapshot.get("continuity") or {}
            level_summary = progress.get("level_summary") or snapshot.get("primary_levels") or market_evidence.get("primary_levels") or []
            volume_gate = progress.get("volume_gate") or market_evidence.get("volume_gate") or {}

            raw_reasons: list[str] = []
            raw_reasons.extend(str(value) for value in (run or {}).get("blocked_reasons") or [])
            raw_reasons.extend(str(value) for value in (profile or {}).get("blocked_reasons") or [])
            raw_reasons.extend(str(value) for value in (continuity or {}).get("blockers") or [])
            if bool((build_state.get("self_repair") or {}).get("circuit_open")):
                raw_reasons.append("recovery_circuit_open")
            blockers: list[dict[str, Any]] = []
            seen_codes: set[str] = set()
            for reason in raw_reasons:
                code = self._target_blocker_code(reason)
                if not code or code in seen_codes:
                    continue
                seen_codes.add(code)
                blockers.append({
                    "code": code,
                    "retryable": code != "recovery_circuit_open",
                    "detail": reason,
                })

            profile_status = "building"
            if blockers or build_state.get("status") in {"blocked", "failed"}:
                profile_status = "blocked"
            elif profile and str(profile.get("status") or "") == "closed":
                profile_status = "superseded"
            elif profile and str(profile.get("status") or "") == "active":
                levels = level_summary if isinstance(level_summary, list) else list((level_summary or {}).values())
                has_action_ready = any(
                    isinstance(level, dict) and level.get("automation_status") == "action_ready"
                    for level in levels
                )
                profile_status = "active" if has_action_ready or not levels else "watch_only"
            elif profile and str(profile.get("status") or "") in {"pending_review", "paused"}:
                profile_status = "watch_only"

            if not build_state:
                build_state = {
                    "status": "active" if profile_status == "active" else profile_status,
                    "stage": "ready" if profile_status in {"active", "watch_only"} else "queued",
                    "stage_label": "监控档案已建立" if profile_status in {"active", "watch_only"} else "等待建档",
                    "progress_percent": 100 if profile else 5,
                    "attempt": 0,
                    "terminal": bool(profile),
                    "updated_at": (profile or {}).get("updated_at") or (run or {}).get("updated_at"),
                    "self_repair": {
                        "policy": "bounded",
                        "infrastructure_retry_limit": 1,
                        "infrastructure_retries_used": 0,
                        "agent_iteration_limit": 2,
                        "agent_token_budget": 12000,
                        "strategy": "continuity_then_multi_method",
                        "full_report_retry_enabled": False,
                        "circuit_open": False,
                        "token_spend_allowed": False,
                    },
                }
            holding = holdings.get(symbol) or {}
            detailed_profile = (
                self.store.get_profile(str(profile.get("profile_id")))
                if profile and profile.get("profile_id")
                else profile
            )
            risk_preference = self.store.get_risk_preference(symbol)
            latest_draft = self.store.latest_condition_order_draft(symbol)
            decision = self.decision_engine.build(
                symbol=symbol,
                name=str(holding.get("name") or (profile or {}).get("name") or symbol.split(".", 1)[0]),
                profile_status=profile_status,
                blockers=blockers,
                continuity=dict(continuity or {}),
                volume_gate=dict(volume_gate or {}),
                snapshot=snapshot,
                market_evidence=market_evidence,
                profile=detailed_profile,
                holding=holding,
                risk_preference=risk_preference,
                latest_draft=latest_draft,
            )
            if latest_draft and latest_draft.get("status") in {"draft", "validated"}:
                draft_status: str | None = None
                valid_until = str(latest_draft.get("valid_until") or "")
                if valid_until:
                    try:
                        if datetime.fromisoformat(valid_until.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                            draft_status = "expired"
                    except ValueError:
                        draft_status = "stale"
                if (
                    draft_status is None
                    and latest_draft.get("evidence_fingerprint")
                    != decision.get("evidence_fingerprint")
                ):
                    draft_status = "stale"
                if draft_status:
                    latest_draft = self.store.update_condition_order_draft_status(
                        str(latest_draft["draft_id"]), draft_status
                    )
                    decision["latest_draft"] = latest_draft
            cards.append({
                "symbol": symbol,
                "name": holding.get("name") or (profile or {}).get("name") or symbol.split(".", 1)[0],
                "profile_id": (profile or {}).get("profile_id"),
                "profile_status": profile_status,
                "build_state": build_state,
                "blockers": blockers,
                "continuity": continuity,
                "level_summary": level_summary,
                "volume_gate": volume_gate,
                "self_repair": build_state.get("self_repair") or {},
                "decision_id": decision["decision_id"],
                "decision_revision": decision["decision_revision"],
                "evidence_fingerprint": decision["evidence_fingerprint"],
                "level_snapshot_id": decision["level_snapshot_id"],
                "decision_brief": decision["decision_brief"],
                "risk_assessment": decision["risk_assessment"],
                "level_ladder": decision["level_ladder"],
                "action_playbook": decision["action_playbook"],
                "available_choices": decision["available_choices"],
                "monitoring_thesis": decision["monitoring_thesis"],
                "scenario_comparison": decision["scenario_comparison"],
                "risk_preference": decision["risk_preference"],
                "latest_draft": decision["latest_draft"],
                "thesis_changed_at": decision["thesis_changed_at"],
                "selection_mode": decision["selection_mode"],
                "selected": symbol in selected,
                "updated_at": build_state.get("updated_at") or (profile or {}).get("updated_at") or (run or {}).get("updated_at"),
            })
        return cards

    def get_target_decision(self, symbol: str) -> dict[str, Any]:
        normalized = normalize_symbol(symbol).upper()
        card = next(
            (item for item in self.list_monitoring_targets() if item.get("symbol") == normalized),
            None,
        )
        if card is None:
            raise KeyError(normalized)
        return card

    def set_risk_preference(self, symbol: str, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_symbol(symbol).upper()
        if not normalized:
            raise ValueError("symbol is required")
        preference = validate_risk_preference(payload)
        return self.store.save_risk_preference(normalized, preference)

    def record_decision_choice(
        self,
        *,
        decision_id: str,
        choice_id: str,
        decision_revision: int,
        evidence_fingerprint: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        card = next(
            (item for item in self.list_monitoring_targets() if item.get("decision_id") == decision_id),
            None,
        )
        if card is None:
            raise ValueError("decision is stale or no longer available")
        if int(card.get("decision_revision") or 0) != int(decision_revision):
            raise ValueError("decision revision is stale")
        if str(card.get("evidence_fingerprint") or "") != str(evidence_fingerprint or ""):
            raise ValueError("decision evidence has changed")
        choices = {item.get("choice_id"): item for item in card.get("available_choices") or []}
        if choice_id not in choices:
            raise ValueError("choice is not available for the current decision")
        return self.store.record_decision_choice(
            {
                "decision_id": decision_id,
                "symbol": card["symbol"],
                "choice_id": choice_id,
                "decision_revision": decision_revision,
                "evidence_fingerprint": evidence_fingerprint,
                "idempotency_key": idempotency_key,
                "status": "recorded",
                "choice": choices[choice_id],
                "trade_execution": "forbidden",
            }
        )

    def create_condition_order_draft(
        self,
        *,
        decision_id: str,
        choice_id: str,
        decision_revision: int,
        evidence_fingerprint: str,
    ) -> dict[str, Any]:
        card = next(
            (item for item in self.list_monitoring_targets() if item.get("decision_id") == decision_id),
            None,
        )
        if card is None:
            raise ValueError("decision is stale or no longer available")
        if int(card.get("decision_revision") or 0) != int(decision_revision):
            raise ValueError("decision revision is stale")
        if str(card.get("evidence_fingerprint") or "") != str(evidence_fingerprint or ""):
            raise ValueError("decision evidence has changed")
        state = load_state()
        holding = next(
            (
                item
                for item in state.holdings
                if normalize_symbol(str(item.get("symbol") or item.get("code") or "")).upper()
                == card["symbol"]
            ),
            {},
        )
        decision = {
            key: card.get(key)
            for key in (
                "decision_id", "decision_revision", "evidence_fingerprint",
                "level_snapshot_id",
                "available_choices", "monitoring_thesis", "risk_assessment", "level_ladder",
            )
        }
        draft = self.decision_engine.create_draft(
            decision=decision,
            choice_id=choice_id,
            risk_preference=self.store.get_risk_preference(card["symbol"]),
            holding=holding,
            cash=state.cash,
        )
        return self.store.save_condition_order_draft(draft)

    def validate_condition_order_draft(self, draft_id: str) -> dict[str, Any]:
        draft = self.store.get_condition_order_draft(draft_id)
        if draft is None:
            raise KeyError(draft_id)
        card = self.get_target_decision(str(draft.get("symbol") or ""))
        if draft.get("evidence_fingerprint") != card.get("evidence_fingerprint"):
            return self.store.update_condition_order_draft_status(draft_id, "stale")
        valid_until = datetime.fromisoformat(str(draft["valid_until"]).replace("Z", "+00:00"))
        if valid_until <= datetime.now(timezone.utc):
            return self.store.update_condition_order_draft_status(draft_id, "expired")
        if draft.get("status") != "draft" or not draft.get("quantity"):
            raise ValueError("draft is not eligible for validation")
        return self.store.update_condition_order_draft_status(draft_id, "validated")

    def cancel_condition_order_draft(self, draft_id: str) -> dict[str, Any]:
        if self.store.get_condition_order_draft(draft_id) is None:
            raise KeyError(draft_id)
        return self.store.update_condition_order_draft_status(draft_id, "cancelled")

    def _quarantine_unsafe_autopilot_profile(
        self,
        symbol: str,
        blocked_reasons: list[str],
    ) -> str | None:
        """Close an active autonomous plan when its price basis is no longer safe."""

        profile = self.store.get_profile_by_symbol(symbol)
        if not profile or str(profile.get("status") or "") == "closed":
            return None
        plans = list(profile.get("plans") or [])
        active_version = int(profile.get("active_plan_version") or 0)
        active_plan = next(
            (
                candidate
                for candidate in plans
                if int(candidate.get("version") or 0) == active_version
            ),
            plans[0] if plans else {},
        )
        if str(active_plan.get("created_by") or "") != "autopilot":
            return None
        methods = {
            str((rule.get("calculation_basis") or {}).get("method") or "")
            for rule in ((active_plan.get("plan") or {}).get("market_rules") or [])
            if isinstance(rule, dict)
        }
        legacy = bool(methods & _LEGACY_LEVEL_METHODS)
        hard_block = next(
            (reason for reason in blocked_reasons if reason in _HARD_STRUCTURAL_BLOCK_REASONS),
            None,
        )
        if not legacy and hard_block is None:
            return None
        reason = "level_method_migration" if legacy else str(hard_block)
        try:
            self.store.close_autopilot_profile(
                str(profile["profile_id"]),
                delivery_mode=str(
                    self.store.get_autopilot_config().get("runtime_mode") or "shadow"
                ),
                reason=reason,
            )
        except (KeyError, ValueError):
            return None
        return reason

    def _autopilot_input_key(
        self,
        run: dict[str, Any],
        item: dict[str, Any] | None,
    ) -> str:
        payload = dict(run.get("payload") or {})
        progress = dict((item or {}).get("progress") or {})
        structural = {
            key: progress.get(key)
            for key in (
                "daily_tail_hash",
                "intraday_tail_hash",
                "volume_signature",
                "adjustment_factor_revision",
                "method_registry_version",
                "level_snapshot_id",
                "continuity",
                "volume_gate",
            )
            if progress.get(key) not in (None, "", [], {})
        }
        # Holding and report evidence are allowed circuit-reset signals too, but
        # report_ref itself is deliberately excluded because a newly frozen
        # snapshot can receive a different identifier without changing any
        # decision evidence.
        if payload.get("holding_hash"):
            structural["holding_hash"] = payload["holding_hash"]
        if payload.get("report_hash"):
            structural["report_hash"] = payload["report_hash"]
        symbol = normalize_symbol(str(run.get("symbol") or "")).upper()
        risk_preference = self.store.get_risk_preference(symbol) if symbol else None
        if risk_preference:
            structural["risk_preference_revision"] = risk_preference.get("revision")
        if structural:
            return hashlib.sha256(
                json.dumps(
                    structural,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        parts = [
            str(payload.get("report_hash") or ""),
            str(payload.get("holding_hash") or ""),
            str(payload.get("evidence_fingerprint") or run.get("evidence_fingerprint") or ""),
        ]
        return "|".join(parts) if any(parts) else ""

    def _current_autopilot_input_item(self, symbol: str) -> dict[str, Any] | None:
        """Build a read-only structural fingerprint before spending model tokens.

        A queued trigger has no planner item yet.  Recomputing only the
        deterministic market snapshot lets the circuit distinguish genuinely
        changed tails/factors/methods from a repeat of the same blocked input.
        """

        holding = self._holding(symbol)
        if holding is None:
            return None
        try:
            _plan, evidence, _blocked = self.planner.build(holding)
        except Exception:
            return None
        return {
            "progress": {
                key: evidence.get(key)
                for key in (
                    "daily_tail_hash",
                    "intraday_tail_hash",
                    "volume_signature",
                    "adjustment_factor_revision",
                    "method_registry_version",
                    "level_snapshot_id",
                    "continuity",
                    "volume_gate",
                )
                if evidence.get(key) not in (None, "", [], {})
            }
        }

    def _autopilot_circuit_state(
        self,
        symbol: str,
        current_run: dict[str, Any],
        current_item: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Open a no-token circuit after repeated failures on unchanged inputs."""

        current_key = self._autopilot_input_key(current_run, current_item)
        failures: list[dict[str, Any]] = []
        failure_groups: dict[str, list[dict[str, Any]]] = {}
        hard_reason = ""
        for run in self.store.list_autopilot_triggers(limit=100):
            if str(run.get("symbol") or "") != symbol:
                continue
            if str(run.get("status") or "") not in {"blocked", "failed"}:
                continue
            job_id = str(run.get("planner_job_id") or "")
            job = self.store.get_planner_job(job_id) if job_id else None
            item = next(
                (
                    candidate
                    for candidate in (job or {}).get("items", [])
                    if str(candidate.get("symbol") or "") == symbol
                ),
                None,
            )
            blocked_reasons = list((item or {}).get("blocked_reasons") or [])
            failure_key = self._autopilot_input_key(run, item)
            if current_key and failure_key and current_key != failure_key:
                continue
            if "structural_report_refresh_retry_exhausted" in blocked_reasons:
                hard_reason = "structural_report_refresh_retry_exhausted"
            failure = {
                "trigger_id": run.get("trigger_id"),
                "reason": blocked_reasons[0] if blocked_reasons else str(
                    (item or {}).get("error") or run.get("error") or "planner_failed"
                ),
            }
            group = failure_groups.setdefault(str(failure["reason"]), [])
            group.append(failure)
            if hard_reason or len(group) >= _AUTOPILOT_FAILURE_CIRCUIT_THRESHOLD:
                failures = group
                break
        if not failures and failure_groups:
            failures = max(failure_groups.values(), key=len)
        reason = hard_reason or (failures[-1]["reason"] if failures else "")
        return {
            "open": bool(
                hard_reason
                or len(failures) >= _AUTOPILOT_FAILURE_CIRCUIT_THRESHOLD
            ),
            "failure_count": len(failures),
            "threshold": _AUTOPILOT_FAILURE_CIRCUIT_THRESHOLD,
            "reason": reason,
            "input_key": current_key,
        }

    @staticmethod
    def _autopilot_build_state(
        run: dict[str, Any],
        job: dict[str, Any] | None,
        item: dict[str, Any] | None,
    ) -> dict[str, Any]:
        progress = dict((item or {}).get("progress") or {})
        stage = str(progress.get("stage") or "")
        item_status = str((item or {}).get("status") or "")
        run_status = str(run.get("status") or "queued")
        if not stage:
            stage = item_status or ("reconciling" if run_status == "completed" else run_status)
        stage_progress = {
            "queued": (5, "等待建档"),
            "report_frozen": (10, "已锁定研究报告"),
            "market_refresh": (15, "刷新只读行情"),
            "continuity_check": (30, "检查价格连续性"),
            "structure_analysis": (50, "计算多方法结构点位"),
            "researching": (50, "补齐研究证据"),
            "planning": (65, "AI选择结构候选"),
            "ai_selection": (65, "选择结构候选"),
            "action_playbook": (75, "生成行动剧本"),
            "rule_mapping": (85, "映射监控规则"),
            "gate_validation": (95, "执行启用门禁"),
            "structural_report_refresh_requested": (50, "刷新结构化研究"),
            "structural_report_refresh_completed": (60, "研究刷新完成，重新规划"),
            "deterministic_market_repair": (50, "计算多方法结构点位"),
            "validating": (95, "执行启用门禁"),
            "ready": (100, "监控档案已建立"),
            "blocked": (100, "建档受阻"),
            "failed": (100, "建档失败"),
            "cancelled": (100, "建档已取消"),
            "reconciling": (60, "核对未完成任务"),
            "structural_report_refresh_failed_validation": (100, "研究结果未通过门禁"),
            "structural_report_refresh_retry_exhausted": (100, "自动修复已停止"),
            "structural_report_refresh_queue_failed": (100, "研究任务入队失败"),
            "no_action": (100, "未形成监控档案"),
        }
        percent, label = stage_progress.get(
            stage,
            stage_progress.get(item_status, (20, "准备监控档案")),
        )
        blocked_reasons = list((item or {}).get("blocked_reasons") or [])
        refresh = dict(progress.get("structural_report_refresh") or {})
        retry_used = int(refresh.get("retry_attempt") or 0)
        circuit_open = bool(
            str(run.get("error") or "").startswith("autopilot_recovery_circuit_open")
            or "structural_report_refresh_retry_exhausted" in blocked_reasons
        )
        legacy_no_profile_terminal = bool(
            item_status == "ready"
            and not (item or {}).get("profile_id")
            and stage == "no_action"
        )
        active_terminal = bool(
            item_status == "ready" and (item or {}).get("profile_id")
        )
        terminal = (
            run_status in {"blocked", "failed", "cancelled"}
            or item_status in {"blocked", "failed", "cancelled"}
            or active_terminal
            or legacy_no_profile_terminal
        )
        return {
            "status": (
                "active"
                if (item or {}).get("profile_id") and item_status == "ready"
                else "blocked"
                if run_status == "blocked" or item_status == "blocked" or legacy_no_profile_terminal
                else "failed"
                if run_status == "failed" or item_status == "failed"
                else "cancelled"
                if run_status == "cancelled" or item_status == "cancelled"
                else "building"
            ),
            "stage": stage,
            "stage_label": label,
            "progress_percent": percent,
            "planner_status": str((job or {}).get("status") or ""),
            "item_status": item_status,
            "attempt": int((item or {}).get("attempt") or 0),
            "profile_id": (item or {}).get("profile_id"),
            "plan_version": (item or {}).get("plan_version"),
            "updated_at": (item or {}).get("updated_at") or run.get("updated_at"),
            "terminal": terminal,
            "self_repair": {
                "policy": "bounded",
                "infrastructure_retry_limit": _AUTOPILOT_INFRASTRUCTURE_RETRY_LIMIT,
                "infrastructure_retries_used": retry_used,
                "agent_iteration_limit": 2,
                "agent_token_budget": 12000,
                "strategy": "continuity_then_multi_method",
                "full_report_retry_enabled": False,
                "circuit_open": circuit_open,
                "token_spend_allowed": not circuit_open and stage == "ai_selection",
            },
        }

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
        if trigger_type in {"approaching", "invalidated"}:
            # These are durable runtime observations, not changes to the
            # evidence used to build a monitoring profile.  Re-planning here
            # creates a feedback loop: a new plan opens a new watch episode,
            # which emits another approaching event and creates yet another
            # plan.  The monitoring event/recommendation pipeline has already
            # persisted the observation and remains responsible for alerts.
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

    def _settle_planner_job(self, job_id: str) -> str | None:
        """Derive durable job/trigger state without treating background research as done."""

        latest = self.store.get_planner_job(job_id)
        if not latest or latest["status"] == "cancelled":
            return None
        statuses = [str(item["status"]) for item in latest["items"]]
        non_terminal = {
            "queued",
            "researching",
            "planning",
            "validating",
            "continuity_check",
            "structure_analysis",
            "ai_selection",
            "action_playbook",
            "rule_mapping",
            "gate_validation",
        }
        if any(status in non_terminal for status in statuses):
            terminal = "researching"
        elif any(status == "ready" for status in statuses):
            terminal = "ready"
        elif statuses and all(status == "blocked" for status in statuses):
            terminal = "blocked"
        elif statuses and all(status == "cancelled" for status in statuses):
            terminal = "cancelled"
        else:
            terminal = "failed"
        self.store.update_planner_job_status(job_id, terminal)
        trigger_id = str(latest.get("autopilot_trigger_id") or "")
        if trigger_id:
            if terminal == "researching":
                trigger_status = "running"
                trigger_error = None
            else:
                trigger_status = "completed" if terminal == "ready" else terminal
                trigger_error = None if terminal == "ready" else f"planner_job_{terminal}"
            self.store.update_autopilot_trigger(
                trigger_id,
                status=trigger_status,
                planner_job_id=job_id,
                evidence_fingerprint=latest.get("evidence_fingerprint"),
                error=trigger_error,
            )
        return terminal

    def _reconcile_structural_report_refreshes(self) -> int:
        """Poll durable child reports and repair pre-fix false-completed planner rows."""

        reconciled = 0
        triggers = [
            trigger
            for trigger in self.store.list_autopilot_triggers(limit=500)
            if str(trigger.get("status") or "") in {"running", "completed"}
            and trigger.get("planner_job_id")
        ]
        for trigger in triggers:
            job_id = str(trigger["planner_job_id"])
            job = self.store.get_planner_job(job_id)
            if not job:
                continue
            changed = False
            should_submit = False
            for item in job.get("items") or []:
                progress = dict(item.get("progress") or {})
                if (
                    progress.get("stage") == "no_action"
                    and progress.get("outcome") == "report_has_no_qualified_monitoring_points"
                    and str(item.get("status") or "") == "ready"
                    and not item.get("profile_id")
                ):
                    refresh = dict(progress.get("structural_report_refresh") or {})
                    reason = (
                        "structural_report_refresh_failed_validation"
                        if str(refresh.get("report_quality_status") or "") == "failed_validation"
                        else "report_has_no_qualified_monitoring_points"
                    )
                    self.store.update_planner_item(
                        job_id,
                        str(item.get("symbol") or ""),
                        status="blocked",
                        report_ref=item.get("report_ref"),
                        report_snapshot_id=item.get("report_snapshot_id"),
                        research_date=item.get("research_date"),
                        blocked_reasons=[reason],
                        progress={
                            **progress,
                            "stage": reason,
                            "outcome": reason,
                        },
                        error=str(refresh.get("error") or "") or None,
                    )
                    changed = True
                    reconciled += 1
                    continue
                if (
                    progress.get("stage") != "structural_report_refresh_requested"
                    or str(item.get("status") or "") not in {"ready", "researching"}
                    or item.get("profile_id")
                ):
                    continue
                symbol = str(item.get("symbol") or "").upper()
                if not symbol or not self._autopilot_authorized(symbol):
                    self.store.update_planner_item(job_id, symbol, status="cancelled")
                    changed = True
                    continue
                holding = self._holding(symbol)
                selected_candidate, _candidate_reasons = self.report_catalog.choose_candidate(
                    symbol,
                    str(item.get("report_ref") or "") or None,
                )
                selected_candidate = dict(selected_candidate or {})
                selected_metadata = (
                    dict(selected_candidate.get("metadata") or {})
                    if isinstance(selected_candidate.get("metadata"), dict)
                    else {}
                )
                item_report_ref = str(item.get("report_ref") or "")
                parent_report_id = str(
                    progress.get("structural_parent_report_id")
                    or progress.get("catalog_report_id")
                    or selected_metadata.get("report_id")
                    or selected_candidate.get("source_id")
                    or (
                        item_report_ref.removeprefix("deep-report:")
                        if item_report_ref.startswith("deep-report:")
                        else ""
                    )
                )
                report_type = str(
                    progress.get("structural_report_type")
                    or selected_candidate.get("report_type")
                    or ""
                )
                if holding is None or not parent_report_id or not report_type:
                    self.store.update_planner_item(
                        job_id,
                        symbol,
                        status="failed",
                        report_ref=item.get("report_ref"),
                        report_snapshot_id=item.get("report_snapshot_id"),
                        error="structural_refresh_reconciliation_context_missing",
                        progress=progress,
                    )
                    changed = True
                    continue
                selected = {
                    **selected_candidate,
                    "report_ref": str(item.get("report_ref") or f"deep-report:{parent_report_id}"),
                    "report_type": report_type,
                    "source_id": parent_report_id,
                    "monitoring_bundle_sha256": str(
                        progress.get("structural_source_bundle_sha256")
                        or selected_candidate.get("monitoring_bundle_sha256")
                        or ""
                    ),
                    "metadata": {
                        **selected_metadata,
                        "report_id": parent_report_id,
                        "generation_source": str(
                            progress.get("structural_generation_source")
                            or selected_metadata.get("generation_source")
                            or "manual"
                        ),
                    },
                }
                try:
                    refresh = self._maybe_queue_structural_report_refresh(
                        autonomous=True,
                        job_id=job_id,
                        symbol=symbol,
                        holding=holding,
                        selected=selected,
                        research_date=str(
                            item.get("research_date")
                            or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
                        ),
                        trigger_type=str(job.get("trigger_type") or ""),
                        bundle_status=str(
                            progress.get("structural_bundle_status") or "not_recommended"
                        ),
                    )
                except Exception as exc:
                    refresh = {
                        "status": "queue_failed",
                        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    }
                refresh = dict(refresh or {})
                refresh_status = str(refresh.get("status") or "")
                next_progress = {
                    **progress,
                    "structural_report_refresh": refresh,
                    "refresh_attempted": True,
                    "structural_parent_report_id": parent_report_id,
                    "structural_report_type": report_type,
                    "structural_generation_source": str(
                        selected["metadata"].get("generation_source") or "manual"
                    ),
                    "structural_source_bundle_sha256": str(
                        selected.get("monitoring_bundle_sha256") or ""
                    ),
                    "structural_bundle_status": str(
                        progress.get("structural_bundle_status")
                        or (selected.get("monitoring_bundle") or {}).get("monitoring_status")
                        or "not_recommended"
                    ),
                }
                if refresh_status in {"queued", "pending", "running"}:
                    self.store.update_planner_item(
                        job_id,
                        symbol,
                        status="researching",
                        report_ref=item.get("report_ref"),
                        report_snapshot_id=item.get("report_snapshot_id"),
                        research_date=item.get("research_date"),
                        progress=next_progress,
                    )
                elif (
                    refresh_status == "refresh_already_attempted"
                    and str(refresh.get("refresh_outcome") or "") == "completed"
                ):
                    report_id = str(refresh.get("report_id") or "")
                    quality_status = str(refresh.get("report_quality_status") or "")
                    if report_id and quality_status != "failed_validation":
                        self.store.requeue_planner_item_for_report(
                            job_id,
                            symbol,
                            report_ref=f"deep-report:{report_id}",
                            progress={
                                **next_progress,
                                "stage": "structural_report_refresh_completed",
                                "outcome": "report_refresh_completed_replan_queued",
                                "refreshed_report_id": report_id,
                                "refreshed_report_quality_status": quality_status,
                            },
                        )
                        should_submit = True
                    else:
                        self.store.update_planner_item(
                            job_id,
                            symbol,
                            status="blocked",
                            report_ref=item.get("report_ref"),
                            report_snapshot_id=item.get("report_snapshot_id"),
                            research_date=item.get("research_date"),
                            blocked_reasons=["structural_report_refresh_failed_validation"],
                            progress={
                                **next_progress,
                                "stage": "structural_report_refresh_failed_validation",
                                "outcome": "report_has_no_qualified_monitoring_points",
                                "refreshed_report_id": report_id or None,
                                "refreshed_report_quality_status": quality_status or None,
                            },
                        )
                elif refresh_status == "refresh_already_attempted":
                    refresh_outcome = str(refresh.get("refresh_outcome") or "")
                    terminal_status = "failed" if refresh_outcome in {"failed", "cancelled"} else "blocked"
                    reason = (
                        "structural_report_refresh_retry_exhausted"
                        if refresh.get("retry_exhausted")
                        else "report_has_no_qualified_monitoring_points"
                    )
                    self.store.update_planner_item(
                        job_id,
                        symbol,
                        status=terminal_status,
                        report_ref=item.get("report_ref"),
                        report_snapshot_id=item.get("report_snapshot_id"),
                        research_date=item.get("research_date"),
                        blocked_reasons=[reason],
                        progress={
                            **next_progress,
                            "stage": reason,
                            "outcome": reason,
                        },
                        error=str(refresh.get("error") or reason),
                    )
                else:
                    self.store.update_planner_item(
                        job_id,
                        symbol,
                        status="failed",
                        report_ref=item.get("report_ref"),
                        report_snapshot_id=item.get("report_snapshot_id"),
                        research_date=item.get("research_date"),
                        blocked_reasons=["structural_report_refresh_queue_failed"],
                        progress={
                            **next_progress,
                            "stage": "structural_report_refresh_queue_failed",
                            "outcome": "structural_report_refresh_queue_failed",
                        },
                        error=str(refresh.get("error") or "structural report refresh queue failed"),
                    )
                changed = True
                reconciled += 1
            if changed:
                self._settle_planner_job(job_id)
            if should_submit:
                self._submit_planner_job(job_id)
        return reconciled

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
            reconciled_refreshes = self._reconcile_structural_report_refreshes()
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
                                "holding_hash": holding_fingerprint,
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
                        payload={
                            **identity_payload,
                            "market_date": close_key,
                            "holding_hash": holding_fingerprint,
                            "report_ref": (selected or {}).get("report_ref"),
                            "report_hash": report_hash or None,
                        },
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
                                            "holding_hash": holding_fingerprint,
                                            "report_ref": selected_for_probe.get("report_ref"),
                                            "report_hash": report_hash or None,
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
                circuit = self._autopilot_circuit_state(
                    symbol,
                    trigger,
                    self._current_autopilot_input_item(symbol),
                )
                if circuit["open"]:
                    self.store.update_autopilot_trigger(
                        str(trigger["trigger_id"]),
                        status="blocked",
                        error=(
                            "autopilot_recovery_circuit_open:"
                            f"{circuit['reason'] or 'repeated_unchanged_input_failure'}"
                        ),
                    )
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
                "reconciled_refreshes": reconciled_refreshes,
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
        self._settle_planner_job(job_id)

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
        structured_bundle = isinstance(selected.get("monitoring_bundle"), dict)
        # Autonomous monitoring levels come from the deterministic market
        # engine. A missing/stale/conflicting report remains visible context,
        # but must not trigger a model call or block the market-data repair.
        research_required = (
            bool(research_reasons) and not structured_bundle and not autonomous
        )
        bundle_status = str((selected.get("monitoring_bundle") or {}).get("monitoring_status") or "")
        bundle_candidates = (selected.get("monitoring_bundle") or {}).get("candidates") or []
        research_date = now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        structural_market_repair = bool(
            structured_bundle
            and bundle_status in {"not_recommended", "data_insufficient"}
            and not bundle_candidates
        )
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
                if any(
                    reason in {
                        "price_series_discontinuity_unverified",
                        "adjustment_factor_unverified",
                    }
                    for reason in evidence_blocked
                ):
                    self.store.update_planner_item(
                        job_id,
                        symbol,
                        status="planning",
                        report_ref=snapshot["report_ref"],
                        report_snapshot_id=snapshot["snapshot_id"],
                        progress={
                            "stage": "continuity_check",
                            "adjustment_evidence_refresh": "requested_once",
                        },
                    )
                    try:
                        self.planner.market_service.refresh_sync(
                            symbols=[symbol],
                            profile="portfolio_monitor_adjustment_factor_validation",
                            items=[("1D", "qfq")],
                            force=True,
                            read_only=True,
                            deadline=time.monotonic() + 30,
                        )
                    except Exception:
                        # The original continuity blocker is more actionable
                        # than a transient provider exception and remains the
                        # durable reason shown on the target card.
                        pass
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
            if autonomous:
                on_stage(
                    "continuity_check",
                    {
                        "repair_strategy": "continuity_then_multi_method_no_model",
                        "model_calls": 0,
                        "source_bundle_status": bundle_status or "unavailable",
                        "report_candidates_role": "priority_evidence_only",
                    },
                )
                on_stage(
                    "structure_analysis",
                    {
                        "method_registry_version": METHOD_REGISTRY_VERSION,
                        "model_calls": 0,
                    },
                )
                plan, manifest, research_candidate = (
                    self.report_planner.build_from_verified_market_repair(
                        holding=holding,
                        report_snapshot=snapshot,
                        autonomous=True,
                    )
                )
                on_stage(
                    "ai_selection",
                    {
                        "selection_mode": "deterministic_fallback",
                        "model_calls": 0,
                        "token_spend": 0,
                    },
                )
                on_stage(
                    "action_playbook",
                    {"sizing_source": "requires_user_risk_preferences"},
                )
                on_stage(
                    "rule_mapping",
                    {
                        "level_snapshot_id": (
                            (manifest.get("market_evidence") or {}).get("level_snapshot_id")
                        ),
                    },
                )
            elif structured_bundle:
                if research_reasons:
                    raise PlanValidationError(", ".join(research_reasons))
                bundle_horizon = str(
                    (selected.get("monitoring_bundle") or {}).get("horizon") or ""
                )
                if structural_market_repair:
                    on_stage(
                        "continuity_check",
                        {
                            "repair_strategy": "continuity_then_multi_method_no_model",
                            "model_calls": 0,
                            "source_bundle_status": bundle_status,
                            "report_candidates_role": "priority_evidence_only",
                        },
                    )
                    on_stage(
                        "structure_analysis",
                        {
                            "method_registry_version": METHOD_REGISTRY_VERSION,
                            "model_calls": 0,
                        },
                    )
                    plan, manifest, research_candidate = (
                        self.report_planner.build_from_verified_market_repair(
                            holding=holding,
                            report_snapshot=snapshot,
                            autonomous=autonomous,
                        )
                    )
                    on_stage(
                        "ai_selection",
                        {
                            "selection_mode": "deterministic_fallback",
                            "model_calls": 0,
                            "token_spend": 0,
                        },
                    )
                    on_stage(
                        "action_playbook",
                        {"sizing_source": "requires_user_risk_preferences"},
                    )
                    on_stage(
                        "rule_mapping",
                        {
                            "level_snapshot_id": (
                                (manifest.get("market_evidence") or {}).get("level_snapshot_id")
                            ),
                        },
                    )
                elif bundle_horizon == "structural":
                    plan, manifest, research_candidate = (
                        self.report_planner.build_from_structural_monitoring_bundle(
                            holding=holding,
                            report_snapshot=snapshot,
                        )
                    )
                else:
                    plan, manifest, research_candidate = (
                        self.report_planner.build_from_monitoring_bundle(
                            holding=holding,
                            report_snapshot=snapshot,
                            autonomous=autonomous,
                        )
                    )
            else:
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
                from src.reports.catalog import register_monitor_research_safely

                register_monitor_research_safely(
                    research_candidate,
                    snapshot_id=research_snapshot_id,
                )
                plan, manifest = self.report_planner.finalize_research_snapshot(
                    plan,
                    manifest,
                    research_snapshot,
                )
            if self.store.planner_job_cancel_requested(job_id):
                raise InterruptedError("planner job cancelled")
            if autonomous and not self._autopilot_authorized(symbol):
                raise InterruptedError("autopilot symbol is no longer selected")
            on_stage(
                "gate_validation",
                {
                    "trade_execution": "forbidden",
                    "selection_mode": (manifest.get("market_evidence") or {}).get(
                        "selection_mode"
                    ),
                },
            )
            market = symbol.rsplit(".", 1)[-1] if "." in symbol else "UNKNOWN"
            code = symbol.split(".")[0]
            instrument_type = (
                "etf"
                if code.startswith(("15", "16", "50", "51", "52", "56", "58"))
                else "company_equity"
            )
            report_requires_manual_activation = bool(
                structured_bundle
                and (selected.get("monitoring_bundle") or {}).get("activation_policy")
                == "manual_confirmation_required"
            )
            requires_manual_activation = bool(
                manifest.get(
                    "requires_manual_activation",
                    report_requires_manual_activation,
                )
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
                    "autonomous_analysis": {
                        "analyzed_by": "autopilot",
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
                        "report_activation_decision": {
                            "source_requires_manual_confirmation": (
                                report_requires_manual_activation
                            ),
                            "autonomous_approval": bool(
                                (manifest.get("autonomous_report_approval") or {}).get(
                                    "approved"
                                )
                            ),
                            "decision_authority": (
                                "selected_ai_autonomous_holding"
                                if not requires_manual_activation
                                and report_requires_manual_activation
                                else "source_report_policy"
                            ),
                        },
                    },
                }
                if (
                    not requires_manual_activation
                    and previous_profile
                    and previous_profile.get("status") == "closed"
                ):
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
                model_id=str(
                    manifest.get("planner_model_id") or self.report_planner.model_id
                ),
                created_by="autopilot" if autonomous else "monitor_planner",
            )
            activated = False
            if autonomous and not requires_manual_activation:
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
                    "requires_manual_activation": (
                        requires_manual_activation or not autonomous
                    ),
                    "activated": activated,
                    "activation_mode": (
                        "autonomous_analysis"
                        if autonomous and requires_manual_activation
                        else "autonomous"
                        if autonomous
                        else "manual"
                    ),
                    "catalog_report_id": selected.get("catalog_report_id"),
                    "source_horizon": (selected.get("monitoring_bundle") or {}).get("horizon"),
                    "level_snapshot_id": (manifest.get("market_evidence") or {}).get(
                        "level_snapshot_id"
                    ),
                    "continuity": (manifest.get("market_evidence") or {}).get("continuity"),
                    "level_summary": (
                        (manifest.get("market_evidence") or {}).get("level_snapshot") or {}
                    ).get("primary_levels"),
                    "volume_gate": (manifest.get("market_evidence") or {}).get("volume_gate"),
                    "selection_mode": (manifest.get("market_evidence") or {}).get(
                        "selection_mode"
                    ),
                    "daily_tail_hash": (manifest.get("market_evidence") or {}).get(
                        "daily_tail_hash"
                    ),
                    "intraday_tail_hash": (manifest.get("market_evidence") or {}).get(
                        "intraday_tail_hash"
                    ),
                    "volume_signature": (manifest.get("market_evidence") or {}).get(
                        "volume_signature"
                    ),
                    "adjustment_factor_revision": (
                        manifest.get("market_evidence") or {}
                    ).get("adjustment_factor_revision"),
                    "method_registry_version": (manifest.get("market_evidence") or {}).get(
                        "method_registry_version"
                    ),
                    "trade_execution": "forbidden",
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
            # Autonomous profiles are market-structure first.  Missing/stale
            # reports may affect candidate priority, but must never masquerade
            # as the reason a deterministic build is blocked.
            blocked_reasons = [] if autonomous else [*research_reasons]
            if isinstance(exc, DeterministicMarketRepairError):
                blocked_reasons.extend(exc.reasons)
                repair_evidence = dict(exc.evidence)
                if autonomous:
                    quarantine_reason = self._quarantine_unsafe_autopilot_profile(
                        symbol,
                        list(dict.fromkeys(blocked_reasons)),
                    )
                    if quarantine_reason:
                        repair_evidence["superseded_profile_reason"] = quarantine_reason
            else:
                blocked_reasons.append("planner_validation_failed")
                repair_evidence = {}
            self.store.update_planner_item(
                job_id,
                symbol,
                status="blocked",
                report_ref=snapshot["report_ref"],
                report_snapshot_id=snapshot["snapshot_id"],
                research_date=research_date if research_required else None,
                blocked_reasons=list(dict.fromkeys(blocked_reasons)),
                validation_errors=[str(exc)],
                progress={
                    "stage": "blocked",
                    "level_snapshot_id": repair_evidence.get("level_snapshot_id"),
                    "continuity": repair_evidence.get("continuity"),
                    "level_summary": (
                        repair_evidence.get("level_snapshot") or {}
                    ).get("primary_levels"),
                    "volume_gate": repair_evidence.get("volume_gate"),
                    "selection_mode": repair_evidence.get("selection_mode"),
                    "daily_tail_hash": repair_evidence.get("daily_tail_hash"),
                    "intraday_tail_hash": repair_evidence.get("intraday_tail_hash"),
                    "volume_signature": repair_evidence.get("volume_signature"),
                    "adjustment_factor_revision": repair_evidence.get(
                        "adjustment_factor_revision"
                    ),
                    "method_registry_version": repair_evidence.get(
                        "method_registry_version"
                    ),
                    "superseded_profile_reason": repair_evidence.get(
                        "superseded_profile_reason"
                    ),
                },
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
        if not delivery_target_id:
            delivery_target_id = self.store.get_delivery_settings().get("effective_target_id")
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
