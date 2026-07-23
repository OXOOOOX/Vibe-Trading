from __future__ import annotations

from pathlib import Path

import pytest

from src.research.knowledge import ResearchKnowledgeStore
from src.research.source_classification import (
    audit_source_classifications,
    classify_source_kind,
)


@pytest.mark.parametrize(
    ("domain", "document", "expected"),
    [
        (
            "etf_universe",
            {
                "source_class": "mainstream_media",
                "canonical_url": "https://oss-ch.csindex.com.cn/000688closeweight.xls",
                "title": "csi_official_close_weight",
            },
            "index_constituents",
        ),
        (
            "market",
            {
                "source_class": "mainstream_media",
                "canonical_url": "market-cache:588870.SH:1D:raw",
                "title": "mootdx、tencent",
            },
            "market_data",
        ),
        (
            "financial_statement",
            {
                "source_class": "mainstream_media",
                "canonical_url": "603738.SH/income/quarter/2025-09-30",
                "title": "eastmoney",
            },
            "structured_financial",
        ),
        (
            "consensus",
            {
                "source_class": "broker_research",
                "canonical_url": "https://basic.10jqka.com.cn/603738/worth.html",
                "title": "东北证券一致预期",
            },
            "consensus_data",
        ),
        (
            "announcement",
            {
                "source_class": "mainstream_media",
                "canonical_url": "https://data.eastmoney.com/notices/detail/603738/a.html",
                "title": "2026年半年度业绩预告",
            },
            "company_disclosure",
        ),
        (
            "announcement",
            {
                "source_class": "mainstream_media",
                "canonical_url": "https://www.nbd.com.cn/articles/2026/06/15/a.html",
                "title": "媒体报道公司风险提示公告",
            },
            "news",
        ),
        (
            "other",
            {
                "source_class": "mainstream_media",
                "canonical_url": "financial_rigor implied_terminal_earnings 2026-07-17",
                "title": "派生计算",
            },
            "derived_analysis",
        ),
        (
            "fund_share_scale",
            {
                "source_class": "company_disclosure",
                "canonical_url": "https://www.sse.com.cn/market/funddata/volumn/etfvolumn/",
                "title": "ETF 基金份额",
            },
            "fund_share_scale",
        ),
    ],
)
def test_classify_source_kind_uses_material_type(
    domain: str,
    document: dict,
    expected: str,
) -> None:
    assert classify_source_kind(document, domain) == expected


def test_unmarked_etf_sources_are_split_by_material_type() -> None:
    assert classify_source_kind(
        {
            "source_class": "company_disclosure",
            "canonical_url": "https://www.sse.com.cn/assortment/fund/etf/list/",
            "title": "上海证券交易所 ETF 产品列表",
        },
        "",
        current_kind="fund_share_scale",
    ) == "fund_product"
    assert classify_source_kind(
        {
            "source_class": "company_disclosure",
            "canonical_url": "https://www.sse.com.cn/assortment/fund/etf/price/",
            "title": "上海证券交易所 ETF 行情快照",
        },
        "",
        current_kind="fund_share_scale",
    ) == "market_data"


def test_audit_repairs_every_misclassified_observation(tmp_path: Path) -> None:
    store = ResearchKnowledgeStore(
        path=tmp_path / "research.sqlite3",
        object_dir=tmp_path / "objects",
    )
    document = store.store_document(
        url="https://oss-ch.csindex.com.cn/000688closeweight.xls",
        content="000688 official constituent weights",
        title="csi_official_close_weight",
        publisher="csi_official_close_weight",
        source_class="mainstream_media",
    )
    store.record_source_observation(
        document_ref=document.document_ref,
        subject_key="588870.SH",
        source_kind="news",
        provider_id="csi_official_close_weight",
        provider_record_id=document.document_ref,
        verification_status="live_retrieved",
        body_status="full_text",
        origin_type="report",
        origin_id="report-fixture",
        metadata={"domain": "etf_universe"},
    )

    before = audit_source_classifications(store)
    applied = audit_source_classifications(store, apply=True)
    after = audit_source_classifications(store)

    assert before["misclassified"] == 1
    assert applied["corrected"] == 1
    assert after["misclassified"] == 0
    assert store.list_subject_sources("588870.SH")["sources"][0][
        "source_kind"
    ] == "index_constituents"
