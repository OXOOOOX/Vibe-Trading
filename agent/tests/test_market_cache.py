from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from src.market_cache.service import (
    MarketRefreshService,
    _actual_adjustment,
    _source_fingerprint,
    _volume_consensus,
    _volume_policy,
    source_candidates,
)
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


def _service(tmp_path, monkeypatch, fetcher, *, now_factory=None):
    monkeypatch.setenv("VIBE_TRADING_PORTFOLIO_STATE_PATH", str(tmp_path / "portfolio.json"))
    return MarketRefreshService(
        store=MarketCacheStore(tmp_path / "market_cache.sqlite3"),
        fetcher=fetcher,
        summary_dir=tmp_path / "summaries",
        now_factory=now_factory,
    )


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)


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


def test_volume_consensus_fails_closed_when_units_conflict() -> None:
    result = _volume_consensus([
        {"actual_source": "source-a", "volume": 10_000, "volume_unit": "shares"},
        {"actual_source": "source-b", "volume": 10_000, "volume_unit": "lots"},
    ])

    assert result["status"] == "conflict"
    assert result["volume"] is None
    assert result["unit"] is None
    assert result["sources"] == []


def test_mootdx_china_volume_is_normalized_from_lots_to_shares() -> None:
    assert _volume_policy("mootdx", "000651.SZ", "1D") == ("lot", 100.0)
    assert _volume_policy("mootdx", "000651.SZ", "1m") == ("share", 1.0)


def test_mootdx_volume_contract_migration_preserves_raw_evidence(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch, lambda **kwargs: _outcome(kwargs))
    outcome = _outcome(
        {
            "requested_source": "mootdx",
            "start_date": "2026-07-10",
            "end_date": "2026-07-10",
            "adjustment": "raw",
        },
        actual_source="mootdx",
    )
    normalized = service._normalize_records(
        outcome,
        symbol="000651.SZ",
        interval="1D",
        batch_id="legacy",
        acquisition_mode="cache",
    )[0]
    legacy = dict(normalized, volume=normalized["raw_volume"], volume_unit="share")
    service.store.upsert_source_bars([legacy])

    assert service.store.migrate_mootdx_volume_contract_v3() == [("000651.SZ", "1D", "raw")]
    migrated = service.store.source_bars("000651.SZ", "1D", "raw")[0]
    assert migrated["raw_volume"] == 100
    assert migrated["raw_volume_unit"] == "lot"
    assert migrated["volume"] == 10_000
    assert migrated["volume_unit"] == "share"
    assert service.store.migrate_mootdx_volume_contract_v3() == []


def test_mootdx_intraday_migration_restores_share_unit(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch, lambda **kwargs: _outcome(kwargs))
    outcome = _outcome(
        {
            "requested_source": "mootdx",
            "start_date": "2026-07-10",
            "end_date": "2026-07-10",
            "adjustment": "raw",
        },
        actual_source="mootdx",
    )
    normalized = service._normalize_records(
        outcome,
        symbol="000651.SZ",
        interval="1m",
        batch_id="legacy-intraday",
        acquisition_mode="cache",
    )[0]
    corrupted = dict(
        normalized,
        volume=normalized["raw_volume"] * 100,
        raw_volume_unit="lot",
    )
    service.store.upsert_source_bars([corrupted])

    assert service.store.migrate_mootdx_intraday_volume_contract_v4() == [
        ("000651.SZ", "1m", "raw")
    ]
    repaired = service.store.source_bars("000651.SZ", "1m", "raw")[0]
    assert repaired["raw_volume"] == 100
    assert repaired["raw_volume_unit"] == "share"
    assert repaired["volume"] == 100


