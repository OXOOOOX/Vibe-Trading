from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from src.reports.etf_product_profile import (
    ETFProductProfileService,
    ETFShareFlowProvider,
    OfficialETFProductProvider,
    _FetchedSource,
    _finalize_product_profile_state,
    _fund_name_matches_index,
    _index_name_aliases,
    decode_official_html,
)
from src.reports.etf_research import ETFResearchStore
from src.reports.etf_source_registry import ETFSourceRegistry, source_context
from src.reports.profile import get_report_profile
from src.reports.service import DeepReportService


def _field(value, source_id: str, *, unit: str | None = None, as_of: str = "2026-07-17"):
    return {
        "value": value,
        "status": "available" if value is not None else "missing",
        "unit": unit,
        "data_as_of": as_of,
        "source_ids": [source_id] if source_id else [],
        "semantics": "fixture",
        "note": None,
    }


def _raw_profile(manager: str = "汇添富基金管理股份有限公司") -> dict:
    fund_source = "etfsource_fund"
    index_source = "etfsource_index"
    share_source = "etfsource_share"
    return {
        "symbol": "588870.SH",
        "data_as_of": "2026-07-17",
        "retrieved_at": "2026-07-18T00:00:00+00:00",
        "identity": {
            "fund_full_name": _field("汇添富上证科创板50成份交易型开放式指数证券投资基金", fund_source),
            "manager": _field(manager, fund_source),
            "custodian": _field("中信证券股份有限公司", fund_source),
            "exchange": _field("上海证券交易所", fund_source),
            "contract_effective_date": _field("2025-01-20", fund_source),
            "listing_date": _field("2025-01-27", fund_source),
            "tracked_index_code": _field("000688.SH", fund_source),
            "tracked_index_name": _field("上证科创板50成份指数", fund_source),
        },
        "index_methodology": {
            "index_code": _field("000688.SH", index_source, as_of="2020-12"),
            "index_name": _field("上证科创板50成份指数", index_source, as_of="2020-12"),
            "version": _field("V1.1", index_source, as_of="2020-12"),
            "source_url": _field("https://example.test/methodology.pdf", index_source, as_of="2020-12"),
            "target_component_count": _field(50, index_source, unit="count", as_of="2020-12"),
            "single_constituent_weight_cap": _field(0.10, index_source, unit="ratio", as_of="2020-12"),
            "top_five_weight_cap": _field(0.40, index_source, unit="ratio", as_of="2020-12"),
            "review_frequency": _field("quarterly", index_source, as_of="2020-12"),
        },
        "product_metrics": {
            "management_fee_rate": _field(0.0015, fund_source, unit="ratio"),
            "custody_fee_rate": _field(0.0005, fund_source, unit="ratio"),
            "unit_nav": _field(1.739, fund_source, unit="CNY_per_fund_unit"),
            "fund_units": _field(464_552_000, share_source, unit="fund_units"),
            "published_net_assets": _field(710_743_392.87, fund_source, unit="CNY", as_of="2025-12-31"),
            "exchange_market_value": _field(808_000_000, share_source, unit="CNY"),
            "iopv": _field(None, fund_source, unit="CNY_per_fund_unit"),
            "premium_discount_rate": _field(None, fund_source, unit="ratio"),
        },
        "sources": [
            {
                "source_id": fund_source, "kind": "fund_product", "title": "基金年度报告",
                "publisher": "汇添富基金管理股份有限公司", "url": "https://example.test/fund.pdf",
                "content_hash": "a" * 64, "content": "基金产品资料", "retrieved_at": "2026-07-18T00:00:00+00:00",
                "verification_status": "official_primary", "body_status": "full_text",
            },
            {
                "source_id": index_source, "kind": "index_methodology", "title": "指数编制方案 V1.1",
                "publisher": "中证指数有限公司", "url": "https://example.test/methodology.pdf",
                "content_hash": "b" * 64, "content": "指数规则", "retrieved_at": "2026-07-18T00:00:00+00:00",
                "verification_status": "official_primary", "body_status": "full_text",
            },
            {
                "source_id": share_source, "kind": "fund_share_scale", "title": "交易所份额",
                "publisher": "上海证券交易所", "url": "https://example.test/shares?fields=f43%2Cf50",
                "content_hash": "c" * 64, "content": "464552000", "retrieved_at": "2026-07-18T00:00:00+00:00",
                "verification_status": "official_primary", "body_status": "full_text",
            },
        ],
        "hard_gate_status": "passed",
        "quality_status": "passed_with_gaps",
        "missing_hard_fields": [],
        "missing_optional_fields": ["iopv", "premium_discount_rate"],
        "conflicts": [],
        "source_errors": [],
        "source_acquisition": {
            "registry_version": "etf-source-rules-v1",
            "rules": [{
                "rule_id": "99fund.product_detail.v1",
                "label": "汇添富产品资料与费率",
                "phase": "product_profile",
                "slot": "product",
                "source_kind": "fund_product",
                "publisher": "汇添富基金管理股份有限公司",
                "verification_status": "official_primary",
                "priority": 95,
                "parser_id": "manager_fee_page_v1",
                "response_type": "html",
                "provides": ["management_fee_rate", "custody_fee_rate"],
                "required_for_publish": False,
                "freshness_days": 30,
                "refresh_trigger": "when_stale_or_explicit_refresh",
                "failure_policy": "warn_and_use_cache",
                "status": "completed",
                "source_id": fund_source,
                "url": "https://example.test/fund.pdf",
            }],
        },
    }


