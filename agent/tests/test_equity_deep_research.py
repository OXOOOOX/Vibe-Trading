from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.reports.contracts import ModuleResult
from src.reports.financial_analysis import normalize_financial_snapshot
from src.reports.service import DeepReportService, report_pdf_filename
from src.session.events import EventBus
from src.session.service import (
    SessionService,
    _EQUITY_DEEP_FINANCIAL_COMMANDS,
    _EQUITY_DEEP_RESEARCH_TOOL_NAMES,
)
from src.session.store import SessionStore
from src.tools import build_filtered_registry
from src.tools.financial_snapshot_analysis_tool import FinancialSnapshotAnalysisTool
from src.tools.financial_rigor_tool import implied_terminal_earnings, validate_terminal_scenarios
from src.tools.report_evidence_tool import RecordReportEvidenceTool


def _rows():
    return {
        ("income", "annual"): [
            {
                "REPORT_DATE": "2024-12-31", "UPDATE_DATE": "2025-03-20",
                "TOTAL_OPERATE_INCOME": 120, "OPERATE_COST": 72, "GROSS_PROFIT": 48,
                "OPERATE_PROFIT": 24, "PARENT_NETPROFIT": 15,
                "ASSET_IMPAIRMENT_LOSS": 1, "CREDIT_IMPAIRMENT_LOSS": 0.5,
            },
            {
                "REPORT_DATE": "2023-12-31", "UPDATE_DATE": "2024-03-20",
                "TOTAL_OPERATE_INCOME": 100, "OPERATE_COST": 60, "GROSS_PROFIT": 40,
                "OPERATE_PROFIT": 20, "PARENT_NETPROFIT": 14,
                "ASSET_IMPAIRMENT_LOSS": 0.5, "CREDIT_IMPAIRMENT_LOSS": 0.2,
            },
        ],
        ("balance", "annual"): [
            {
                "REPORT_DATE": "2024-12-31", "UPDATE_DATE": "2025-03-20",
                "TOTAL_ASSETS": 200, "TOTAL_LIABILITIES": 80, "TOTAL_EQUITY": 120, "TOTAL_PARENT_EQUITY": 120,
                "MONETARYFUNDS": 30, "ACCOUNTS_RECE": 40, "INVENTORY": 35,
                "GOODWILL": 10, "SHORT_LOAN": 20, "LONG_LOAN": 10,
            },
            {
                "REPORT_DATE": "2023-12-31", "UPDATE_DATE": "2024-03-20",
                "TOTAL_ASSETS": 180, "TOTAL_LIABILITIES": 70, "TOTAL_EQUITY": 110, "TOTAL_PARENT_EQUITY": 110,
                "MONETARYFUNDS": 25, "ACCOUNTS_RECE": 25, "INVENTORY": 20,
                "GOODWILL": 8, "SHORT_LOAN": 15, "LONG_LOAN": 10,
            },
        ],
        ("cashflow", "annual"): [
            {
                "REPORT_DATE": "2024-12-31", "NETCASH_OPERATE": 8,
                "CONSTRUCT_LONG_ASSET": 5, "BEGIN_CASH": 25, "END_CASH": 30,
                "NET_INCREASE_CASH": 5, "EFFECT_EXCHANGE_RATE": 0,
            },
            {
                "REPORT_DATE": "2023-12-31", "NETCASH_OPERATE": 7,
                "CONSTRUCT_LONG_ASSET": 4, "BEGIN_CASH": 22, "END_CASH": 25,
                "NET_INCREASE_CASH": 3, "EFFECT_EXCHANGE_RATE": 0,
            },
        ],
        ("income", "quarter"): [
            {"REPORT_DATE": "2025-03-31", "TOTAL_OPERATE_INCOME": 32, "PARENT_NETPROFIT": 4},
        ],
        ("balance", "quarter"): [
            {"REPORT_DATE": "2025-03-31", "TOTAL_ASSETS": 205, "TOTAL_LIABILITIES": 82, "TOTAL_EQUITY": 123, "TOTAL_PARENT_EQUITY": 123},
        ],
        ("cashflow", "quarter"): [
            {"REPORT_DATE": "2025-03-31", "NETCASH_OPERATE": 3},
        ],
    }


def test_normalized_snapshot_preserves_lineage_and_safe_review_signals() -> None:
    result = normalize_financial_snapshot(
        symbol="000001.SZ",
        security_name="示例公司",
        market="a_share",
        currency="CNY",
        statement_rows=_rows(),
        data_as_of="2025-04-01T00:00:00+00:00",
    )
    assert result["financial_gate"]["status"] == "passed"
    assert result["latest_quarter"]["status"] == "passed"
    assert result["snapshot"]["coverage"]["coverage_ratio"] > 0.7
    balance = next(
        period for period in result["snapshot"]["periods"]
        if period["statement_type"] == "balance" and period["period_end"] == "2024-12-31"
    )
    assert balance["values"]["contract_assets"] is None
    revenue_yoy = next(fact for fact in result["facts"] if fact["metric"] == "revenue_yoy")
    assert revenue_yoy["formula"]
    assert len(revenue_yoy["input_fact_ids"]) == 2
    assert all(item.startswith("fact_") for item in revenue_yoy["input_fact_ids"])
    assert any(check["rule"] == "assets = liabilities + equity" and check["status"] == "pass" for check in result["reconciliations"])
    assert any(check["rule"] == "ending_cash = beginning_cash + net_change_cash" and check["status"] == "pass" for check in result["reconciliations"])
    alert_rules = {alert["rule"] for alert in result["alerts"]}
    assert "receivables_growth_outpaces_revenue" in alert_rules
    assert "inventory_growth_outpaces_revenue" in alert_rules
    assert "cash_conversion_weak_two_years" in alert_rules
    assert all("不构成财务造假判断" in alert["wording_guard"] for alert in result["alerts"])


def test_duplicate_period_keeps_latest_update_and_records_superseded_evidence() -> None:
    rows = _rows()
    rows[("income", "annual")].append({
        "REPORT_DATE": "2024-12-31", "UPDATE_DATE": "2025-02-01",
        "TOTAL_OPERATE_INCOME": 999, "OPERATE_COST": 1, "PARENT_NETPROFIT": 999,
    })
    result = normalize_financial_snapshot(
        symbol="000001.SZ", security_name="示例公司", market="a_share", currency="CNY",
        statement_rows=rows,
    )
    revenue = next(
        fact for fact in result["facts"]
        if fact["metric"] == "revenue" and fact["period"] == "2024-12-31"
    )
    assert revenue["value"] == "120"
    assert result["snapshot"]["superseded_evidence_ids"]