def test_baostock_etf_qfq_rows_are_quarantined_as_unverified_source_default(
    tmp_path, monkeypatch
) -> None:
    service = _service(tmp_path, monkeypatch, lambda **kwargs: _outcome(kwargs))
    probe = _outcome(
        {
            "requested_source": "baostock",
            "start_date": "2026-07-10",
            "end_date": "2026-07-10",
            "adjustment": "qfq",
        },
        actual_source="baostock",
    )
    normalized = service._normalize_records(
        probe,
        symbol="159516.SZ",
        interval="1D",
        batch_id="legacy-qfq",
        acquisition_mode="cache",
    )
    service.store.upsert_source_bars(normalized)

    assert service.store.migrate_etf_adjustment_contract_v2() == ["159516.SZ"]
    assert service.store.source_bars("159516.SZ", "1D", "qfq") == []
    quarantined = service.store.source_bars("159516.SZ", "1D", "source_default")
    assert len(quarantined) == 1
    assert quarantined[0]["adjustment_confidence"] == "provider_etf_adjustment_unverified"
    assert service.store.migrate_etf_adjustment_contract_v2() == []


def test_two_continuous_qfq_sources_validate_event_factor(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch, lambda **kwargs: _outcome(kwargs))
    start = date(2026, 1, 1)
    raw_records = []
    qfq_records = []
    for offset in range(130):
        day = start + timedelta(days=offset)
        raw_close = 100.0 if offset < 80 else 50.0
        raw_records.append({
            "trade_date": day.isoformat(), "open": raw_close, "high": raw_close + 1,
            "low": raw_close - 1, "close": raw_close, "volume": 1000,
            "amount": raw_close * 1000,
        })
        qfq_records.append({
            "trade_date": day.isoformat(), "open": 50.0, "high": 51.0,
            "low": 49.0, "close": 50.0, "volume": 2000,
            "amount": 100_000,
        })

    def normalized(source: str, adjustment: str, records: list[dict]):
        return service._normalize_records(
            {
                "requested_source": source,
                "actual_source": source,
                "adapter_name": "tests.fake",
                "source_fingerprint": source,
                "requested_adjustment": adjustment,
                "actual_adjustment": adjustment,
                "adjustment_confidence": "test",
                "records": records,
            },
            symbol="159999.SZ",
            interval="1D",
            batch_id=f"{source}-{adjustment}",
            acquisition_mode="cache",
        )

    for source in ("raw-a", "raw-b"):
        service.store.upsert_source_bars(normalized(source, "raw", raw_records))
    service._recompute_consensus("159999.SZ", "1D", "raw", "test-factor")
    for source in ("qfq-a", "qfq-b"):
        service.store.upsert_source_bars(normalized(source, "qfq", qfq_records))

    candidates = service.derive_adjustment_factor_candidates("159999.SZ")
    rows = service.store.list_adjustment_factors("159999.SZ")

    assert len(candidates) == 2
    assert {row["source"] for row in rows} == {
        "derived_qfq:qfq-a", "derived_qfq:qfq-b",
    }
    assert {row["factor"] for row in rows} == {0.5}


def test_daily_upserts_remove_legacy_timestamp_variants(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch, lambda **kwargs: _outcome(kwargs))
    outcome = _outcome(
        {
            "requested_source": "yahoo",
            "start_date": "2026-07-13",
            "end_date": "2026-07-13",
            "adjustment": "raw",
        },
        actual_source="yahoo",
        close=317.31,
    )
    canonical = service._normalize_records(
        outcome,
        symbol="AAPL.US",
        interval="1D",
        batch_id="canonical",
        acquisition_mode="network",
    )[0]
    legacy = dict(canonical, bar_time="2026-07-13T00:00:00+00:00", batch_id="legacy")

    service.store.upsert_source_bars([legacy])
    service.store.upsert_source_bars([canonical])
    source_rows = service.store.source_bars("AAPL.US", "1D", "raw")

    assert len(source_rows) == 1
    assert source_rows[0]["bar_time"] == "2026-07-13T04:00:00+00:00"

    consensus = service._recompute_consensus("AAPL.US", "1D", "raw", "canonical")[0]
    old_consensus = dict(
        consensus,
        bar_time="2026-07-13T00:00:00+00:00",
        batch_id="legacy",
    )
    service.store.replace_consensus([old_consensus])
    service.store.replace_consensus([consensus])

    bars = service.store.query_bars(
        symbol="AAPL.US", interval="1D", adjustment="raw", view="consensus"
    )
    assert len(bars) == 1
    assert bars[0]["bar_time"] == "2026-07-13T04:00:00+00:00"