class _Provider:
    def __init__(self) -> None:
        self.payload = _raw_profile()

    def fetch(self, *args, **kwargs):
        return deepcopy(self.payload)


class _Shares:
    def fetch(self, *args, **kwargs):
        return {
            "symbol": "588870.SH",
            "tracked_index_code": "000688.SH",
            "tracked_index_name": "上证科创板50成份指数",
            "data_as_of": "2026-07-17",
            "members": [{
                "symbol": "588870.SH", "name": "科创50ETF汇添富",
                "mapping_status": "official_index_code", "data_as_of": "2026-07-17",
                "current_units": 464_552_000, "delta_1d": 42_000_000,
                "delta_5d": 132_000_000, "delta_20d": 147_000_000,
                "estimated_net_flow_1d": 73_122_000,
                "estimated_net_flow_semantics": "share_delta_times_current_market_price_proxy",
                "source_ids": ["etfsource_share"],
                "history": [
                    {"data_as_of": "2026-07-17", "fund_units": 464_552_000, "source_ids": ["etfsource_share"]},
                    {"data_as_of": "2026-07-16", "fund_units": 422_552_000, "source_ids": ["etfsource_share"]},
                ],
            }],
            "member_count": 1, "official_index_mapping_count": 1,
            "name_mapped_count": 0, "estimated_net_flow_1d": 73_122_000,
            "inflow_member_ratio_1d": 1.0, "flow_coverage_ratio": 1.0,
            "unit_change_coverage_ratio": 1.0, "warnings": [], "errors": [],
            "sources": [{
                "source_id": "etfsource_share", "kind": "fund_share_scale",
                "title": "交易所份额", "publisher": "上海证券交易所",
                "url": "https://example.test/shares", "content_hash": "c" * 64,
                "content": "464552000", "retrieved_at": "2026-07-18T00:00:00+00:00",
                "verification_status": "official_primary", "body_status": "full_text",
            }],
            "source_acquisition": {
                "registry_version": "etf-source-rules-v1",
                "rules": [{
                    "rule_id": "sse.etf_share_scale_history.v1",
                    "label": "上交所 ETF 日终份额",
                    "phase": "share_flow",
                    "slot": "sse_scale",
                    "source_kind": "fund_share_scale",
                    "publisher": "上海证券交易所",
                    "verification_status": "official_primary",
                    "priority": 100,
                    "parser_id": "sse_etf_scale_v1",
                    "response_type": "json",
                    "provides": ["fund_units", "fund_units_change"],
                    "required_for_publish": False,
                    "freshness_days": 1,
                    "refresh_trigger": "explicit_refresh_and_report_generation",
                    "failure_policy": "warn_and_use_cache",
                    "status": "completed",
                    "source_id": "etfsource_share",
                    "url": "https://example.test/shares",
                }],
            },
        }


class _Ingestion:
    def ingest(self, source, *, origin_type, origin_id):
        return {
            "document_ref": f"doc:{source.source_kind}:{source.subject_key}",
            "verification_status": source.verification_status,
        }


def _flow_source(source_id: str, kind: str = "fund_share_scale") -> dict:
    return {
        "source_id": source_id,
        "kind": kind,
        "title": source_id,
        "publisher": "测试交易所",
        "url": f"https://example.test/{source_id}",
        "content_hash": source_id.ljust(64, "0")[:64],
        "content": source_id,
        "retrieved_at": "2026-07-21T16:00:00+08:00",
        "verification_status": "official_primary",
        "body_status": "full_text",
    }


class _MultiIndexShareProvider(ETFShareFlowProvider):
    def __init__(self, *, catalog: list[dict], szse_rows: list[dict], scales: dict[str, list[dict]], prices: dict[str, float]):
        super().__init__()
        self.catalog = catalog
        self.szse_rows = szse_rows
        self.scales = scales
        self.prices = prices

    def _sse_catalog(self, context):
        return deepcopy(self.catalog), _flow_source("sse-catalog", "fund_product")

    def _szse_current(self, context):
        return deepcopy(self.szse_rows), _flow_source("szse-current")

    def _sse_scale(self, day):
        day_text = day.isoformat()
        rows = deepcopy(self.scales.get(day_text, []))
        return day_text, rows, _flow_source(f"sse-scale-{day_text}")

    def _prices(self, symbols, context):
        selected = {symbol: price for symbol, price in self.prices.items() if symbol in symbols}
        return selected, _flow_source("sse-prices", "market_data") if selected else None


