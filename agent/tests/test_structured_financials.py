from __future__ import annotations

from pathlib import Path

from src.research.knowledge import ResearchKnowledgeStore
from src.research.official_filings import (
    OfficialFilingProvider,
    OfficialFilingRecord,
    OfficialFilingService,
)
from src.research.source_ingestion import CollectedSource, SourceIngestionService
from src.research.structured_financials import OfficialFinancialExtractionService


def _store(tmp_path: Path) -> ResearchKnowledgeStore:
    return ResearchKnowledgeStore(
        path=tmp_path / "research.sqlite3",
        object_dir=tmp_path / "objects",
    )


def _official_document(store: ResearchKnowledgeStore, body: str) -> str:
    archived = SourceIngestionService(store).ingest(
        CollectedSource(
            subject_key="600036.SH",
            market="CN",
            source_kind="official_filing",
            provider_id="cninfo",
            provider_record_id="annual-2025",
            publisher="巨潮资讯",
            title="招商银行2025年度报告",
            source_locator="https://static.cninfo.com.cn/finalpage/annual-2025.pdf",
            content=body,
            published_at="2026-03-20",
            verification_status="official_primary",
            body_status="full_text",
            source_class="regulatory_filing",
        ),
        origin_type="official_refresh",
        origin_id="refresh-1",
    )
    return str(archived["document_ref"])


def test_official_financial_extraction_is_validated_persisted_and_replayed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    document_ref = _official_document(
        store,
        """招商银行股份有限公司 证券代码 600036
2025年度报告
主要会计数据和财务指标
（除特别说明外，单位人民币百万元）
营业收入 337,532 337,488 0.01%
营业利润 179,252 179,019 0.13%
归属于本行股东的净利润 150,181 148,391 1.21%
总资产 13,070,523 12,152,036 7.56%
总负债 11,789,624 10,918,561 7.98%
股东权益合计 1,280,899 1,233,475 3.84%
经营活动产生的现金流量净额 300,000 250,000 20.00%
基本每股收益（人民币元） 5.70 5.66 0.71%
""",
    )
    service = OfficialFinancialExtractionService(store=store)

    first = service.extract_document(document_ref, "600036.SH")
    second = service.extract_document(document_ref, "600036.SH")

    assert first["status"] == "validated"
    assert first["ocr_performed"] is False
    assert first["metrics_count"] >= 8
    assert second["cached"] is True
    assert second["extraction_id"] == first["extraction_id"]

    snapshots = store.list_financial_snapshots("600036.SH", validated_only=True)
    assert snapshots["count"] == 1
    snapshot = snapshots["snapshots"][0]
    assert snapshot["reporting_period_end"] == "2025-12-31"
    assert snapshot["ocr_performed"] is False
    metrics = {item["metric"]: item for item in snapshot["metrics"]}
    assert metrics["revenue"]["value"] == "337532000000"
    assert metrics["basic_eps"]["value"] == "5.7"
    source = store.list_subject_sources("600036.SH")["sources"][0]
    assert source["structured_status"] == "validated"
    assert source["structured_metrics_count"] >= 8
    assert source["ocr_performed"] is False

    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM structured_document_extractions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM financial_statement_snapshots").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM fact_records WHERE symbol='600036.SH'"
        ).fetchone()[0] >= 8


