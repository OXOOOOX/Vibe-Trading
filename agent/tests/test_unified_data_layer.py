from __future__ import annotations

from datetime import date, datetime, timedelta
import asyncio
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import src.data_layer.service as data_service
from src.data_layer.service import UnifiedDataService
from src.data_layer.store import DataControlStore, ResearchCacheStore
from src.market_cache.service import MarketRefreshService
from src.market_cache.storage import MarketCacheStore
from src.data_layer.prewarm import ChinaMarketCalendar, DataPrewarmScheduler
from src.tools import build_registry


def _outcome(kwargs, *, close: float = 2.0):
    start = date.fromisoformat(kwargs["start_date"])
    end = date.fromisoformat(kwargs["end_date"])
    rows = []
    cursor = start
    while cursor <= end:
        rows.append({"trade_date": cursor.isoformat(), "open": close, "high": close, "low": close, "close": close, "volume": 100, "amount": 200})
        cursor += timedelta(days=1)
    return {
        "requested_source": kwargs["requested_source"], "actual_source": kwargs["requested_source"],
        "adapter_name": "tests.fake", "source_fingerprint": kwargs["requested_source"],
        "requested_adjustment": kwargs["adjustment"], "actual_adjustment": kwargs["adjustment"],
        "adjustment_confidence": "test", "records": rows,
    }


class _ResearchTool:
    def __init__(self, kind: str) -> None:
        self.kind = kind

    def execute(self, **kwargs):
        if self.kind == "news":
            return '{"ok": true, "source": "test-news", "data": {"articles": [{"title": "Live headline", "published_at": "2026-07-13"}]}}'
        if self.kind == "fundamental":
            return '{"ok": true, "source": "test-fundamentals", "data": {"periods": [{"REPORT_DATE": "2025-12-31"}]}}'
        return '{"ok": true, "source": "test-report", "data": {"reports": [{"title": "Live report", "publish_date": "2026-07-13"}]}}'


def _unified(tmp_path: Path, fetcher):
    market = MarketRefreshService(
        store=MarketCacheStore(tmp_path / "market.sqlite3"), fetcher=fetcher, summary_dir=tmp_path / "summaries"
    )
    return UnifiedDataService(
        control=DataControlStore(tmp_path / "control.sqlite3"),
        research=ResearchCacheStore(tmp_path / "research.sqlite3"),
        market_service=market, news_tool=_ResearchTool("news"), reports_tool=_ResearchTool("report"), fundamentals_tool=_ResearchTool("fundamental"),
    )