def test_identical_active_refresh_is_deduplicated(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch, lambda **kwargs: _outcome(kwargs))
    request = dict(symbols=["588870.SH"], profile="portfolio_default")

    first, first_deduplicated = service.create_refresh(**request)
    second, second_deduplicated = service.create_refresh(**request)

    assert first_deduplicated is False
    assert second_deduplicated is True
    assert second["run_id"] == first["run_id"]


def test_cache_summaries_can_be_scoped_to_current_holdings(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch, lambda **kwargs: _outcome(kwargs))
    service.refresh_sync(
        symbols=["588870.SH", "000905.SH"],
        profile="test",
        sources=["source-a", "source-b"],
        start_date="2026-07-10",
        end_date="2026-07-10",
        items=[("1D", "raw")],
    )

    assert {row["symbol"] for row in service.store.cache_summaries()} == {
        "588870.SH",
        "000905.SH",
    }
    assert [
        row["symbol"]
        for row in service.store.cache_summaries(symbols=["588870.SH"])
    ] == ["588870.SH"]
    assert service.store.cache_summaries(symbols=[]) == []


def test_a_share_raw_sources_try_fast_independent_pair_first() -> None:
    assert source_candidates("588870.SH", "1m", "raw") == [
        "tencent", "mootdx", "eastmoney",
    ]
    assert source_candidates("600036.SH", "1D", "raw") == [
        "tencent", "mootdx", "eastmoney",
    ]
    assert source_candidates("600036.SH", "1D", "qfq") == [
        "tencent", "baostock", "eastmoney", "akshare",
    ]
    assert source_candidates("159516.SZ", "1D", "qfq") == [
        "tencent", "eastmoney",
    ]
    assert source_candidates("UNRESOLVED", "1D", "raw") == []


def test_us_equity_uses_supported_sources_and_profile_variants(tmp_path, monkeypatch) -> None:
    assert source_candidates("AAPL.US", "1m", "raw") == [
        "yahoo", "nasdaq", "eastmoney",
    ]
    assert source_candidates("AAPL.US", "5m", "raw") == [
        "yahoo", "nasdaq", "eastmoney",
    ]
    assert source_candidates("AAPL.US", "1D", "raw") == [
        "sina", "eastmoney",
    ]
    assert source_candidates("AAPL.US", "1D", "qfq") == ["yahoo"]
    assert source_candidates("AAPL.US", "1D", "hfq") == []

    service = _service(tmp_path, monkeypatch, lambda **kwargs: _outcome(kwargs))
    run, _ = service.create_refresh(symbols=["AAPL.US"], profile="portfolio_default")

    assert [(item["interval"], item["adjustment"]) for item in run["items"]] == [
        ("1m", "raw"),
        ("5m", "raw"),
        ("1D", "raw"),
        ("1D", "qfq"),
    ]

    save_state(
        PortfolioState(holdings=[{
            "code": "AAPL",
            "symbol": "AAPL",
            "name": "Apple Inc.",
            "quantity": 2,
            "cost_price": 1.5,
        }]),
        tmp_path / "portfolio.json",
    )
    service.refresh_sync(
        symbols=["AAPL.US"],
        profile="us-single-source",
        sources=["yahoo"],
        items=[("1m", "raw")],
    )

    quote = service.store.quote("AAPL.US")
    assert quote is not None
    assert quote["status"] == "single_source"
    holding = load_state(tmp_path / "portfolio.json").holdings[0]
    assert holding["symbol"] == "AAPL.US"
    assert holding["last_price"] == 2.0
    assert holding["market_status"] == "single_source"


