"""Policy service behind ``get_data_context`` and the Data Center API."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import suppress
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from src.market_cache import get_market_refresh_service
from src.market_verification import PRICE_BASIS_DEFINITIONS
from src.portfolio.state import load_state, normalize_symbol
from src.tools.financial_statements_tool import FinancialStatementsTool
from src.tools.research_reports_tool import ResearchReportsTool
from src.tools.stock_news_tool import StockNewsTool
from src.usage import record_current_resource

from .store import DataControlStore, ResearchCacheStore, utc_now


_LIVE_TIMEOUT_SECONDS = int(os.getenv("VIBE_TRADING_DATA_LIVE_TIMEOUT_SECONDS", "45"))
_BAR_PAGE_SIZE = 500
_AGENT_BAR_PREVIEW = 120
_SOFT_LIMIT_BYTES = int(float(os.getenv("VIBE_TRADING_DATA_STORAGE_SOFT_LIMIT_GB", "10")) * 1024**3)
_EVICT_AT_BYTES = int(_SOFT_LIMIT_BYTES * 0.9)

# The profile is a lower bound. Callers may request a longer lookback, but
# cannot downgrade a task to a coarser interval or fewer history bars.
PROFILES: dict[str, dict[str, Any]] = {
    # Deep research can run before the exchange opens. Keep the live intraday
    # quote first, but also fetch a raw daily close so a verified latest-session
    # price remains usable for timestamped market-cap calculations pre-open.
    # Raw is required here because adjusted closes must never be multiplied by
    # current shares outstanding.
    "latest_price": {
        "items": [("1m", "raw"), ("1D", "raw")],
        "lookback_days": 7,
        "item_lookback_days": {"1m:raw": 2, "1D:raw": 7},
        "live": True,
    },
    # Analysis sessions are cache-first.  Timed prewarm jobs own live refreshes
    # so a report never serially re-fetches every holding and spends its entire
    # data budget waiting on a slow public provider.  ``force_live=True`` keeps
    # an explicit on-demand refresh available for callers that really need it.
    "holding": {
        "items": [("1m", "raw"), ("5m", "raw"), ("1D", "qfq")],
        "lookback_days": 250,
        "item_lookback_days": {"1m:raw": 10, "5m:raw": 10, "1D:qfq": 250},
        "live": False,
    },
    "premarket": {"items": [("1D", "qfq")], "lookback_days": 300, "live": False},
    "intraday": {
        "items": [("1m", "raw"), ("5m", "raw"), ("1D", "qfq")],
        "lookback_days": 300,
        "item_lookback_days": {"1m:raw": 10, "5m:raw": 10, "1D:qfq": 300},
        "live": False,
    },
    "long_term": {"items": [("1D", "qfq")], "lookback_days": 750, "live": False},
    "backtest": {"items": [("1D", "qfq")], "lookback_days": 2500, "live": False},
}


def _parse_json(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {"ok": False, "error": "invalid upstream payload"}


def _normalize_symbols(symbols: Iterable[str]) -> list[str]:
    result: list[str] = []
    for raw in symbols:
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            symbol = normalize_symbol(raw).upper()
        except Exception:
            symbol = raw.strip().upper()
        if symbol and symbol not in result:
            result.append(symbol)
    return result


class UnifiedDataService:
    """The only policy entry point exposed to an Agent.

    It coordinates existing source adapters rather than reimplementing them:
    Loader Cache remains a transport cache, market bars remain in
    ``MarketCacheStore``, and research has its own SQLite/FTS cache.
    """

    def __init__(
        self,
        *,
        control: DataControlStore | None = None,
        research: ResearchCacheStore | None = None,
        market_service: Any | None = None,
        news_tool: Any | None = None,
        reports_tool: Any | None = None,
        fundamentals_tool: Any | None = None,
    ) -> None:
        self.control = control or DataControlStore()
        self.research = research or ResearchCacheStore()
        self.market_service = market_service or get_market_refresh_service()
        self.news_tool = news_tool or StockNewsTool()
        self.reports_tool = reports_tool or ResearchReportsTool()
        self.fundamentals_tool = fundamentals_tool or FinancialStatementsTool()
        self._inflight: dict[str, threading.Event] = {}
        self._inflight_results: dict[str, dict[str, Any]] = {}
        self._inflight_lock = threading.Lock()

    def get_context(
        self,
        *,
        symbols: list[str],
        purpose: str = "holding",
        lookback_days: int | None = None,
        include: list[str] | None = None,
        force_live: bool | None = None,
    ) -> dict[str, Any]:
        normalized = _normalize_symbols(symbols)
        if not normalized:
            raise ValueError("at least one normalized symbol is required")
        if len(normalized) > 25:
            raise ValueError("at most 25 symbols are allowed per request")
        if purpose not in PROFILES:
            raise ValueError(f"unsupported purpose: {purpose}")
        profile = PROFILES[purpose]
        requested_days = max(int(lookback_days or 0), 0)
        effective_days = max(requested_days, int(profile["lookback_days"]))
        include_set = {entry.strip().lower() for entry in (include or ["market", "fundamentals", "news", "reports"])}
        invalid_include = include_set - {"market", "fundamentals", "news", "reports"}
        if invalid_include:
            raise ValueError(f"unsupported data domains: {', '.join(sorted(invalid_include))}")
        live = bool(profile["live"] if force_live is None else force_live)
        fingerprint_data = {
            "symbols": normalized, "purpose": purpose, "days": effective_days,
            "requested_days": requested_days,
            "include": sorted(include_set), "live": live,
        }
        fingerprint = hashlib.sha256(json.dumps(fingerprint_data, sort_keys=True).encode()).hexdigest()
        with self._inflight_lock:
            previous = self._inflight.get(fingerprint)
            if previous is None:
                event = threading.Event()
                self._inflight[fingerprint] = event
                leader = True
            else:
                event = previous
                leader = False
        if not leader:
            event.wait(_LIVE_TIMEOUT_SECONDS + 5)
            shared = self._inflight_results.get(fingerprint)
            if shared is not None:
                return {**shared, "deduplicated": True}
            return {"status": "partial", "deduplicated": True, "error": "equivalent request did not complete"}

        request_id = uuid.uuid4().hex
        self.control.start_request(request_id, fingerprint, purpose, normalized)
        started = time.monotonic()
        try:
            result = self._build_context(
                request_id=request_id,
                symbols=normalized,
                purpose=purpose,
                profile=profile,
                effective_days=effective_days,
                requested_days=requested_days,
                include=include_set,
                live=live,
                started=started,
            )
            self.control.finish_request(request_id, result, status=result["status"])
            self._inflight_results[fingerprint] = result
            return result
        except Exception as exc:
            result = {"request_id": request_id, "status": "offline", "error": str(exc), "symbols": normalized}
            self.control.finish_request(request_id, result, status="failed", error=str(exc))
            self._inflight_results[fingerprint] = result
            return result
        finally:
            with self._inflight_lock:
                done = self._inflight.pop(fingerprint, None)
                if done:
                    done.set()

    def _build_context(
        self,
        *,
        request_id: str,
        symbols: list[str],
        purpose: str,
        profile: dict[str, Any],
        effective_days: int,
        requested_days: int,
        include: set[str],
        live: bool,
        started: float,
    ) -> dict[str, Any]:
        market: dict[str, Any] = {}
        research: dict[str, Any] = {}
        tasks: list[tuple[str, Any]] = []
        cancel_event = threading.Event()
        pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="unified-data")
        try:
            if "market" in include:
                tasks.append(("market", pool.submit(
                    self._market_context,
                    symbols,
                    profile,
                    effective_days,
                    requested_days,
                    live,
                    started,
                    cancel_event,
                )))
            if "fundamentals" in include:
                tasks.append(("fundamentals", pool.submit(self._research_context, "fundamental", symbols, started, cancel_event)))
            if "news" in include:
                tasks.append(("news", pool.submit(self._research_context, "news", symbols, started, cancel_event)))
            if "reports" in include:
                tasks.append(("reports", pool.submit(self._research_context, "report", symbols, started, cancel_event)))
            done, _ = wait([task for _, task in tasks], timeout=max(0.1, _LIVE_TIMEOUT_SECONDS - (time.monotonic() - started)))
            if len(done) != len(tasks):
                # Every data task checks this before starting another provider or
                # symbol.  It prevents a timed-out context from continuing its
                # serial refresh sequence in the background.
                cancel_event.set()
            for name, task in tasks:
                try:
                    value = task.result() if task in done else {"status": "partial", "error": "live request deadline reached"}
                except Exception as exc:
                    value = {"status": "partial", "error": str(exc)}
                if name == "market":
                    market = value
                else:
                    research[name] = value
        finally:
            cancel_event.set()
            # A blocked upstream call must not extend the user-visible request
            # budget. An in-flight socket may finish, but cooperative tasks do
            # not begin another source or symbol after this point.
            pool.shutdown(wait=False, cancel_futures=True)
        status = self._overall_status(market, research)
        decision_scopes = self._decision_scopes(symbols, market, research)
        provider_health = self._context_provider_health(market)
        self.control.log_event(request_id, "context", status, f"purpose={purpose}")
        self._enforce_retention()
        return {
            "request_id": request_id,
            "status": status,
            "purpose": purpose,
            "symbols": symbols,
            "retrieved_at": utc_now(),
            "deadline_seconds": _LIVE_TIMEOUT_SECONDS,
            "market": market,
            "research": research,
            "decision_scopes": decision_scopes,
            "provider_health": provider_health,
            "policy": {
                "historical": "cache_first_fill_gaps",
                "mutable_tail": "scheduled_prewarm_cache_first_force_live_on_demand",
                "price_sensitive": "two_source_quorum_then_third_on_conflict",
                "news": "live_revalidated_with_historical_background_fallback",
                "bar_preview": _AGENT_BAR_PREVIEW,
                "bar_page_size": _BAR_PAGE_SIZE,
                "reporting_guard": {
                    "analysis_only": "no_exact_price_position_size_or_trade_action",
                    "confirmed_claim": "direct_source_url_and_timestamp_required",
                },
            },
        }

    def _market_context(
        self,
        symbols: list[str],
        profile: dict[str, Any],
        days: int,
        requested_days: int,
        live: bool,
        started: float,
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"status": "offline", "series": [], "bars_handles": [], "quotes": [], "runs": []}
        for interval, profile_adjustment in profile["items"]:
            if cancel_event.is_set() or time.monotonic() - started >= _LIVE_TIMEOUT_SECONDS:
                result["status"] = "partial"
                result.setdefault("warnings", []).append("live request deadline reached")
                break
            item_live = live
            item_defaults = profile.get("item_lookback_days") or {}
            item_default_days = int(
                item_defaults.get(f"{interval}:{profile_adjustment}", days)
            )
            item_days = max(requested_days, item_default_days)
            target_start = (date.today() - timedelta(days=item_days)).isoformat()
            symbols_by_adjustment: dict[str, list[str]] = {}
            for symbol in symbols:
                # Adjustment names are a market-neutral contract. Never weaken
                # qfq/hfq to source_default merely because a provider uses
                # different terminology; the adapter must prove an exact mapping.
                symbols_by_adjustment.setdefault(profile_adjustment, []).append(symbol)

            for adjustment, item_symbols in symbols_by_adjustment.items():
                cache_complete = self._has_coverage(
                    item_symbols, interval, adjustment, target_start
                )
                run: dict[str, Any] | None = None
                # Settled historical data does not refetch merely because it was read.
                # Mutable intraday tails always revalidate, and any coverage gap is filled.
                if item_live or not cache_complete:
                    run = self.market_service.refresh_sync(
                        symbols=item_symbols,
                        profile="unified_data",
                        force=False,
                        start_date=target_start,
                        end_date=date.today().isoformat(),
                        items=[(interval, adjustment)],
                        deadline=started + _LIVE_TIMEOUT_SECONDS,
                        should_cancel=cancel_event.is_set,
                    )
                    self._record_market_sources(run)
                    result["runs"].append(
                        {
                            "run_id": run.get("run_id"),
                            "status": run.get("status"),
                            "interval": interval,
                            "adjustment": adjustment,
                            "symbols": item_symbols,
                        }
                    )
                    if run.get("status") == "interrupted":
                        result["status"] = "partial"
                        result.setdefault("warnings", []).append(
                            "market refresh reached its source budget"
                        )
                repair_attempted = False
                for symbol in item_symbols:
                    bars = self.market_service.store.query_bars(
                        symbol=symbol,
                        interval=interval,
                        adjustment=adjustment,
                        start=target_start,
                        limit=max(_AGENT_BAR_PREVIEW, 2500 if interval == "1D" else 5000),
                    )
                    summary = self._series_summary(
                        symbol, interval, adjustment, bars, run, item_live
                    )
                    retryable_reasons = {
                        "insufficient_independent_sources",
                        "live_refresh_not_verified",
                        "unresolved",
                        "unresolved_conflict",
                    }
                    if (
                        run
                        and not repair_attempted
                        and summary.get("actionability") == "analysis_only"
                        and retryable_reasons.intersection(
                            summary.get("blocked_reasons") or []
                        )
                        and not cancel_event.is_set()
                        and _LIVE_TIMEOUT_SECONDS - (time.monotonic() - started) > 2.0
                    ):
                        repair_attempted = True
                        time.sleep(0.25)
                        repair_run = self.market_service.refresh_sync(
                            symbols=item_symbols,
                            profile="unified_data_repair",
                            force=True,
                            start_date=target_start,
                            end_date=date.today().isoformat(),
                            items=[(interval, adjustment)],
                            deadline=started + _LIVE_TIMEOUT_SECONDS,
                            should_cancel=cancel_event.is_set,
                        )
                        self._record_market_sources(repair_run)
                        result["runs"].append(
                            {
                                "run_id": repair_run.get("run_id"),
                                "status": repair_run.get("status"),
                                "interval": interval,
                                "adjustment": adjustment,
                                "symbols": item_symbols,
                                "repair_attempt": 1,
                            }
                        )
                        run = repair_run
                        bars = self.market_service.store.query_bars(
                            symbol=symbol,
                            interval=interval,
                            adjustment=adjustment,
                            start=target_start,
                            limit=max(
                                _AGENT_BAR_PREVIEW,
                                2500 if interval == "1D" else 5000,
                            ),
                        )
                        summary = self._series_summary(
                            symbol, interval, adjustment, bars, run, item_live
                        )
                    handle = uuid.uuid4().hex
                    self.control.put_handle(
                        handle,
                        {
                            "symbol": symbol,
                            "interval": interval,
                            "adjustment": adjustment,
                            "start": target_start,
                        },
                    )
                    summary["handle"] = handle
                    # Never stride-sample technical bars: this is the latest contiguous window.
                    summary["bars"] = bars[-_AGENT_BAR_PREVIEW:]
                    result["series"].append(summary)
                    result["bars_handles"].append(
                        {
                            "symbol": symbol,
                            "interval": interval,
                            "adjustment": adjustment,
                            "handle": handle,
                        }
                    )
        quotes = self.market_service.store.list_quotes(symbols)
        preferred_series: dict[str, dict[str, Any]] = {}
        for series in result["series"]:
            symbol = str(series["symbol"])
            current = preferred_series.get(symbol)
            rank = {"1m": 0, "5m": 1, "1D": 2}.get(str(series["interval"]), 9)
            current_rank = {"1m": 0, "5m": 1, "1D": 2}.get(str((current or {}).get("interval")), 9)
            if current is None or rank < current_rank:
                preferred_series[symbol] = series
        for quote in quotes:
            summary = preferred_series.get(str(quote["symbol"])) or {}
            quote["decision_status"] = summary.get("decision_status", quote.get("status"))
            quote["actionability"] = summary.get("actionability", "analysis_only")
            quote["selected_quote"] = summary.get("selected_quote")
            quote["blocked_reasons"] = summary.get("blocked_reasons", [])
        result["quotes"] = quotes
        if result["series"]:
            statuses = {series["retrieval"]["mode"] for series in result["series"]}
            computed_status = "live" if statuses <= {"live"} else (
                "partial" if statuses - {"live", "cache"} else "live"
            )
            result["status"] = "partial" if result["status"] == "partial" else computed_status
            result["actionability"] = (
                "price_actionable"
                if all(series["actionability"] == "price_actionable" for series in result["series"])
                else "analysis_only"
            )
        return result

    def _has_coverage(self, symbols: list[str], interval: str, adjustment: str, target_start: str) -> bool:
        coverage = self.market_service.store.list_coverage(symbols)
        target_date = date.fromisoformat(target_start)
        # A calendar lookback often starts on a weekend or exchange holiday.
        # Accept the first observed session within the following week instead
        # of treating an impossible weekend bar as a permanent coverage gap.
        latest_acceptable_start = target_date + timedelta(days=7)
        for symbol in symbols:
            matched = [row for row in coverage if row["symbol"] == symbol and row["interval"] == interval and row["actual_adjustment"] == adjustment]
            if not matched:
                return False
            earliest = min(date.fromisoformat(str(row["min_bar_time"])[:10]) for row in matched)
            if earliest > latest_acceptable_start:
                return False
        return True

    def _series_summary(
        self,
        symbol: str,
        interval: str,
        adjustment: str,
        bars: list[dict[str, Any]],
        run: dict[str, Any] | None,
        live_requested: bool,
    ) -> dict[str, Any]:
        latest = bars[-1] if bars else None
        run_item = next((item for item in (run or {}).get("items", []) if item.get("symbol") == symbol and item.get("interval") == interval and item.get("adjustment") == adjustment), {})
        live_sources = list(run_item.get("actual_sources") or [])
        source_attempts = list(run_item.get("attempts") or [])
        latest_status = str((latest or {}).get("status") or "unresolved")
        if latest_status == "conflict":
            latest_status = "unresolved_conflict"
        current_batch = bool(
            latest
            and run
            and latest.get("batch_id") == run.get("run_id")
            and live_sources
        )
        flags = set((latest or {}).get("quality_flags") or [])
        forming = "forming_bar" in flags
        fresh = self._is_fresh_market_bar(symbol, interval, latest)
        if latest is None:
            mode = "offline"
        elif live_requested and not current_batch:
            mode = "cache_fallback"
        elif live_requested and latest_status != "verified":
            mode = latest_status
        elif not live_requested:
            # Cache-first profiles deliberately avoid an in-request provider
            # refresh.  Do not mislabel that read as live merely because the
            # cached consensus is usable and verified.
            mode = "cache"
        else:
            mode = "live"
        blocked_reasons: list[str] = []
        if latest_status != "verified":
            blocked_reasons.append(latest_status)
        if int((latest or {}).get("source_count") or 0) < 2:
            blocked_reasons.append("insufficient_independent_sources")
        if forming:
            blocked_reasons.append("forming_bar_before_15_10")
        preopen_intraday = self._is_preopen_intraday_bar(symbol, interval, latest)
        if latest is not None and not fresh:
            blocked_reasons.append(
                "intraday_not_started" if preopen_intraday else "stale_verification"
            )
        if live_requested and not current_batch and not preopen_intraday:
            blocked_reasons.append("live_refresh_not_verified")
        actionability = "price_actionable" if not blocked_reasons else "analysis_only"
        selected_quote = None
        if latest is not None and actionability == "price_actionable":
            selected_quote = {
                "symbol": symbol,
                "interval": interval,
                "adjustment": adjustment,
                "bar_time": latest.get("bar_time"),
                "price": latest.get("close"),
                "sources": latest.get("sources", []),
                "verified_at": latest.get("verified_at"),
            }
        if mode in {"cache", "cache_fallback"}:
            record_current_resource(
                provider="verified_market_cache",
                category="market",
                status="ok",
                elapsed_ms=0,
                cache_mode="stale_fallback" if mode == "cache_fallback" else "cache_hit",
                query={
                    "code": symbol,
                    "interval": interval,
                    "source": ",".join(
                        str(source) for source in ((latest or {}).get("sources") or [])
                    ),
                },
                network_request=False,
                cache_access=True,
            )
        return {
            "symbol": symbol,
            "interval": interval,
            "adjustment": adjustment,
            "bar_count": len(bars),
            "coverage": {"start": bars[0]["bar_time"] if bars else None, "end": latest["bar_time"] if latest else None},
            "latest": latest,
            "decision_status": "cache_fallback" if mode == "cache_fallback" else latest_status,
            "actionability": actionability,
            "selected_quote": selected_quote,
            "blocked_reasons": list(dict.fromkeys(blocked_reasons)),
            "freshness": {
                "bar_time": latest.get("bar_time") if latest else None,
                "verified_at": latest.get("verified_at") if latest else None,
                "forming": forming,
                "fresh": fresh,
                "current_batch": current_batch,
                "source_count": latest.get("source_count", 0) if latest else 0,
                "independent_source_count": latest.get("source_count", 0) if latest else 0,
            },
            "source_attempts": source_attempts,
            "retrieval": {
                "mode": "preopen" if preopen_intraday else mode,
                "live_requested": live_requested,
                "live_sources": live_sources,
                "cache_fallback_used": mode == "cache_fallback",
                "verification_status": latest_status,
                "source_count": latest.get("source_count", 0) if latest else 0,
                "sources": latest.get("sources", []) if latest else [],
                "verified_at": latest.get("verified_at") if latest else None,
            },
        }

    @staticmethod
    def _is_preopen_intraday_bar(
        symbol: str, interval: str, latest: dict[str, Any] | None
    ) -> bool:
        """Distinguish a normal pre-open previous bar from a stale live failure."""

        if interval == "1D" or not latest or not latest.get("bar_time"):
            return False
        if not symbol.upper().endswith((".SH", ".SZ", ".BJ", ".HK")):
            return False
        market_tz = ZoneInfo("Asia/Shanghai")
        now = datetime.now(market_tz)
        if now.weekday() >= 5 or now.time() >= datetime.strptime("09:30", "%H:%M").time():
            return False
        try:
            bar_local = datetime.fromisoformat(
                str(latest["bar_time"]).replace("Z", "+00:00")
            ).astimezone(market_tz)
        except (TypeError, ValueError):
            return False
        return bar_local.date() < now.date()

    @staticmethod
    def _is_fresh_market_bar(symbol: str, interval: str, latest: dict[str, Any] | None) -> bool:
        if not latest or not latest.get("verified_at"):
            return False
        try:
            verified_at = datetime.fromisoformat(
                str(latest["verified_at"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except (TypeError, ValueError):
            return False
        now = datetime.now(timezone.utc)
        maximum_age = timedelta(hours=96 if interval == "1D" else 1)
        if now - verified_at > maximum_age:
            return False
        if interval != "1D":
            market_tz = ZoneInfo("Asia/Shanghai") if symbol.upper().endswith((".SH", ".SZ", ".BJ", ".HK")) else ZoneInfo("UTC")
            try:
                bar_local = datetime.fromisoformat(
                    str(latest["bar_time"]).replace("Z", "+00:00")
                ).astimezone(market_tz)
            except (TypeError, ValueError):
                return False
            if bar_local.date() != datetime.now(market_tz).date():
                return False
        return True

    def _record_market_sources(self, run: dict[str, Any]) -> None:
        """Record market-source successes and failures without mislabelling run time.

        A refresh run is serial across symbols, so its total duration is not a
        single provider's latency.  Persist endpoint health by source instead;
        the Data Center can then expose an Eastmoney failure even when Tencent
        later supplied usable bars.
        """
        for item in run.get("items") or []:
            for attempt in item.get("attempts") or []:
                requested = str(attempt.get("requested_source") or "unknown")
                status = str(attempt.get("status") or "failed")
                succeeded = status == "success"
                transport_events = list(attempt.get("transport_events") or [])
                warning = transport_events[-1].get("primary_error") if transport_events else None
                self.control.record_source(
                    f"{requested}:market",
                    succeeded=succeeded,
                    status="ok_with_transport_fallback" if succeeded and warning else ("ok" if succeeded else status),
                    latency_ms=attempt.get("latency_ms"),
                    error=warning or attempt.get("error"),
                    error_category="transport_fallback" if warning else attempt.get("error_category"),
                    requested_source=requested,
                    actual_source=attempt.get("actual_source"),
                    upstream_source=attempt.get("upstream_source"),
                )

    def _research_context(
        self, kind: str, symbols: list[str], started: float, cancel_event: threading.Event
    ) -> dict[str, Any]:
        output: dict[str, Any] = {"status": "live", "items": {}}
        for symbol in symbols:
            if cancel_event.is_set() or time.monotonic() - started >= _LIVE_TIMEOUT_SECONDS:
                output["status"] = "partial"
                break
            if kind == "report" and not symbol.upper().endswith((".SH", ".SZ", ".BJ")):
                output["items"][symbol] = {
                    "mode": "not_applicable",
                    "documents": [],
                    "reason_code": "market_not_supported",
                    "warning": "A股研报源不适用于该市场",
                    "provider_attempts": [],
                }
                continue
            attempt_log: list[dict[str, Any]] = []
            try:
                before = time.monotonic()
                envelope, attempt_log = self._fetch_research_with_retry(
                    kind, symbol, started=started, cancel_event=cancel_event
                )
                data = envelope.get("data") or {}
                documents = data.get("articles") or data.get("reports") or data.get("matches") or []
                if kind == "fundamental":
                    documents = [{"title": "annual indicators", "published_at": data.get("as_of") or data.get("report_date"), "summary": "latest annual indicator payload", "data": data}]
                source = envelope.get("source")
                self._record_research_sources(kind, symbol, source, succeeded=True, latency_ms=round((time.monotonic() - before) * 1000, 1))
                self._record_declared_source_statuses(kind, data.get("source_statuses"))
                self.research.replace_documents(kind, symbol, [item for item in documents if isinstance(item, dict)], source=str(source) if source else None)
                output["items"][symbol] = {
                    "mode": "live",
                    "source": source,
                    "documents": self.research.latest(kind, symbol),
                    "provider_attempts": attempt_log,
                }
            except Exception as exc:
                attempt_log = list(getattr(exc, "provider_attempts", attempt_log) or [])
                self._record_research_sources(kind, symbol, None, succeeded=False, error=str(exc))
                cached = self.research.latest(kind, symbol)
                if cached:
                    record_current_resource(
                        provider="research_cache",
                        category="financial" if kind == "fundamental" else "web",
                        status="ok",
                        elapsed_ms=0,
                        cache_mode="stale_fallback",
                        query={"code": symbol, "source": kind},
                        network_request=False,
                        cache_access=True,
                    )
                output["status"] = "partial"
                output["items"][symbol] = {
                    "mode": "historical_background" if cached else "unavailable",
                    "documents": cached,
                    "warning": "latest live research unavailable; cached material is historical background only" if cached else str(exc),
                    "provider_attempts": attempt_log,
                }
        return output

    def _fetch_research_with_retry(
        self,
        kind: str,
        symbol: str,
        *,
        started: float,
        cancel_event: threading.Event,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Run at most two research attempts while honoring circuit and request budgets."""

        default_provider = (
            "eastmoney"
            if kind != "news" or symbol.upper().endswith((".SH", ".SZ", ".BJ"))
            else "yahoo"
        )
        health = {
            str(item.get("source") or ""): item for item in self.control.source_health()
        }
        circuit = health.get(f"{default_provider}:{kind}") or {}
        if circuit.get("circuit_open"):
            raise RuntimeError(
                f"provider circuit open: {default_provider}:{kind} until "
                f"{circuit.get('circuit_open_until') or 'cooldown'}"
            )

        attempts: list[dict[str, Any]] = []
        last_error: Exception | None = None
        for attempt_number in (1, 2):
            if cancel_event.is_set() or time.monotonic() - started >= _LIVE_TIMEOUT_SECONDS:
                break
            attempt_started = time.monotonic()
            try:
                if kind == "news":
                    raw = self.news_tool.execute(code=symbol, limit=20)
                elif kind == "report":
                    raw = self.reports_tool.execute(code=symbol, limit=20)
                else:
                    raw = self.fundamentals_tool.execute(
                        code=symbol, statement="indicators", period="annual"
                    )
                envelope = _parse_json(raw)
                if not envelope.get("ok"):
                    raise RuntimeError(
                        str(envelope.get("error") or "research source failed")
                    )
            except Exception as exc:  # noqa: BLE001 - bounded provider repair
                last_error = exc
                elapsed_ms = int((time.monotonic() - attempt_started) * 1000)
                record_current_resource(
                    provider=default_provider,
                    category="financial" if kind == "fundamental" else "web",
                    status="error",
                    elapsed_ms=elapsed_ms,
                    cache_mode="network",
                    query={"code": symbol, "source": kind},
                    network_request=True,
                    cache_access=False,
                    metadata={"attempt": attempt_number, "error_type": type(exc).__name__},
                )
                attempts.append(
                    {
                        "attempt": attempt_number,
                        "status": "failed",
                        "latency_ms": round((time.monotonic() - attempt_started) * 1000, 1),
                        "error": str(exc),
                    }
                )
                remaining = _LIVE_TIMEOUT_SECONDS - (time.monotonic() - started)
                if attempt_number == 2 or remaining <= 0.35 or cancel_event.is_set():
                    break
                time.sleep(min(0.25 * attempt_number, max(0.0, remaining - 0.1)))
                continue
            attempts.append(
                {
                    "attempt": attempt_number,
                    "status": "success",
                    "latency_ms": round((time.monotonic() - attempt_started) * 1000, 1),
                }
            )
            record_current_resource(
                provider=str(envelope.get("source") or default_provider),
                category="financial" if kind == "fundamental" else "web",
                status="ok",
                elapsed_ms=int((time.monotonic() - attempt_started) * 1000),
                cache_mode="network",
                query={"code": symbol, "source": kind},
                network_request=True,
                cache_access=False,
                metadata={"attempt": attempt_number},
            )
            return envelope, attempts
        if last_error is not None:
            with suppress(Exception):
                setattr(last_error, "provider_attempts", attempts)
            raise last_error
        raise RuntimeError("research request deadline reached before provider attempt")

    def _record_research_sources(
        self,
        kind: str,
        symbol: str,
        source: Any,
        *,
        succeeded: bool,
        latency_ms: float | None = None,
        error: str | None = None,
    ) -> None:
        """Persist endpoint-level health for research adapters."""
        if isinstance(source, str) and source.strip():
            providers = [part.strip() for part in source.split("+") if part.strip()]
        elif kind == "news":
            providers = ["eastmoney"] if symbol.upper().endswith((".SH", ".SZ", ".BJ")) else ["yahoo"]
        elif kind == "report":
            providers = ["eastmoney"]
        else:
            providers = ["eastmoney"]
        for provider in providers:
            self.control.record_source(
                f"{provider}:{kind}", succeeded=succeeded, latency_ms=latency_ms, error=error
            )

    def _record_declared_source_statuses(self, kind: str, statuses: Any) -> None:
        """Apply a tool's granular provider outcome to endpoint health."""
        if not isinstance(statuses, dict):
            return
        for provider, status in statuses.items():
            normalized_status = str(status).lower()
            if normalized_status in {"live", "ok", "success"}:
                continue
            self.control.record_source(
                f"{provider}:{kind}",
                succeeded=False,
                status="unavailable",
                error=f"provider declared {normalized_status}",
                error_category="provider_unavailable",
                requested_source=str(provider),
                actual_source=str(provider),
                upstream_source=str(provider),
            )

    @staticmethod
    def _overall_status(market: dict[str, Any], research: dict[str, Any]) -> str:
        statuses = [market.get("status")] + [value.get("status") for value in research.values()]
        if any(status == "offline" for status in statuses if status):
            return "partial" if any(status in {"live", "partial"} for status in statuses if status) else "offline"
        if any(status == "partial" for status in statuses if status):
            return "partial"
        return "live"

    @staticmethod
    def _scope_from_series(
        series: list[dict[str, Any]], *, intraday: bool
    ) -> dict[str, Any]:
        candidates = [
            item
            for item in series
            if (str(item.get("interval") or "") != "1D") == intraday
        ]
        actionable = [
            item for item in candidates if item.get("actionability") == "price_actionable"
        ]
        chosen = actionable[0] if actionable else (candidates[0] if candidates else {})
        reasons = list(chosen.get("blocked_reasons") or [])
        if actionable:
            status = "verified"
        elif intraday and "intraday_not_started" in reasons:
            status = "not_started"
        elif candidates:
            status = "unavailable"
        else:
            status = "not_requested"
        selected = chosen.get("selected_quote") or {}
        freshness = chosen.get("freshness") or {}
        return {
            "status": status,
            "actionability": "price_actionable" if actionable else "analysis_only",
            "as_of": selected.get("bar_time") or freshness.get("bar_time"),
            "verified_at": selected.get("verified_at") or freshness.get("verified_at"),
            "interval": chosen.get("interval"),
            "adjustment": chosen.get("adjustment"),
            "reason": ", ".join(reasons) or None,
            "blocked_reasons": reasons,
        }

    @staticmethod
    def _research_scope(
        research: dict[str, Any], domain: str, symbol: str
    ) -> dict[str, Any]:
        domain_payload = research.get(domain) or {}
        items = domain_payload.get("items") if isinstance(domain_payload, dict) else {}
        item: dict[str, Any] = {}
        if isinstance(items, dict):
            direct = items.get(symbol)
            if isinstance(direct, dict):
                item = direct
            else:
                for raw_symbol, candidate in items.items():
                    if str(raw_symbol).upper() == symbol and isinstance(candidate, dict):
                        item = candidate
                        break
        documents = list(item.get("documents") or [])
        mode = str(item.get("mode") or "")
        if mode == "not_applicable":
            status = "not_applicable"
        elif documents and mode == "live":
            status = "verified"
        elif documents:
            status = "partial"
        elif item or domain_payload:
            status = "unavailable"
        else:
            status = "not_requested"
        newest = next(
            (
                str(document.get("published_at") or document.get("date") or "")
                for document in documents
                if isinstance(document, dict)
                and (document.get("published_at") or document.get("date"))
            ),
            "",
        )
        return {
            "status": status,
            "actionability": "analysis_only",
            "as_of": newest or None,
            "document_count": len(documents),
            "mode": mode or None,
            "reason": str(item.get("warning") or domain_payload.get("error") or "") or None,
        }

    def _decision_scopes(
        self,
        symbols: list[str],
        market: dict[str, Any],
        research: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        """Project broad context health into conclusion-specific safety scopes."""

        output: dict[str, dict[str, Any]] = {}
        all_series = list(market.get("series") or []) if isinstance(market, dict) else []
        for raw_symbol in symbols:
            symbol = str(raw_symbol).upper()
            series = [
                item
                for item in all_series
                if str(item.get("symbol") or "").upper() == symbol
            ]
            daily = self._scope_from_series(series, intraday=False)
            intraday = self._scope_from_series(series, intraday=True)
            exact_basis = next(
                (
                    scope
                    for scope in (intraday, daily)
                    if scope.get("actionability") == "price_actionable"
                ),
                None,
            )
            output[symbol] = {
                "daily_trend": daily,
                "intraday": intraday,
                "condition_order": {
                    "status": "verified" if exact_basis else (
                        "not_started" if intraday.get("status") == "not_started" else "unavailable"
                    ),
                    "actionability": "price_actionable" if exact_basis else "analysis_only",
                    "basis": (
                        "intraday"
                        if exact_basis is intraday
                        else "daily"
                        if exact_basis is daily
                        else None
                    ),
                    "as_of": (exact_basis or {}).get("as_of"),
                    "reason": None if exact_basis else (
                        intraday.get("reason") or daily.get("reason") or "no verified price scope"
                    ),
                },
                "fund_flow": {
                    "status": "not_requested",
                    "actionability": "analysis_only",
                    "as_of": None,
                    "reason": "fund flow is provided by a separate live tool",
                },
                "news": self._research_scope(research, "news", symbol),
                "fundamentals": self._research_scope(research, "fundamentals", symbol),
                "reports": self._research_scope(research, "reports", symbol),
            }
        return output

    def _context_provider_health(self, market: dict[str, Any]) -> list[dict[str, Any]]:
        """Attach provider diagnostics relevant to the current context request."""

        attempts: dict[str, int] = {}
        for series in market.get("series") or []:
            for attempt in series.get("source_attempts") or []:
                source = str(attempt.get("requested_source") or "unknown")
                attempts[source] = attempts.get(source, 0) + 1
        health = []
        for item in self.sources().get("sources") or []:
            row = dict(item)
            source = str(row.get("requested_source") or row.get("source") or "")
            row["request_attempt_count"] = attempts.get(source, 0)
            health.append(row)
        return health

    def read_bars(self, handle: str, cursor: int = 0) -> dict[str, Any]:
        params = self.control.get_handle(handle)
        if not params:
            if self.control.get_request(handle) is not None:
                raise KeyError("received a request_id, not a bars handle; use market.bars_handles[].handle from the context result")
            raise KeyError("unknown or expired data handle")
        bars = self.market_service.store.query_bars(
            symbol=params["symbol"], interval=params["interval"], adjustment=params["adjustment"],
            start=params.get("start"), limit=20_000,
        )
        offset = max(0, int(cursor))
        page = bars[offset : offset + _BAR_PAGE_SIZE]
        next_cursor = offset + len(page)
        return {
            "handle": handle,
            "symbol": params["symbol"], "interval": params["interval"], "adjustment": params["adjustment"],
            "cursor": offset, "next_cursor": next_cursor if next_cursor < len(bars) else None,
            "total": len(bars), "bars": page,
        }

    def coverage(self) -> dict[str, Any]:
        rows = self.market_service.store.list_coverage()
        holding_names: dict[str, str] = {}
        for item in load_state().holdings:
            symbols = _normalize_symbols(
                [str(item.get("symbol") or item.get("code") or "")]
            )
            name = str(item.get("name") or "").strip()
            if symbols and name:
                holding_names[symbols[0]] = name
        rows = [
            {
                **row,
                "name": row.get("name") or holding_names.get(str(row.get("symbol") or "")),
            }
            for row in rows
        ]
        return {"status": "ok", "coverage": rows, "watchlist": self.control.list_watchlist(), "retention": self._retention_policy()}

    def sources(self) -> dict[str, Any]:
        stored = {
            row["source"]: row
            for row in self.control.source_health()
            if str(row.get("requested_source") or "").lower() != "auto"
        }
        for current in self._latest_market_source_health():
            stored[current["source"]] = {**stored.get(current["source"], {}), **current}
        scoped_providers = {
            str(row.get("requested_source") or "")
            for row in stored.values()
            if row.get("capability") not in {None, "", "general"}
        }
        rows = []
        for row in stored.values():
            normalized = dict(row)
            requested = str(normalized.get("requested_source") or "")
            if normalized.get("capability") == "general" and requested in scoped_providers:
                continue
            error = str(normalized.get("last_error") or "")
            if "research reports are China A-share only" in error:
                normalized.update(
                    last_status="not_applicable",
                    effective_status="not_applicable",
                    error_category="not_applicable",
                    stale=False,
                )
            elif error.startswith("provider declared unavailable"):
                normalized.update(
                    last_status="unavailable",
                    effective_status="unavailable",
                    error_category="provider_unavailable",
                )
            rows.append(normalized)
        return {
            "status": "ok",
            "sources": sorted(rows, key=lambda row: str(row.get("source") or "")),
            "quorum": "two fresh independent sources; a third independent source decides disagreements",
            "price_basis_definitions": PRICE_BASIS_DEFINITIONS,
            "price_basis_rule": (
                "Only observations with the same canonical price basis may enter "
                "one consensus; source_default and unknown are never comparable."
            ),
        }

    def _latest_market_source_health(self) -> list[dict[str, Any]]:
        """Project the latest cache refresh attempts into current source health.

        Portfolio refreshes do not pass through ``get_data_context``. Reading the
        persisted run here keeps Data Center accurate without rewriting history or
        treating an old endpoint failure as a current outage.
        """
        records: list[dict[str, Any]] = []
        latest_attempts = getattr(
            self.market_service.store, "latest_source_attempts", lambda: []
        )()
        for attempt in latest_attempts:
            requested = str(attempt.get("requested_source") or "").strip()
            if not requested or requested.lower() == "auto":
                continue
            updated_at = str(attempt.get("updated_at") or utc_now())
            try:
                completed = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                if completed.tzinfo is None:
                    completed = completed.replace(tzinfo=timezone.utc)
            except ValueError:
                completed = datetime.now(timezone.utc)
            stale = (
                datetime.now(timezone.utc) - completed.astimezone(timezone.utc)
                > timedelta(minutes=30)
            )
            transport_events = list(attempt.get("transport_events") or [])
            fallback_event = transport_events[-1] if transport_events else None
            raw_status = str(attempt.get("status") or "failed")
            if raw_status == "success" and fallback_event:
                status = "ok_with_transport_fallback"
            elif raw_status == "success":
                status = "ok"
            else:
                status = raw_status
            error = (
                fallback_event.get("primary_error")
                if fallback_event
                else attempt.get("error")
            )
            category = (
                "transport_fallback"
                if fallback_event
                else attempt.get("error_category")
            )
            records.append({
                "source": f"{requested}:market",
                "requested_source": requested,
                "actual_source": attempt.get("actual_source") or requested,
                "upstream_source": attempt.get("upstream_source") or requested,
                "capability": "market",
                "consecutive_failures": 0 if raw_status == "success" else 1,
                "circuit_open": False,
                "circuit_open_until": None,
                "last_status": status,
                "effective_status": "stale" if stale else status,
                "stale": stale,
                "last_latency_ms": attempt.get("latency_ms"),
                "error_category": category,
                "last_error": error,
                "updated_at": updated_at,
            })
        return records

    def storage(self) -> dict[str, Any]:
        entries = []
        for kind, path in [
            ("market_cache", self.market_service.store.path),
            ("unified_control", self.control.path),
            ("research_cache", self.research.path),
        ]:
            entries.append({"kind": kind, "path": str(path), "bytes": path.stat().st_size if path.exists() else 0})
        loader_path = self._loader_cache_path()
        if loader_path is not None:
            entries.append({"kind": "loader_cache", "path": str(loader_path), "bytes": self._dir_size(loader_path)})
        total = sum(int(entry["bytes"]) for entry in entries)
        return {"status": "ok", "entries": entries, "total_bytes": total, "soft_limit_bytes": _SOFT_LIMIT_BYTES, "evict_at_bytes": _EVICT_AT_BYTES, "retention": self._retention_policy()}

    @staticmethod
    def _dir_size(path: Path) -> int:
        if path.is_file():
            return path.stat().st_size
        if not path.exists():
            return 0
        return sum(file.stat().st_size for file in path.rglob("*") if file.is_file())

    @staticmethod
    def _loader_cache_path() -> Path | None:
        """Return Loader Cache v3's real root only when its opt-in is enabled."""
        try:
            from backtest.loaders.base import loader_cache_enabled

            if not loader_cache_enabled():
                return None
        except Exception:
            return None
        return Path.home() / ".vibe-trading" / "cache" / "loaders"

    @staticmethod
    def _retention_policy() -> dict[str, Any]:
        return {
            "protected": {"1m_sessions": 10, "5m_sessions": 60, "1D_sessions": 2500},
            "temporary": {"1m_sessions": 5, "5m_sessions": 20, "1D_sessions": 750},
            "news_body_days": 180, "news_metadata_days": 730, "report_body_days": 1825,
            "loader_cache_days": 30,
        }

    def _enforce_retention(self) -> None:
        self.control.prune()
        self.research.prune()
        if self.storage()["total_bytes"] < _EVICT_AT_BYTES:
            return
        holdings = _normalize_symbols([str(item.get("symbol") or item.get("code") or "") for item in load_state().holdings])
        protected = set(holdings) | {item["symbol"] for item in self.control.list_watchlist()}
        seen: set[tuple[str, str, str]] = set()
        for row in self.market_service.store.list_coverage():
            key = (row["symbol"], row["interval"], row["actual_adjustment"])
            if key in seen:
                continue
            seen.add(key)
            keep = self._retention_policy()["protected" if row["symbol"] in protected else "temporary"]
            sessions = int(keep[f"{row['interval']}_sessions"])
            self.market_service.store.prune_sessions(*key, sessions)
        self._prune_loader_cache()

    def _prune_loader_cache(self) -> None:
        """Evict Loader Cache entries unused for 30 days after pressure begins."""
        root = self._loader_cache_path()
        if root is None or not root.exists():
            return
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
        for entry in root.rglob("*.parquet"):
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink()
            except OSError:
                continue

    def prewarm(self, *, phase: str = "premarket") -> dict[str, Any]:
        holdings = _normalize_symbols([str(item.get("symbol") or item.get("code") or "") for item in load_state().holdings])
        symbols = list(dict.fromkeys(holdings + [item["symbol"] for item in self.control.list_watchlist()]))
        if not symbols:
            raise ValueError("no holdings or explicit watchlist symbols to prewarm")
        purpose = "intraday" if phase == "intraday" else "premarket"
        return self.get_context(
            symbols=symbols,
            purpose=purpose,
            include=["market", "news", "reports"],
            force_live=True,
        )


_service: UnifiedDataService | None = None
_service_lock = threading.Lock()


def get_unified_data_service() -> UnifiedDataService:
    global _service
    with _service_lock:
        if _service is None:
            _service = UnifiedDataService()
        return _service