def test_long_term_history_is_cache_first_and_returns_contiguous_pages(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_PORTFOLIO_STATE_PATH", str(tmp_path / "portfolio.json"))
    calls: list[dict] = []

    def fetcher(**kwargs):
        calls.append(kwargs)
        return _outcome(kwargs)

    service = _unified(tmp_path, fetcher)
    first = service.get_context(symbols=["588870.SH"], purpose="long_term", lookback_days=20, include=["market"])
    assert first["status"] == "live"
    assert calls
    handle = first["market"]["series"][0]["handle"]
    page = service.read_bars(handle)
    assert page["bars"] == sorted(page["bars"], key=lambda row: row["bar_time"])
    calls.clear()
    second = service.get_context(symbols=["588870.SH"], purpose="long_term", lookback_days=20, include=["market"])
    assert second["market"]["series"][0]["retrieval"]["mode"] == "cache"
    assert calls == []  # Existing settled coverage is read, not re-fetched.


def test_live_failure_uses_explicit_stale_market_cache_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_PORTFOLIO_STATE_PATH", str(tmp_path / "portfolio.json"))
    service = _unified(tmp_path, lambda **kwargs: _outcome(kwargs))
    service.get_context(symbols=["588870.SH"], purpose="latest_price", include=["market"])

    def unavailable(**kwargs):
        raise RuntimeError("network unavailable")

    service.market_service.fetcher = unavailable
    result = service.get_context(symbols=["588870.SH"], purpose="latest_price", include=["market"])
    series = result["market"]["series"][0]
    assert series["retrieval"]["mode"] == "cache_fallback"
    assert series["retrieval"]["cache_fallback_used"] is True
    assert series["actionability"] == "analysis_only"
    assert series["selected_quote"] is None


def test_premarket_context_reuses_covered_cache_and_publishes_bar_handles(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_PORTFOLIO_STATE_PATH", str(tmp_path / "portfolio.json"))
    calls: list[dict] = []

    def fetcher(**kwargs):
        calls.append(kwargs)
        return _outcome(kwargs)

    service = _unified(tmp_path, fetcher)
    first = service.get_context(
        symbols=["588870.SH"], purpose="premarket", include=["market"], force_live=True
    )
    assert calls
    handle = first["market"]["bars_handles"][0]["handle"]
    assert handle == first["market"]["series"][0]["handle"]
    calls.clear()

    cached = service.get_context(symbols=["588870.SH"], purpose="premarket", include=["market"])
    assert cached["market"]["runs"] == []
    assert calls == []
    assert cached["market"]["series"][0]["retrieval"]["mode"] == "cache"
    try:
        service.read_bars(cached["request_id"])
    except KeyError as exc:
        assert "request_id" in str(exc)
    else:  # pragma: no cover - the request id must never be a bar handle
        raise AssertionError("request_id unexpectedly resolved as a bars handle")


def test_holding_uses_resolution_specific_history_and_reuses_weekday_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_PORTFOLIO_STATE_PATH", str(tmp_path / "portfolio.json"))
    calls: list[dict] = []

    def weekday_fetcher(**kwargs):
        calls.append(dict(kwargs))
        outcome = _outcome(kwargs)
        outcome["records"] = [
            row for row in outcome["records"]
            if date.fromisoformat(str(row["trade_date"])[:10]).weekday() < 5
        ]
        return outcome

    service = _unified(tmp_path, weekday_fetcher)
    first = service.get_context(symbols=["588870.SH"], purpose="holding", include=["market"])
    first_starts: dict[tuple[str, str], str] = {}
    for call in calls:
        first_starts.setdefault((call["interval"], call["adjustment"]), call["start_date"])

    assert first["market"]["runs"]
    assert (date.today() - date.fromisoformat(first_starts[("1m", "raw")])).days == 10
    assert (date.today() - date.fromisoformat(first_starts[("5m", "raw")])).days == 10
    assert (date.today() - date.fromisoformat(first_starts[("1D", "qfq")])).days == 250

    calls.clear()
    second = service.get_context(symbols=["588870.SH"], purpose="holding", include=["market"])
    assert second["market"]["runs"] == []
    assert calls == []


def test_live_research_is_cached_and_failed_refresh_is_historical_background(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_PORTFOLIO_STATE_PATH", str(tmp_path / "portfolio.json"))
    service = _unified(tmp_path, lambda **kwargs: _outcome(kwargs))
    fresh = service.get_context(symbols=["588870.SH"], purpose="long_term", include=["fundamentals", "news", "reports"])
    assert fresh["research"]["fundamentals"]["items"]["588870.SH"]["mode"] == "live"
    assert fresh["research"]["news"]["items"]["588870.SH"]["mode"] == "live"
    assert fresh["research"]["reports"]["items"]["588870.SH"]["mode"] == "live"

    class _Down:
        def execute(self, **kwargs):
            return '{"ok": false, "error": "upstream offline"}'

    service.news_tool = _Down()
    service.reports_tool = _Down()
    fallback = service.get_context(symbols=["588870.SH"], purpose="long_term", include=["news", "reports"])
    assert fallback["research"]["news"]["items"]["588870.SH"]["mode"] == "historical_background"
    assert fallback["research"]["reports"]["items"]["588870.SH"]["mode"] == "historical_background"
    health = {item["source"]: item for item in service.control.source_health()}
    assert health["eastmoney:news"]["last_status"] == "failed"
    assert health["eastmoney:report"]["last_status"] == "failed"


def test_deadline_stops_refresh_after_the_inflight_source_returns(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_PORTFOLIO_STATE_PATH", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(data_service, "_LIVE_TIMEOUT_SECONDS", 0.05)
    calls: list[dict] = []

    def slow_fetcher(**kwargs):
        calls.append(kwargs)
        time.sleep(0.03)
        return _outcome(kwargs)

    service = _unified(tmp_path, slow_fetcher)
    result = service.get_context(
        symbols=["588870.SH", "510300.SH"], purpose="latest_price", include=["market"]
    )
    assert result["status"] == "partial"
    time.sleep(0.08)
    # One source may still be in flight at the deadline, but cancellation stops
    # the next source/symbol instead of continuing the whole refresh run.
    assert len(calls) <= 2


def test_watchlist_and_source_circuit_policy(tmp_path) -> None:
    control = DataControlStore(tmp_path / "control.sqlite3")
    assert control.add_watchlist("510300.sh")["symbol"] == "510300.SH"
    for _ in range(3):
        control.record_source("eastmoney", succeeded=False, error="timeout")
    health = control.source_health()[0]
    assert health["consecutive_failures"] == 3
    assert health["circuit_open"] is True
    control.record_source("eastmoney", succeeded=True, latency_ms=22)
    assert control.source_health()[0]["circuit_open"] is False


def test_sources_include_latest_portfolio_refresh_attempts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_PORTFOLIO_STATE_PATH", str(tmp_path / "portfolio.json"))

    def fetcher(**kwargs):
        if kwargs["requested_source"] == "offline-source":
            raise ConnectionError("endpoint refused connection")
        outcome = _outcome(kwargs)
        outcome["source_fingerprint"] = "independent-upstream"
        return outcome

    service = _unified(tmp_path, fetcher)
    service.market_service.refresh_sync(
        symbols=["588870.SH"], profile="test",
        sources=["healthy-source", "offline-source"],
        start_date="2026-07-10", end_date="2026-07-10", items=[("1D", "raw")],
    )

    sources = {row["source"]: row for row in service.sources()["sources"]}
    assert sources["healthy-source:market"]["effective_status"] == "ok"
    assert sources["healthy-source:market"]["upstream_source"] == "independent-upstream"
    assert sources["offline-source:market"]["effective_status"] == "failed"
    assert sources["offline-source:market"]["error_category"] == "transport_error"


def test_registry_can_hide_low_level_data_tools_without_hiding_the_facade() -> None:
    registry = build_registry(exclude_tool_names={"get_market_data", "verified_market_data", "get_stock_news", "get_research_reports", "get_financial_statements", "get_stock_profile"})
    assert "get_data_context" in registry.tool_names
    assert "get_market_data" not in registry.tool_names
    assert "verified_market_data" not in registry.tool_names
    assert "get_financial_statements" not in registry.tool_names


class _CalendarPayload:
    columns = ["trade_date"]

    def __init__(self, days: list[str]) -> None:
        self.days = days

    def __getitem__(self, _key):
        return self

    def tolist(self) -> list[str]:
        return self.days


def test_market_calendar_fails_closed_without_verified_calendar(tmp_path: Path) -> None:
    def unavailable():
        raise RuntimeError("calendar unavailable")

    calendar = ChinaMarketCalendar(
        unavailable, cache_path=tmp_path / "missing-calendar.json"
    )

    assert calendar.is_trading_day(date(2026, 10, 1)) is False
    assert calendar.mode == "calendar_unavailable"


def test_market_calendar_reuses_persisted_verified_calendar(tmp_path: Path) -> None:
    cache_path = tmp_path / "calendar.json"
    fetched = ChinaMarketCalendar(
        lambda: _CalendarPayload(["2026-07-14"]), cache_path=cache_path
    )
    assert fetched.is_trading_day(date(2026, 7, 14)) is True
    assert cache_path.exists()

    def unavailable():
        raise RuntimeError("calendar unavailable")

    cached = ChinaMarketCalendar(unavailable, cache_path=cache_path)
    assert cached.is_trading_day(date(2026, 7, 14)) is True
    assert cached.is_trading_day(date(2026, 10, 1)) is False
    assert cached.mode == "cached_exchange_calendar"


def test_market_calendar_accepts_bundled_compact_dates(tmp_path: Path) -> None:
    calendar = ChinaMarketCalendar(
        lambda: ["20260714", "20260715"], cache_path=tmp_path / "calendar.json"
    )

    assert calendar.is_trading_day(date(2026, 7, 14)) is True
    assert calendar.is_trading_day(date(2026, 7, 16)) is False


def test_prewarm_runs_each_market_calendar_slot_once() -> None:
    calls: list[str] = []

    class _Service:
        def prewarm(self, *, phase):
            calls.append(phase)
            return {"status": "live", "request_id": "prewarm-1"}

    class _Calendar:
        mode = "exchange_calendar"

        @staticmethod
        def is_trading_day(value):
            return True

    scheduler = DataPrewarmScheduler(lambda: _Service(), calendar=_Calendar())
    now = datetime(2026, 7, 13, 9, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    first = asyncio.run(scheduler.run_due_once(now))
    second = asyncio.run(scheduler.run_due_once(now))
    lunch = asyncio.run(scheduler.run_due_once(datetime(2026, 7, 13, 11, 25, tzinfo=ZoneInfo("Asia/Shanghai"))))
    assert [record["phase"] for record in first] == ["premarket"]
    assert second == []
    assert [record["phase"] for record in lunch] == ["intraday"]
    assert calls == ["premarket", "intraday"]