def test_us_daily_price_bases_are_not_relabelled_for_compatibility() -> None:
    assert _actual_adjustment("yahoo", "AAPL.US", "1D", "qfq") == (
        "qfq",
        "adjusted_close_factor",
    )
    assert _actual_adjustment("yahoo", "AAPL.US", "1D", "raw") == (
        "split_adjusted",
        "provider_contract",
    )
    assert _actual_adjustment("sina", "AAPL.US", "1D", "raw") == (
        "raw",
        "loader_contract",
    )
    assert _actual_adjustment("stooq", "AAPL.US", "1D", "qfq")[0] == (
        "source_default"
    )

def test_yahoo_and_yfinance_are_not_counted_as_independent_upstreams() -> None:
    assert _source_fingerprint("yahoo", "AAPL.US") == "yahoo_chart"
    assert _source_fingerprint("yfinance", "AAPL.US") == "yahoo_chart"


def test_startup_migration_removes_legacy_yahoo_daily_raw_bars(
    tmp_path, monkeypatch
) -> None:
    service = _service(tmp_path, monkeypatch, lambda **kwargs: _outcome(kwargs))
    service.refresh_sync(
        symbols=["AAPL.US"],
        profile="legacy-yahoo-raw",
        sources=["yahoo"],
        start_date="2026-07-10",
        end_date="2026-07-10",
        items=[("1D", "raw")],
    )
    assert service.store.source_bars("AAPL.US", "1D", "raw")
    assert service.store.query_bars(
        symbol="AAPL.US", interval="1D", adjustment="raw"
    )

    service.prepare_startup()

    assert service.store.source_bars("AAPL.US", "1D", "raw") == []
    assert service.store.query_bars(
        symbol="AAPL.US", interval="1D", adjustment="raw"
    ) == []
    assert service.store.coverage("AAPL.US", "yahoo", "1D", "raw") is None


def test_recent_verified_us_bar_is_preferred_to_newer_forming_bar(tmp_path, monkeypatch) -> None:
    def fetcher(**kwargs):
        outcome = _outcome(kwargs, close=313.0)
        first_timestamp = (
            "2026-07-14 15:55:00"
            if kwargs["requested_source"] == "yahoo"
            else "2026-07-14 11:55:00"
        )
        outcome["records"] = [
            {
                "trade_date": first_timestamp,
                "open": 313.0, "high": 313.0, "low": 313.0, "close": 313.0,
            }
        ]
        if kwargs["requested_source"] == "yahoo":
            outcome["records"].append(
                {
                    "trade_date": "2026-07-14 16:00:00",
                    "open": 313.1, "high": 313.1, "low": 313.1, "close": 313.1,
                }
            )
        return outcome

    service = _service(tmp_path, monkeypatch, fetcher)
    service.refresh_sync(
        symbols=["AAPL.US"], profile="test", sources=["yahoo", "nasdaq"],
        start_date="2026-07-14", end_date="2026-07-14", items=[("5m", "raw")],
    )

    quote = service.store.quote("AAPL.US")
    assert quote is not None
    assert quote["last_price"] == 313.0
    with service.store.connect() as connection:
        stored = connection.execute(
            "SELECT status FROM latest_quotes WHERE symbol='AAPL.US'"
        ).fetchone()
    assert stored["status"] == "verified"


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


def test_fresh_successful_sources_are_reused_without_network_calls(tmp_path, monkeypatch) -> None:
    clock = MutableClock(datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc))
    calls: list[str] = []

    def fetcher(**kwargs):
        calls.append(kwargs["requested_source"])
        outcome = _outcome(
            kwargs,
            close=2.0 if kwargs["requested_source"] == "source-a" else 2.001,
        )
        outcome["records"] = [{
            "trade_date": "2026-07-15 11:00:00",
            "open": 2.0,
            "high": 2.001,
            "low": 2.0,
            "close": 2.0 if kwargs["requested_source"] == "source-a" else 2.001,
            "volume": 100,
            "amount": 200,
        }]
        return outcome

    service = _service(tmp_path, monkeypatch, fetcher, now_factory=clock)
    request = dict(
        symbols=["AAPL.US"],
        profile="adaptive",
        sources=["source-a", "source-b"],
        items=[("1m", "raw")],
    )
    first = service.refresh_sync(**request)
    assert calls == ["source-a", "source-b"]
    calls.clear()

    second = service.refresh_sync(**request)

    assert first["items"][0]["status"] == "verified"
    assert second["items"][0]["status"] == "verified"
    assert calls == []
    assert [attempt["status"] for attempt in second["items"][0]["attempts"]] == [
        "cache_fresh",
        "cache_fresh",
    ]


