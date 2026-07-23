from __future__ import annotations

from src.reports.etf_report_readiness import (
    etf_report_presentation,
    evaluate_etf_report_readiness,
    project_etf_module_namespaces,
)
from src.reports.contracts import DeepReportRecord
from src.reports.service import DeepReportService, report_display_title, report_pdf_filename


def _modules(*, research: bool = False) -> dict:
    selected = [
        {"component_symbol": "A", "component_weight": 0.10, "digest_status": "missing"},
        {"component_symbol": "B", "component_weight": 0.45, "digest_status": "missing"},
    ]
    component = {
        "status": "warning",
        "coverage": 0.0,
        "selected_count": 2,
        "research_coverage": 0.0,
        "fully_supported_coverage": 0.0,
        "reusable_count": 0,
        "partial_reusable_count": 0,
        "missing_count": 2,
        "conflicted_count": 0,
        "selected_components": selected,
    }
    if research:
        component.update({
            "status": "passed",
            "coverage": 1.0,
            "research_coverage": 0.90,
            "fully_supported_coverage": 0.80,
            "reusable_count": 2,
            "missing_count": 0,
            "selected_components": [
                {**item, "digest_status": "reusable"} for item in selected
            ],
        })
    return {
        "identity": {"status": "passed", "coverage": 1.0},
        "product_profile": {
            "status": "warning",
            "coverage": 0.75,
            "details": {"missing_optional_fields": ["iopv", "premium_discount_rate"]},
        },
        "universe": {"status": "passed", "coverage": 0.98},
        "market_data": {"status": "passed", "coverage": 1.0},
        "holding_penetration": {
            "status": "passed",
            "coverage": 0.55,
            "selected_weight_coverage": 0.55,
            "selected_components": selected,
        },
        "component_research": component,
        "index_and_product": {"status": "passed", "coverage": 1.0},
        "holding_penetration_section": {"status": "passed", "coverage": 1.0},
    }


def test_zero_component_research_is_structure_only() -> None:
    readiness = evaluate_etf_report_readiness(
        quality_status="passed_with_gaps",
        analysis_modules=_modules(),
    )

    assert readiness["status"] == "structure_ready"
    assert readiness["metrics"]["component_research_coverage"] == 0.0
    assert "component_research_coverage_below_threshold" in readiness["reason_codes"]
    assert etf_report_presentation(readiness)["title_label"] == "ETF 结构研究"


def test_complete_weighted_component_research_is_penetration_ready() -> None:
    readiness = evaluate_etf_report_readiness(
        quality_status="passed_with_gaps",
        analysis_modules=_modules(research=True),
    )

    assert readiness["status"] == "penetration_ready"
    assert readiness["metrics"]["fully_supported_etf_weight"] == 0.44
    assert readiness["reason_codes"] == []


def test_failed_evidence_quality_is_never_publishable() -> None:
    readiness = evaluate_etf_report_readiness(
        quality_status="failed_validation",
        analysis_modules=_modules(research=True),
    )

    assert readiness["status"] == "not_publishable"
    assert readiness["hard_gate_passed"] is False


def test_product_profile_warning_downgrades_reader_section() -> None:
    pipeline, sections = project_etf_module_namespaces(_modules())

    assert pipeline["product_profile"]["status"] == "warning"
    assert sections["index_and_product"]["status"] == "warning"
    assert sections["index_and_product"]["reason"] == "product_profile_not_ready"
    assert sections["index_and_product"]["missing_items"] == [
        "iopv",
        "premium_discount_rate",
    ]


def test_structure_only_report_never_uses_penetration_ready_title() -> None:
    readiness = evaluate_etf_report_readiness(
        quality_status="passed_with_gaps",
        analysis_modules=_modules(),
    )
    record = DeepReportRecord(
        profile="etf_deep_research",
        instrument_type="etf",
        symbol="159516.SZ",
        security_name="半导体设备ETF国泰",
        report_date="2026-07-23",
        quality_status="passed_with_gaps",
        etf_readiness=readiness,
    )

    assert report_display_title(record).endswith("ETF 结构研究")
    assert report_pdf_filename(record).endswith("_ETF结构研究.pdf")
    assert "穿透式深度研究" not in report_pdf_filename(record)


def test_formal_validation_rejects_incomplete_derived_fact_lineage(tmp_path) -> None:
    service = DeepReportService(tmp_path / "reports")

    validation = service.validate(
        "# 半导体设备ETF国泰（159516.SZ）ETF 结构研究\n",
        profile="etf_deep_research",
        analysis_available=True,
        available_fact_ids={"derived_fact"},
        available_evidence_ids=set(),
        available_facts=[{
            "fact_id": "derived_fact",
            "metric": "exchange_market_value",
            "value": "100",
            "formula": "fund_units * current_price",
            "input_fact_ids": [],
            "metadata": {"source_kind": "derived"},
        }],
    )

    assert "derived_fact_lineage_incomplete:derived_fact" in validation["issues"]
    assert validation["quality_status"] == "failed_validation"