def _service(tmp_path, provider: _Provider | None = None) -> ETFProductProfileService:
    return ETFProductProfileService(
        store=ETFResearchStore(tmp_path / "research.sqlite3"),
        provider=provider or _Provider(),
        share_provider=_Shares(),
        ingestion=_Ingestion(),
    )


@pytest.mark.parametrize(
    ("index_code", "index_name", "fund_name"),
    [
        ("931787.CSI", "中证香港创新药指数", "港股创新药ETF"),
        ("000300.SH", "沪深300指数", "沪深300ETF"),
        ("000852.SH", "中证1000指数", "1000ETF"),
    ],
)
def test_cross_exchange_aliases_cover_supported_index_families(
    index_code: str,
    index_name: str,
    fund_name: str,
) -> None:
    aliases = _index_name_aliases(index_code, index_name)
    assert _fund_name_matches_index(fund_name, aliases)
    assert not _fund_name_matches_index("中证500ETF", aliases)


@pytest.mark.parametrize(
    ("symbol", "index_code", "index_name", "expected_symbols"),
    [
        ("513120.SH", "931787.CSI", "中证香港创新药指数", {"513120.SH", "513121.SH"}),
        ("510300.SH", "000300.SH", "沪深300指数", {"510300.SH", "510310.SH"}),
        ("560010.SH", "000852.SH", "中证1000指数", {"560010.SH", "512100.SH"}),
    ],
)
def test_same_index_sse_peer_groups_are_selected_by_index_code_for_multiple_etfs(
    symbol: str,
    index_code: str,
    index_name: str,
    expected_symbols: set[str],
) -> None:
    catalog = [
        {"fundCode": "513120", "fundAbbr": "港股创新药ETF", "INDEX_CODE": "931787"},
        {"fundCode": "513121", "fundAbbr": "同指数测试ETF", "INDEX_CODE": "931787"},
        {"fundCode": "510300", "fundAbbr": "沪深300ETF", "INDEX_CODE": "000300"},
        {"fundCode": "510310", "fundAbbr": "沪深300ETF易方达", "INDEX_CODE": "000300"},
        {"fundCode": "560010", "fundAbbr": "中证1000ETF", "INDEX_CODE": "000852"},
        {"fundCode": "512100", "fundAbbr": "中证1000ETF南方", "INDEX_CODE": "000852"},
        {"fundCode": "510500", "fundAbbr": "中证500ETF", "INDEX_CODE": "000905"},
    ]
    scales = {
        "2026-07-21": [
            {"SEC_CODE": row["fundCode"], "TOT_VOL": 120}
            for row in catalog
        ],
        "2026-07-20": [
            {"SEC_CODE": row["fundCode"], "TOT_VOL": 100}
            for row in catalog
        ],
    }
    provider = _MultiIndexShareProvider(
        catalog=catalog,
        szse_rows=[],
        scales=scales,
        prices={f"{row['fundCode']}.SH": 2.0 for row in catalog},
    )

    result = provider.fetch(
        symbol,
        tracked_index_code=index_code,
        tracked_index_name=index_name,
        as_of="2026-07-21",
    )

    assert {item["symbol"] for item in result["members"]} == expected_symbols
    assert result["official_index_mapping_count"] == len(expected_symbols)
    assert result["flow_coverage_ratio"] == 1.0


def test_513120_cross_exchange_peer_uses_explicit_nav_proxy_and_distinct_day_history() -> None:
    provider = _MultiIndexShareProvider(
        catalog=[
            {"fundCode": "513120", "fundAbbr": "港股创新药ETF", "INDEX_CODE": "931787"},
        ],
        szse_rows=[{
            "基金代码": "159999",
            "基金简称": "港股创新药ETF测试",
            "基金管理人": "测试基金",
            "当前规模(份)": 300_000,
            "净值": 1.5,
        }],
        scales={
            "2026-07-21": [{"SEC_CODE": "513120", "TOT_VOL": 120}],
            "2026-07-20": [{"SEC_CODE": "513120", "TOT_VOL": 100}],
        },
        prices={"513120.SH": 2.0},
    )
    previous = SimpleNamespace(payload={
        "members": [{
            "symbol": "159999.SZ",
            "data_as_of": "2026-07-20",
            "current_units": 250_000,
            "source_ids": ["szse-previous"],
        }],
        "_source_catalog": [_flow_source("szse-previous")],
    })

    result = provider.fetch(
        "513120.SH",
        tracked_index_code="931787.CSI",
        tracked_index_name="中证香港创新药指数",
        as_of="2026-07-21",
        previous_peer_group=previous,
    )

    members = {item["symbol"]: item for item in result["members"]}
    sz_peer = members["159999.SZ"]
    assert sz_peer["mapping_status"] == "name_alias_requires_cross_check"
    assert sz_peer["delta_1d"] == 50_000
    assert sz_peer["estimation_price_type"] == "exchange_published_nav_proxy"
    assert sz_peer["estimated_net_flow_1d"] == 75_000
    assert result["market_price_flow_count"] == 1
    assert result["nav_proxy_flow_count"] == 1
    assert result["flow_coverage_ratio"] == 1.0
    assert result["warnings"]
    assert "szse-previous" in {item["source_id"] for item in result["sources"]}
    facts, evidence = ETFProductProfileService.to_report_records({
        "symbol": "513120.SH",
        "data_as_of": "2026-07-21",
        "retrieved_at": "2026-07-21T16:00:00+08:00",
        "identity": {},
        "index_methodology": {},
        "product_metrics": {},
        "share_history": {},
        "peer_group": result,
        "sources": result["sources"],
    })
    evidence_ids = {item["evidence_id"] for item in evidence}
    nav_fact = next(
        item for item in facts
        if item["symbol"] == "159999.SZ" and item["metric"] == "peer_member_nav_proxy"
    )
    flow_fact = next(
        item for item in facts
        if item["symbol"] == "159999.SZ"
        and item["metric"] == "peer_member_estimated_net_flow_1d"
    )
    assert nav_fact["evidence_ids"]
    assert set(nav_fact["evidence_ids"]).issubset(evidence_ids)
    assert flow_fact["formula"] == "share_delta_1d * estimation_price"
    assert nav_fact["fact_id"] in flow_fact["input_fact_ids"]


