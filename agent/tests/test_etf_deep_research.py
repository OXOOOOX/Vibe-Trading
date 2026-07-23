"""P0-P3 contracts and reuse behavior for ETF Deep Research."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.reports import (
    ETFAnalysisRouter,
    ETFResearchStore,
    build_deep_research_prompt,
    build_etf_snapshot,
    get_report_profile,
    module_input_fingerprint,
    snapshot_is_reusable,
)
from src.reports.contracts import DeepReportRecord
from src.reports.service import DeepReportService


FIXTURE = Path(__file__).parent / "fixtures" / "588870_etf_research_scenarios.json"


def _market_snapshot(*, price: float = 1.0, data_as_of: str = "2026-07-18T02:00:00+00:00"):
    return build_etf_snapshot(
        symbol="588870.SH",
        snapshot_type="market",
        data_as_of=data_as_of,
        payload={"last_price": price, "price_verified": True, "volume_ratio": 1.2},
        coverage_ratio=1.0,
        source_ids=["market_cache:588870.SH"],
        fact_ids=["fact_etf_price"],
        evidence_ids=["evidence_market_cache"],
        freshness_expires_at="2026-07-18T03:00:00+00:00",
    )


def test_registered_etf_profile_and_prompt_are_independent_from_equity_financial_gate() -> None:
    profile = get_report_profile("etf_deep_research")
    assert [item[0] for item in profile["required_sections"]] == [
        "executive_summary",
        "index_and_product",
        "exposure_structure",
        "aggregate_fundamentals",
        "price_volume_structure",
        "flow_liquidity_tracking",
        "holding_penetration",
        "scenarios_watchlist",
    ]
    prompt = build_deep_research_prompt("etf_deep_research", "研究 588870.SH")
    assert "ETF_DEEP_RESEARCH_PROFILE" in prompt
    assert "不得套用上市公司三张财务报表" in prompt
    assert "24,000 tokens" in prompt
    with pytest.raises(ValueError, match="unsupported report profile"):
        get_report_profile("unknown_profile")


def test_etf_repair_prompt_requires_resubmitting_globally_rejected_section() -> None:
    prompt = build_deep_research_prompt(
        "etf_deep_research",
        "修复父报告的发布审查问题",
        parent_report_id="report_parent",
        revision_sections=["flow_liquidity_tracking"],
        revision_mode="repair",
    )

    assert "必须 inspect 并重新提交 flow_liquidity_tracking" in prompt
    assert "章节显示 status=passed" in prompt
    assert "claim_support_gate" in prompt
    assert "禁止仅修改 Claim 类型来绕过审查" in prompt


def test_snapshot_has_stable_id_is_idempotent_and_fails_closed_for_stale_price(tmp_path) -> None:
    store = ETFResearchStore(tmp_path / "research_cache.sqlite3")
    first = _market_snapshot()
    duplicate = _market_snapshot()
    assert duplicate.snapshot_id == first.snapshot_id
    assert duplicate.content_hash == first.content_hash

    stored, reused = store.save_snapshot(first)
    repeated, repeated_reused = store.save_snapshot(duplicate)
    changed, changed_reused = store.save_snapshot(_market_snapshot(price=1.01))
    assert reused is False
    assert repeated_reused is True
    assert repeated.snapshot_id == stored.snapshot_id
    assert changed_reused is False
    assert changed.snapshot_id != stored.snapshot_id
    assert snapshot_is_reusable(
        stored,
        now="2026-07-18T02:10:00+00:00",
        price_sensitive=True,
    )
    assert not snapshot_is_reusable(
        stored,
        now="2026-07-18T03:00:01+00:00",
        price_sensitive=True,
    )

    weak = build_etf_snapshot(
        symbol="588870.SH",
        snapshot_type="market",
        data_as_of="2026-07-18T02:00:00+00:00",
        payload={"last_price": 1.0, "price_verified": False},
        coverage_ratio=0.4,
        source_ids=["single_source"],
    )
    assert weak.quality_status == "failed_validation"
    assert not snapshot_is_reusable(weak, price_sensitive=True)


def test_588870_router_fixture_selects_smallest_sufficient_refresh() -> None:
    scenarios = json.loads(FIXTURE.read_text(encoding="utf-8"))
    router = ETFAnalysisRouter()
    for name in ("unchanged", "market_delta", "structural_change"):
        case = scenarios[name]
        decision = router.decide(
            symbol=scenarios["symbol"],
            changed_snapshot_types=case["changed_snapshot_types"],
            trigger_flags=case["trigger_flags"],
            prior_report_id="report_1111111111111111",
        )
        assert decision.mode == case["expected_mode"]
        if "expected_modules" in case:
            assert decision.refresh_modules == case["expected_modules"]
        if "expected_sections" in case:
            assert decision.stale_sections == case["expected_sections"]

    initial = router.decide(symbol=scenarios["symbol"])
    assert initial.mode == "full_refresh"
    assert initial.reused_report_id is None


def test_decision_audit_records_reuse_without_creating_report_artifact(tmp_path) -> None:
    store = ETFResearchStore(tmp_path / "research_cache.sqlite3")
    decision = ETFAnalysisRouter().decide(
        symbol="588870.SH",
        prior_report_id="report_1111111111111111",
    )
    store.record_decision(decision)
    metrics = store.baseline_metrics("588870.SH")
    assert metrics["decision_counts"] == {"reuse": 1}
    assert metrics["cache_hits"] == 1
    assert not (tmp_path / "reports").exists()


def test_module_cache_and_single_flight_run_same_input_once(tmp_path) -> None:
    store = ETFResearchStore(tmp_path / "research_cache.sqlite3")
    fingerprint = module_input_fingerprint(
        module_id="price_volume",
        snapshot_ids=[_market_snapshot().snapshot_id],
    )
    calls = 0
    calls_lock = threading.Lock()

    def runner():
        nonlocal calls
        with calls_lock:
            calls += 1
        return {
            "status": "passed",
            "signal": "volume_expansion",
            "input_tokens": 800,
            "output_tokens": 120,
        }

    def execute():
        return store.execute_module(
            symbol="588870.SH",
            module_id="price_volume",
            input_fingerprint=fingerprint,
            runner=runner,
            model_id="test-model",
            estimated_input_tokens=800,
            estimated_output_tokens=120,
        )

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(lambda _: execute(), range(6)))

    assert calls == 1
    assert sum(1 for _result, cache_hit in results if cache_hit) == 5
    assert len({result.cache_id for result, _cache_hit in results}) == 1
    metrics = store.baseline_metrics("588870.SH")
    assert metrics["module_runs"] == 1
    assert metrics["model_runs"] == 1
    assert metrics["deterministic_runs"] == 0
    assert metrics["cache_hits"] == 5
    assert metrics["saved_tokens"] == 5 * 920


def test_etf_report_workspace_uses_etf_sections_and_snapshot_gate(tmp_path) -> None:
    service = DeepReportService(tmp_path / "reports")
    record = service.begin(
        session_id="session-etf",
        attempt_id="attempt-etf",
        request_content="研究 588870.SH",
        profile="etf_deep_research",
    )
    assert set(record.analysis_modules) == {
        key for key, _heading in get_report_profile("etf_deep_research")["required_sections"]
    }
    with pytest.raises(
        ValueError, match="etf_analysis_snapshot_required_before_section_submission"
    ):
        service.submit_section(
            record.report_id,
            section_id="product_index_profile",
            body_markdown="准备失败后不得提交正文。",
        )
    with pytest.raises(
        ValueError, match="etf_analysis_snapshot_required_before_monitoring_submission"
    ):
        service.submit_monitoring_bundle(record.report_id, monitoring_bundle={})
    attached = service.attach_etf_analysis(record.report_id, {
        "profile": "etf_deep_research",
        "symbol": "588870.SH",
        "security_name": "科创板 50 ETF",
        "data_as_of": "2026-07-18T02:00:00+00:00",
        "snapshot": {
            "symbol": "588870.SH",
            "data_as_of": "2026-07-18T02:00:00+00:00",
            "snapshot_ids": {
                "identity": "etfsnap_aaaaaaaaaaaaaaaaaaaaaaaa",
                "universe": "etfsnap_bbbbbbbbbbbbbbbbbbbbbbbb",
                "market": "etfsnap_cccccccccccccccccccccccc",
            },
            "coverage_ratio": 0.98,
            "price_verified": True,
        },
        "facts": [],
        "evidence": [],
    })
    assert attached.profile == "etf_deep_research"
    workspace = service.inspect_workspace(record.report_id)
    assert "price_volume_structure" in workspace["sections"]
    assert "financial_quality" not in workspace["sections"]
    assert workspace["analysis_available"] is True
    with pytest.raises(ValueError, match="financial analysis is only valid"):
        service.attach_analysis(record.report_id, {"status": "ok"})

    rejected_path = (
        tmp_path
        / "reports"
        / record.report_id
        / "workspace"
        / "rejected_sections"
        / "executive_summary.json"
    )
    with pytest.raises(ValueError, match="unknown_fact_reference"):
        service.submit_section(
            record.report_id,
            section_id="executive_summary",
            body_markdown="Unsupported value [Fact:fact_missing].",
        )
    assert rejected_path.exists()
    service.submit_section(
        record.report_id,
        section_id="executive_summary",
        body_markdown=(
            "The available evidence does not support a complete conclusion. "
            "[data_gap]"
        ),
    )
    assert not rejected_path.exists()

    headings = [heading for _key, heading in get_report_profile("etf_deep_research")["required_sections"]]
    draft = "\n\n".join([
        "# 科创板 50 ETF（588870.SH）穿透式深度研究",
        *[f"## {heading}\n\n本节暂不包含数字。" for heading in headings],
        "## 数据缺口与方法说明\n\n当前仅验证 Profile 门控。",
    ])
    validation = service.validate(draft, profile="etf_deep_research")
    assert "financial_analysis_snapshot_missing" not in validation["issues"]
    assert "title_must_include_company_and_symbol" not in validation["issues"]


def test_etf_report_begin_preserves_confirmed_subject_before_snapshot(tmp_path) -> None:
    service = DeepReportService(tmp_path / "reports")

    record = service.begin(
        session_id="session-confirmed-subject",
        attempt_id="attempt-confirmed-subject",
        request_content="研究对象已由用户确认：半导体设备ETF国泰（159516.SZ）。",
        profile="etf_deep_research",
        symbol="159516.SZ",
        security_name="半导体设备ETF国泰",
        security_name_source="user_confirmed",
    )

    assert record.symbol == "159516.SZ"
    assert record.security_name == "半导体设备ETF国泰"
    assert record.security_name_source == "user_confirmed"
    inspected = service.inspect_workspace(record.report_id)
    assert inspected["symbol"] == "159516.SZ"
    assert inspected["security_name"] == "半导体设备ETF国泰"


def test_legacy_failed_etf_manifest_recovers_confirmed_subject() -> None:
    record = DeepReportRecord.from_dict({
        "profile": "etf_deep_research",
        "symbol": "",
        "security_name": "",
        "request_content": (
            "研究对象已由用户确认：SEMI EQUIPMENT ETF（159516.SZ）。\n"
            "用户原始请求：159516"
        ),
    })

    assert record.symbol == "159516.SZ"
    assert record.security_name == "SEMI EQUIPMENT ETF"
    assert record.security_name_source == "user_confirmed"


def test_token_budget_stops_oversized_module_before_runner(tmp_path) -> None:
    store = ETFResearchStore(tmp_path / "research_cache.sqlite3")
    called = False

    def runner():
        nonlocal called
        called = True
        return {"status": "passed"}

    with pytest.raises(ValueError, match="input token budget exceeded"):
        store.execute_module(
            symbol="588870.SH",
            module_id="holding_penetration",
            input_fingerprint="etfinput_budget",
            runner=runner,
            estimated_input_tokens=24_001,
        )
    assert called is False