def test_financial_gate_rejects_unaligned_annual_statement_periods() -> None:
    rows = _rows()
    rows[("cashflow", "annual")][0]["REPORT_DATE"] = "2022-12-31"
    rows[("cashflow", "annual")][1]["REPORT_DATE"] = "2021-12-31"
    result = normalize_financial_snapshot(
        symbol="000001.SZ",
        security_name="示例公司",
        market="a_share",
        currency="CNY",
        statement_rows=rows,
    )

    assert result["financial_gate"]["status"] == "failed_validation"
    assert result["financial_gate"]["common_full_periods"] == []


def test_us_and_hk_aliases_map_into_the_same_financial_contract() -> None:
    for symbol, market, currency in (("AAPL.US", "us", "USD"), ("00700.HK", "hk", "HKD")):
        rows = {
            ("income", "annual"): [
                {"REPORT_DATE": "2024-12-31", "TOTAL_REVENUE": 100, "COST_OF_REVENUE": 60, "NET_INCOME": 20},
                {"REPORT_DATE": "2023-12-31", "TOTAL_REVENUE": 90, "COST_OF_REVENUE": 55, "NET_INCOME": 18},
            ],
            ("balance", "annual"): [
                {"REPORT_DATE": "2024-12-31", "CASH_AND_CASH_EQUIVALENTS": 25, "TOTALASSETS": 200, "TOTAL_LIABILITY": 80, "TOTAL_EQUITY": 120, "STOCKHOLDERS_EQUITY": 120},
                {"REPORT_DATE": "2023-12-31", "CASH_AND_CASH_EQUIVALENTS": 20, "TOTALASSETS": 180, "TOTAL_LIABILITY": 72, "TOTAL_EQUITY": 108, "STOCKHOLDERS_EQUITY": 108},
            ],
            ("cashflow", "annual"): [
                {"REPORT_DATE": "2024-12-31", "OPERATING_CASH_FLOW": 22, "CAPITAL_EXPENDITURE": 8, "NET_CHANGE_IN_CASH": 5},
                {"REPORT_DATE": "2023-12-31", "OPERATING_CASH_FLOW": 19, "CAPITAL_EXPENDITURE": 7, "NET_CHANGE_IN_CASH": 4},
            ],
        }
        result = normalize_financial_snapshot(
            symbol=symbol,
            security_name="Example",
            market=market,
            currency=currency,
            statement_rows=rows,
        )

        assert result["financial_gate"]["status"] == "passed"
        assert result["snapshot"]["report_currency"] == currency
        assert any(item["metric"] == "revenue" and item["value"] == "100" for item in result["facts"])


def _valid_report() -> str:
    sections = [
        ("核心结论", "核心事实 [Fact:fact_revenue]。"),
        ("公司业务与产业位置", "产业证据 [Evidence:ev_industry]。"),
        ("三张报表与财务质量", "现金利润匹配 [Fact:fact_cfo]。"),
        ("会计科目异常与核查清单", "异常信号只用于核查 [Fact:fact_alert]。"),
        ("市值隐含预期", "该反推不是完整 DCF，也不是目标价 [Fact:fact_terminal]。"),
        ("长期经营情景与叙事阶段", "当前阶段属于推断 [inference] [Evidence:ev_stage]。"),
        ("反方论证、风险与催化剂", "反方条件 [Evidence:ev_risk]。"),
        ("结论与跟踪框架", "跟踪指标 [Fact:fact_watch]。"),
        ("数据缺口与方法说明", "数据截至2025-04-01；Fact/Evidence索引完整。"),
    ]
    body = "\n\n".join(f"## {title}\n\n{text}" for title, text in sections)
    return (
        "# 示例公司（000001.SZ）穿透式深度研究\n\n"
        "报告类型：equity_deep_research\n股票：000001.SZ\n数据截至时间：2025-04-01\n质量状态：passed\n\n"
        + body
    )


def _attach_test_ledger(
    service: DeepReportService,
    report_id: str,
    *,
    symbol: str = "000001.SZ",
    security_name: str = "示例公司",
) -> None:
    analysis = normalize_financial_snapshot(
        symbol=symbol,
        security_name=security_name,
        market="a_share",
        currency="CNY",
        statement_rows=_rows(),
        data_as_of="2025-04-01T00:00:00+00:00",
    )
    service.attach_analysis(report_id, analysis)
    evidence = [
        {
            "evidence_id": evidence_id,
            "symbol": symbol,
            "domain": "industry",
            "source": "test-source",
            "source_locator": f"https://example.test/{evidence_id}",
            "retrieved_at": "2025-04-01T00:00:00+00:00",
            "published_at": "2025-04-01",
            "content_hash": evidence_id,
            "summary": "test evidence",
            "status": "recorded_from_opened_source",
            "metadata": {},
        }
        for evidence_id in ("ev_industry", "ev_stage", "ev_risk")
    ]
    facts = [
        {
            "fact_id": fact_id,
            "symbol": symbol,
            "metric": fact_id.removeprefix("fact_"),
            "value": "1",
            "unit": "test",
            "period": "2025",
            "formula": None,
            "input_fact_ids": [],
            "evidence_ids": ["ev_industry"],
            "calculation_version": "test-v1",
            "validation_status": "pass",
            "statement_type": None,
            "metadata": {},
        }
        for fact_id in ("fact_revenue", "fact_cfo", "fact_alert", "fact_terminal", "fact_watch")
    ]
    service.attach_external_evidence(report_id, {"evidence": evidence, "facts": facts})


def _attach_test_audit(service: DeepReportService, report_id: str, content: str) -> None:
    service.attach_audit_result(report_id, {
        "audit_id": "audit_test",
        "audit_status": "complete",
        "verdict": "PASS",
        "content_binding_verified": True,
        "report_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "expected_sample_size": 3,
        "total": 3,
        "pass_count": 3,
        "warn_count": 0,
        "fail_count": 0,
    })


def _submit_valid_workspace(service: DeepReportService, report_id: str) -> None:
    bodies = {
        "executive_summary": "核心事实 [Fact:fact_revenue]。",
        "business_position": "产业证据 [Evidence:ev_industry]。",
        "financial_quality": "现金利润匹配 [Fact:fact_cfo]。",
        "accounting_review": "异常信号只用于核查 [Fact:fact_alert]。",
        "implied_expectations": "[data_gap] 当前证据不足，不运行反推，也不提供目标价。",
        "terminal_narrative": "当前阶段属于推断 [inference] [Evidence:ev_stage]。",
        "counter_thesis": "反方条件 [Evidence:ev_risk]。",
        "conclusion_watchlist": "跟踪指标 [Fact:fact_watch]。",
    }
    for section_id, body in bodies.items():
        service.submit_section(report_id, section_id=section_id, body_markdown=body)


