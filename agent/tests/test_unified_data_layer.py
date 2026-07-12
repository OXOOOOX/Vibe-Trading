from __future__ import annotations

from datetime import date, datetime, timedelta
import asyncio
from pathlib import Path
from zoneinfo import ZoneInfo

from src.data_layer.service import UnifiedDataService
from src.data_layer.store import DataControlStore, ResearchCacheStore
from src.market_cache.service import MarketRefreshService
from src.market_cache.storage import MarketCacheStore
from src.data_layer.prewarm import DataPrewarmScheduler
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
        return '{"ok": true, "source": "test-report", "data": {"reports": [{"title": "Live report", "publish_date": "2026-07-13"}]}}'


def _unified(tmp_path: Path, fetcher):
    market = MarketRefreshService(
        store=MarketCacheStore(tmp_path / "market.sqlite3"), fetcher=fetcher, summary_dir=tmp_path / "summaries"
    )
    return UnifiedDataService(
        control=DataControlStore(tmp_path / "control.sqlite3"),
        research=ResearchCacheStore(tmp_path / "research.sqlite3"),
        market_service=market, news_tool=_ResearchTool("news"), reports_tool=_ResearchTool("report"),
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
    assert second["market"]["series"][0]["retrieval"]["mode"] == "live"
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
    assert series["retrieval"]["mode"] == "stale_cache"
    assert series["retrieval"]["cache_fallback_used"] is True


def test_live_research_is_cached_and_failed_refresh_is_historical_background(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_PORTFOLIO_STATE_PATH", str(tmp_path / "portfolio.json"))
    service = _unified(tmp_path, lambda **kwargs: _outcome(kwargs))
    fresh = service.get_context(symbols=["588870.SH"], purpose="long_term", include=["news", "reports"])
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


def test_registry_can_hide_low_level_data_tools_without_hiding_the_facade() -> None:
    registry = build_registry(exclude_tool_names={"get_market_data", "verified_market_data", "get_stock_news", "get_research_reports"})
    assert "get_data_context" in registry.tool_names
    assert "get_market_data" not in registry.tool_names
    assert "verified_market_data" not in registry.tool_names


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
    assert [record["phase"] for record in first] == ["premarket"]
    assert second == []
    assert calls == ["premarket"]