def test_annual_extraction_rejects_single_quarter_values_from_quarterly_indicator_table(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    document_ref = _official_document(
        store,
        """招商银行股份有限公司 证券代码 600036
2025年度报告
八、分季度主要财务指标
单位：人民币元
项目 第一季度 第二季度 第三季度 第四季度
营业收入 35,000,000,000 45,000,000,000 50,000,000,000 60,000,000,000
归属于上市公司股东的净利润 4,000,000,000 5,000,000,000 6,000,000,000 7,000,000,000
经营活动产生的现金流量净额 3,000,000,000 4,000,000,000 5,000,000,000 6,000,000,000

合并利润表
单位：人民币元
营业收入 190,000,000,000 175,000,000,000
营业利润 27,000,000,000 25,000,000,000
归属于上市公司股东的净利润 24,000,000,000 22,000,000,000

合并资产负债表
单位：人民币元
资产总计 350,000,000,000 320,000,000,000
负债合计 250,000,000,000 225,000,000,000
所有者权益合计 100,000,000,000 95,000,000,000

合并现金流量表
单位：人民币元
经营活动产生的现金流量净额 28,000,000,000 25,000,000,000
""",
    )

    result = OfficialFinancialExtractionService(store=store).extract_document(
        document_ref,
        "600036.SH",
    )

    assert result["status"] == "validated"
    snapshot = store.list_financial_snapshots("600036.SH", validated_only=True)["snapshots"][0]
    metrics = {item["metric"]: item["value"] for item in snapshot["metrics"]}
    assert metrics["revenue"] == "190000000000"
    assert metrics["net_profit_parent"] == "24000000000"
    assert metrics["cfo"] == "28000000000"


def test_review_required_result_is_cached_but_never_registered_as_fact(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    document_ref = _official_document(
        store,
        "招商银行 证券代码 600036 2025年度报告\n营业收入 100",
    )
    service = OfficialFinancialExtractionService(store=store)

    first = service.extract_document(document_ref, "600036.SH")
    second = service.extract_document(document_ref, "600036.SH")

    assert first["status"] == "needs_review"
    assert second["cached"] is True
    source = store.list_subject_sources("600036.SH")["sources"][0]
    assert source["structured_auto_repair_available"] is True
    assert "minimum_metric_count" in source["structured_failed_checks"]
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM fact_records").fetchone()[0] == 0


def test_financial_highlights_skip_ratio_aliases_and_statement_footnotes(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    document_ref = _official_document(
        store,
        """招商银行股份有限公司 证券代码 600036
2025年度报告
主要会计数据和财务指标（人民币百万元，特别注明除外）
营业收入 169,969 172,945 -1.72
营业利润 88,952 89,664 -0.79
归属于本行股东的净利润 74,930 74,743 0.25
归属于本行普通股股东的基本每股收益(1) 2.89 2.89 - 2.93
归属于本行股东的平均总资产收益率 1.21 1.32 下降0.11个百分点
归属于本行普通股股东的平均净资产收益率(1) 13.85 15.44 下降1.59个百分点
规模指标
总资产 12,657,151 12,152,036 4.16
总负债 11,360,291 10,918,561 4.05
归属于本行股东权益 1,289,233 1,226,014 5.16
拨备覆盖率(1) 410.93 411.98 下降1.05个百分点
""",
    )

    result = OfficialFinancialExtractionService(store=store).extract_document(
        document_ref,
        "600036.SH",
    )

    assert result["status"] == "validated"
    snapshot = store.list_financial_snapshots("600036.SH", validated_only=True)[
        "snapshots"
    ][0]
    metrics = {item["metric"]: item for item in snapshot["metrics"]}
    assert metrics["total_assets"]["value"] == "12657151000000"
    assert metrics["basic_eps"]["value"] == "2.89"
    assert metrics["roe_reported"]["value"] == "13.85"
    assert metrics["provision_coverage_ratio"]["value"] == "410.93"
    checks = snapshot["validation"]["checks"]
    assert checks["balance_sheet_scale_consistent"] is True
    assert checks["balance_sheet_reconciles"] is True


def test_numbered_note_markers_are_not_persisted_as_metric_values(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    document_ref = _official_document(
        store,
        """招商银行股份有限公司 证券代码 600036
2025年度报告
主要财务数据（除特别说明外，单位人民币百万元）
营业收入 337,532 337,488 0.01%
营业利润 179,252 179,019 0.13%
归属于本行股东的净利润 150,181 148,391 1.21%
归属于本行普通股股东的基本
每股收益（人民币元）
1 5.70 5.66 0.71%
归属于本行普通股股东的加权平均净资产收益率（%）1 13.44 14.49
总资产 13,070,523 12,152,036 7.56%
总负债 11,789,624 10,918,561 7.98%
归属于本行股东权益 1,272,875 1,226,014 3.82%
拨备覆盖率 4 391.79 411.98
注：
1. 基本每股收益和加权平均净资产收益率按监管规则计算。
4. 拨备覆盖率为贷款损失准备除以不良贷款余额。
""",
    )

    result = OfficialFinancialExtractionService(store=store).extract_document(
        document_ref,
        "600036.SH",
    )

    assert result["status"] == "validated"
    snapshot = store.list_financial_snapshots("600036.SH", validated_only=True)[
        "snapshots"
    ][0]
    metrics = {item["metric"]: item["value"] for item in snapshot["metrics"]}
    assert metrics["basic_eps"] == "5.7"
    assert metrics["roe_reported"] == "13.44"
    assert metrics["provision_coverage_ratio"] == "391.79"


def test_quarterly_layout_is_auto_repaired_and_supersedes_review_snapshot(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    document_ref = _official_document(
        store,
        """招商银行股份有限公司 证券代码 600036
2026年第一季度报告
主要会计数据及财务指标（人民币百万元，特别注明除外）
总资产 13,484,882 13,070,523 3.17
归属于本行股东权益 1,282,355 1,272,875 0.74
营业收入 86,940 83,751 3.81
归属于本行股东的净利润 37,852 37,286 1.52
归属于本行普通股股东的基本每股收益(人民币元)
(1) 1.49 1.48 0.68
年化后归属于本行普通股股东的加权平均净资产
收益率(%)
(1)
13.48 14.13 下降0.65个百分点
经营活动产生的现金流量净额(2) 125,849 95,026 32.44
截至报告期末，本集团资产总额134,848.82亿元；负债总额121,942.97亿元。
截至报告期末，本集团不良贷款余额698.58亿元；不良贷款率0.94%，与上年
末持平；拨备覆盖率387.76%，下降4.03个百分点。
本公司房地产业不良贷款率4.44%，下降0.20个百分点。
未经审计合并资产负债表
（除特别注明外，货币单位均以人民币百万元列示）
资产合计 13,484,882 13,070,523
负债合计 12,194,297 11,789,624
股东权益合计 1,290,585 1,280,899
""",
    )
    store.record_structured_extraction(
        document_ref=document_ref,
        subject_key="600036.SH",
        extractor_id="official_financial_statement",
        extractor_version="v6",
        extraction_method="native_text",
        status="needs_review",
        validation={"status": "needs_review"},
        error="balance_sheet_reconciles",
    )

    result = OfficialFinancialExtractionService(store=store).extract_document(
        document_ref,
        "600036.SH",
    )

    assert result["status"] == "validated"
    snapshot = store.list_financial_snapshots("600036.SH", validated_only=True)[
        "snapshots"
    ][0]
    metrics = {item["metric"]: item["value"] for item in snapshot["metrics"]}
    assert metrics["total_liabilities"] == "12194297000000"
    assert metrics["basic_eps"] == "1.49"
    assert metrics["roe_reported"] == "13.48"
    assert metrics["nonperforming_loan_ratio"] == "0.94"
    with store.connect() as conn:
        old = conn.execute(
            """SELECT status FROM structured_document_extractions
               WHERE document_ref=? AND extractor_version='v6'""",
            (document_ref,),
        ).fetchone()
    assert old["status"] == "superseded"


def test_wrapped_cn_quarterly_rows_do_not_use_change_reason_percentages(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    archived = SourceIngestionService(store).ingest(
        CollectedSource(
            subject_key="688256.SH",
            market="CN",
            source_kind="official_filing",
            provider_id="sse",
            provider_record_id="q1-2026",
            publisher="上海证券交易所",
            title="中科寒武纪科技股份有限公司2026年第一季度报告",
            source_locator="https://star.sse.com.cn/688256-q1-2026.pdf",
            content="""中科寒武纪科技股份有限公司 证券代码 688256
2026年第一季度报告
一、主要财务数据
单位：元 币种：人民币
项目 本报告期 上年同期 增减变动幅度(%)
营业收入 2,884,696,746.86 1,111,398,926.80 159.56
归属于上市公司股东的净
1,013,213,581.94 355,465,241.04 185.04
利润
经营活动产生的现金流量
833,967,832.10 -1,399,358,712.85 不适用
净额
基本每股收益（元/股） 2.40 0.85 182.35
稀释每股收益（元/股） 2.38 0.85 180.00
加权平均净资产收益率
8.20 6.32 增加 1.88 个百分点
（%）
总资产 15,400,896,356.83 13,437,714,065.91 14.61
归属于上市公司股东的所
12,871,311,672.92 11,836,173,972.81 8.75
有者权益
归属于上市公司股东的 主要系报告期内营业收入较上年同期大幅增长
185.04
净利润 增长，带动归属于上市公司股东的净利润增长。
基本每股收益（元/股） 182.35 主要系净利润大幅增长所致。
稀释每股收益（元/股） 180.00 大幅增长所致。
""",
            verification_status="official_primary",
            body_status="full_text",
            source_class="regulatory_filing",
        ),
        origin_type="official_refresh",
        origin_id="q1-refresh",
    )

    result = OfficialFinancialExtractionService(store=store).extract_document(
        str(archived["document_ref"]),
        "688256.SH",
    )

    assert result["status"] == "validated"
    snapshot = store.list_financial_snapshots("688256.SH", validated_only=True)[
        "snapshots"
    ][0]
    metrics = {item["metric"]: item["value"] for item in snapshot["metrics"]}
    assert metrics["net_profit_parent"] == "1013213581.94"
    assert metrics["cfo"] == "833967832.1"
    assert metrics["parent_equity"] == "12871311672.92"
    assert metrics["basic_eps"] == "2.4"
    assert metrics["diluted_eps"] == "2.38"
    assert metrics["roe_reported"] == "8.2"


def test_cn_annual_totals_beat_subtotals_and_prose_ratios(tmp_path: Path) -> None:
    store = _store(tmp_path)
    archived = SourceIngestionService(store).ingest(
        CollectedSource(
            subject_key="688256.SH",
            market="CN",
            source_kind="official_filing",
            provider_id="cninfo",
            provider_record_id="annual-2025",
            publisher="巨潮资讯",
            title="中科寒武纪科技股份有限公司2025年年度报告",
            source_locator="https://static.cninfo.com.cn/688256-annual-2025.pdf",
            content="""中科寒武纪科技股份有限公司 证券代码 688256
2025年年度报告
近三年，公司前五大客户的销售金额合计占营业收入比例分别为 92.36%、94.63%和 88.66%。
存货账面价值为 494,353.25 万元，占期末资产总额的比例为 36.79%。
归属于上市公司股东的净利润增加 21,696.34 万元，详见财务报告。
主要会计数据和财务指标 单位：元
营业收入 6,497,196,198.68 1,174,464,377.35 453.21
营业利润 2,061,390,034.78 -455,745,242.97
归属于上市公司股东的净利润 2,059,228,538.67 -452,338,791.01
经营活动产生的现金流量净额 -498,398,137.01 -1,617,960,236.90
基本每股收益（元/股） 4.93 -1.09
稀释每股收益（元/股） 4.88 -1.09
加权平均净资产收益率（%） 26.96 -8.18
合并资产负债表
流动资产合计 12,080,444,359.15 5,800,316,619.44
非流动资产合计 1,357,269,706.76 917,495,890.26
资产总计 13,437,714,065.91 6,717,812,509.70
流动负债合计 1,332,968,851.41 818,135,979.38
非流动负债合计 261,491,479.03 469,196,958.67
负债合计 1,594,460,330.44 1,287,332,938.05
归属于上市公司股东的净资产 11,836,173,972.81 5,422,658,659.68
母公司资产负债表
资产总计 14,743,163,813.07 8,458,676,697.86
负债合计 1,495,264,073.68 1,325,157,400.85
合并利润表
一、营业总收入 七、61 6,497,196,198.68 1,174,464,377.35
合并现金流量表
经营活动产生的现金流量净额 七、79(1) -498,398,137.01 -1,617,960,236.90
""",
            verification_status="official_primary",
            body_status="full_text",
            source_class="regulatory_filing",
        ),
        origin_type="official_refresh",
        origin_id="annual-refresh",
    )

    result = OfficialFinancialExtractionService(store=store).extract_document(
        str(archived["document_ref"]),
        "688256.SH",
    )

    assert result["status"] == "validated"
    snapshot = store.list_financial_snapshots("688256.SH", validated_only=True)[
        "snapshots"
    ][0]
    metrics = {item["metric"]: item["value"] for item in snapshot["metrics"]}
    assert metrics["revenue"] == "6497196198.68"
    assert metrics["cfo"] == "-498398137.01"
    assert metrics["net_profit_parent"] == "2059228538.67"
    assert metrics["total_assets"] == "13437714065.91"
    assert metrics["total_liabilities"] == "1594460330.44"
    assert snapshot["validation"]["checks"]["balance_sheet_reconciles"] is True


def test_annual_title_wins_over_interim_reference_in_body(tmp_path: Path) -> None:
    store = _store(tmp_path)
    archived = SourceIngestionService(store).ingest(
        CollectedSource(
            subject_key="000651.SZ",
            market="CN",
            source_kind="official_filing",
            provider_id="cninfo",
            provider_record_id="gree-annual-2025",
            publisher="巨潮资讯",
            title="珠海格力电器股份有限公司 2025年年度报告摘要",
            source_locator="https://static.cninfo.com.cn/gree-annual-2025.pdf",
            content="""珠海格力电器股份有限公司 证券代码 000651
2025年年度报告摘要
营业收入 170,447,058,533.57 189,163,654,064.64
归属于上市公司股东的净利润 29,003,103,411.66 32,184,570,372.28
总资产 391,371,999,819.49 368,031,704,522.86
归属于上市公司股东的净资产 145,929,297,804.02 137,416,898,946.39
经营活动产生的现金流量净额 46,383,114,754.02 29,369,250,570.66
基本每股收益（元/股） 5.20 5.83
上述指标与公司已披露半年度报告不存在重大差异。
""",
            verification_status="official_primary",
            body_status="full_text",
            source_class="regulatory_filing",
        ),
        origin_type="official_refresh",
        origin_id="gree-annual-refresh",
    )

    result = OfficialFinancialExtractionService(store=store).extract_document(
        str(archived["document_ref"]),
        "000651.SZ",
    )

    assert result["status"] == "validated"
    snapshot = store.list_financial_snapshots("000651.SZ", validated_only=True)[
        "snapshots"
    ][0]
    assert snapshot["filing_type"] == "annual"
    assert snapshot["reporting_period_end"] == "2025-12-31"


def test_parent_equity_row_is_not_reused_as_total_equity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    archived = SourceIngestionService(store).ingest(
        CollectedSource(
            subject_key="000651.SZ",
            market="CN",
            source_kind="official_filing",
            provider_id="cninfo",
            provider_record_id="gree-interim-2025",
            publisher="巨潮资讯",
            title="珠海格力电器股份有限公司 2025年半年度报告",
            source_locator="https://static.cninfo.com.cn/gree-interim-2025.pdf",
            content="""珠海格力电器股份有限公司 证券代码 000651
2025年半年度报告
单位：元
营业收入 97,619,383,061.89 100,286,553,308.24
归属于上市公司股东的净利润 14,412,407,113.84 14,136,119,367.60
资产总计 401,189,411,937.90 368,031,704,522.86
负债合计 261,597,726,966.35 226,518,009,574.89
归属于上市公司股东的净资产 135,395,447,305.45 137,416,898,946.39
归属于母公司所有者权益合计 135,395,447,305.45 137,416,898,946.39
所有者权益合计 139,591,684,971.55 141,513,694,947.97
经营活动产生的现金流量净额 28,328,562,187.20 5,122,166,411.40
基本每股收益（元/股） 2.60 2.56
""",
            verification_status="official_primary",
            body_status="full_text",
            source_class="regulatory_filing",
        ),
        origin_type="official_refresh",
        origin_id="gree-interim-refresh",
    )

    result = OfficialFinancialExtractionService(store=store).extract_document(
        str(archived["document_ref"]),
        "000651.SZ",
    )

    assert result["status"] == "validated"
    snapshot = store.list_financial_snapshots("000651.SZ", validated_only=True)[
        "snapshots"
    ][0]
    metrics = {item["metric"]: item["value"] for item in snapshot["metrics"]}
    assert metrics["total_equity"] == "139591684971.55"
    assert metrics["parent_equity"] == "135395447305.45"
    assert snapshot["validation"]["checks"]["balance_sheet_reconciles"] is True


def test_repair_subject_does_not_expand_errors_to_unprocessed_documents(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first_ref = _official_document(
        store,
        "招商银行 证券代码 600036 2025年度报告\n营业收入 100",
    )
    second = SourceIngestionService(store).ingest(
        CollectedSource(
            subject_key="600036.SH",
            market="CN",
            source_kind="official_filing",
            provider_id="cninfo",
            provider_record_id="annual-2024",
            publisher="巨潮资讯",
            title="招商银行2024年度报告",
            source_locator="https://static.cninfo.com.cn/finalpage/annual-2024.pdf",
            content="招商银行 证券代码 600036 2024年度报告\n营业收入 90",
            verification_status="official_primary",
            body_status="full_text",
            source_class="regulatory_filing",
        ),
        origin_type="official_refresh",
        origin_id="refresh-2",
    )
    service = OfficialFinancialExtractionService(store=store)
    service.extract_document(first_ref, "600036.SH")

    result = service.extract_subject(
        "600036.SH",
        force=True,
        repair_only=True,
    )

    assert result["repairable_before"] == 1
    assert result["documents"] == 1
    assert result["remaining"] == 1
    assert all(item["document_ref"] == first_ref for item in result["results"])
    with store.connect() as conn:
        second_count = conn.execute(
            "SELECT COUNT(*) FROM structured_document_extractions WHERE document_ref=?",
            (str(second["document_ref"]),),
        ).fetchone()[0]
    assert second_count == 0


def test_sec_companyfacts_native_xbrl_is_saved_once_without_ocr(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = OfficialFinancialExtractionService(store=store)

    def concept(unit: str, value: int | float, *, form: str = "10-K") -> dict:
        return {
            "units": {
                unit: [
                    {
                        "end": "2025-12-31",
                        "val": value,
                        "form": form,
                        "fp": "FY",
                        "filed": "2026-02-01",
                        "accn": "0000000000-26-000001",
                    }
                ]
            }
        }

    payload = {
        "cik": 320193,
        "entityName": "Apple Inc.",
        "facts": {
            "us-gaap": {
                "Revenues": concept("USD", 100),
                "OperatingIncomeLoss": concept("USD", 30),
                "NetIncomeLoss": concept("USD", 25),
                "Assets": concept("USD", 200),
                "Liabilities": concept("USD", 120),
                "StockholdersEquity": concept("USD", 80),
                "NetCashProvidedByUsedInOperatingActivities": concept("USD", 35),
                "EarningsPerShareDiluted": concept("USD/shares", 2.5),
            }
        },
    }

    first = service.ingest_sec_companyfacts(
        "AAPL.US",
        payload=payload,
        cik="320193",
    )
    second = service.ingest_sec_companyfacts(
        "AAPL.US",
        payload=payload,
        cik="320193",
    )

    assert first["status"] == "validated"
    assert first["ocr_performed"] is False
    assert second["cached"] is True
    snapshots = store.list_financial_snapshots("AAPL.US", validated_only=True)
    assert snapshots["count"] == 1
    assert snapshots["snapshots"][0]["extraction_method"] == "native_xbrl"
    with store.connect() as conn:
        observation = conn.execute(
            "SELECT verification_status,source_kind FROM source_observations"
        ).fetchone()
    assert observation["verification_status"] == "official_primary"
    assert observation["source_kind"] == "structured_financial"


class _AnnualProvider(OfficialFilingProvider):
    provider_id = "annual_fixture"

    def supports(self, symbol: str) -> bool:
        return symbol == "600036.SH"

    def list_filings(self, symbol: str, *, limit: int = 8):
        return [
            OfficialFilingRecord(
                provider_id=self.provider_id,
                provider_record_id="annual-live-2025",
                symbol=symbol,
                title="招商银行2025年度报告",
                publisher="上海证券交易所",
                document_url="https://www.sse.com.cn/disclosure/600036-2025.html",
                published_at="2026-03-20",
                filing_type="annual",
            )
        ]


class _AnnualResponse:
    url = "https://www.sse.com.cn/disclosure/600036-2025.html"
    headers = {"content-type": "text/html; charset=utf-8"}
    encoding = "utf-8"

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size: int):
        body = """<html><body><h1>招商银行2025年度报告 证券代码600036</h1>
        <p>主要会计数据 （单位：人民币百万元）</p>
        <p>营业收入 100 90</p><p>营业利润 30 25</p>
        <p>归属于本行股东的净利润 20 18</p>
        <p>总资产 200 180</p><p>总负债 120 110</p>
        <p>股东权益合计 80 70</p>
        <p>经营活动产生的现金流量净额 35 30</p></body></html>"""
        yield body.encode("utf-8")


class _AnnualSession:
    def get(self, *args, **kwargs):
        return _AnnualResponse()


def test_official_refresh_immediately_builds_structured_snapshot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = OfficialFilingService(
        store=store,
        ingestion=SourceIngestionService(store),
        providers=[_AnnualProvider()],
        session=_AnnualSession(),  # type: ignore[arg-type]
    )

    result = service.refresh("600036.SH", force=True)

    assert result["refreshed"] == 1
    assert result["structured"]["validated"] == 1
    assert result["structured"]["metrics"] >= 7
    assert store.list_financial_snapshots("600036.SH", validated_only=True)["count"] == 1