def test_same_day_szse_snapshot_is_not_mislabeled_as_one_day_flow() -> None:
    provider = _MultiIndexShareProvider(
        catalog=[{"fundCode": "513120", "fundAbbr": "港股创新药ETF", "INDEX_CODE": "931787"}],
        szse_rows=[{"基金代码": "159999", "基金简称": "港股创新药ETF测试", "当前规模(份)": 300_000, "净值": 1.5}],
        scales={},
        prices={},
    )
    previous = SimpleNamespace(payload={
        "members": [{
            "symbol": "159999.SZ",
            "data_as_of": "2026-07-21",
            "current_units": 250_000,
            "source_ids": ["szse-same-day"],
        }],
        "_source_catalog": [_flow_source("szse-same-day")],
    })

    result = provider.fetch(
        "513120.SH",
        tracked_index_code="931787.CSI",
        tracked_index_name="中证香港创新药指数",
        as_of="2026-07-21",
        previous_peer_group=previous,
    )

    sz_peer = next(item for item in result["members"] if item["symbol"] == "159999.SZ")
    assert sz_peer["history"] == [{
        "data_as_of": "2026-07-21",
        "fund_units": 300_000,
        "source_ids": ["szse-current"],
    }]
    assert sz_peer["delta_1d"] is None
    assert sz_peer["estimated_net_flow_1d"] is None


def test_profile_snapshot_is_idempotent_and_content_change_creates_new_id(tmp_path) -> None:
    provider = _Provider()
    service = _service(tmp_path, provider)
    first = service.refresh("588870.SH")
    second = service.refresh("588870.SH")
    assert first["profile_snapshot_id"] == second["profile_snapshot_id"]
    assert first["snapshot_ids"] == second["snapshot_ids"]

    provider.payload["identity"]["manager"]["value"] = "测试基金管理有限公司"
    changed = service.refresh("588870.SH")
    assert changed["profile_snapshot_id"] != first["profile_snapshot_id"]
    assert changed["snapshot_ids"]["identity"] != first["snapshot_ids"]["identity"]


def test_source_registry_selects_and_renders_reusable_rules() -> None:
    registry = ETFSourceRegistry()
    context = {
        **source_context(
            "588870.SH",
            manager="汇添富基金管理股份有限公司",
            index_code="000688.SH",
        ),
        "data_as_of": "2026-07-17",
    }

    product_plan = registry.plan(phase="product_profile", context=context)
    product_rules = {item["rule_id"]: item for item in product_plan["rules"]}
    assert product_plan["registry_version"] == "etf-source-rules-v1"
    assert "sse.etf_product_catalog.v1" in product_rules
    assert "subClass" not in registry.get(
        "sse.etf_product_catalog.v1"
    ).resolved_params(context)
    assert product_rules["99fund.product_detail.v1"]["url"].endswith("fundId=588870")
    assert product_rules["csindex.methodology_pdf.v1"]["url"].endswith(
        "/000688_Index_Methodology_cn.pdf"
    )
    assert product_rules["99fund.588870.annual_report_2025.v1"]["required_for_publish"] is True

    share_plan = registry.plan(phase="share_flow", context=context)
    share_rules = {item["rule_id"]: item for item in share_plan["rules"]}
    assert {
        "sse.etf_peer_catalog.v1",
        "sse.etf_share_scale_history.v1",
        "szse.etf_share_scale_current.v1",
        "sse.etf_market_price.v1",
    }.issubset(share_rules)
    assert share_rules["sse.etf_peer_catalog.v1"]["source_kind"] == "fund_product"
    assert "subClass" not in registry.get(
        "sse.etf_peer_catalog.v1"
    ).resolved_params(context)
    assert share_rules["sse.etf_share_scale_history.v1"]["freshness_days"] == 1
    assert share_rules["sse.etf_share_scale_history.v1"]["source_kind"] == "fund_share_scale"
    assert share_rules["sse.etf_market_price.v1"]["source_kind"] == "market_data"


