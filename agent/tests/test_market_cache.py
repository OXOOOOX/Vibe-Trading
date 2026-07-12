from __future__ import annotations

from datetime import date, timedelta

from src.market_cache.service import MarketRefreshService
from src.market_cache.storage import MarketCacheStore
from src.portfolio.state import PortfolioState, load_state, save_state


def _outcome(kwargs, *, actual_source=None, close=2.0, amount=20_000.0):
    source = actual_source or kwargs["requested_source"]
    start = date.fromisoformat(kwargs["start_date"])
    end = date.fromisoformat(kwargs["end_date"])
    records = []
    cursor = start
    while cursor <= end:
        records.append(
            {
                "trade_date": cursor.isoformat(),
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 100,
                "amount": amount,
            }
        )
        cursor += timedelta(days=1)
    return {
        "requested_source": kwargs["requested_source"],
        "actual_source": source,
        "adapter_name": "tests.fake",
        "source_fingerprint": source,
        "requested_adjustment": kwargs["adjustment"],
        "actual_adjustment": kwargs["adjustment"],
        "adjustment_confidence": "test",
        "records": records,
    }


def _service(tmp_path, monkeypatch, fetcher):
    monkeypatch.setenv("VIBE_TRADING_PORTFOLIO_STATE_PATH", str(tmp_path / "portfolio.json"))
    return MarketRefreshService(
        store=MarketCacheStore(tmp_path / "market_cache.sqlite3"),
        fetcher=fetcher,
        summary_dir=tmp_path / "summaries",
    )


def test_schema_is_idempotent_and_running_jobs_are_interrupted(tmp_path) -> None:
    path = tmp_path / "market_cache.sqlite3"
    store = MarketCacheStore(path)
    MarketCacheStore(path)
    store.create_run(
        run_id="run-1",
        dedupe_key="same",
        profile="test",
        symbols=["588870.SH"],
        config={"items": [["1D", "raw"]]},
        items=[("588870.SH", "1D", "raw")],
    )

    assert store.mark_running_interrupted() == 1
    assert store.get_run("run-1")["status"] == "interrupted"


def test_identical_active_refresh_is_deduplicated(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch, lambda **kwargs: _outcome(kwargs))
    request = dict(symbols=["588870.SH"], profile="portfolio_default")

    first, first_deduplicated = service.create_refresh(**request)
    second, second_deduplicated = service.create_refresh(**request)

    assert first_deduplicated is False
    assert second_deduplicated is True
    assert second["run_id"] == first["run_id"]


def test_second_refresh_only_requests_overlap_tail_without_duplicate_bars(tmp_path, monkeypatch) -> None:
    calls = []

    def fetcher(**kwargs):
        calls.append(dict(kwargs))
        close = 2.0 if kwargs["requested_source"] == "source-a" else 2.001
        return _outcome(kwargs, close=close)

    service = _service(tmp_path, monkeypatch, fetcher)
    request = dict(
        symbols=["588870.SH"], profile="test", sources=["source-a", "source-b"],
        start_date="2026-07-01", end_date="2026-07-10", items=[("1D", "raw")],
    )
    first = service.refresh_sync(**request)
    first_count = service.store.list_coverage()[0]["row_count"]
    calls.clear()
    second = service.refresh_sync(**request)
    second_count = service.store.list_coverage()[0]["row_count"]

    assert first["status"] == second["status"] == "completed"
    assert calls[0]["start_date"] >= "2026-07-08"
    assert first_count == second_count == 10


def test_same_actual_upstream_is_not_counted_twice(tmp_path, monkeypatch) -> None:
    def fetcher(**kwargs):
        return _outcome(kwargs, actual_source="shared-upstream")

    service = _service(tmp_path, monkeypatch, fetcher)
    service.refresh_sync(
        symbols=["588870.SH"], profile="test", sources=["alias-a", "alias-b"],
        start_date="2026-07-10", end_date="2026-07-10", items=[("1D", "raw")],
    )

    summary = service.store.cache_summaries()[0]
    assert summary["status"] == "single_source"
    assert summary["source_count"] == 1
    assert summary["sources"] == ["shared-upstream"]


def test_third_source_excludes_one_price_outlier(tmp_path, monkeypatch) -> None:
    closes = {"source-a": 2.0, "source-b": 2.2, "source-c": 2.001}

    def fetcher(**kwargs):
        return _outcome(kwargs, close=closes[kwargs["requested_source"]])

    service = _service(tmp_path, monkeypatch, fetcher)
    service.refresh_sync(
        symbols=["588870.SH"], profile="test",
        sources=["source-a", "source-b", "source-c"],
        start_date="2026-07-10", end_date="2026-07-10", items=[("1D", "raw")],
    )

    summary = service.store.cache_summaries()[0]
    excluded = next(row for row in summary["observations"] if row["actual_source"] == "source-b")
    assert summary["status"] == "verified"
    assert summary["source_count"] == 2
    assert excluded["included_in_consensus"] is False
    assert excluded["exclude_reason"] == "price_outlier"


def test_adjustments_are_isolated_and_amount_builds_vwap(tmp_path, monkeypatch) -> None:
    def fetcher(**kwargs):
        close = 2.0 if kwargs["adjustment"] == "raw" else 1.8
        return _outcome(kwargs, close=close, amount=20_000.0)

    service = _service(tmp_path, monkeypatch, fetcher)
    service.refresh_sync(
        symbols=["588870.SH"], profile="test", sources=["eastmoney", "tencent"],
        start_date="2026-07-10", end_date="2026-07-10",
        items=[("1D", "raw"), ("1D", "qfq")],
    )

    summaries = service.store.cache_summaries()
    assert {(row["actual_adjustment"], row["consensus_close"]) for row in summaries} == {
        ("raw", 2.0), ("qfq", 1.8),
    }
    raw = next(row for row in summaries if row["actual_adjustment"] == "raw")
    assert raw["volume"] == 10_000.0
    assert raw["amount"] == 20_000.0
    assert raw["vwap"] == 2.0


def test_conflict_preserves_existing_holding_price(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "portfolio.json"
    monkeypatch.setenv("VIBE_TRADING_PORTFOLIO_STATE_PATH", str(state_path))
    save_state(
        PortfolioState(
            holdings=[{
                "name": "科创50指", "code": "588870", "symbol": "588870.SH",
                "quantity": 1000, "cost_price": 1.8, "last_price": 1.95,
            }]
        ),
        state_path,
    )

    def fetcher(**kwargs):
        close = 2.0 if kwargs["requested_source"] == "source-a" else 2.2
        return _outcome(kwargs, close=close)

    service = MarketRefreshService(
        store=MarketCacheStore(tmp_path / "market_cache.sqlite3"),
        fetcher=fetcher,
        summary_dir=tmp_path / "summaries",
    )
    run = service.refresh_sync(
        symbols=["588870.SH"], profile="test", sources=["source-a", "source-b"],
        start_date="2026-07-10", end_date="2026-07-10", items=[("1D", "raw")],
    )
    holding = load_state(state_path).holdings[0]

    assert run["items"][0]["status"] == "conflict"
    assert holding["last_price"] == 1.95
    assert holding["market_status"] == "conflict"