def test_report_workspace_rejects_owned_headings_and_unreplayable_numbers(tmp_path: Path) -> None:
    service = DeepReportService(tmp_path / "reports")
    record = service.begin(session_id="s1", attempt_id="a1", request_content="研究000001.SZ")
    _attach_test_ledger(service, record.report_id)
    revenue = next(
        fact for fact in service.inspect_workspace(record.report_id)["facts"]
        if fact["metric"] == "revenue" and fact["period"] == "2024-12-31"
    )

    with pytest.raises(ValueError, match="compiler_owned_heading_detected"):
        service.submit_section(
            record.report_id,
            section_id="executive_summary",
            body_markdown="## 自定义核心结论\n正文。",
        )
    with pytest.raises(ValueError, match="numeric_fact_mismatch"):
        service.submit_section(
            record.report_id,
            section_id="executive_summary",
            body_markdown=f"2024 年营业收入为 121 元 [Fact:{revenue['fact_id']}]。",
        )
    with pytest.raises(ValueError, match="valuation_direction_without_implied_expectations"):
        service.submit_section(
            record.report_id,
            section_id="executive_summary",
            body_markdown=f"当前结论为显著高估 [Fact:{revenue['fact_id']}]。",
        )

    section = service.submit_section(
        record.report_id,
        section_id="executive_summary",
        body_markdown=f"2024 年营业收入为 120 元 [Fact:{revenue['fact_id']}]。",
    )
    assert section.status == "passed"
    assert section.fact_ids == [revenue["fact_id"]]


def test_final_numeric_audit_binds_exact_published_markdown(tmp_path: Path) -> None:
    service = DeepReportService(tmp_path / "reports")
    record = service.begin(session_id="s1", attempt_id="a1", request_content="研究000001.SZ")
    _attach_test_ledger(service, record.report_id)
    _submit_valid_workspace(service, record.report_id)
    revenue = next(
        fact for fact in service.inspect_workspace(record.report_id)["facts"]
        if fact["metric"] == "revenue" and fact["period"] == "2024-12-31"
    )
    service.submit_section(
        record.report_id,
        section_id="executive_summary",
        body_markdown=f"2024 年营业收入为 120 元 [Fact:{revenue['fact_id']}]。",
    )

    published = service.publish_workspace(record.report_id)
    markdown = service.read_markdown(published.report_id)
    audit = json.loads(
        (tmp_path / "reports" / published.report_id / "numeric_audit.json").read_text(encoding="utf-8")
    )
    markdown_bytes = (
        tmp_path / "reports" / published.report_id / "report.md"
    ).read_bytes()
    assert audit["verdict"] == "PASS"
    assert audit["total"] >= 1
    assert audit["report_sha256"] == hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    assert audit["report_sha256"] == hashlib.sha256(markdown_bytes).hexdigest()


def test_pdf_derivative_uses_clean_title_and_omits_duplicate_compiler_h1(tmp_path: Path) -> None:
    service = DeepReportService(tmp_path / "reports")
    record = service.begin(session_id="s1", attempt_id="a1", request_content="research 000001.SZ")
    _attach_test_ledger(service, record.report_id)
    _submit_valid_workspace(service, record.report_id)
    published = service.publish_workspace(record.report_id)
    captured: dict[str, str] = {}

    def renderer(title: str, content: str) -> bytes:
        captured.update(title=title, content=content)
        return b"%PDF-1.4\n"

    pdf_path, _ = service.ensure_pdf(published.report_id, renderer)

    assert captured["title"] == "示例公司（000001.SZ）穿透式深度研究"
    assert not captured["content"].lstrip().startswith("# 示例公司")
    assert pdf_path.read_bytes() == b"%PDF-1.4\n"

    service.ensure_pdf(
        published.report_id,
        lambda _title, _content: b"%PDF-1.4\nrefreshed\n",
        force=True,
    )
    assert pdf_path.read_bytes() == b"%PDF-1.4\nrefreshed\n"


def test_hard_module_failure_generates_actionable_diagnostic(tmp_path: Path) -> None:
    service = DeepReportService(tmp_path / "reports")
    record = service.begin(session_id="s1", attempt_id="a1", request_content="研究000001.SZ")
    _attach_test_ledger(service, record.report_id)
    _submit_valid_workspace(service, record.report_id)

    record = service.require(record.report_id)
    failure = ModuleResult(
        status="failed_validation",
        reason="timestamped_price_and_market_cap_required",
    )
    record.analysis_modules["report_gate"] = failure
    record.analysis_modules["market_data"] = failure
    service._write_manifest(record)
    index_path = tmp_path / "reports" / record.report_id / "analysis" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["module_statuses"]["report_gate"] = {
        "status": failure.status,
        "reason": failure.reason,
    }
    index["module_statuses"]["market_data"] = {
        "status": failure.status,
        "reason": failure.reason,
    }
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    published = service.publish_workspace(record.report_id)

    assert published.quality_status == "failed_validation"
    assert (
        "module_failed_validation:report_gate:timestamped_price_and_market_cap_required"
        in published.validation_issues
    )
    assert service.repair_blockers(record.report_id) == [
        "market_data:timestamped_price_and_market_cap_required",
        "report_gate:timestamped_price_and_market_cap_required",
    ]
    diagnostic = service.read_markdown(record.report_id)
    assert "unknown_validation_failure" not in diagnostic
    assert "timestamped_price_and_market_cap_required" not in diagnostic
    assert "failed_validation" not in diagnostic
    assert "缺少同一时点、可核验的最新价格和总市值" in diagnostic
    assert "用新数据更新" in diagnostic


