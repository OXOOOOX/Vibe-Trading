from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.report_library_routes import register_report_library_routes
from src.reports import ReportEnvelope, ReportLibraryService
from src.reports.instrument_profile import (
    EastmoneyInstrumentProfileProvider,
    InstrumentProfileService,
)
from src.research.knowledge import ResearchKnowledgeStore


QUOTE_TIME = 1_784_275_919


def _fake_get_json(url: str, *, params, **_kwargs):
    if "stock/get" in url:
        code = str(params["secid"]).split(".", 1)[1]
        if code in {"510050", "513180"}:
            return {"data": {
                "f43": 0.586,
                "f57": "513180",
                "f58": "上证50ETF" if code == "510050" else "恒生科技ETF华夏",
                "f84": 69_827_760_128,
                "f85": 69_827_760_128,
                "f86": QUOTE_TIME,
                "f116": 40_919_067_435.008,
                "f117": 40_919_067_435.008,
                "f168": 11.12,
                "f170": -4.4,
                "f189": 20210525,
            }}
        return {"data": {
            "f43": 1253.0,
            "f57": "600519",
            "f58": "贵州茅台",
            "f84": 1_250_081_601,
            "f85": 1_250_081_601,
            "f86": QUOTE_TIME,
            "f116": 1_566_352_246_053,
            "f117": 1_566_352_246_053,
            "f127": "白酒Ⅱ",
            "f128": "贵州板块",
            "f129": "酿酒概念,超级品牌,白酒",
            "f162": 14.37,
            "f163": 19.03,
            "f164": 18.94,
            "f167": 6.64,
            "f168": 0.47,
            "f170": -0.48,
            "f173": 10.57,
            "f189": 20010827,
        }}
    return {"result": {"data": [
        {
            "REPORT_DATE": "2026-06-30 00:00:00",
            "ASSIGN_PROGRESS": "预披露",
        },
        {
            "REPORT_DATE": "2025-12-31 00:00:00",
            "ASSIGN_PROGRESS": "实施分配",
            "IMPL_PLAN_PROFILE": "10派280.2423元(含税)",
            "PRETAX_BONUS_RMB": 280.2423,
            "DIVIDENT_RATIO": 0.023120394357,
            "EX_DIVIDEND_DATE": "2026-06-26 00:00:00",
        },
        {
            "REPORT_DATE": "2025-09-30 00:00:00",
            "ASSIGN_PROGRESS": "实施分配",
            "IMPL_PLAN_PROFILE": "10派239.57元(含税)",
            "PRETAX_BONUS_RMB": 239.57,
            "DIVIDENT_RATIO": 0.016741439553,
            "EX_DIVIDEND_DATE": "2025-12-19 00:00:00",
        },
        {
            "REPORT_DATE": "2024-12-31 00:00:00",
            "ASSIGN_PROGRESS": "实施分配",
            "IMPL_PLAN_PROFILE": "10派276.73元(含税)",
            "PRETAX_BONUS_RMB": 276.73,
            "DIVIDENT_RATIO": 0.019272770326,
            "EX_DIVIDEND_DATE": "2025-06-26 00:00:00",
        },
    ]}}


def _fake_get_text(url: str) -> str:
    if "sh510050" in url:
        return (
            'var sh510050hfq={"total":2,"data":['
            '{"d":"2025-12-17","f":"1","s":"1","u":"0.797"},'
            '{"d":"2024-12-02","f":"1","s":"1","u":"0.717"}]};'
        )
    return (
        'var etfhfq={"total":1,"data":['
        '{"d":"1900-01-01","f":"1","s":"1","u":"0"}]};'
    )


def _provider() -> EastmoneyInstrumentProfileProvider:
    return EastmoneyInstrumentProfileProvider(
        get_json_fn=_fake_get_json,
        get_text_fn=_fake_get_text,
        now_provider=lambda: "2026-07-19T05:00:00+00:00",
    )