def test_failed_source_retries_when_due_while_fresh_quorum_stays_cached(tmp_path, monkeypatch) -> None:
    clock = MutableClock(datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc))
    calls: list[str] = []

    def fetcher(**kwargs):
        source = kwargs["requested_source"]
        calls.append(source)
        if source == "source-c":
            raise ConnectionError("still unavailable")
        return _outcome(kwargs, close=2.0 if source == "source-a" else 2.001)

    service = _service(tmp_path, monkeypatch, fetcher, now_factory=clock)
    request = dict(
        symbols=["AAPL.US"],
        profile="adaptive-retry",
        sources=["source-a", "source-b", "source-c"],
        items=[("1D", "raw")],
    )
    service.refresh_sync(**request)
    service.store.record_source_attempt(
        symbol="AAPL.US",
        requested_source="source-c",
        interval="1D",
        adjustment="raw",
        status="failed",
        attempted_at=clock().isoformat(),
        error_category="transport_error",
        error="offline",
        retry_base_seconds=60,
    )
    calls.clear()

    service.refresh_sync(**request)
    assert calls == []

    clock.advance(seconds=61)
    retried = service.refresh_sync(**request)

    assert calls == ["source-c"]
    assert retried["items"][0]["status"] == "verified"
    assert [attempt["status"] for attempt in retried["items"][0]["attempts"]] == [
        "cache_fresh",
        "cache_fresh",
        "failed",
    ]
    state = service.store.source_poll_state("AAPL.US", "source-c", "1D", "raw")
    assert state is not None
    assert state["consecutive_failures"] == 2


def test_closed_market_success_is_reused_until_the_next_open(tmp_path, monkeypatch) -> None:
    clock = MutableClock(datetime(2026, 7, 15, 20, 5, tzinfo=timezone.utc))
    calls: list[str] = []

    def fetcher(**kwargs):
        calls.append(kwargs["requested_source"])
        outcome = _outcome(kwargs, close=2.0)
        outcome["records"] = [{
            "trade_date": "2026-07-15 16:00:00",
            "open": 2.0,
            "high": 2.0,
            "low": 2.0,
            "close": 2.0,
            "volume": 100,
            "amount": 200,
        }]
        return outcome

    service = _service(tmp_path, monkeypatch, fetcher, now_factory=clock)
    request = dict(
        symbols=["AAPL.US"],
        profile="closed-market",
        sources=["source-a", "source-b"],
        items=[("1m", "raw")],
    )
    service.refresh_sync(**request)
    calls.clear()

    service.refresh_sync(**request)
    assert calls == []

    clock.value = datetime(2026, 7, 16, 13, 31, tzinfo=timezone.utc)
    service.refresh_sync(**request)
    assert calls == ["source-a", "source-b"]


def test_cached_refresh_preserves_source_lag_diagnostics(tmp_path, monkeypatch) -> None:
    clock = MutableClock(datetime(2026, 7, 15, 20, 5, tzinfo=timezone.utc))

    def fetcher(**kwargs):
        outcome = _outcome(kwargs, close=2.0)
        if kwargs["requested_source"] == "lagging-source":
            outcome["records"] = outcome["records"][:-1]
        return outcome

    service = _service(tmp_path, monkeypatch, fetcher, now_factory=clock)
    request = dict(
        symbols=["AAPL.US"],
        profile="cached-lag",
        sources=["current-source", "lagging-source"],
        items=[("1D", "raw")],
    )

    first = service.refresh_sync(**request)
    second = service.refresh_sync(**request)

    assert first["items"][0]["status"] == "source_lag"
    assert second["items"][0]["status"] == "source_lag"
    assert [attempt["status"] for attempt in second["items"][0]["attempts"]] == [
        "cache_fresh",
        "cache_fresh",
    ]