def test_revision_workspace_reuses_hashes_and_full_refresh_stales_everything(tmp_path: Path) -> None:
    service = DeepReportService(tmp_path / "reports")
    first = service.begin(session_id="s1", attempt_id="a1", request_content="研究000001.SZ")
    _attach_test_ledger(service, first.report_id)
    _submit_valid_workspace(service, first.report_id)
    first = service.publish_workspace(first.report_id)
    parent_workspace = service.inspect_workspace(first.report_id)["sections"]

    targeted = service.begin(
        session_id="s1",
        attempt_id="a2",
        request_content="只重写风险",
        parent_report_id=first.report_id,
        revision_mode="section_revision",
        revision_sections=["counter_thesis"],
    )
    targeted_workspace = service.inspect_workspace(targeted.report_id)["sections"]
    assert targeted_workspace["counter_thesis"]["status"] == "stale"
    for section_id, section in targeted_workspace.items():
        if section_id == "counter_thesis":
            continue
        assert section["status"] == "passed"
        assert section["content_hash"] == parent_workspace[section_id]["content_hash"]
    service.submit_section(
        targeted.report_id,
        section_id="counter_thesis",
        body_markdown="新的反方条件 [Evidence:ev_risk]。",
    )
    targeted = service.publish_workspace(targeted.report_id)
    diff = service.artifact_path(targeted.report_id, "diff").read_text(encoding="utf-8")
    assert "核心结论：未变化，复用父版本" in diff
    assert "## 反方论证、风险与催化剂" in diff

    refreshed = service.begin(
        session_id="s1",
        attempt_id="a3",
        request_content="使用新数据",
        parent_report_id=targeted.report_id,
        revision_mode="full_refresh",
    )
    refreshed_workspace = service.inspect_workspace(refreshed.report_id)
    assert refreshed_workspace["analysis_available"] is False
    assert all(
        "body_markdown" not in section
        for section in refreshed_workspace["sections"].values()
    )
    assert all(
        "fact_ids" not in section and "evidence_ids" not in section
        for section in refreshed_workspace["sections"].values()
    )
    assert all(
        "fact_ref_count" in section and "evidence_ref_count" in section
        for section in refreshed_workspace["sections"].values()
    )
    explicitly_requested = service.inspect_workspace(
        refreshed.report_id,
        include_section_bodies=True,
    )
    assert all(
        "body_markdown" not in section
        and section["body_blocked_reason"] == "parent_section_unavailable_in_full_refresh"
        for section in explicitly_requested["sections"].values()
    )
    assert all(
        section["body_available"] is True
        for section in refreshed_workspace["sections"].values()
    )
    assert all(
        section["status"] == "stale"
        for section in refreshed_workspace["sections"].values()
    )


def test_automatic_repair_is_allowed_only_once(tmp_path: Path) -> None:
    service = DeepReportService(tmp_path / "reports")
    record = service.begin(session_id="s1", attempt_id="a1", request_content="研究000001.SZ")
    _attach_test_ledger(service, record.report_id)
    evaluation = service.evaluate_workspace(record.report_id)
    assert service.should_auto_repair(record.report_id, evaluation) is True
    service.mark_repairing(record.report_id)
    assert service.should_auto_repair(record.report_id, evaluation) is False


def test_repair_revision_reuses_passed_sections_and_stales_only_failures(tmp_path: Path) -> None:
    service = DeepReportService(tmp_path / "reports")
    parent = service.begin(session_id="s1", attempt_id="a1", request_content="research 000001.SZ")
    _attach_test_ledger(service, parent.report_id)
    _submit_valid_workspace(service, parent.report_id)
    failed = service._read_section(parent.report_id, "implied_expectations")
    assert failed is not None
    failed.status = "failed_validation"
    failed.validation_issues = ["deterministic_module_conflict"]
    service._write_section(parent.report_id, failed)

    repair = service.begin(
        session_id="s1",
        attempt_id="a2",
        request_content="repair failed sections",
        parent_report_id=parent.report_id,
        revision_mode="repair",
    )
    sections = service.inspect_workspace(repair.report_id)["sections"]

    assert sections["implied_expectations"]["status"] == "stale"
    assert all(
        section["status"] == "passed"
        for section_id, section in sections.items()
        if section_id != "implied_expectations"
    )


def test_missing_financial_section_is_repairable_when_deterministic_gate_passed(
    tmp_path: Path,
) -> None:
    service = DeepReportService(tmp_path / "reports")
    record = service.begin(session_id="s1", attempt_id="a1", request_content="research 000001.SZ")
    record.analysis_modules["financial_quality"] = ModuleResult(
        status="failed_validation",
        reason="missing section: financial quality",
        details={"deterministic_analysis": {"status": "passed"}},
    )
    service._write_manifest(record)

    assert service.repair_blockers(record.report_id) == []
    assert service._recoverable_validation(
        ["missing_required_section:financial_quality"],
        {
            "financial_quality": {
                "status": "failed_validation",
                "reason": "missing section: financial quality",
                "details": {"deterministic_analysis": {"status": "passed"}},
            },
        },
    ) is True


def test_deep_report_service_persists_validated_artifacts_and_revision(tmp_path: Path) -> None:
    service = DeepReportService(tmp_path / "reports")
    first = service.begin(
        session_id="s1",
        attempt_id="a1",
        request_content="研究000001.SZ",
        generation_source="portfolio_monitor_autopilot",
        generation_reason="原报告过期",
    )
    _attach_test_ledger(service, first.report_id)
    _submit_valid_workspace(service, first.report_id)
    first = service.publish_workspace(first.report_id)
    assert first.status == "completed"
    assert first.quality_status == "passed_with_gaps"
    assert first.symbol == "000001.SZ"
    assert first.generation_source == "portfolio_monitor_autopilot"
    assert first.generation_reason == "原报告过期"
    assert report_pdf_filename(first) == f"{first.report_date}_示例公司（000001.SZ）_穿透式深度研究.pdf"
    compiled = service.read_markdown(first.report_id)
    assert compiled.startswith("# 示例公司")
    assert "阅读提示" in compiled
    assert "研究已完成；部分判断因公开证据不足而保留" in compiled
    assert "数据依据" in compiled
    assert "资料来源" in compiled
    assert "〔数据1〕" in compiled
    assert "〔来源1〕" in compiled
    assert "编译器校验摘要" not in compiled
    assert "equity_deep_research" not in compiled
    assert "passed_with_gaps" not in compiled
    assert "[Fact:" not in compiled
    assert "[Evidence:" not in compiled
    assert "fact_revenue" not in compiled
    assert (tmp_path / "reports" / first.report_id / "claims.jsonl").exists()

    revision = service.begin(
        session_id="s1", attempt_id="a2", request_content="更新风险",
        parent_report_id=first.report_id,
    )
    assert revision.revision == 2
    assert revision.parent_report_id == first.report_id
    assert revision.generation_source == "portfolio_monitor_autopilot"
    assert revision.generation_reason == "原报告过期"
    assert (tmp_path / "reports" / revision.report_id / "analysis" / "facts.jsonl").exists()
    assert revision.symbol == "000001.SZ"


def test_deep_report_validation_fails_closed_without_sections_or_facts(tmp_path: Path) -> None:
    service = DeepReportService(tmp_path / "reports")
    record = service.begin(session_id="s1", attempt_id="a1", request_content="研究")
    record = service.finalize(record.report_id, "# 随便写的报告\n\n没有证据。")
    assert record.status == "completed"
    assert record.quality_status == "failed_validation"
    assert "missing_fact_references" in record.validation_issues
    assert (tmp_path / "reports" / record.report_id / "rejected_draft.md").exists()
    published = service.read_markdown(record.report_id)
    assert "系统因此没有给出投资结论" in published
    assert "没有证据" not in published