def test_equity_profile_uses_explicit_base_units_and_dividend_semantics() -> None:
    profile = _provider().fetch("600519.SH")
    metrics = {item["key"]: item for item in profile["metrics"]}

    assert profile["instrument_type"] == "company_equity"
    assert profile["identity"]["industry"] == "白酒Ⅱ"
    assert profile["identity"]["listing_date"] == "2001-08-27"
    assert metrics["total_market_cap"]["value"] == 1_566_352_246_053
    assert metrics["total_market_cap"]["unit"] == "CNY"
    assert metrics["total_shares"]["value"] == 1_250_081_601
    assert metrics["total_shares"]["unit"] == "shares"
    assert metrics["pe_ttm"]["value"] == 18.94
    expected_dividend_per_share = 28.02423 + 23.957
    assert metrics["dividend_per_share_ttm"]["value"] == expected_dividend_per_share
    assert metrics["dividend_yield_ttm"]["value"] == expected_dividend_per_share / 1253.0
    assert metrics["dividend_yield_ttm"]["semantics"] == (
        "trailing_365d_adjusted_cash_dividend_per_current_share_divided_by_current_price"
    )
    assert profile["sources"][1]["distribution_count"] == 2
    assert len(profile["sources"][1]["distributions"]) == 2


def test_stock_ttm_dividend_adjusts_cash_to_post_transfer_share_basis() -> None:
    def transfer_get_json(url: str, *, params, **kwargs):
        if "stock/get" in url:
            payload = _fake_get_json(url, params=params, **kwargs)
            payload["data"].update({
                "f43": 1190.58,
                "f57": "688256",
                "f58": "寒武纪",
            })
            return payload
        return {"result": {"data": [{
            "REPORT_DATE": "2025-12-31 00:00:00",
            "ASSIGN_PROGRESS": "实施分配",
            "IMPL_PLAN_PROFILE": "10转4.90股派15.00元(含税)",
            "PRETAX_BONUS_RMB": 15.0,
            "BONUS_IT_RATIO": 4.9,
            "IT_RATIO": 4.9,
            "EX_DIVIDEND_DATE": "2026-05-08 00:00:00",
        }]}}

    provider = EastmoneyInstrumentProfileProvider(
        get_json_fn=transfer_get_json,
        get_text_fn=_fake_get_text,
        now_provider=lambda: "2026-07-19T05:00:00+00:00",
    )
    profile = provider.fetch("688256.SH")
    metrics = {item["key"]: item for item in profile["metrics"]}
    expected_current_share_cash = 1.5 / 1.49

    assert metrics["dividend_per_share_ttm"]["value"] == round(
        expected_current_share_cash, 12
    )
    assert metrics["dividend_yield_ttm"]["value"] == (
        round(expected_current_share_cash, 12) / 1190.58
    )
    distribution = profile["sources"][1]["distributions"][0]
    assert distribution["share_adjustment_factor"] == 1.49


def test_etf_profile_keeps_fund_scale_separate_from_company_valuation() -> None:
    profile = _provider().fetch("513180.SH")
    metrics = {item["key"]: item for item in profile["metrics"]}

    assert profile["instrument_type"] == "etf"
    assert metrics["total_shares"]["label"] == "基金份额"
    assert metrics["total_shares"]["unit"] == "fund_units"
    assert metrics["total_market_cap"]["semantics"] == "market_price_times_total_units"
    assert "pe_ttm" not in metrics
    assert "pb" not in metrics
    assert profile["warnings"] == [
        "ETF 自身不适用公司 PE/PB；跟踪指数估值应使用独立指数口径。"
    ]


def test_quote_refresh_falls_back_to_labelled_delay_channel() -> None:
    def flaky_get_json(url: str, *, params, **kwargs):
        if "push2.eastmoney.com" in url:
            raise ConnectionError("primary channel rate limited")
        return _fake_get_json(url, params=params, **kwargs)

    provider = EastmoneyInstrumentProfileProvider(
        get_json_fn=flaky_get_json,
        get_text_fn=_fake_get_text,
        now_provider=lambda: "2026-07-19T05:00:00+00:00",
    )
    profile = provider.fetch("513180.SH")

    assert profile["sources"][0]["provider_id"] == "eastmoney_push2_delay_quote"
    assert profile["sources"][0]["label"] == "东方财富延迟行情"
    assert "push2delay.eastmoney.com" in profile["sources"][0]["url"]
    assert profile["warnings"][0] == (
        "主行情通道限流，本快照使用东方财富延迟行情容灾通道。"
    )


def test_etf_distribution_yield_uses_trailing_cash_distribution_per_unit() -> None:
    profile = _provider().fetch("510050.SH")
    metrics = {item["key"]: item for item in profile["metrics"]}

    assert metrics["distribution_per_unit_ttm"]["value"] == 0.08
    assert metrics["distribution_per_unit_ttm"]["unit"] == "CNY_per_fund_unit"
    assert metrics["distribution_yield_ttm"]["value"] == 0.08 / 0.586
    assert metrics["distribution_yield_ttm"]["semantics"] == (
        "trailing_365d_cash_distribution_per_unit_divided_by_current_price"
    )
    assert profile["sources"][1]["provider_id"] == "sina_etf_distribution"