def test_513120_official_identity_and_methodology_pass_hard_gate(monkeypatch) -> None:
    provider = OfficialETFProductProvider()
    catalog_source = _FetchedSource(
        source_id="etfsource_catalog", kind="fund_product", title="上交所 ETF 产品目录",
        publisher="上海证券交易所", url="https://www.sse.com.cn/assortment/fund/etf/list/",
        content="513120", content_hash="a" * 64,
        retrieved_at="2026-07-22T00:00:00+00:00",
    )
    methodology_source = _FetchedSource(
        source_id="etfsource_methodology", kind="index_methodology", title="931787 指数编制方案",
        publisher="中证指数有限公司",
        url=(
            "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/"
            "detail/files/zh_CN/931787_Index_Methodology_cn.pdf"
        ),
        content=(
            "2025 年 9 月 | 版本号 V2.1\n"
            "指数名称：中证香港创新药指数\n指数代码：931787（港元）\n"
            "选取排名靠前的 50 只证券作为指数样本。\n"
            "单个样本权重不超过 8%，其余单个样本权重不超过 10%。\n"
            "指数样本每半年调整一次，每次调整的样本比例一般不超过 20%。"
        ),
        content_hash="b" * 64, retrieved_at="2026-07-22T00:00:00+00:00",
    )
    sse_row = {
        "fundCode": "513120", "subClass": "33", "listingDate": "20220712",
        "companyName": "广发基金管理有限公司", "INDEX_NAME": "中证香港创新药指数",
        "fundAbbr": "HK创新药", "INDEX_CODE": "931787", "secNameFull": "港股创新药ETF广发",
        "TRUSTEE_NAME": "交通银行股份有限公司",
    }
    monkeypatch.setattr(
        provider, "_sse_list_source",
        lambda *, rule, context: (catalog_source, [sse_row]),
    )
    monkeypatch.setattr(
        provider, "_fetch_rule",
        lambda rule, context: methodology_source,
    )
    universe = SimpleNamespace(payload={"mapping": {
        "tracked_index_code": "931787.CSI",
        "tracked_index_name": "中证香港创新药指数",
    }})

    profile = provider.fetch("513120.SH", universe_snapshot=universe)

    assert profile["hard_gate_status"] == "passed"
    assert profile["missing_hard_fields"] == []
    assert profile["identity"]["fund_short_name"]["value"] == "港股创新药ETF广发"
    assert profile["identity"]["manager"]["value"] == "广发基金管理有限公司"
    assert profile["identity"]["tracked_index_code"]["value"] == "931787.CSI"
    identity_source_ids = profile["identity"]["manager"]["source_ids"]
    assert len(identity_source_ids) == 1
    for key in ("exchange", "tracked_index_code", "tracked_index_name"):
        assert profile["identity"][key]["source_ids"] == identity_source_ids
    assert any(
        source["source_id"] == identity_source_ids[0]
        and source["publisher"] == "上海证券交易所"
        for source in profile["sources"]
    )
    methodology = profile["index_methodology"]
    assert methodology["version"]["value"] == "V2.1"
    assert methodology["published_at"]["value"] == "2025-09"
    assert methodology["index_code"]["value"] == "931787.CSI"
    assert methodology["target_component_count"]["value"] == 50
    assert methodology["review_frequency"]["value"] == "semiannual"
    assert methodology["source_url"]["source_ids"] == ["etfsource_methodology"]


def test_513120_sse_index_code_is_qualified_as_csindex_without_mapping(monkeypatch) -> None:
    provider = OfficialETFProductProvider()
    catalog_source = _FetchedSource(
        source_id="etfsource_catalog", kind="fund_product", title="SSE ETF catalog",
        publisher="SSE", url="https://www.sse.com.cn/assortment/fund/etf/list/",
        content="513120", content_hash="a" * 64,
        retrieved_at="2026-07-22T00:00:00+00:00",
    )
    methodology_source = _FetchedSource(
        source_id="etfsource_methodology", kind="index_methodology",
        title="931787 methodology", publisher="CSI",
        url=(
            "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/"
            "detail/files/zh_CN/931787_Index_Methodology_cn.pdf"
        ),
        content=(
            "2025 年 9 月 | 版本号 V2.1\n"
            "指数名称：中证香港创新药指数\n指数代码：931787（港元）\n"
        ),
        content_hash="b" * 64, retrieved_at="2026-07-22T00:00:00+00:00",
    )
    sse_row = {
        "fundCode": "513120", "subClass": "33",
        "companyName": "广发基金管理有限公司",
        "INDEX_NAME": "中证香港创新药指数", "INDEX_CODE": "931787",
        "secNameFull": "港股创新药ETF广发",
    }
    monkeypatch.setattr(
        provider, "_sse_list_source",
        lambda *, rule, context: (catalog_source, [sse_row]),
    )
    monkeypatch.setattr(provider, "_fetch_rule", lambda rule, context: methodology_source)

    profile = provider.fetch("513120.SH")

    assert profile["identity"]["tracked_index_code"]["value"] == "931787.CSI"