def test_new_secondary_source_backfills_full_requested_window(tmp_path, monkeypatch) -> None:
    calls = []

    def fetcher(**kwargs):
        calls.append(dict(kwargs))
        return _outcome(
            kwargs,
            close=2.0 if kwargs["requested_source"] == "source-a" else 2.001,
        )

    service = _service(tmp_path, monkeypatch, fetcher)
    service.refresh_sync(
        symbols=["AAPL.US"], profile="test", sources=["source-a", "source-b"],
        start_date="2026-06-01", end_date="2026-07-10", items=[("1D", "raw")],
    )

    assert [call["start_date"] for call in calls] == ["2026-06-01", "2026-06-01"]


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


def test_intraday_mootdx_volume_uses_same_share_contract_as_other_cn_sources(
    tmp_path,
    monkeypatch,
) -> None:
    raw_volumes = {"tencent": 100, "mootdx": 10_100, "eastmoney": 102}
    calls: list[str] = []

    def fetcher(**kwargs):
        source = kwargs["requested_source"]
        calls.append(source)
        outcome = _outcome(kwargs, actual_source=source, close=2.0)
        outcome["records"] = [{
            "trade_date": "2026-07-10 10:00:00",
            "open": 2.0,
            "high": 2.01,
            "low": 1.99,
            "close": 2.0,
            "volume": raw_volumes[source],
            "amount": 20_200,
        }]
        return outcome

    service = _service(tmp_path, monkeypatch, fetcher)
    service.refresh_sync(
        symbols=["588870.SH"],
        profile="volume-majority",
        sources=["tencent", "mootdx", "eastmoney"],
        start_date="2026-07-10",
        end_date="2026-07-10",
        items=[("5m", "raw")],
        require_volume_quorum=True,
    )

    summary = service.store.cache_summaries()[0]
    assert calls == ["tencent", "mootdx"]
    assert summary["status"] == "verified"
    assert summary["volume_status"] == "verified"
    assert summary["volume_source_count"] == 2
    assert summary["volume_sources"] == ["tencent", "mootdx"]
    assert summary["volume"] == 10_050
    mootdx = next(
        row for row in summary["observations"] if row["actual_source"] == "mootdx"
    )
    assert mootdx["raw_volume"] == 10_100
    assert mootdx["raw_volume_unit"] == "share"
    assert mootdx["volume"] == 10_100
    assert mootdx["included_in_volume_consensus"] is True


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

    assert run["items"][0]["status"] == "unresolved_conflict"
    assert holding["last_price"] == 1.95
    assert holding["market_status"] == "unresolved_conflict"


def test_failed_first_source_continues_until_two_fresh_sources_agree(tmp_path, monkeypatch) -> None:
    calls: list[str] = []

    def fetcher(**kwargs):
        source = kwargs["requested_source"]
        calls.append(source)
        if source == "source-a":
            raise ConnectionError("first provider offline")
        return _outcome(kwargs, close=2.0 if source == "source-b" else 2.001)

    service = _service(tmp_path, monkeypatch, fetcher)
    run = service.refresh_sync(
        symbols=["588870.SH"], profile="test",
        sources=["source-a", "source-b", "source-c"],
        start_date="2026-07-10", end_date="2026-07-10", items=[("1D", "raw")],
    )

    assert calls == ["source-a", "source-b", "source-c"]
    assert run["items"][0]["status"] == "verified"
    assert run["items"][0]["attempts"][0]["error_category"] == "transport_error"


