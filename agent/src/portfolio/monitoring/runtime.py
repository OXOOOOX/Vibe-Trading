"""Single-leader async runtime for active monitor profiles."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from src.channels.bus.events import (
    DeliveryReceipt,
    DeliveryRejectedError,
    DeliveryUncertainError,
)
from src.data_layer.prewarm import ChinaMarketCalendar
from src.market_cache import get_market_refresh_service
from src.portfolio.state import load_state, normalize_symbol

from .models import DEFAULT_PRICE_VOLUME_POLICY
from .compound import CompoundConditionEvaluator
from .recommendations import RecommendationResolver
from .price_volume import (
    PriceVolumeAnalyzer,
    disabled_price_volume,
    maximum_bar_lag_seconds,
    target_distance_bps,
)
from .store import MonitoringStore, StaleLeaderError


logger = logging.getLogger(__name__)
DeliveryCallback = Callable[
    [dict[str, Any], dict[str, Any]],
    Awaitable[DeliveryReceipt | dict[str, Any] | str | None],
]
_TRUE = {"1", "true", "yes", "on"}
_MODES = {"shadow", "deliver"}
_PRICE_VOLUME_MODES = {"off", "shadow", "deliver"}
_TIER_SECONDS = {"low": 900, "normal": 300, "active": 60}
_SUPPORTED_MARKET_SCHEDULES = {"SH", "SZ", "BJ"}
_PRICE_VOLUME_EVENT_KINDS = {
    "price_volume_accelerated_decline",
    "target_proximity",
    "target_assessment_changed",
}
_CN_TZ = ZoneInfo("Asia/Shanghai")


class MonitoringRuntime:
    def __init__(
        self,
        *,
        store: MonitoringStore | None = None,
        market_service: Any | None = None,
        delivery_callback: DeliveryCallback | None = None,
        calendar: Any | None = None,
        now_factory: Callable[[], datetime] | None = None,
        price_volume_analyzer: PriceVolumeAnalyzer | None = None,
        compound_evaluator: CompoundConditionEvaluator | None = None,
        recommendation_resolver: RecommendationResolver | None = None,
        autopilot_tick_callback: Callable[[], Any] | None = None,
        autopilot_event_callback: Callable[..., Any] | None = None,
    ) -> None:
        self.store = store or MonitoringStore()
        self.market_service = market_service or get_market_refresh_service()
        self.delivery_callback = delivery_callback
        self.calendar = calendar or ChinaMarketCalendar()
        self.now_factory = now_factory or (lambda: datetime.now(_CN_TZ))
        self.price_volume_analyzer = price_volume_analyzer or PriceVolumeAnalyzer()
        self.compound_evaluator = compound_evaluator or CompoundConditionEvaluator()
        self.recommendation_resolver = recommendation_resolver or RecommendationResolver()
        self.autopilot_tick_callback = autopilot_tick_callback
        self.autopilot_event_callback = autopilot_event_callback
        self.owner_id = uuid.uuid4().hex
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_tick: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.current_tick_started_at: str | None = None
        self.calendar_status: dict[str, Any] = {
            "mode": getattr(self.calendar, "mode", "uninitialized"),
            "market_date": None,
            "is_trading_day": None,
            "session": "unknown",
            "open": False,
        }
        self.leader = False
        self.fencing_token: int | None = None

    @staticmethod
    def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
        try:
            return max(minimum, int(os.getenv(name, str(default))))
        except ValueError:
            return max(minimum, default)

    @staticmethod
    def _deliver_allowlist() -> set[str]:
        return {
            value.strip().upper()
            for value in os.getenv("VIBE_TRADING_MONITOR_DELIVER_ALLOWLIST", "").split(",")
            if value.strip()
        }

    def deliver_readiness(self) -> dict[str, Any]:
        return self.store.deliver_readiness(
            allowlist=self._deliver_allowlist(),
            test_target_id=(
                os.getenv("VIBE_TRADING_MONITOR_DELIVER_TEST_TARGET_ID", "").strip()
                or None
            ),
            daily_limit=self._env_int(
                "VIBE_TRADING_MONITOR_DAILY_ALERT_LIMIT",
                5,
            ),
            max_profiles=self._env_int(
                "VIBE_TRADING_MONITOR_DELIVER_MAX_PROFILES",
                1,
            ),
            soak_approved=os.getenv(
                "VIBE_TRADING_MONITOR_SOAK_APPROVED",
                "0",
            ).strip().lower()
            in _TRUE,
            callback_ready=self.delivery_callback is not None,
        )

    def _effective_config_state(self) -> dict[str, Any]:
        config = self._config_state()
        readiness = self.deliver_readiness()
        config["deliver_readiness"] = readiness
        if config["mode"] == "deliver" and not readiness["ready"]:
            config["mode"] = "shadow"
            config["mode_reason"] = "deliver_readiness_failed"
        return config

    @staticmethod
    def _config_state() -> dict[str, Any]:
        enabled = os.getenv("VIBE_TRADING_MONITORING_ENABLED", "0").strip().lower() in _TRUE
        requested = os.getenv("VIBE_TRADING_MONITORING_MODE", "shadow").strip().lower()
        valid = requested in _MODES
        if not enabled:
            mode = "off"
            reason = "global_kill_switch"
        elif valid:
            mode = requested
            reason = None
        else:
            mode = "shadow"
            reason = "invalid_mode_fell_back_to_shadow"
        return {
            "enabled_by_config": enabled,
            "requested_mode": requested,
            "mode": mode,
            "mode_valid": valid,
            "mode_reason": reason,
        }

    def mode(self) -> str:
        return str(self._effective_config_state()["mode"])

    def enabled(self) -> bool:
        return self.mode() != "off"

    @staticmethod
    def _price_volume_config_state() -> dict[str, Any]:
        requested = os.getenv(
            "VIBE_TRADING_MONITOR_PRICE_VOLUME_MODE",
            "off",
        ).strip().lower()
        valid = requested in _PRICE_VOLUME_MODES
        return {
            "requested_mode": requested,
            "mode": requested if valid else "off",
            "mode_valid": valid,
            "mode_reason": None if valid else "invalid_mode_fell_back_to_off",
        }

    async def start(self, *, force: bool = False) -> None:
        if not self.enabled():
            if force:
                logger.warning("portfolio monitoring start rejected by the global kill switch")
            return
        if self._task is not None and not self._task.done():
            return
        self.store.recover_stale_deliveries(
            timeout_seconds=self._env_int(
                "VIBE_TRADING_MONITOR_DELIVERY_CLAIM_TIMEOUT_SECONDS",
                180,
            )
        )
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="portfolio-monitoring-runtime")

    async def stop(self) -> None:
        preserve_pending = self.mode() == "deliver"
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.store.release_lease("portfolio_monitoring_runtime", self.owner_id)
        self.store.mark_delivering_uncertain(
            reason="runtime stopped during delivery",
            owner_id=self.owner_id,
        )
        if not preserve_pending:
            self.store.suppress_pending_deliveries(reason="runtime_stopped")
        self.leader = False
        self.fencing_token = None

    async def _run(self) -> None:
        try:
            while not self._stop.is_set() and self.enabled():
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # keep next tick alive; expose failure in status/log
                    logger.exception("portfolio monitoring tick failed")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=30)
                except asyncio.TimeoutError:
                    continue
        finally:
            self.store.release_lease("portfolio_monitoring_runtime", self.owner_id)
            if self.mode() == "off":
                self.store.suppress_pending_deliveries(reason="global_kill_switch")
            self.leader = False
            self.fencing_token = None

    def _now(self) -> datetime:
        value = self.now_factory()
        if value.tzinfo is None:
            value = value.replace(tzinfo=_CN_TZ)
        return value.astimezone(_CN_TZ)

    @staticmethod
    def _session_name(local_now: datetime) -> str:
        minutes = local_now.hour * 60 + local_now.minute
        if minutes < 9 * 60 + 35:
            return "preopen"
        if minutes <= 11 * 60 + 30:
            return "morning"
        if minutes < 13 * 60 + 5:
            return "lunch"
        if minutes <= 15 * 60:
            return "afternoon"
        return "closed"

    async def _market_session(self, local_now: datetime) -> dict[str, Any]:
        session = self._session_name(local_now)
        try:
            trading_day = bool(
                await asyncio.to_thread(self.calendar.is_trading_day, local_now.date())
            )
            error = None
        except Exception as exc:
            trading_day = False
            error = f"{type(exc).__name__}: {exc}"
        status = {
            "mode": getattr(self.calendar, "mode", "calendar_unavailable"),
            "market_date": local_now.date().isoformat(),
            "is_trading_day": trading_day,
            "session": session,
            "open": trading_day and session in {"morning", "afternoon"},
            "checked_at": local_now.isoformat(),
            "error": error,
        }
        self.calendar_status = status
        return status

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _maximum_schedule_lag_ms(
        cls,
        profiles: list[dict[str, Any]],
        now_utc: datetime,
    ) -> float:
        values = [
            max(0.0, (now_utc - due_at).total_seconds() * 1000)
            for profile in profiles
            if (due_at := cls._parse_time(profile.get("next_quote_run_at"))) is not None
        ]
        return round(max(values), 3) if values else 0.0

    @staticmethod
    def _has_supported_market_schedule(profile: dict[str, Any]) -> bool:
        market = str(profile.get("market") or "").strip().upper()
        if market in _SUPPORTED_MARKET_SCHEDULES:
            return True
        symbol = str(profile.get("symbol") or "").strip().upper()
        return any(symbol.endswith(f".{suffix}") for suffix in _SUPPORTED_MARKET_SCHEDULES)

    def _finish_tick(self, tick: dict[str, Any], started: float) -> dict[str, Any]:
        duplicate_before = tick.pop("_duplicate_event_count_before", None)
        if duplicate_before is None:
            tick["duplicate_events"] = 0
        else:
            try:
                duplicate_after = self.store.counter_value("duplicate_event_count")
                tick["duplicate_events"] = max(
                    0,
                    duplicate_after - int(duplicate_before),
                )
            except Exception:
                tick["duplicate_events"] = 0
        tick["finished_at"] = datetime.now(timezone.utc).isoformat()
        tick["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
        self.current_tick_started_at = None
        self.last_tick = tick
        try:
            self.store.record_runtime_tick(tick)
        except Exception:
            logger.exception("failed to persist portfolio monitoring runtime metrics")
        return tick

    def _record_profile_outcome(
        self,
        *,
        tick: dict[str, Any],
        profile_id: str,
        status: str,
        reason_code: str,
        lease_guard: dict[str, Any],
        outcomes: set[str],
    ) -> None:
        inserted = self.store.record_profile_tick_outcome(
            tick_id=str(tick["tick_id"]),
            profile_id=profile_id,
            status=status,
            reason_code=reason_code,
            lease_guard=lease_guard,
        )
        outcomes.add(profile_id)
        if not inserted:
            raise RuntimeError(
                f"profile tick outcome already exists: {tick['tick_id']}:{profile_id}"
            )

    async def run_once(self) -> dict[str, Any]:
        started = time.monotonic()
        local_now = self._now()
        now_utc = local_now.astimezone(timezone.utc)
        config = self._effective_config_state()
        price_volume_config = self._price_volume_config_state()
        tick: dict[str, Any] = {
            "tick_id": uuid.uuid4().hex,
            "owner_id": self.owner_id,
            "mode": config["mode"],
            "started_at": now_utc.isoformat(),
            "at": local_now.isoformat(),
            "decision": "starting",
            "due_profiles": 0,
            "all_due_profiles": 0,
            "unsupported_market_profiles": 0,
            "evaluated_profiles": 0,
            "blocked_profiles": 0,
            "supported_blocked_profiles": 0,
            "outcome_profiles": 0,
            "events_created": 0,
            "shadow_suppressed": 0,
            "schedule_lag_ms": None,
            "closed_session_due_profiles": 0,
            "closed_session_backlog_lag_ms": None,
            "bar_lag_ms": None,
            "error": None,
            "price_volume_mode": price_volume_config["mode"],
        }
        self.current_tick_started_at = tick["started_at"]
        all_due: list[dict[str, Any]] = []
        supported_due_ids: set[str] = set()
        outcomes: set[str] = set()
        lease_guard: dict[str, Any] | None = None
        try:
            if config["mode"] == "off":
                tick["decision"] = "off"
                return self._finish_tick(tick, started)

            self.fencing_token = self.store.acquire_fenced_lease(
                "portfolio_monitoring_runtime",
                self.owner_id,
                ttl_seconds=90,
            )
            self.leader = self.fencing_token is not None
            if not self.leader:
                tick["decision"] = "standby_not_leader"
                return self._finish_tick(tick, started)
            tick["_duplicate_event_count_before"] = self.store.counter_value(
                "duplicate_event_count"
            )
            lease_guard = {
                "lease_key": "portfolio_monitoring_runtime",
                "owner_id": self.owner_id,
                "fencing_token": int(self.fencing_token),
                "tick_id": tick["tick_id"],
            }
            if self.autopilot_tick_callback is not None:
                try:
                    tick["autopilot"] = await asyncio.to_thread(self.autopilot_tick_callback)
                except Exception as exc:
                    tick["autopilot_error"] = f"{type(exc).__name__}: {exc}"
                    logger.warning("monitor autopilot tick failed: %s", exc)
            self.store.maintain_profiles(self._holding_hashes())
            all_due = self.store.due_profiles()
            tick["all_due_profiles"] = len(all_due)
            due = [
                profile for profile in all_due
                if self._has_supported_market_schedule(profile)
            ]
            unsupported_due = [
                profile for profile in all_due
                if not self._has_supported_market_schedule(profile)
            ]
            tick["due_profiles"] = len(due)
            supported_due_ids = {str(profile["profile_id"]) for profile in due}
            tick["unsupported_market_profiles"] = len(unsupported_due)
            session = await self._market_session(local_now)
            tick["calendar"] = session
            events: list[dict[str, Any]] = []
            bar_lags: list[float] = []
            deliver_profile_ids = set(
                config.get("deliver_readiness", {}).get(
                    "resolved_profile_ids",
                    [],
                )
            )
            local_minutes = local_now.hour * 60 + local_now.minute
            if (
                session.get("is_trading_day") is True
                and session.get("session") == "preopen"
                and local_minutes >= 9 * 60
            ):
                events.extend(
                    self.store.create_preopen_notices(
                        market_date=local_now.date().isoformat(),
                        first_check_at=local_now.replace(
                            hour=9,
                            minute=35,
                            second=0,
                            microsecond=0,
                        ).isoformat(),
                        delivery_mode="deliver" if config["mode"] == "deliver" else "shadow",
                        lease_guard=lease_guard,
                        allowed_profile_ids=(
                            deliver_profile_ids if config["mode"] == "deliver" else None
                        ),
                    )
                )

            for profile in unsupported_due:
                self._record_profile_outcome(
                    tick=tick,
                    profile_id=str(profile["profile_id"]),
                    status="blocked",
                    reason_code="unsupported_market_schedule",
                    lease_guard=lease_guard,
                    outcomes=outcomes,
                )
                tick["blocked_profiles"] += 1

            if due and session["open"]:
                tick["schedule_lag_ms"] = self._maximum_schedule_lag_ms(due, now_utc)
                claimed_ids = self.store.claim_profiles(
                    lease_key=str(lease_guard["lease_key"]),
                    owner_id=self.owner_id,
                    fencing_token=int(self.fencing_token),
                    tick_id=str(tick["tick_id"]),
                    profile_ids=[str(profile["profile_id"]) for profile in due],
                    ttl_seconds=90,
                )
                unclaimed = [
                    profile
                    for profile in due
                    if str(profile["profile_id"]) not in claimed_ids
                ]
                due = [
                    profile
                    for profile in due
                    if str(profile["profile_id"]) in claimed_ids
                ]
                for profile in unclaimed:
                    self._record_profile_outcome(
                        tick=tick,
                        profile_id=str(profile["profile_id"]),
                        status="blocked",
                        reason_code="profile_claim_unavailable",
                        lease_guard=lease_guard,
                        outcomes=outcomes,
                    )
                    tick["blocked_profiles"] += 1
                    tick["supported_blocked_profiles"] += 1
                symbols = sorted({str(profile["symbol"]) for profile in due})
                profile_intervals = {
                    profile["profile_id"]: self._profile_intervals(profile)
                    for profile in due
                }
                intervals = sorted({
                    interval
                    for values in profile_intervals.values()
                    for interval in values
                }) or ["5m"]
                deadline = time.monotonic() + 25
                try:
                    await asyncio.to_thread(
                        self.market_service.refresh_sync,
                        symbols=symbols,
                        profile="portfolio_monitoring",
                        items=[(interval, "raw") for interval in intervals],
                        force=False,
                        read_only=True,
                        deadline=deadline,
                    )
                except Exception as exc:
                    tick["refresh_error"] = f"{type(exc).__name__}: {exc}"
                    logger.warning("monitoring quote refresh failed: %s", exc)
                if not self.store.heartbeat_lease(
                    str(lease_guard["lease_key"]),
                    self.owner_id,
                    int(self.fencing_token),
                    ttl_seconds=90,
                ):
                    raise StaleLeaderError("runtime lease expired during market refresh")

                market_date = local_now.date().isoformat()
                for profile in due:
                    if not self.store.heartbeat_lease(
                        str(lease_guard["lease_key"]),
                        self.owner_id,
                        int(self.fencing_token),
                        ttl_seconds=90,
                    ):
                        raise StaleLeaderError("runtime lease expired before profile evaluation")
                    profile_delivery_mode = (
                        "deliver"
                        if config["mode"] == "deliver"
                        and str(profile["profile_id"]) in deliver_profile_ids
                        else "shadow"
                    )
                    plan = self.store.get_plan(
                        profile["profile_id"],
                        int(profile["active_plan_version"]),
                    )
                    plan_payload = (plan or {}).get("plan", {})
                    tier = str(plan_payload.get("quote_tier") or "normal")
                    allow_single_source = plan_payload.get("data_mode") == "single_source"
                    seconds = self._tier_seconds(tier)
                    quotes = [
                        quote
                        for interval in profile_intervals[profile["profile_id"]]
                        if (
                            quote := self._quote_for_interval(
                                profile["symbol"],
                                interval,
                                market_date,
                                now_utc,
                                allow_single_source=allow_single_source,
                            )
                        )
                    ]
                    if not quotes:
                        blocked_reason = (
                            "fresh_quote_unavailable"
                            if allow_single_source
                            else "fresh_verified_quote_unavailable"
                        )
                        tick["blocked_profiles"] += 1
                        tick["supported_blocked_profiles"] += 1
                        self.store.schedule_next(
                            profile["profile_id"],
                            seconds=seconds,
                            success=False,
                            blocked_reasons=[blocked_reason],
                            lease_guard=lease_guard,
                        )
                        events.extend(
                            self.store.record_data_health(
                                str(profile["profile_id"]),
                                healthy=False,
                                reason_code=blocked_reason,
                                delivery_mode=profile_delivery_mode,
                                lease_guard=lease_guard,
                            )
                        )
                        self._record_profile_outcome(
                            tick=tick,
                            profile_id=str(profile["profile_id"]),
                            status="blocked",
                            reason_code=blocked_reason,
                            lease_guard=lease_guard,
                            outcomes=outcomes,
                        )
                        continue
                    policy = plan_payload.get("price_volume_policy")
                    schema_version = int(plan_payload.get("schema_version") or 1)
                    price_volume: dict[str, Any] | None = None
                    price_volume_bar_time: str | None = None
                    if schema_version >= 2 and isinstance(policy, dict):
                        if not bool(policy.get("enabled", True)):
                            price_volume = disabled_price_volume("price_volume_policy_disabled")
                        elif price_volume_config["mode"] == "off":
                            price_volume = disabled_price_volume()
                        else:
                            price_volume, price_volume_bar_time = self.price_volume_analyzer.analyze(
                                market_store=self.market_service.store,
                                symbol=str(profile["symbol"]),
                                now_utc=now_utc,
                                policy=policy,
                                allow_single_source=allow_single_source,
                            )

                    cumulative_volume: dict[str, Any] | None = None
                    if schema_version >= 4 and any(
                        str((scenario.get("volume_confirmation") or {}).get("metric") or "")
                        in {"same_clock_cumulative_volume_ratio", "absolute_cumulative_volume"}
                        for scenario in (plan_payload.get("watch_scenarios") or [])
                        if isinstance(scenario, dict)
                    ):
                        cumulative_volume = self.price_volume_analyzer.analyze_cumulative(
                            market_store=self.market_service.store,
                            symbol=str(profile["symbol"]),
                            now_utc=now_utc,
                            policy=policy if isinstance(policy, dict) else DEFAULT_PRICE_VOLUME_POLICY,
                            allow_single_source=allow_single_source,
                            interval="5m",
                        )
                    cumulative_amount: dict[str, Any] | None = None
                    if schema_version >= 5 and any(
                        str(condition.get("kind") or "") == "cumulative_turnover"
                        for scenario in (plan_payload.get("watch_scenarios") or [])
                        for group_name in ("entry_conditions", "confirmation_conditions", "invalidation_conditions")
                        for condition in ((scenario.get(group_name) or {}).get("conditions") or [])
                        if isinstance(scenario, dict) and isinstance(condition, dict)
                    ):
                        cumulative_amount = self.price_volume_analyzer.analyze_cumulative_amount(
                            market_store=self.market_service.store,
                            symbol=str(profile["symbol"]),
                            now_utc=now_utc,
                            policy=policy if isinstance(policy, dict) else DEFAULT_PRICE_VOLUME_POLICY,
                            allow_single_source=allow_single_source,
                            interval="5m",
                        )

                    compound_assessments: dict[str, dict[str, Any]] = {}
                    if schema_version >= 5:
                        supplemental = (
                            ((plan or {}).get("evidence_manifest") or {})
                            .get("market_evidence", {})
                            .get("supplemental_evidence", {})
                        )
                        auxiliary = dict(
                            supplemental.get("auxiliary")
                            if isinstance(supplemental.get("auxiliary"), dict)
                            else {}
                        )
                        if price_volume is not None:
                            auxiliary["price_volume"] = price_volume
                        if cumulative_volume is not None:
                            auxiliary["cumulative_volume"] = cumulative_volume
                        if cumulative_amount is not None:
                            auxiliary["cumulative_amount"] = cumulative_amount
                        compound_assessments = self.compound_evaluator.evaluate(
                            plan=plan_payload,
                            symbol=str(profile["symbol"]),
                            market_store=self.market_service.store,
                            now_utc=now_utc,
                            auxiliary=auxiliary,
                            allow_single_source=allow_single_source,
                        )

                    volume_rules = [
                        rule
                        for rule in plan_payload.get("market_rules", [])
                        if rule.get("enabled", True)
                        and rule.get("kind") == "volume_ratio_above"
                    ]
                    rule_volume_ratios: dict[str, float | None] = {}
                    ratio_cache: dict[tuple[str, int, int], float | None] = {}
                    for volume_rule in volume_rules:
                        parameters = volume_rule.get("parameters") or {}
                        interval = str(parameters.get("interval") or "5m")
                        ratio_policy = {
                            **(policy if isinstance(policy, dict) else DEFAULT_PRICE_VOLUME_POLICY),
                            "interval": interval,
                            "baseline_sessions": int(parameters.get("baseline_sessions", 10)),
                            "min_samples": int(parameters.get("min_samples", 5)),
                        }
                        cache_key = (
                            interval,
                            ratio_policy["baseline_sessions"],
                            ratio_policy["min_samples"],
                        )
                        if (
                            interval == "5m"
                            and price_volume is not None
                            and price_volume.get("status") == "ready"
                            and isinstance(policy, dict)
                            and int(policy.get("baseline_sessions", 10)) == cache_key[1]
                            and int(policy.get("min_samples", 5)) == cache_key[2]
                        ):
                            ratio_cache[cache_key] = price_volume.get("volume_ratio")
                        elif cache_key not in ratio_cache:
                            ratio_payload, _ratio_bar_time = self.price_volume_analyzer.analyze(
                                market_store=self.market_service.store,
                                symbol=str(profile["symbol"]),
                                now_utc=now_utc,
                                policy=ratio_policy,
                                allow_single_source=allow_single_source,
                                interval=interval,
                                require_pattern=False,
                            )
                            ratio_cache[cache_key] = (
                                ratio_payload.get("volume_ratio")
                                if ratio_payload.get("status") == "ready"
                                else None
                            )
                        rule_volume_ratios[str(volume_rule.get("client_rule_id") or "")] = ratio_cache[cache_key]
                    for quote in quotes:
                        if price_volume is not None:
                            quote["price_volume"] = price_volume
                            quote["price_volume_bar_time"] = price_volume_bar_time
                        if cumulative_volume is not None:
                            quote["cumulative_volume_evidence"] = cumulative_volume
                            quote["cumulative_volume"] = cumulative_volume.get("cumulative_volume")
                            quote["cumulative_volume_ratio"] = cumulative_volume.get("cumulative_volume_ratio")
                            quote["cumulative_volume_unit"] = cumulative_volume.get("volume_unit")
                        if cumulative_amount is not None:
                            quote["cumulative_amount_evidence"] = cumulative_amount
                            quote["cumulative_amount"] = cumulative_amount.get("cumulative_amount")
                            quote["cumulative_amount_ratio"] = cumulative_amount.get("cumulative_amount_ratio")
                        if compound_assessments:
                            quote["compound_assessments"] = compound_assessments
                            recommendation_candidates: dict[str, dict[str, Any]] = {}
                            scenarios_by_rule = {
                                str(item.get("client_rule_id") or ""): item
                                for item in plan_payload.get("watch_scenarios") or []
                            }
                            for client_rule_id, assessment in compound_assessments.items():
                                scenario = scenarios_by_rule.get(client_rule_id)
                                if not scenario or not assessment.get("confirmation_met"):
                                    continue
                                recommendation_candidates[client_rule_id] = self.recommendation_resolver.resolve(
                                    symbol=str(profile["symbol"]),
                                    scenario=scenario,
                                    current_price=quote.get("last_price"),
                                    now_utc=now_utc,
                                    profile_id=str(profile["profile_id"]),
                                    plan_version=int(profile["active_plan_version"]),
                                    compound=assessment,
                                )
                            if recommendation_candidates:
                                quote["recommendation_candidates"] = recommendation_candidates
                        quote_interval = str(quote.get("interval") or "")
                        matching_ratios = {
                            str(rule.get("client_rule_id") or ""): rule_volume_ratios.get(
                                str(rule.get("client_rule_id") or "")
                            )
                            for rule in volume_rules
                            if str(rule.get("parameters", {}).get("interval") or "5m") == quote_interval
                        }
                        if matching_ratios:
                            quote["volume_ratios"] = matching_ratios
                            quote["volume_ratio"] = next(iter(matching_ratios.values()))
                        bar_at = self._parse_time(quote.get("bar_time"))
                        if bar_at is not None:
                            bar_lags.append(
                                max(0.0, (now_utc - bar_at).total_seconds() * 1000)
                            )
                        quote_events = self.store.evaluate_quote(
                                profile["profile_id"],
                                quote,
                                delivery_mode=profile_delivery_mode,
                                price_volume_mode=str(price_volume_config["mode"]),
                                lease_guard=lease_guard,
                            )
                        events.extend(quote_events)
                        if self.autopilot_event_callback is not None:
                            for event in quote_events:
                                trigger_type = (
                                    "approaching" if event.get("phase") == "approaching"
                                    else "invalidated" if event.get("outcome") in {"invalidated", "rejected"}
                                    else None
                                )
                                if trigger_type:
                                    with suppress(Exception):
                                        await asyncio.to_thread(
                                            self.autopilot_event_callback,
                                            trigger_type=trigger_type,
                                            symbol=str(profile["symbol"]),
                                            fingerprint=str(event.get("episode_id") or event.get("event_id")),
                                            payload={"event_id": event.get("event_id"), "phase": event.get("phase")},
                                        )
                    events.extend(
                        self.store.record_data_health(
                            str(profile["profile_id"]),
                            healthy=True,
                            reason_code="fresh_quote_available",
                            delivery_mode=profile_delivery_mode,
                            lease_guard=lease_guard,
                        )
                    )
                    tick["evaluated_profiles"] += 1
                    latest_quote = max(
                        quotes,
                        key=lambda item: str(item.get("bar_time") or ""),
                    )
                    effective_tier = self._effective_quote_tier(plan_payload, latest_quote)
                    self.store.schedule_next(
                        profile["profile_id"],
                        seconds=self._tier_seconds(effective_tier),
                        success=True,
                        lease_guard=lease_guard,
                    )
                    self._record_profile_outcome(
                        tick=tick,
                        profile_id=str(profile["profile_id"]),
                        status="evaluated",
                        reason_code="quote_evaluated",
                        lease_guard=lease_guard,
                        outcomes=outcomes,
                    )
                tick["decision"] = "evaluated"
            elif due:
                tick["decision"] = "calendar_closed"
                tick["closed_session_due_profiles"] = len(due)
                tick["closed_session_backlog_lag_ms"] = self._maximum_schedule_lag_ms(
                    due,
                    now_utc,
                )
                for profile in due:
                    profile_delivery_mode = (
                        "deliver"
                        if config["mode"] == "deliver"
                        and str(profile["profile_id"]) in deliver_profile_ids
                        else "shadow"
                    )
                    if local_now.hour >= 15:
                        events.extend(
                            self.store.resolve_market_close_episodes(
                                str(profile["profile_id"]),
                                local_now.date().isoformat(),
                                delivery_mode=profile_delivery_mode,
                                price_volume_mode=str(price_volume_config["mode"]),
                                lease_guard=lease_guard,
                            )
                        )
                    self._record_profile_outcome(
                        tick=tick,
                        profile_id=str(profile["profile_id"]),
                        status="blocked",
                        reason_code="calendar_closed",
                        lease_guard=lease_guard,
                        outcomes=outcomes,
                    )
                    tick["blocked_profiles"] += 1
                    tick["supported_blocked_profiles"] += 1
            elif unsupported_due:
                tick["decision"] = "unsupported_market_schedule"
            else:
                tick["decision"] = "no_due_profiles"

            tick["outcome_profiles"] = len(outcomes)
            if tick["outcome_profiles"] != tick["all_due_profiles"]:
                raise RuntimeError(
                    "profile outcome invariant failed: "
                    f"outcomes={tick['outcome_profiles']} all_due={tick['all_due_profiles']}"
                )
            supported_terminal = (
                int(tick["evaluated_profiles"])
                + int(tick["supported_blocked_profiles"])
            )
            if supported_terminal != tick["due_profiles"]:
                raise RuntimeError(
                    "supported profile outcome invariant failed: "
                    f"terminal={supported_terminal} due={tick['due_profiles']}"
                )
            tick["outcome_invariant_ok"] = True

            tick["events_created"] = len(events)
            tick["bar_lag_ms"] = round(max(bar_lags), 3) if bar_lags else None
            new_shadow_records = sum(
                1
                for event in events
                for delivery in event.get("deliveries", [])
                if delivery.get("status") == "shadow_suppressed"
            )
            tick["shadow_suppressed"] = new_shadow_records + await self._deliver_pending(
                str(config["mode"]),
                lease_guard=lease_guard,
            )
            if os.getenv("VIBE_TRADING_MONITOR_MAINTENANCE_ENABLED", "1").lower() in _TRUE:
                maintenance = await asyncio.to_thread(self.store.run_maintenance)
                tick["maintenance_status"] = maintenance.get("status")
                tick["maintenance_skipped"] = bool(maintenance.get("skipped"))
            self.last_error = None
            return self._finish_tick(tick, started)
        except Exception as exc:
            if lease_guard is not None and not isinstance(exc, StaleLeaderError):
                for profile in all_due:
                    profile_id = str(profile["profile_id"])
                    if profile_id in outcomes:
                        continue
                    with suppress(Exception):
                        self._record_profile_outcome(
                            tick=tick,
                            profile_id=profile_id,
                            status="blocked",
                            reason_code="tick_failed",
                            lease_guard=lease_guard,
                            outcomes=outcomes,
                        )
                        tick["blocked_profiles"] += 1
                        if profile_id in supported_due_ids:
                            tick["supported_blocked_profiles"] += 1
            tick["outcome_profiles"] = len(outcomes)
            tick["outcome_invariant_ok"] = (
                tick["outcome_profiles"] == tick["all_due_profiles"]
                and int(tick["evaluated_profiles"])
                + int(tick["supported_blocked_profiles"])
                == tick["due_profiles"]
            )
            error = f"{type(exc).__name__}: {exc}"
            tick["decision"] = "failed"
            tick["error"] = error
            self.last_error = error
            self._finish_tick(tick, started)
            raise

    @staticmethod
    def _holding_hashes() -> dict[str, str]:
        hashes: dict[str, str] = {}
        for holding in load_state().holdings:
            symbol = normalize_symbol(
                str(holding.get("symbol") or holding.get("code") or "")
            ).upper()
            if not symbol:
                continue
            selected = {
                key: holding.get(key)
                for key in (
                    "symbol",
                    "code",
                    "name",
                    "quantity",
                    "cost_price",
                    "updated_at",
                )
            }
            encoded = json.dumps(
                selected,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
            hashes[symbol] = hashlib.sha256(encoded).hexdigest()
        return hashes

    def _profile_intervals(self, profile: dict[str, Any]) -> list[str]:
        version = profile.get("active_plan_version")
        plan = (
            self.store.get_plan(profile["profile_id"], int(version))
            if version is not None
            else None
        )
        intervals = {
            str(rule.get("parameters", {}).get("interval") or "5m")
            for rule in (plan or {}).get("plan", {}).get("market_rules", [])
            if rule.get("enabled", True)
        }
        plan_payload = (plan or {}).get("plan", {})
        if int(plan_payload.get("schema_version") or 1) >= 4:
            intervals.update(
                str((scenario.get("approach_policy") or {}).get("check_interval") or "1m")
                for scenario in (plan_payload.get("watch_scenarios") or [])
                if isinstance(scenario, dict)
            )
        if int(plan_payload.get("schema_version") or 1) >= 5:
            # 30m is synthesized from closed 5m bars; 1D is refreshed only as
            # evidence and is never treated as an intraday quote.
            intervals.update({"1m", "5m", "1D"})
        policy = plan_payload.get("price_volume_policy")
        if (
            self._price_volume_config_state()["mode"] != "off"
            and int(plan_payload.get("schema_version") or 1) >= 2
            and isinstance(policy, dict)
            and bool(policy.get("enabled", True))
        ):
            intervals.add("5m")
        return sorted(intervals)

    @staticmethod
    def _tier_seconds(tier: str) -> int:
        try:
            minimum = int(
                os.getenv("VIBE_TRADING_MONITOR_MIN_QUOTE_INTERVAL_SECONDS", "60")
            )
        except ValueError:
            minimum = 60
        return max(minimum, _TIER_SECONDS.get(tier, 300))

    @staticmethod
    def _effective_quote_tier(
        plan: dict[str, Any],
        quote: dict[str, Any],
    ) -> str:
        normal_tier = str(plan.get("quote_tier") or "normal")
        scenario_distances = {
            str(item.get("client_rule_id") or ""): float(
                (item.get("approach_policy") or {}).get("distance_bps") or 100
            )
            for item in (plan.get("watch_scenarios") or [])
            if isinstance(item, dict)
        }
        try:
            threshold = float(plan.get("near_trigger_distance_bps", 100))
        except (TypeError, ValueError):
            return normal_tier
        near = [
            distance <= scenario_distances.get(
                str(rule.get("client_rule_id") or ""),
                threshold,
            )
            for rule in plan.get("market_rules", [])
            if rule.get("enabled", True) and str(rule.get("kind") or "").startswith("price_")
            if (distance := target_distance_bps(rule, quote.get("last_price"))) is not None
        ]
        if any(near):
            return str(plan.get("near_trigger_tier") or "active")
        return normal_tier

    def _quote_for_interval(
        self,
        symbol: str,
        interval: str,
        market_date: str,
        now_utc: datetime,
        *,
        allow_single_source: bool = False,
    ) -> dict[str, Any] | None:
        bars = self.market_service.store.query_bars(
            symbol=symbol,
            interval=interval,
            adjustment="raw",
            view="consensus",
            limit=300,
        )
        if not bars:
            return None
        interval_minutes = {"1m": 1, "5m": 5}.get(interval)
        if interval_minutes is None:
            return None
        accepted_statuses = (
            {"verified", "single_source"} if allow_single_source else {"verified"}
        )
        closed: list[tuple[datetime, dict[str, Any]]] = []
        for bar in bars:
            if bar.get("status") not in accepted_statuses or bar.get("session_date") != market_date:
                continue
            bar_time = self._parse_time(bar.get("bar_time"))
            if bar_time is None:
                continue
            if now_utc >= bar_time + timedelta(minutes=interval_minutes):
                closed.append((bar_time, bar))
        if not closed:
            return None
        bar_time, bar = max(closed, key=lambda item: item[0])
        session_bars = [
            item
            for item in bars
            if item.get("status") in accepted_statuses
            and item.get("session_date") == market_date
            and self._parse_time(item.get("bar_time")) is not None
        ]
        opening_bar = min(
            session_bars,
            key=lambda item: self._parse_time(item.get("bar_time")) or now_utc,
            default=None,
        )
        session_open = (
            opening_bar.get("open") if opening_bar else None
        )
        if session_open is None and opening_bar:
            session_open = opening_bar.get("close")
        session_high_values: list[float] = []
        session_low_values: list[float] = []
        for session_bar in session_bars:
            try:
                high = float(session_bar.get("high") or session_bar.get("close"))
                low = float(session_bar.get("low") or session_bar.get("close"))
            except (TypeError, ValueError):
                continue
            if high > 0:
                session_high_values.append(high)
            if low > 0:
                session_low_values.append(low)
        session_high = max(session_high_values, default=None)
        session_low = min(session_low_values, default=None)
        max_lag_seconds = maximum_bar_lag_seconds(interval)
        if (now_utc - bar_time).total_seconds() > max_lag_seconds:
            return None
        if not bar.get("sources"):
            return None
        daily = self.market_service.store.query_bars(
            symbol=symbol,
            interval="1D",
            adjustment="raw",
            view="consensus",
            limit=10,
        )
        previous_close = next(
            (
                item.get("close")
                for item in reversed(daily)
                if item.get("status") in accepted_statuses
                and str(item.get("session_date") or "") < market_date
            ),
            None,
        )
        return {
            "symbol": symbol,
            "interval": interval,
            "bar_time": bar.get("bar_time"),
            "session_date": bar.get("session_date"),
            "adjustment": "raw",
            "last_price": bar.get("close"),
            "volume": bar.get("volume"),
            "amount": bar.get("amount"),
            "previous_close": previous_close,
            "session_open": session_open,
            "session_high": session_high,
            "session_low": session_low,
            "status": bar.get("status"),
            "sources": bar.get("sources") or [],
            "verified_at": bar.get("verified_at"),
        }

    @staticmethod
    def _coerce_delivery_receipt(
        value: DeliveryReceipt | dict[str, Any] | str | None,
    ) -> dict[str, Any]:
        if isinstance(value, DeliveryReceipt):
            receipt = value.as_dict()
        elif isinstance(value, dict):
            receipt = dict(value)
        elif isinstance(value, str) and value.strip():
            receipt = {
                "provider": "legacy",
                "remote_message_id": value.strip(),
                "provider_request_id": None,
                "accepted_at": datetime.now(timezone.utc).isoformat(),
                "status": "delivered",
            }
        else:
            raise DeliveryUncertainError(
                "delivery callback returned no durable provider receipt"
            )
        remote_message_id = str(receipt.get("remote_message_id") or "").strip()
        if not remote_message_id:
            raise DeliveryUncertainError("provider receipt has no remote_message_id")
        status = str(receipt.get("status") or "delivered")
        if status != "delivered":
            raise DeliveryRejectedError(f"provider receipt status is {status}")
        return {
            "provider": str(receipt.get("provider") or "unknown"),
            "remote_message_id": remote_message_id,
            "provider_request_id": (
                str(receipt.get("provider_request_id"))
                if receipt.get("provider_request_id")
                else None
            ),
            "accepted_at": str(
                receipt.get("accepted_at") or datetime.now(timezone.utc).isoformat()
            ),
            "status": status,
        }

    async def _deliver_pending(
        self,
        mode: str,
        *,
        lease_guard: dict[str, Any] | None = None,
    ) -> int:
        effective_mode = self.mode()
        if effective_mode == "off":
            return self.store.suppress_pending_deliveries(reason="global_kill_switch")
        if mode == "shadow" or effective_mode == "shadow":
            return self.store.suppress_pending_deliveries(reason="shadow_mode")
        if mode != "deliver" or effective_mode != "deliver":
            return 0
        readiness = self.deliver_readiness()
        if not readiness["ready"]:
            return self.store.suppress_pending_deliveries(
                reason="deliver_readiness_failed"
            )
        suppressed = 0
        price_volume_mode = str(self._price_volume_config_state()["mode"])
        allowed_profiles = set(readiness["resolved_profile_ids"])
        for delivery in self.store.pending_deliveries():
            event = self.store.get_event(delivery["event_id"])
            if str(delivery.get("profile_id") or "") not in allowed_profiles:
                if self.store.suppress_delivery(
                    delivery["delivery_id"],
                    reason="not_in_deliver_allowlist",
                ):
                    suppressed += 1
                continue
            if (
                event
                and event.get("kind") in _PRICE_VOLUME_EVENT_KINDS
                and price_volume_mode != "deliver"
            ):
                if self.store.suppress_delivery(
                    delivery["delivery_id"],
                    reason=f"price_volume_mode_{price_volume_mode}",
                ):
                    suppressed += 1
                continue
            if self.delivery_callback is None:
                continue
            if not self.store.claim_delivery(
                delivery["delivery_id"],
                lease_guard=lease_guard,
                daily_limit=int(readiness["daily_limit"]),
                required_target_id=str(readiness["test_target_id"]),
            ):
                continue
            if not event:
                self.store.finish_delivery(
                    delivery["delivery_id"],
                    status="delivery_uncertain",
                    receipt_status="delivery_uncertain",
                    error="event record missing",
                    lease_guard=lease_guard,
                )
                continue
            try:
                receipt = self._coerce_delivery_receipt(
                    await self.delivery_callback(event, delivery)
                )
                self.store.finish_delivery(
                    delivery["delivery_id"],
                    status="delivered",
                    remote_message_id=receipt["remote_message_id"],
                    provider=receipt["provider"],
                    provider_request_id=receipt["provider_request_id"],
                    accepted_at=receipt["accepted_at"],
                    receipt_status=receipt["status"],
                    lease_guard=lease_guard,
                )
            except DeliveryRejectedError as exc:
                self.store.finish_delivery(
                    delivery["delivery_id"],
                    status="rejected",
                    receipt_status="rejected",
                    error=f"{type(exc).__name__}: {exc}",
                    lease_guard=lease_guard,
                )
            except Exception as exc:  # remote acceptance may have happened: never blind retry
                self.store.finish_delivery(
                    delivery["delivery_id"],
                    status="delivery_uncertain",
                    receipt_status="delivery_uncertain",
                    error=f"{type(exc).__name__}: {exc}",
                    lease_guard=lease_guard,
                )
        return suppressed

    async def send_test_delivery(self) -> dict[str, Any]:
        """Send one clearly labelled message to the configured private canary target."""

        target_id = os.getenv(
            "VIBE_TRADING_MONITOR_DELIVER_TEST_TARGET_ID",
            "",
        ).strip()
        target = self.store.get_target(target_id) if target_id else None
        if (
            not target
            or target.get("status") != "active"
            or target.get("chat_type") != "p2p"
        ):
            raise ValueError("an active private monitoring test target is required")
        if self.delivery_callback is None:
            raise RuntimeError("delivery callback is unavailable")
        test_id = f"monitor-test-{uuid.uuid4().hex}"
        receipt = self._coerce_delivery_receipt(
            await self.delivery_callback(
                {
                    "event_id": test_id,
                    "kind": "delivery_test",
                    "symbol": "TEST",
                    "title": "AI 持仓监控投递测试",
                    "summary": "这是一条私聊投递链路测试消息，不代表任何行情触发。",
                    "facts": {
                        "last_price": "-",
                        "bar_time": datetime.now(timezone.utc).isoformat(),
                        "sources": ["monitoring_delivery_test"],
                    },
                },
                {
                    "delivery_id": test_id,
                    "delivery_target_id": target_id,
                    "channel": target["channel"],
                    "chat_id": target["chat_id"],
                    "chat_type": target["chat_type"],
                    "session_key": target.get("session_key") or "",
                },
            )
        )
        return {**receipt, "test_delivery_id": test_id, "target_id": target_id}

    def status(self) -> dict[str, Any]:
        config = self._effective_config_state()
        price_volume = self._price_volume_config_state()
        return {
            **config,
            "enabled": config["mode"] != "off",
            "running": self._task is not None and not self._task.done(),
            "leader": self.leader,
            "owner_id": self.owner_id,
            "fencing_token": self.fencing_token,
            "current_tick_started_at": self.current_tick_started_at,
            "last_tick": self.last_tick,
            "last_error": self.last_error,
            "calendar": self.calendar_status,
            "delivery_callback_ready": self.delivery_callback is not None,
            "price_volume": price_volume,
        }
