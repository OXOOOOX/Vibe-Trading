from __future__ import annotations

import json
from pathlib import Path

from src.tools.verified_market_data_tool import VerifiedMarketDataTool


def _summary(close: float, *, verified_at: str, bar_time: str = "2026-07-10T15:00:00+08:00"):
    return {
        "symbol": "588870.SH",
        "status": "verified",
        "consensus_close": close,
        "actual_adjustment": "qfq",
        "interval": "1D",
        "bar_time": bar_time,
        "verified_at": verified_at,
        "sources": ["eastmoney", "tencent"],
    }


class _Store:
    def __init__(self, before, after, path: Path):
        self.before = before
        self.after = after
        self.path = path
        self.refreshed = False

    def cache_summaries(self, limit=1000):
        return self.after if self.refreshed else self.before


class _Service:
    def __init__(self, store: _Store, item: dict, status: str = "completed"):
        self.store = store
        self.item = item
        self.status = status

    def refresh_sync(self, **kwargs):
        self.store.refreshed = True
        return {
            "status": self.status,
            "run_id": "run-1",
            "items": [self.item],
        }


def _execute(monkeypatch, tmp_path, *, before, after, actual_sources, item_status="verified"):
    store = _Store(before, after, tmp_path / "cache" / "market_cache.sqlite3")
    service = _Service(
        store,
        {
            "symbol": "588870.SH",
            "interval": "1D",
            "adjustment": "qfq",
            "status": item_status,
            "actual_sources": actual_sources,
            "message": None,
        },
    )
    monkeypatch.setattr(
        "src.tools.verified_market_data_tool.get_market_refresh_service",
        lambda: service,
    )
    return json.loads(
        VerifiedMarketDataTool().execute(
            codes=["588870.SH"],
            start_date="2026-07-01",
            end_date="2026-07-10",
            interval="1D",
        )
    )


def test_live_failure_returns_explicit_cache_fallback(monkeypatch, tmp_path) -> None:
    cached = _summary(2.0, verified_at="2026-07-10T08:00:00Z")
    payload = _execute(
        monkeypatch,
        tmp_path,
        before=[cached],
        after=[cached],
        actual_sources=[],
        item_status="unresolved",
    )

    result = payload["results"]["588870.SH"]
    assert payload["data_mode"] == "cache_fallback"
    assert result["consensus_close"] == 2.0
    assert result["retrieval"]["live_fetch_succeeded"] is False
    assert result["retrieval"]["cache_fallback_used"] is True


def test_live_success_compares_against_pre_refresh_cache(monkeypatch, tmp_path) -> None:
    cached = _summary(2.0, verified_at="2026-07-10T08:00:00Z")
    refreshed = _summary(
        2.1,
        verified_at="2026-07-11T08:00:00Z",
        bar_time="2026-07-11T15:00:00+08:00",
    )
    payload = _execute(
        monkeypatch,
        tmp_path,
        before=[cached],
        after=[refreshed],
        actual_sources=["eastmoney", "tencent"],
    )

    result = payload["results"]["588870.SH"]
    comparison = result["retrieval"]["cache_comparison"]
    assert payload["data_mode"] == "live_with_cache_comparison"
    assert result["consensus_close"] == 2.1
    assert comparison["cached_close"] == 2.0
    assert round(comparison["change_pct"], 6) == 5.0
    assert comparison["independent_source"] is False
    assert comparison["comparison_status"] == "different_bar_time"


def test_no_live_data_and_no_cache_is_unresolved(monkeypatch, tmp_path) -> None:
    payload = _execute(
        monkeypatch,
        tmp_path,
        before=[],
        after=[],
        actual_sources=[],
        item_status="unresolved",
    )

    result = payload["results"]["588870.SH"]
    assert payload["data_mode"] == "unresolved"
    assert result["retrieval"]["cache_fallback_used"] is False