def test_us_equity_uses_same_profile_contract_with_yahoo_currency() -> None:
    def fake_yahoo(symbol: str, modules: list[str]):
        assert symbol == "AAPL.US"
        assert "summaryDetail" in modules
        return {
            "price": {
                "longName": "Apple Inc.",
                "currency": "USD",
                "exchangeName": "NasdaqGS",
                "regularMarketPrice": {"raw": 333.74},
                "regularMarketTime": {"raw": QUOTE_TIME},
                "regularMarketChangePercent": {"raw": 0.00144026},
                "marketCap": {"raw": 4_901_757_779_968},
            },
            "summaryDetail": {
                "trailingPE": {"raw": 40.502426},
                "forwardPE": {"raw": 34.625034},
                "dividendYield": {"raw": 0.0032},
                "dividendRate": {"raw": 1.08},
            },
            "defaultKeyStatistics": {
                "priceToBook": {"raw": 45.969696},
                "sharesOutstanding": {"raw": 14_687_356_000},
                "floatShares": {"raw": 14_662_387_495},
            },
            "financialData": {"returnOnEquity": {"raw": 1.4147099}},
            "assetProfile": {
                "industry": "Consumer Electronics",
                "sector": "Technology",
                "country": "United States",
            },
        }

    provider = EastmoneyInstrumentProfileProvider(
        get_json_fn=_fake_get_json,
        get_text_fn=_fake_get_text,
        get_quote_summary_fn=fake_yahoo,
        now_provider=lambda: "2026-07-19T05:00:00+00:00",
    )
    profile = provider.fetch("AAPL.US")
    metrics = {item["key"]: item for item in profile["metrics"]}

    assert profile["identity"]["name"] == "Apple Inc."
    assert profile["identity"]["currency"] == "USD"
    assert metrics["total_market_cap"]["unit"] == "USD"
    assert metrics["pe_ttm"]["value"] == 40.502426
    assert metrics["dividend_yield_indicated"]["value"] == 0.0032
    assert metrics["indicated_dividend_rate"]["unit"] == "USD_per_share"


def test_profile_snapshots_are_durable_and_read_is_network_free(tmp_path: Path) -> None:
    knowledge = ResearchKnowledgeStore(
        path=tmp_path / "research_cache.sqlite3",
        object_dir=tmp_path / "objects",
    )
    service = InstrumentProfileService(knowledge, provider=_provider())

    saved = service.refresh("600519.SH")
    loaded = service.latest_snapshot("600519.SH")

    assert loaded is not None
    assert loaded["snapshot_id"] == saved["snapshot_id"]
    assert loaded["history_count"] == 1
    assert service.refresh("600519.SH")["history_count"] == 1


def test_report_subject_reads_and_explicitly_refreshes_shared_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("VIBE_TRADING_REPORT_LIBRARY_ENABLED", "1")
    knowledge = ResearchKnowledgeStore(
        path=tmp_path / "research_cache.sqlite3",
        object_dir=tmp_path / "objects",
    )
    library = ReportLibraryService(knowledge)
    library.register_report(ReportEnvelope(
        report_id="report_profile_api",
        family_id="report_profile_api",
        report_kind="deep_research",
        subject_type="symbol",
        subject_key="600519.SH",
        symbol="600519.SH",
        security_name="贵州茅台",
        status="published",
        report_quality_status="passed",
        coverage_status="complete",
        generated_at="2026-07-19T00:00:00+00:00",
        data_as_of="2026-07-18T00:00:00+00:00",
        source_type="deep_report",
        source_id="report_profile_api",
    ))
    profiles = InstrumentProfileService(knowledge, provider=_provider())
    profiles.refresh("600519.SH")

    class _UniverseService:
        def latest_snapshot(self, _symbol):
            return None

    app = FastAPI()
    register_report_library_routes(
        app,
        lambda: None,
        get_service=lambda: library,
        get_etf_universe_service=lambda: _UniverseService(),
        get_instrument_profile_service=lambda: profiles,
    )
    client = TestClient(app)

    subject = client.get("/report-library/subjects/600519.SH")
    assert subject.status_code == 200
    assert subject.json()["instrument_profile"]["identity"]["name"] == "贵州茅台"
    assert subject.json()["profile"]["equity"]["instrument"]["snapshot_id"]

    refreshed = client.post(
        "/report-library/subjects/600519.SH/instrument-profile/refresh"
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["history_count"] == 1
