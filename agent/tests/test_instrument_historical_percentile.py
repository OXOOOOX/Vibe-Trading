from src.reports.etf_valuation_percentile import ETFValuationPercentileService
from src.reports.instrument_historical_percentile import (
    BaoStockEquityValuationProvider,
    InstrumentHistoricalPercentileService,
    YahooPricePercentileProvider,
)
from src.research.knowledge import ResearchKnowledgeStore


def _knowledge(tmp_path):
    return ResearchKnowledgeStore(
        path=tmp_path / "research_cache.sqlite3",
        object_dir=tmp_path / "objects",
    )


def _equity_rows():
    return [
        {"date": "2026-07-14", "peTTM": "10", "pbMRQ": "1", "psTTM": "2"},
        {"date": "2026-07-15", "peTTM": "20", "pbMRQ": "2", "psTTM": "3"},
        {"date": "2026-07-16", "peTTM": "-5", "pbMRQ": "3", "psTTM": "4"},
        {"date": "2026-07-17", "peTTM": "30", "pbMRQ": "4", "psTTM": "5"},
    ]


def test_mainland_company_uses_same_definition_and_excludes_negative_pe() -> None:
    provider = BaoStockEquityValuationProvider(
        get_history_fn=lambda _symbol: _equity_rows(),
        now_provider=lambda: "2026-07-19T12:00:00+00:00",
        minimum_observations=3,
    )

    snapshot = provider.fetch("600036.SH", instrument_name="招商银行")

    assert snapshot["status"] == "available"
    assert snapshot["valuation_basis"] == "company_valuation"
    assert snapshot["sample_start"] == "2026-07-14"
    assert snapshot["sample_end"] == "2026-07-17"
    pe = next(item for item in snapshot["metrics"] if item["key"] == "pe_ttm")
    assert pe["value"] == 30.0
    assert pe["observation_count"] == 3
    assert pe["percentile"] == 2 / 3 * 100
    assert pe["temperature"] == "正常"
    assert snapshot["source"]["provider_id"] == "baostock_daily_valuation"


def test_negative_current_pe_is_not_replaced_with_a_stale_positive_value() -> None:
    rows = _equity_rows()
    rows[-1]["peTTM"] = "-1"
    provider = BaoStockEquityValuationProvider(
        get_history_fn=lambda _symbol: rows,
        minimum_observations=3,
    )

    snapshot = provider.fetch("688256.SH", instrument_name="寒武纪")

    assert snapshot["status"] == "available"
    assert "pe_ttm" not in {item["key"] for item in snapshot["metrics"]}
    assert {item["key"] for item in snapshot["metrics"]} == {"pb_mrq", "ps_ttm"}
    assert any("当前值无效或不适用" in warning for warning in snapshot["warnings"])


def test_overseas_company_fallback_is_explicitly_price_not_valuation() -> None:
    provider = YahooPricePercentileProvider(
        get_history_fn=lambda _symbol: [
            ("2026-07-14", 10.0),
            ("2026-07-15", 20.0),
            ("2026-07-16", 30.0),
            ("2026-07-17", 40.0),
        ],
        now_provider=lambda: "2026-07-19T12:00:00+00:00",
        minimum_observations=3,
    )

    snapshot = provider.fetch(
        "AAPL.US",
        instrument_name="Apple Inc.",
        currency="USD",
    )

    assert snapshot["valuation_basis"] == "adjusted_price_history"
    assert snapshot["metrics"][0]["percentile"] == 75.0
    assert snapshot["metrics"][0]["temperature"] == "偏高"
    assert "非估值" in snapshot["scope_label"]
    assert any("不把它冒充 PE/PB/PS" in warning for warning in snapshot["warnings"])


def test_generic_store_migrates_existing_etf_snapshots(tmp_path) -> None:
    knowledge = _knowledge(tmp_path)

    class _LegacyProvider:
        def fetch(self, symbol, *, tracked_index_code, tracked_index_name):
            return {
                "schema_version": 1,
                "symbol": symbol,
                "tracked_index_code": tracked_index_code,
                "tracked_index_name": tracked_index_name,
                "status": "available",
                "lookback_years": 10,
                "data_as_of": "2026-07-17T00:00:00+08:00",
                "retrieved_at": "2026-07-19T12:00:00+00:00",
                "mapping_method": "tracked_index_code_exact",
                "metrics": [{
                    "key": "pe", "label": "PE · 市盈率", "value": 12.0,
                    "percentile": 40.0, "temperature": "正常",
                }],
                "source": {
                    "source_id": "legacy", "provider_id": "baifenwei_index_valuation",
                    "label": "百分位 · 指数估值", "publisher": "百分位",
                    "verification_status": "public_secondary", "url": "https://baifenwei.com/",
                    "methodology_url": "https://baifenwei.com/methodology/",
                    "retrieved_at": "2026-07-19T12:00:00+00:00",
                },
                "unavailable_reason": None,
                "warnings": [],
            }

    ETFValuationPercentileService(knowledge, provider=_LegacyProvider()).refresh(
        "588870.SH",
        tracked_index_code="000688",
        tracked_index_name="科创50",
    )

    service = InstrumentHistoricalPercentileService(knowledge)
    loaded = service.latest_snapshot("588870.SH")

    assert loaded is not None
    assert loaded["instrument_type"] == "etf"
    assert loaded["valuation_basis"] == "tracked_index_valuation"
    assert loaded["scope_label"] == "科创50 · 跟踪指数估值"
    assert loaded["history_count"] == 1


def test_generic_refresh_is_idempotent_for_company_equity(tmp_path) -> None:
    knowledge = _knowledge(tmp_path)
    equity = BaoStockEquityValuationProvider(
        get_history_fn=lambda _symbol: _equity_rows(),
        now_provider=lambda: "2026-07-19T12:00:00+00:00",
        minimum_observations=3,
    )
    service = InstrumentHistoricalPercentileService(
        knowledge,
        equity_provider=equity,
    )

    first = service.refresh(
        "600036.SH",
        instrument_type="company_equity",
        instrument_name="招商银行",
    )
    second = service.refresh(
        "600036.SH",
        instrument_type="company_equity",
        instrument_name="招商银行",
    )

    assert first["snapshot_id"] == second["snapshot_id"]
    assert second["history_count"] == 1