def test_old_intraday_snapshot_is_excluded_from_current_batch(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.market_cache.service._session_is_settled", lambda *_: False)
    service = _service(tmp_path, monkeypatch, lambda **kwargs: _outcome(kwargs, close=2.0))
    service.refresh_sync(
        symbols=["588870.SH"], profile="test", sources=["old-source"],
        start_date="2026-07-10", end_date="2026-07-10", items=[("1D", "raw")],
    )
    service.fetcher = lambda **kwargs: _outcome(kwargs, close=2.2)
    service.refresh_sync(
        symbols=["588870.SH"], profile="test", sources=["closing-source"],
        start_date="2026-07-10", end_date="2026-07-10", items=[("1D", "raw")],
    )

    summary = service.store.cache_summaries()[0]
    assert summary["status"] == "provisional_mix"
    excluded = next(row for row in summary["observations"] if row["actual_source"] == "old-source")
    assert excluded["included_in_consensus"] is False
    assert excluded["exclude_reason"] == "stale_observation"


def test_settled_session_reuses_cached_independent_confirmation(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.market_cache.service._session_is_settled", lambda *_: True)
    service = _service(tmp_path, monkeypatch, lambda **kwargs: _outcome(kwargs, close=2.0))
    service.refresh_sync(
        symbols=["588870.SH"], profile="test", sources=["old-source"],
        start_date="2026-07-10", end_date="2026-07-10", items=[("1D", "raw")],
    )
    service.fetcher = lambda **kwargs: _outcome(kwargs, close=2.001)
    run = service.refresh_sync(
        symbols=["588870.SH"], profile="test", sources=["refresh-source"],
        start_date="2026-07-10", end_date="2026-07-10", items=[("1D", "raw")],
    )

    summary = service.store.cache_summaries()[0]
    assert run["items"][0]["status"] == "verified"
    assert summary["status"] == "verified"
    assert summary["source_count"] == 2
    assert "cached_confirmation_reused" in summary["quality_flags"]


def test_different_latest_trade_dates_are_source_lag_not_conflict(tmp_path, monkeypatch) -> None:
    def fetcher(**kwargs):
        outcome = _outcome(kwargs, close=2.0)
        if kwargs["requested_source"] == "lagging-source":
            outcome["records"] = outcome["records"][:-1]
        return outcome

    service = _service(tmp_path, monkeypatch, fetcher)
    run = service.refresh_sync(
        symbols=["588870.SH"], profile="test", sources=["current-source", "lagging-source"],
        start_date="2026-07-09", end_date="2026-07-10", items=[("1D", "raw")],
    )

    assert run["items"][0]["status"] == "source_lag"
    assert service.store.cache_summaries()[0]["status"] == "source_lag"


def test_daily_midnight_and_close_timestamp_share_one_trading_session(tmp_path, monkeypatch) -> None:
    def fetcher(**kwargs):
        outcome = _outcome(kwargs, close=2.0)
        if kwargs["requested_source"] == "close-stamped-source":
            outcome["records"][-1]["trade_date"] = "2026-07-10 15:00:00"
        return outcome

    service = _service(tmp_path, monkeypatch, fetcher)
    run = service.refresh_sync(
        symbols=["588870.SH"], profile="test",
        sources=["midnight-source", "close-stamped-source"],
        start_date="2026-07-10", end_date="2026-07-10", items=[("1D", "raw")],
    )

    summary = service.store.cache_summaries()[0]
    assert run["items"][0]["status"] == "verified"
    assert summary["status"] == "verified"
    assert summary["source_count"] == 2


def test_three_sources_without_majority_leave_price_null(tmp_path, monkeypatch) -> None:
    closes = {"source-a": 2.0, "source-b": 2.2, "source-c": 2.4}
    service = _service(
        tmp_path,
        monkeypatch,
        lambda **kwargs: _outcome(kwargs, close=closes[kwargs["requested_source"]]),
    )
    run = service.refresh_sync(
        symbols=["588870.SH"], profile="test", sources=list(closes),
        start_date="2026-07-10", end_date="2026-07-10", items=[("1D", "raw")],
    )

    summary = service.store.cache_summaries()[0]
    assert run["items"][0]["status"] == "unresolved_conflict"
    assert summary["status"] == "unresolved_conflict"
    assert summary["consensus_close"] is None
    assert summary["source_count"] == 0
