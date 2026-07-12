"""Policy service behind ``get_data_context`` and the Data Center API."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from src.market_cache import get_market_refresh_service
from src.portfolio.state import load_state, normalize_symbol
from src.tools.financial_statements_tool import FinancialStatementsTool
from src.tools.research_reports_tool import ResearchReportsTool
from src.tools.stock_news_tool import StockNewsTool

from .store import DataControlStore, ResearchCacheStore, utc_now


_LIVE_TIMEOUT_SECONDS = int(os.getenv("VIBE_TRADING_DATA_LIVE_TIMEOUT_SECONDS", "45"))
_BAR_PAGE_SIZE = 500
_AGENT_BAR_PREVIEW = 120
_SOFT_LIMIT_BYTES = int(float(os.getenv("VIBE_TRADING_DATA_STORAGE_SOFT_LIMIT_GB", "10")) * 1024**3)
_EVICT_AT_BYTES = int(_SOFT_LIMIT_BYTES * 0.9)

# The profile is a lower bound. Callers may request a longer lookback, but
# cannot downgrade a task to a coarser interval or fewer history bars.
PROFILES: dict[str, dict[str, Any]] = {
    "latest_price": {"items": [("1m", "raw")], "lookback_days": 2, "live": True},
    "holding": {"items": [("1m", "raw"), ("5m", "raw"), ("1D", "qfq")], "lookback_days": 250, "live": True},
    "premarket": {"items": [("1D", "qfq")], "lookback_days": 300, "live": True},
    "intraday": {"items": [("1m", "raw"), ("5m", "raw"), ("1D", "qfq")], "lookback_days": 300, "live": True},
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
        effective_days = max(int(lookback_days or 0), int(profile["lookback_days"]))
        include_set = {entry.strip().lower() for entry in (include or ["market", "fundamentals", "news", "reports"])}
        invalid_include = include_set - {"market", "fundamentals", "news", "reports"}
        if invalid_include:
            raise ValueError(f"unsupported data domains: {', '.join(sorted(invalid_include))}")
        live = bool(profile["live"] if force_live is None else force_live)
        fingerprint_data = {
            "symbols": normalized, "purpose": purpose, "days": effective_days,
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
        include: set[str],
        live: bool,
        started: float,
    ) -> dict[str, Any]:
        market: dict[str, Any] = {}
        research: dict[str, Any] = {}
        tasks = []
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="unified-data") as pool:
            if "market" in include:
                tasks.append(("market", pool.submit(self._market_context, symbols, profile, effective_days, live, started)))
            if "fundamentals" in include:
                tasks.append(("fundamentals", pool.submit(self._research_context, "fundamental", symbols, started)))
            if "news" in include:
                tasks.append(("news", pool.submit(self._research_context, "news", symbols, started)))
            if "reports" in include:
                tasks.append(("reports", pool.submit(self._research_context, "report", symbols, started)))
            for name, task in tasks:
                remaining = max(0.1, _LIVE_TIMEOUT_SECONDS - (time.monotonic() - started))
                try:
                    value = task.result(timeout=remaining)
                except Exception as exc:  # includes timeout; cached result is supplied below where possible
                    value = {"status": "partial", "error": str(exc)}
                if name == "market":
                    market = value
                else:
                    research[name] = value
        status = self._overall_status(market, research)
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
            "policy": {
                "historical": "cache_first_fill_gaps",
                "mutable_tail": "live_first",
                "price_sensitive": "two_source_quorum_then_third_on_conflict",
                "news": "live_revalidated_with_historical_background_fallback",
                "bar_preview": _AGENT_BAR_PREVIEW,
                "bar_page_size": _BAR_PAGE_SIZE,
            },
        }

    def _market_context(self, symbols: list[str], profile: dict[str, Any], days: int, live: bool, started: float) -> dict[str, Any]:
        result: dict[str, Any] = {"status": "offline", "series": [], "quotes": [], "runs": []}
        for interval, adjustment in profile["items"]:
            if time.monotonic() - started >= _LIVE_TIMEOUT_SECONDS:
                result["status"] = "partial"
                result.setdefault("warnings", []).append("live request deadline reached")
                break
            item_live = live or interval in {"1m", "5m"}
            target_start = (date.today() - timedelta(days=days)).isoformat()
            cache_complete = self._has_coverage(symbols, interval, adjustment, target_start)
            run: dict[str, Any] | None = None
            # Settled historical data does not refetch merely because it was read.
            # Mutable intraday tails always revalidate, and any coverage gap is filled.
            if item_live or not cache_complete:
                before = time.monotonic()
                run = self.market_service.refresh_sync(
                    symbols=symbols,
                    profile="unified_data",
                    force=False,
                    start_date=target_start,
                    end_date=date.today().isoformat(),
                    items=[(interval, adjustment)],
                )
                latency_ms = round((time.monotonic() - before) * 1000, 1)
                self._record_market_sources(run, latency_ms)
                result["runs"].append({"run_id": run.get("run_id"), "status": run.get("status"), "interval": interval, "adjustment": adjustment})
            for symbol in symbols:
                bars = self.market_service.store.query_bars(
                    symbol=symbol, interval=interval, adjustment=adjustment,
                    start=target_start, limit=max(_AGENT_BAR_PREVIEW, 2500 if interval == "1D" else 5000),
                )
                summary = self._series_summary(symbol, interval, adjustment, bars, run, item_live)
                handle = uuid.uuid4().hex
                self.control.put_handle(handle, {"symbol": symbol, "interval": interval, "adjustment": adjustment, "start": target_start})
                summary["handle"] = handle
                # Never stride-sample technical bars: this is the latest contiguous window.
                summary["bars"] = bars[-_AGENT_BAR_PREVIEW:]
                result["series"].append(summary)
        result["quotes"] = self.market_service.store.list_quotes(symbols)
        if result["series"]:
            statuses = {series["retrieval"]["mode"] for series in result["series"]}
            result["status"] = "live" if statuses <= {"live"} else ("partial" if "offline" in statuses or "stale_cache" in statuses else "live")
        return result

    def _has_coverage(self, symbols: list[str], interval: str, adjustment: str, target_start: str) -> bool:
        coverage = self.market_service.store.list_coverage(symbols)
        for symbol in symbols:
            matched = [row for row in coverage if row["symbol"] == symbol and row["interval"] == interval and row["actual_adjustment"] == adjustment]
            if not matched or min(str(row["min_bar_time"])[:10] for row in matched) > target_start:
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
        if latest is None:
            mode = "offline"
        elif live_requested and not live_sources:
            mode = "stale_cache"
        else:
            mode = "live"
        return {
            "symbol": symbol,
            "interval": interval,
            "adjustment": adjustment,
            "bar_count": len(bars),
            "coverage": {"start": bars[0]["bar_time"] if bars else None, "end": latest["bar_time"] if latest else None},
            "latest": latest,
            "retrieval": {
                "mode": mode,
                "live_requested": live_requested,
                "live_sources": live_sources,
                "cache_fallback_used": mode == "stale_cache",
                "verification_status": latest.get("status") if latest else "unresolved",
                "source_count": latest.get("source_count", 0) if latest else 0,
                "sources": latest.get("sources", []) if latest else [],
                "verified_at": latest.get("verified_at") if latest else None,
            },
        }

    def _record_market_sources(self, run: dict[str, Any], latency_ms: float) -> None:
        for item in run.get("items") or []:
            sources = item.get("actual_sources") or []
            for source in sources:
                self.control.record_source(str(source), succeeded=True, latency_ms=latency_ms)
            if not sources:
                for message in [item.get("message") or "market source returned no usable bars"]:
                    self.control.record_source("market", succeeded=False, latency_ms=latency_ms, error=str(message))

    def _research_context(self, kind: str, symbols: list[str], started: float) -> dict[str, Any]:
        output: dict[str, Any] = {"status": "live", "items": {}}
        for symbol in symbols:
            if time.monotonic() - started >= _LIVE_TIMEOUT_SECONDS:
                output["status"] = "partial"
                break
            try:
                if kind == "news":
                    raw = self.news_tool.execute(code=symbol, limit=20)
                elif kind == "report":
                    raw = self.reports_tool.execute(code=symbol, limit=20)
                else:
                    raw = self.fundamentals_tool.execute(code=symbol, statement="indicators", period="annual")
                envelope = _parse_json(raw)
                if not envelope.get("ok"):
                    raise RuntimeError(str(envelope.get("error") or "research source failed"))
                data = envelope.get("data") or {}
                documents = data.get("articles") or data.get("reports") or data.get("matches") or []
                if kind == "fundamental":
                    documents = [{"title": "annual indicators", "published_at": data.get("as_of") or data.get("report_date"), "summary": "latest annual indicator payload", "data": data}]
                source = envelope.get("source")
                self.research.replace_documents(kind, symbol, [item for item in documents if isinstance(item, dict)], source=str(source) if source else None)
                output["items"][symbol] = {"mode": "live", "source": source, "documents": self.research.latest(kind, symbol)}
            except Exception as exc:
                cached = self.research.latest(kind, symbol)
                output["status"] = "partial"
                output["items"][symbol] = {
                    "mode": "historical_background" if cached else "unavailable",
                    "documents": cached,
                    "warning": "latest live research unavailable; cached material is historical background only" if cached else str(exc),
                }
        return output

    @staticmethod
    def _overall_status(market: dict[str, Any], research: dict[str, Any]) -> str:
        statuses = [market.get("status")] + [value.get("status") for value in research.values()]
        if any(status == "offline" for status in statuses if status):
            return "partial" if any(status in {"live", "partial"} for status in statuses if status) else "offline"
        if any(status == "partial" for status in statuses if status):
            return "partial"
        return "live"

    def read_bars(self, handle: str, cursor: int = 0) -> dict[str, Any]:
        params = self.control.get_handle(handle)
        if not params:
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
        return {"status": "ok", "coverage": rows, "watchlist": self.control.list_watchlist(), "retention": self._retention_policy()}

    def sources(self) -> dict[str, Any]:
        return {"status": "ok", "sources": self.control.source_health(), "quorum": "two sources; a third is requested when the first two conflict"}

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
        return self.get_context(symbols=symbols, purpose=purpose, include=["market", "news", "reports"])


_service: UnifiedDataService | None = None
_service_lock = threading.Lock()


def get_unified_data_service() -> UnifiedDataService:
    global _service
    with _service_lock:
        if _service is None:
            _service = UnifiedDataService()
        return _service