def test_159516_szse_identity_and_methodology_pass_hard_gate(monkeypatch) -> None:
    provider = OfficialETFProductProvider()
    catalog_source = _FetchedSource(
        source_id="etfsource_szse_catalog", kind="fund_product",
        title="深圳证券交易所 ETF 产品列表 159516", publisher="深圳证券交易所",
        url="https://www.szse.cn/www/market/product/list/etfList/",
        content="159516", content_hash="c" * 64,
        retrieved_at="2026-07-22T00:00:00+00:00", published_at="2026-07-22",
    )
    methodology_source = _FetchedSource(
        source_id="etfsource_931743_methodology", kind="index_methodology",
        title="931743 指数编制方案", publisher="中证指数有限公司",
        url=(
            "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/"
            "detail/files/zh_CN/931743_Index_Methodology_cn.pdf"
        ),
        content=(
            "2023 年 12 月 | 版本号 V1.1\n"
            "指数名称：中证半导体材料设备主题指数\n"
            "指数代码：931743\n"
            "选取排名靠前的 40 只证券作为指数样本。\n"
            "单个样本权重不超过 15%，其余单个样本权重不超过 3%。\n"
            "指数样本每半年调整一次。"
        ),
        content_hash="d" * 64, retrieved_at="2026-07-22T00:00:00+00:00",
    )
    szse_row = {
        "sys_key": "<a><u>159516</u></a>",
        "kzjcurl": "<a><u>半导体设备ETF国泰</u></a>",
        "nhzs": "931743 ",
        "glrmc": "国泰基金管理有限公司",
        "_catalog_data_as_of": "2026-07-22",
    }
    monkeypatch.setattr(
        provider, "_szse_list_source",
        lambda *, rule, context: (catalog_source, [szse_row]),
    )
    monkeypatch.setattr(provider, "_fetch_rule", lambda rule, context: methodology_source)

    profile = provider.fetch("159516.SZ")

    assert profile["hard_gate_status"] == "passed"
    assert profile["missing_hard_fields"] == []
    assert profile["identity"]["fund_short_name"]["value"] == "半导体设备ETF国泰"
    assert profile["identity"]["manager"]["value"] == "国泰基金管理有限公司"
    assert profile["identity"]["exchange"]["value"] == "深圳证券交易所"
    assert profile["identity"]["tracked_index_code"]["value"] == "931743.CSI"
    assert profile["identity"]["tracked_index_name"]["value"] == "中证半导体材料设备主题指数"
    assert profile["identity"]["manager"]["source_ids"]
    assert profile["identity"]["tracked_index_name"]["source_ids"] == [
        "etfsource_931743_methodology"
    ]
    assert profile["index_methodology"]["version"]["value"] == "V1.1"
    assert profile["index_methodology"]["source_url"]["source_ids"] == [
        "etfsource_931743_methodology"
    ]


def test_source_policy_is_persisted_with_profile_snapshot(tmp_path) -> None:
    service = _service(tmp_path)
    profile = service.refresh("588870.SH")
    rules = {item["rule_id"] for item in profile["source_policy"]["rules"]}
    assert "99fund.product_detail.v1" in rules
    assert "sse.etf_share_scale_history.v1" in rules

    cached = service.latest_profile("588870.SH")
    assert cached is not None
    assert cached["profile_snapshot_id"] == profile["profile_snapshot_id"]
    assert cached["source_policy"] == profile["source_policy"]


def test_failed_static_rule_reuses_last_traceable_snapshot(tmp_path) -> None:
    provider = _Provider()
    service = _service(tmp_path, provider)
    original = service.refresh("588870.SH", include_share_flows=False)

    provider.payload["index_methodology"]["version"]["source_ids"] = []
    provider.payload["index_methodology"]["source_url"]["source_ids"] = []
    provider.payload["source_errors"] = [{
        "source": "csindex.methodology_pdf.v1", "error": "timeout",
    }]
    provider.payload["source_acquisition"]["rules"].append({
        "rule_id": "csindex.methodology_pdf.v1",
        "label": "中证指数编制方案",
        "phase": "product_profile",
        "slot": "methodology",
        "source_kind": "index_methodology",
        "publisher": "中证指数有限公司",
        "verification_status": "official_primary",
        "priority": 100,
        "parser_id": "csindex_methodology_pdf_v1",
        "response_type": "pdf",
        "provides": ["index_methodology"],
        "required_for_publish": True,
        "freshness_days": 180,
        "refresh_trigger": "when_stale_or_explicit_refresh",
        "failure_policy": "block_if_no_cache",
        "status": "failed",
        "source_id": None,
        "url": "https://example.test/methodology.pdf",
        "error": "timeout",
    })

    refreshed = service.refresh("588870.SH", include_share_flows=False)

    assert refreshed["hard_gate_status"] == "passed"
    assert refreshed["quality_status"] == "passed_with_gaps"
    assert refreshed["index_methodology"]["version"]["value"] == "V1.1"
    assert refreshed["index_methodology"]["version"]["source_ids"]
    assert refreshed["cache_reused_sections"] == [{
        "section": "index_methodology",
        "snapshot_id": original["snapshot_ids"]["index_methodology"],
    }]
    methodology_rule = next(
        item for item in refreshed["source_policy"]["rules"]
        if item["rule_id"] == "csindex.methodology_pdf.v1"
    )
    assert methodology_rule["status"] == "completed_with_gaps"
    assert methodology_rule["cache_reused"] is True