def test_target_prices_and_uncited_material_numbers_fail_closed(tmp_path: Path) -> None:
    service = DeepReportService(tmp_path / "reports")
    record = service.begin(session_id="s1", attempt_id="a1", request_content="研究000001.SZ")
    _attach_test_ledger(service, record.report_id)
    content = _valid_report().replace(
        "核心事实 [Fact:fact_revenue]。",
        "基础情景目标价：¥23.73；收入增长 30%。",
    )
    _attach_test_audit(service, record.report_id, content)
    record = service.finalize(record.report_id, content)

    assert record.status == "completed"
    assert "target_price_or_reasonable_value_detected" in record.validation_issues
    assert any(issue.startswith("uncited_material_numbers") for issue in record.validation_issues)


def test_two_company_reports_keep_identity_and_artifacts_isolated(tmp_path: Path) -> None:
    service = DeepReportService(tmp_path / "reports")
    first = service.begin(session_id="s1", attempt_id="a1", request_content="研究000001.SZ")
    _attach_test_ledger(service, first.report_id)
    _attach_test_audit(service, first.report_id, _valid_report())
    first = service.finalize(first.report_id, _valid_report())

    second = service.begin(session_id="s2", attempt_id="a2", request_content="研究301308.SZ")
    _attach_test_ledger(service, second.report_id, symbol="301308.SZ", security_name="江波龙")
    second_content = _valid_report().replace("示例公司", "江波龙").replace("000001.SZ", "301308.SZ")
    _attach_test_audit(service, second.report_id, second_content)
    second = service.finalize(second.report_id, second_content)

    assert first.symbol == "000001.SZ"
    assert second.symbol == "301308.SZ"
    assert "江波龙" not in service.read_markdown(first.report_id)
    assert "示例公司" not in service.read_markdown(second.report_id)
    assert "江波龙（301308.SZ）" in report_pdf_filename(second)