def test_official_exchange_end_of_day_units_override_lower_priority_quote_units(tmp_path) -> None:
    provider = _Provider()
    provider.payload["product_metrics"]["fund_units"] = _field(
        422_552_000,
        "etfsource_share",
        unit="fund_units",
    )

    profile = _service(tmp_path, provider).refresh("588870.SH")

    units = profile["product_metrics"]["fund_units"]
    assert units["value"] == 464_552_000
    assert units["semantics"] == "official_exchange_end_of_day_fund_units"
    assert any(
        item.get("field") == "fund_units"
        and item.get("resolution") == "official_exchange_end_of_day_preferred"
        for item in profile["conflicts"]
    )
    cached = _service(tmp_path, provider).latest_profile("588870.SH")
    assert cached is not None
    assert cached["profile_snapshot_id"] == profile["profile_snapshot_id"]
    assert cached["conflicts"] == profile["conflicts"]
    assert cached["missing_optional_fields"] == profile["missing_optional_fields"]


def test_profile_facts_bind_every_available_product_value_to_evidence(tmp_path) -> None:
    service = _service(tmp_path)
    profile = service.refresh("588870.SH")
    facts, evidence = service.to_report_records(profile)
    evidence_ids = {item["evidence_id"] for item in evidence}
    metrics = {item["metric"]: item for item in facts}
    for key in (
        "manager", "custodian", "tracked_index_code", "version",
        "management_fee_rate", "unit_nav", "fund_units",
        "etf_fund_units_change_1d", "peer_group_estimated_net_flow_1d",
    ):
        assert key in metrics
        assert metrics[key]["evidence_ids"]
        assert set(metrics[key]["evidence_ids"]).issubset(evidence_ids)
    fact_ids = {item["fact_id"] for item in facts}
    for key in ("etf_estimated_net_flow_1d", "peer_group_estimated_net_flow_1d"):
        assert metrics[key]["formula"]
        assert metrics[key]["input_fact_ids"]
        assert set(metrics[key]["input_fact_ids"]).issubset(fact_ids)


def test_gb18030_and_product_page_extractors() -> None:
    decoded, encoding = decode_official_html("管理费 0.15% 托管费 0.05%".encode("gb18030"), "gbk")
    assert "管理费" in decoded
    assert encoding == "gbk"
    assert OfficialETFProductProvider._fee_values(decoded) == (0.0015, 0.0005)
    pcf = "2026-07-17日 信息内容 基金份额净值(单位：元) 1.739 是否需要公布IOPV 是"
    assert OfficialETFProductProvider._pcf_values(pcf)["unit_nav"] == 1.739


def test_hard_field_without_official_source_is_not_publishable(tmp_path) -> None:
    provider = _Provider()
    provider.payload["identity"]["manager"] = _field("汇添富基金管理股份有限公司", "")
    provider.payload["hard_gate_status"] = "failed_validation"
    provider.payload["missing_hard_fields"] = ["manager"]
    provider.payload["quality_status"] = "failed_validation"
    profile = _service(tmp_path, provider).refresh("588870.SH", include_share_flows=False)
    assert profile["hard_gate_status"] == "failed_validation"
    assert profile["quality_status"] == "failed_validation"
    assert profile["missing_hard_fields"] == ["manager"]