def test_session_deep_report_mode_wraps_prompt_and_attaches_report_metadata(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("VIBE_TRADING_DEEP_REPORT_ENABLED", "1")

    class FakeSessionService(SessionService):
        async def _run_with_agent(self, attempt, **kwargs):
            assert "[EQUITY_DEEP_RESEARCH_PROFILE]" in attempt.prompt
            _attach_test_ledger(self.deep_reports, attempt.metadata["report_id"])
            _submit_valid_workspace(self.deep_reports, attempt.metadata["report_id"])
            return {
                "status": "success",
                "content": "这段 Agent summary 不得进入正式报告。",
                "run_dir": None,
                "react_trace": [],
            }

    service = FakeSessionService(
        store=SessionStore(tmp_path / "sessions"),
        event_bus=EventBus(),
        runs_dir=tmp_path / "runs",
    )
    session = service.create_session("deep")
    asyncio.run(
        service.execute_message(
            session.session_id,
            "研究000001.SZ",
            message_metadata={"response_mode": "deep_report", "report_profile": "equity_deep_research"},
        )
    )
    reply = service.get_messages(session.session_id)[-1]
    assert reply.metadata["report_profile"] == "equity_deep_research"
    assert reply.metadata["report_quality_status"] == "passed_with_gaps"
    report = service.deep_reports.require(reply.metadata["report_id"])
    assert "Agent summary" not in service.deep_reports.read_markdown(report.report_id)

    asyncio.run(
        service.execute_message(
            session.session_id,
            "使用最新数据更新",
            message_metadata={
                "response_mode": "deep_report",
                "report_profile": "equity_deep_research",
                "parent_report_id": report.report_id,
                "revision_mode": "full_refresh",
            },
        )
    )
    refreshed_reply = service.get_messages(session.session_id)[-1]
    refreshed = service.deep_reports.require(refreshed_reply.metadata["report_id"])
    assert refreshed.parent_report_id == report.report_id
    assert refreshed.revision == 2
    assert refreshed.revision_mode == "full_refresh"


def test_followup_uses_linked_structured_context_without_creating_revision(tmp_path: Path) -> None:
    class FollowupSessionService(SessionService):
        captured_prompt = ""

        async def _run_with_agent(self, attempt, **kwargs):
            self.captured_prompt = attempt.prompt
            return {
                "status": "success",
                "content": "已基于当前报告解释。",
                "run_dir": None,
                "react_trace": [],
            }

    service = FollowupSessionService(
        store=SessionStore(tmp_path / "sessions"),
        event_bus=EventBus(),
        runs_dir=tmp_path / "runs",
    )
    session = service.create_session("deep")
    report = service.deep_reports.begin(
        session_id=session.session_id,
        attempt_id="report-attempt",
        request_content="研究000001.SZ",
    )
    _attach_test_ledger(service.deep_reports, report.report_id)
    _submit_valid_workspace(service.deep_reports, report.report_id)
    report = service.deep_reports.publish_workspace(report.report_id)

    asyncio.run(
        service.execute_message(
            session.session_id,
            "解释当前报告的主要风险",
            message_metadata={
                "response_mode": "chat",
                "linked_report_id": report.report_id,
            },
        )
    )

    assert "[LINKED_DEEP_REPORT_CONTEXT]" in service.captured_prompt
    assert f"report_id={report.report_id}" in service.captured_prompt
    assert "解释当前报告的主要风险" in service.captured_prompt
    assert "[LINKED_STRUCTURED_CONTEXT]" in service.captured_prompt
    assert "[LINKED_MARKDOWN]" not in service.captured_prompt
    assert len(service.deep_reports.list()) == 1


def test_equity_deep_report_registry_excludes_file_writes_and_target_price_commands() -> None:
    registry = build_filtered_registry(
        _EQUITY_DEEP_RESEARCH_TOOL_NAMES,
        include_shell_tools=False,
        financial_rigor_commands=_EQUITY_DEEP_FINANCIAL_COMMANDS,
    )
    assert "write_file" not in registry.tool_names
    assert "publish_obsidian_note" not in registry.tool_names
    assert "report_workspace" in registry.tool_names
    assert "report_audit" not in registry.tool_names
    rigor = registry.get("financial_rigor")
    assert rigor is not None
    commands = rigor.parameters["properties"]["command"]["enum"]
    assert "three_scenario" not in commands
    assert "verify_valuation" not in commands


def test_session_propagates_report_validation_failure_to_attempt_and_reply(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("VIBE_TRADING_DEEP_REPORT_ENABLED", "1")

    class InvalidSessionService(SessionService):
        async def _run_with_agent(self, attempt, **kwargs):
            _attach_test_ledger(self.deep_reports, attempt.metadata["report_id"])
            return {
                "status": "success",
                "content": "## 只有摘要\n\n基础情景目标价：¥23.73",
                "run_dir": None,
                "react_trace": [],
            }

    service = InvalidSessionService(
        store=SessionStore(tmp_path / "sessions"),
        event_bus=EventBus(),
        runs_dir=tmp_path / "runs",
    )
    session = service.create_session("invalid-deep")
    asyncio.run(service.execute_message(
        session.session_id,
        "研究000001.SZ",
        message_metadata={"response_mode": "deep_report", "report_profile": "equity_deep_research"},
    ))
    reply = service.get_messages(session.session_id)[-1]
    report = service.deep_reports.require(reply.metadata["report_id"])

    assert reply.metadata["status"] == "completed"
    assert reply.metadata["report_quality_status"] == "failed_validation"
    assert "打开诊断结果查看原因" in reply.content
    assert report.status == "completed"
    assert report.artifacts[0]["artifact_id"] == "diagnostic"


def test_financial_snapshot_tool_surfaces_module_gaps_without_inventing_values() -> None:
    def fake_fetch(code: str, statement: str, cadence: str):
        return {"ok": True}, list(_rows().get((statement, cadence), []))

    with patch.object(FinancialSnapshotAnalysisTool, "_fetch_statement", side_effect=fake_fetch), patch(
        "src.tools.financial_snapshot_analysis_tool.ResearchReportsTool.execute",
        return_value=json.dumps({"ok": False, "error": "no consensus"}),
    ), patch(
        "src.tools.financial_snapshot_analysis_tool.ShareholderCountTool.execute",
        return_value=json.dumps({"ok": False, "error": "no holder record"}),
    ):
        payload = json.loads(
            FinancialSnapshotAnalysisTool().execute(
                code="000001.SZ", security_name="示例公司", auto_market_data=False,
            )
        )
    assert payload["status"] == "ok"
    assert payload["module_statuses"]["financial_quality"]["status"] == "passed"
    assert payload["module_statuses"]["implied_expectations"]["status"] == "insufficient_evidence"
    assert payload["report_gate"]["status"] == "failed_validation"
    assert payload["quality_status"] == "failed_validation"


def test_financial_snapshot_tool_accepts_only_actionable_timestamped_market_data() -> None:
    def fake_fetch(code: str, statement: str, cadence: str):
        return {"ok": True}, list(_rows().get((statement, cadence), []))

    market_payload = {
        "status": "live",
        "market": {
            "series": [
                {
                    "symbol": "000001.SZ",
                    "interval": "1m",
                    "actionability": "analysis_only",
                    "blocked_reasons": ["intraday_not_started"],
                },
                {
                    "symbol": "000001.SZ",
                    "interval": "1D",
                    "adjustment": "raw",
                    "actionability": "price_actionable",
                    "selected_quote": {
                        "price": 12.5,
                        "bar_time": "2025-04-01T15:00:00+08:00",
                        "verified_at": "2025-04-01T15:10:00+08:00",
                        "sources": ["eastmoney", "sina"],
                    },
                },
            ],
        },
    }
    with patch.object(FinancialSnapshotAnalysisTool, "_fetch_statement", side_effect=fake_fetch), patch(
        "src.tools.financial_snapshot_analysis_tool.DataContextTool.execute",
        return_value=json.dumps(market_payload),
    ), patch(
        "src.tools.financial_snapshot_analysis_tool.ResearchReportsTool.execute",
        return_value=json.dumps({"ok": False, "error": "no consensus"}),
    ), patch(
        "src.tools.financial_snapshot_analysis_tool.ShareholderCountTool.execute",
        return_value=json.dumps({"ok": False, "error": "no holder record"}),
    ):
        payload = json.loads(FinancialSnapshotAnalysisTool().execute(
            code="000001.SZ",
            security_name="示例公司",
            shares=10,
        ))

    assert payload["report_gate"]["status"] == "passed"
    assert payload["module_statuses"]["market_data"]["status"] == "passed"
    market_cap = next(item for item in payload["facts"] if item["metric"] == "market_cap")
    assert market_cap["value"] == "125.0"
    assert market_cap["period"] == "2025-04-01"


def test_financial_snapshot_tool_does_not_value_uncovered_consensus() -> None:
    def fake_fetch(code: str, statement: str, cadence: str):
        return {"ok": True}, list(_rows().get((statement, cadence), []))

    research_payload = {
        "ok": True,
        "source": "eastmoney",
        "data": {
            "consensus_eps": [
                {"fiscal_year": 2025, "consensus_eps": 1.0},
                {"fiscal_year": 2026, "consensus_eps": 1.2},
                {"fiscal_year": 2027, "consensus_eps": 1.4},
            ],
            "reports": [],
        },
    }
    with patch.object(FinancialSnapshotAnalysisTool, "_fetch_statement", side_effect=fake_fetch), patch(
        "src.tools.financial_snapshot_analysis_tool.ResearchReportsTool.execute",
        return_value=json.dumps(research_payload),
    ), patch(
        "src.tools.financial_snapshot_analysis_tool.ShareholderCountTool.execute",
        return_value=json.dumps({"ok": False, "error": "no holder record"}),
    ):
        payload = json.loads(FinancialSnapshotAnalysisTool().execute(
            code="000001.SZ",
            security_name="示例公司",
            auto_market_data=False,
            market_cap=100,
            current_price=10,
            shares=10,
            market_data_source="test-market",
            market_data_as_of="2025-04-01T15:00:00+08:00",
        ))

    assert payload["research_status"]["coverage_count"] == 0
    assert payload["implied_expectations"]["applicability"] == "not_applicable"
    assert payload["module_statuses"]["implied_expectations"]["status"] == "insufficient_evidence"
    assert not any(fact["metric"] == "implied_terminal_earnings" for fact in payload["facts"])


def test_attach_analysis_downgrades_embedded_valuation_with_broken_lineage(tmp_path: Path) -> None:
    def fake_fetch(code: str, statement: str, cadence: str):
        return {"ok": True}, list(_rows().get((statement, cadence), []))

    research_payload = {
        "ok": True,
        "source": "eastmoney",
        "data": {
            "consensus_eps": [
                {"fiscal_year": 2025, "consensus_eps": 1.0},
                {"fiscal_year": 2026, "consensus_eps": 1.2},
                {"fiscal_year": 2027, "consensus_eps": 1.4},
            ],
            "reports": [{"title": "coverage marker"}],
        },
    }
    with patch.object(FinancialSnapshotAnalysisTool, "_fetch_statement", side_effect=fake_fetch), patch(
        "src.tools.financial_snapshot_analysis_tool.ResearchReportsTool.execute",
        return_value=json.dumps(research_payload),
    ), patch(
        "src.tools.financial_snapshot_analysis_tool.ShareholderCountTool.execute",
        return_value=json.dumps({"ok": False, "error": "no holder record"}),
    ):
        payload = json.loads(FinancialSnapshotAnalysisTool().execute(
            code="000001.SZ",
            security_name="示例公司",
            auto_market_data=False,
            market_cap=100,
            current_price=10,
            shares=10,
            market_data_source="test-market",
            market_data_as_of="2025-04-01T15:00:00+08:00",
        ))
    assert payload["implied_expectations"]["applicability"] == "applicable"
    consensus_evidence = next(item for item in payload["evidence"] if item["domain"] == "consensus")
    consensus_evidence["metadata"]["coverage_count"] = 0

    service = DeepReportService(tmp_path / "reports")
    record = service.begin(session_id="s1", attempt_id="a1", request_content="研究000001.SZ")
    service.attach_analysis(record.report_id, payload)

    index = json.loads(
        (tmp_path / "reports" / record.report_id / "analysis" / "index.json").read_text(encoding="utf-8")
    )
    assert index["implied_expectations"]["applicability"] == "not_applicable"
    assert index["module_statuses"]["implied_expectations"]["status"] == "insufficient_evidence"
    assert index["quality_status"] == "passed_with_gaps"
    ledger = (
        tmp_path / "reports" / record.report_id / "analysis" / "facts.jsonl"
    ).read_text(encoding="utf-8")
    assert '"metric": "implied_terminal_earnings"' not in ledger


def test_new_snapshot_clears_parent_audit_and_deterministic_results(tmp_path: Path) -> None:
    service = DeepReportService(tmp_path / "reports")
    parent = service.begin(session_id="s1", attempt_id="a1", request_content="研究000001.SZ")
    analysis = normalize_financial_snapshot(
        symbol="000001.SZ",
        security_name="示例公司",
        market="a_share",
        currency="CNY",
        statement_rows=_rows(),
    )
    service.attach_analysis(parent.report_id, analysis)
    parent_analysis = tmp_path / "reports" / parent.report_id / "analysis"
    deterministic = parent_analysis / "deterministic"
    deterministic.mkdir(parents=True)
    (deterministic / "implied_terminal_earnings.json").write_text(
        json.dumps({"applicability": "applicable"}), encoding="utf-8",
    )
    (parent_analysis / "report_audit.json").write_text(
        json.dumps({"audit_status": "complete"}), encoding="utf-8",
    )

    child = service.begin(
        session_id="s1",
        attempt_id="a2",
        request_content="使用新数据更新",
        parent_report_id=parent.report_id,
    )
    child_analysis = tmp_path / "reports" / child.report_id / "analysis"
    assert (child_analysis / "deterministic" / "implied_terminal_earnings.json").exists()
    assert (child_analysis / "report_audit.json").exists()

    refreshed = normalize_financial_snapshot(
        symbol="000001.SZ",
        security_name="示例公司",
        market="a_share",
        currency="CNY",
        statement_rows=_rows(),
        data_as_of="2025-04-02T00:00:00+00:00",
    )
    service.attach_analysis(child.report_id, refreshed)

    assert not (child_analysis / "deterministic").exists()
    assert not (child_analysis / "report_audit.json").exists()


def test_opened_external_source_registration_returns_persistable_ids() -> None:
    events: list[tuple[str, dict]] = []
    tool = RecordReportEvidenceTool(event_callback=lambda event, payload: events.append((event, payload)))
    payload = json.loads(tool.execute(
        symbol="301308.SZ",
        domain="tam",
        source="行业协会",
        source_locator="https://example.test/tam-report",
        source_read_status="opened_document",
        published_at="2026-06-30",
        excerpt="该行业报告明确给出了带统计口径、年份和币种的市场规模数据。",
        facts=[{"metric": "tam", "value": 1000, "unit": "CNY", "period": "2035", "scope": "global"}],
    ))

    assert payload["status"] == "ok"
    assert payload["evidence_id"].startswith("ev_")
    assert payload["fact_ids"][0].startswith("fact_")
    assert events[0][0] == "report.external_evidence"
    bundle = events[0][1]["bundle"]
    assert bundle["facts"][0]["evidence_ids"] == [payload["evidence_id"]]


def test_consensus_evidence_requires_explicit_coverage_metadata() -> None:
    tool = RecordReportEvidenceTool()
    base = {
        "symbol": "000001.SZ",
        "domain": "consensus",
        "source": "测试券商",
        "source_locator": "https://example.test/forecast",
        "source_read_status": "opened_webpage",
        "published_at": "2025-04-01",
        "excerpt": "测试券商给出了连续三个财年的盈利预测，并明确披露只有一家机构覆盖。",
        "facts": [{"metric": "forecast_net_profit", "value": 10, "unit": "CNY", "period": "2025"}],
    }
    missing = json.loads(tool.execute(**base))
    assert missing["status"] == "error"
    assert "coverage_count" in missing["error"]

    accepted = json.loads(tool.execute(**base, coverage_count=1, forecast_kind="single_broker"))
    assert accepted["status"] == "ok"

    internal = json.loads(tool.execute(
        **{
            **base,
            "source": "company announcement plus management guidance extrapolation",
            "excerpt": (
                "Internal estimates for three years; no consensus forecast values were "
                "published by the tracked brokers."
            ),
            "facts": [{
                "metric": "forecast_net_profit",
                "value": 10,
                "unit": "CNY",
                "period": "2025",
                "scope": "internal estimate",
            }],
        },
        coverage_count=11,
        forecast_kind="consensus",
    ))
    assert internal["status"] == "error"
    assert "internal estimates and extrapolations" in internal["error"]

    invalid_consensus_count = json.loads(tool.execute(
        **base,
        coverage_count=1,
        forecast_kind="consensus",
    ))
    assert invalid_consensus_count["status"] == "error"
    assert "at least 2 for consensus" in invalid_consensus_count["error"]


def test_implied_expectations_attachment_replays_registered_market_and_forecast_facts(
    tmp_path: Path,
) -> None:
    service = DeepReportService(tmp_path / "reports")
    record = service.begin(session_id="s1", attempt_id="a1", request_content="研究000001.SZ")
    _attach_test_ledger(service, record.report_id)
    evidence_id = "ev_consensus"
    evidence = [{
        "evidence_id": evidence_id,
        "symbol": "000001.SZ",
        "domain": "consensus",
        "source": "test-broker",
        "source_locator": "https://example.test/consensus",
        "retrieved_at": "2025-04-01T00:00:00+00:00",
        "published_at": "2025-04-01",
        "content_hash": "consensus",
        "summary": "three forward years",
        "status": "recorded_from_opened_source",
        "metadata": {"coverage_count": 1, "forecast_kind": "single_broker"},
    }]
    raw_inputs = [
        ("fact_market_cap", "market_cap", "1000", "CNY", "2025-04-01"),
        ("fact_e1", "forecast_net_profit", "10", "CNY", "FY2025"),
        ("fact_e2", "forecast_net_profit", "20", "CNY", "2026E"),
        ("fact_e3", "forecast_net_profit", "30", "CNY", "FY2027E"),
    ]
    facts = [{
        "fact_id": fact_id,
        "symbol": "000001.SZ",
        "metric": metric,
        "value": value,
        "unit": unit,
        "period": period,
        "formula": None,
        "input_fact_ids": [],
        "evidence_ids": [evidence_id],
        "calculation_version": "test-v1",
        "validation_status": "pass",
        "statement_type": None,
        "metadata": {},
    } for fact_id, metric, value, unit, period in raw_inputs]
    service.attach_external_evidence(record.report_id, {"evidence": evidence, "facts": facts})
    result = implied_terminal_earnings(
        1000,
        10,
        20,
        30,
        currency="CNY",
        forecast_years=[2025, 2026, 2027],
        base_year=2024,
        source_fact_ids=[item[0] for item in raw_inputs],
        symbol="000001.SZ",
    )
    attached = service.attach_deterministic_result(
        record.report_id, "implied_terminal_earnings", result,
    )
    assert attached.analysis_modules["implied_expectations"].status == "passed"

    missing_market = implied_terminal_earnings(
        1000,
        10,
        20,
        30,
        currency="CNY",
        forecast_years=[2025, 2026, 2027],
        base_year=2024,
        source_fact_ids=["fact_e1", "fact_e2", "fact_e3"],
        symbol="000001.SZ",
    )
    with pytest.raises(ValueError, match="market-cap Fact"):
        service.attach_deterministic_result(
            record.report_id, "implied_terminal_earnings", missing_market,
        )


def test_implied_expectations_rejects_internal_estimates_disguised_as_consensus(
    tmp_path: Path,
) -> None:
    service = DeepReportService(tmp_path / "reports")
    record = service.begin(session_id="s1", attempt_id="a1", request_content="research 000001.SZ")
    _attach_test_ledger(service, record.report_id)
    evidence_id = "ev_fake_consensus"
    evidence = [{
        "evidence_id": evidence_id,
        "symbol": "000001.SZ",
        "domain": "consensus",
        "source": "company guidance extrapolation",
        "source_locator": "https://example.test/company-announcement",
        "retrieved_at": "2025-04-01T00:00:00+00:00",
        "published_at": "2025-04-01",
        "content_hash": "fake-consensus",
        "summary": "Internal estimates extrapolated from management guidance; no consensus values.",
        "status": "recorded_from_opened_source",
        "metadata": {"coverage_count": 11, "forecast_kind": "consensus"},
    }]
    raw_inputs = [
        ("fact_fake_market_cap", "market_cap", "1000", "CNY", "2025-04-01", "market"),
        ("fact_fake_e1", "forecast_net_profit", "10", "CNY", "2025", "internal estimate"),
        ("fact_fake_e2", "forecast_net_profit", "20", "CNY", "2026", "internal estimate"),
        ("fact_fake_e3", "forecast_net_profit", "30", "CNY", "2027", "internal estimate"),
    ]
    facts = [{
        "fact_id": fact_id,
        "symbol": "000001.SZ",
        "metric": metric,
        "value": value,
        "unit": unit,
        "period": period,
        "formula": None,
        "input_fact_ids": [],
        "evidence_ids": [evidence_id],
        "calculation_version": "source-extraction-v1",
        "validation_status": "pass",
        "statement_type": None,
        "metadata": {"scope": scope},
    } for fact_id, metric, value, unit, period, scope in raw_inputs]
    service.attach_external_evidence(record.report_id, {"evidence": evidence, "facts": facts})
    result = implied_terminal_earnings(
        1000,
        10,
        20,
        30,
        currency="CNY",
        forecast_years=[2025, 2026, 2027],
        base_year=2024,
        source_fact_ids=[item[0] for item in raw_inputs],
        symbol="000001.SZ",
    )

    with pytest.raises(ValueError, match="internal estimates and extrapolations"):
        service.attach_deterministic_result(
            record.report_id,
            "implied_terminal_earnings",
            result,
        )


def test_terminal_scenario_results_extend_fact_ledger_without_weighting(tmp_path: Path) -> None:
    service = DeepReportService(tmp_path / "reports")
    record = service.begin(session_id="s1", attempt_id="a1", request_content="研究000001.SZ")
    analysis = normalize_financial_snapshot(
        symbol="000001.SZ",
        security_name="示例公司",
        market="a_share",
        currency="CNY",
        statement_rows=_rows(),
    )
    service.attach_analysis(record.report_id, analysis)
    raw_scenarios = [
        ("conservative", 100, 0.10, 0.10),
        ("base", 120, 0.15, 0.12),
        ("optimistic", 150, 0.20, 0.15),
        ("stretched", 180, 0.25, 0.18),
    ]
    evidence = []
    facts = []
    scenarios = []
    for scenario_id, tam, share, margin in raw_scenarios:
        evidence_id = f"ev_{scenario_id}"
        evidence.append({
            "evidence_id": evidence_id,
            "symbol": "000001.SZ",
            "domain": "tam",
            "source": "test-industry-source",
            "source_locator": f"https://example.test/{scenario_id}",
            "retrieved_at": "2025-04-01T00:00:00+00:00",
            "published_at": "2025-03-01",
            "content_hash": scenario_id,
            "summary": "scenario inputs",
            "status": "recorded_from_opened_source",
            "metadata": {},
        })
        source_ids = []
        for metric, value, unit in (
            ("tam", tam, "CNY"),
            ("long_term_market_share", share, "ratio"),
            ("steady_net_margin", margin, "ratio"),
        ):
            fact_id = f"fact_{scenario_id}_{metric}"
            source_ids.append(fact_id)
            facts.append({
                "fact_id": fact_id,
                "symbol": "000001.SZ",
                "metric": metric,
                "value": str(value),
                "unit": unit,
                "period": "2035",
                "formula": None,
                "input_fact_ids": [],
                "evidence_ids": [evidence_id],
                "calculation_version": "test-v1",
                "validation_status": "pass",
                "statement_type": None,
                "metadata": {},
            })
        scenarios.append({
            "scenario_id": scenario_id,
            "tam": tam,
            "market_share": share,
            "net_margin": margin,
            "source_fact_ids": source_ids,
        })
    service.attach_external_evidence(record.report_id, {"evidence": evidence, "facts": facts})
    result = validate_terminal_scenarios(
        scenarios,
        currency="CNY",
        tam_currency="CNY",
        symbol="000001.SZ",
        steady_year=2035,
    )
    service.attach_deterministic_result(record.report_id, "validate_terminal_scenarios", result)

    refreshed = service.require(record.report_id)
    assert refreshed.analysis_modules["terminal_scenarios"].status == "passed"
    ledger = (tmp_path / "reports" / record.report_id / "analysis" / "facts.jsonl").read_text(encoding="utf-8")
    assert "terminal_scenario_earnings" in ledger
    assert result["probability_weighted_result"] is None