def test_compiler_owns_index_and_product_table_with_fact_evidence_lineage(tmp_path) -> None:
    profiles = _service(tmp_path)
    profile = profiles.refresh("588870.SH")
    facts, evidence = profiles.to_report_records(profile)
    reports = DeepReportService(tmp_path / "reports")
    record = reports.begin(
        session_id="session-product-compiler",
        attempt_id="attempt-product-compiler",
        request_content="研究 588870.SH",
        profile="etf_deep_research",
    )
    reports.attach_etf_analysis(record.report_id, {
        "profile": "etf_deep_research", "symbol": "588870.SH",
        "security_name": "科创50ETF汇添富", "data_as_of": "2026-07-17T15:00:00+08:00",
        "snapshot": {
            "symbol": "588870.SH", "security_name": "科创50ETF汇添富",
            "data_as_of": "2026-07-17T15:00:00+08:00",
            "snapshot_ids": {
                **profile["snapshot_ids"],
                "universe": "etfsnap_universe_fixture00000",
                "market": "etfsnap_market_fixture0000000",
            },
            "coverage_ratio": 1.0, "price_verified": True,
            "subject_profile": profile,
        },
        "facts": facts, "evidence": evidence,
        "quality_status": "passed_with_gaps",
        "module_statuses": {
            "identity": {"status": "passed", "coverage": 1.0},
            "product_profile": {"status": "warning", "coverage": 0.75, "reason": "optional_product_fields_missing"},
            "universe": {"status": "passed", "coverage": 1.0},
            "market_data": {"status": "passed", "coverage": 1.0},
            "peer_flow": {"status": "passed", "coverage": 1.0},
        },
    })
    for section_id, _heading in get_report_profile("etf_deep_research")["required_sections"]:
        reports.submit_section(
            record.report_id,
            section_id=section_id,
            body_markdown="本节解释严格限定于已经登记的产品事实与来源。",
        )

    evaluation = reports.evaluate_workspace(record.report_id)
    inspected = reports.inspect_workspace(record.report_id)
    assert evaluation["validation"]["quality_status"] == "passed_with_gaps"
    assert evaluation["audit"]["verdict"] == "PASS"
    assert "汇添富基金管理股份有限公司" in evaluation["content"]
    assert "中信证券股份有限公司" in evaluation["content"]
    assert "V1.1" in evaluation["content"]
    assert "管理费率" in evaluation["content"]
    assert "份额变化与同指数交易型开放式指数基金组" in evaluation["content"]
    assert "当前证据不足" not in evaluation["content"]
    assert "464552000fund_units" not in evaluation["content"]
    assert "1.739CNY_per_fund_unit" not in evaluation["content"]
    assert "5E+1个" not in evaluation["content"]
    assert inspected["subject_profile"]["source_policy"]["registry_version"] == (
        "etf-source-rules-v1"
    )
    assert any(
        item["rule_id"] == "sse.etf_share_scale_history.v1"
        for item in inspected["subject_profile"]["source_policy"]["rules"]
    )


def test_product_profile_finalization_recomputes_optional_fields_after_late_share_data() -> None:
    source_id = "etfsource_official"
    raw = {
        "identity": {
            "manager": _field("基金公司", source_id),
            "exchange": _field("SZSE", source_id),
            "tracked_index_code": _field("931743.CSI", source_id),
            "tracked_index_name": _field("中证半导体材料设备主题指数", source_id),
        },
        "index_methodology": {
            "version": _field("V1.0", source_id),
            "source_url": _field("https://example.com/method.pdf", source_id),
        },
        "product_metrics": {
            key: _field(None, "")
            for key in (
                "management_fee_rate", "custody_fee_rate", "unit_nav",
                "fund_units", "published_net_assets", "exchange_market_value",
                "iopv", "premium_discount_rate",
            )
        },
        "source_errors": [],
    }
    _finalize_product_profile_state(raw)
    assert "fund_units" in raw["missing_optional_fields"]

    raw["product_metrics"]["fund_units"] = _field(
        518_552_000, source_id, unit="fund_units"
    )
    _finalize_product_profile_state(raw)

    assert "fund_units" not in raw["missing_optional_fields"]
    assert raw["coverage_ratio"] == pytest.approx(0.125)
    assert raw["hard_gate_status"] == "passed"


def test_product_derived_facts_preserve_replayable_lineage() -> None:
    source_id = "etfsource_official"
    profile = {
        "symbol": "159516.SZ",
        "data_as_of": "2026-07-23",
        "sources": [{
            "source_id": source_id,
            "kind": "fund_share_scale",
            "publisher": "深圳证券交易所",
            "url": "https://example.com/share",
            "retrieved_at": "2026-07-23T08:00:00+00:00",
            "content_hash": "abc",
            "verification_status": "official_primary",
        }],
        "identity": {},
        "index_methodology": {},
        "product_metrics": {
            "fund_units": _field(100.0, source_id, unit="fund_units", as_of="2026-07-23"),
            "exchange_market_value": {
                **_field(250.0, source_id, unit="CNY", as_of="2026-07-23"),
                "semantics": "market_price_times_official_exchange_end_of_day_fund_units",
                "formula": "fund_units * current_price",
                "input_metrics": ["fund_units", "current_price"],
                "calculation_version": "etf-product-calc-v1",
                "source_kind": "derived",
            },
        },
    }
    price_fact = {
        "fact_id": "fact_current_price",
        "metric": "current_price",
        "evidence_ids": ["evidence_price"],
    }

    facts, _evidence = ETFProductProfileService.to_report_records(
        profile, base_facts=[price_fact]
    )
    by_metric = {item["metric"]: item for item in facts}
    derived = by_metric["exchange_market_value"]

    assert derived["formula"] == "fund_units * current_price"
    assert derived["input_fact_ids"] == [
        by_metric["fund_units"]["fact_id"],
        "fact_current_price",
    ]
    assert derived["metadata"]["lineage_status"] == "replayable"
    assert derived["metadata"]["source_kind"] == "derived"
