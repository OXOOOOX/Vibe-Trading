"""Formal Deep Report bridge for existing ETF P4A/P4B outputs."""

from __future__ import annotations

import json

from src.reports.contracts import (
    ComponentDigestResolution,
    ETFComponentSelection,
    ETFConcentrationMetrics,
    ModuleResult,
)
from src.reports.etf_research import ETFResearchStore, build_etf_snapshot
from src.reports.etf_universe_provider import ETFUniverseServiceResult
from src.reports.profile import get_report_profile
from src.reports.runtime import persist_report_event
from src.reports.service import (
    DeepReportService,
    _etf_penetration_markdown,
    _etf_penetration_view,
)
from src.tools.etf_research_context_tool import PrepareETFResearchTool


def _attach_etf_snapshot(service: DeepReportService, report_id: str) -> None:
    service.attach_etf_analysis(report_id, {
        "profile": "etf_deep_research",
        "symbol": "588870.SH",
        "security_name": "科创50ETF",
        "data_as_of": "2026-07-18T00:00:00+00:00",
        "snapshot": {
            "symbol": "588870.SH",
            "data_as_of": "2026-07-18T00:00:00+00:00",
            "snapshot_ids": {
                "identity": "etfsnap_aaaaaaaaaaaaaaaaaaaaaaaa",
                "universe": "etfsnap_bbbbbbbbbbbbbbbbbbbbbbbb",
                "market": "etfsnap_cccccccccccccccccccccccc",
            },
            "coverage_ratio": 1.0,
            "price_verified": True,
        },
        "evidence": [{
            "evidence_id": "evidence_etf_universe",
            "symbol": "588870.SH",
            "domain": "etf_universe",
            "source": "中证指数公司成分权重文件",
            "source_locator": "https://example.test/official-index-weights.csv",
            "retrieved_at": "2026-07-18T00:00:00+00:00",
            "published_at": "2026-06-30T00:00:00+00:00",
            "content_hash": "abc",
            "summary": "官方成分权重",
            "status": "verified",
        }],
    })


def _selection(*, selected: bool) -> dict:
    components = [] if not selected else [
        {
            "symbol": "688256.SH",
            "name": "寒武纪",
            "weight": 0.12,
            "score": 0.8,
            "marginal_explanation_gain": 0.12,
            "forced": False,
            "reasons": ["large_weight", "material_price_contribution"],
            "price_contribution": 0.15,
            "earnings_contribution": None,
        },
        {
            "symbol": "688041.SH",
            "name": "海光信息",
            "weight": 0.08,
            "score": 0.7,
            "marginal_explanation_gain": 0.08,
            "forced": False,
            "reasons": ["large_weight"],
            "price_contribution": 0.08,
            "earnings_contribution": None,
        },
    ]
    return {
        "selection_id": "p4aselection_test_bridge",
        "etf_symbol": "588870.SH",
        "input_fingerprint": "etfinput_test_bridge",
        "quality": "complete",
        "concentration": {
            "concentration_class": "focused",
            "expected_component_count": 50,
            "observed_component_count": 50,
            "observed_weight_coverage": 1.0,
            "top1_weight": 0.12,
            "top3_weight": 0.25,
            "top5_weight": 0.35,
            "top10_weight": 0.55,
            "hhi_lower_bound": 0.03,
            "hhi_upper_bound": 0.04,
            "effective_component_count_lower_bound": 25.0,
            "min_penetration_count": 0,
            "max_penetration_count": 5,
        },
        "selected": components,
        "selected_weight_coverage": sum(item["weight"] for item in components),
        "explanation_coverage": sum(
            item["marginal_explanation_gain"] for item in components
        ),
        "stop_reason": (
            "marginal_explanation_gain_below_5pct" if not selected else "max_count_reached"
        ),
        "warnings": [],
        "created_at": "2026-07-18T00:00:00+00:00",
    }


def _resolution(selection: dict) -> dict:
    bindings = [
        {
            "binding_id": f"componentbinding_{index}",
            "etf_symbol": "588870.SH",
            "selection_id": selection["selection_id"],
            "component_symbol": item["symbol"],
            "component_name": item["name"],
            "digest_id": None,
            "digest_status": "missing",
            "component_weight": item["weight"],
            "selection_score": item["score"],
            "marginal_explanation_gain": item["marginal_explanation_gain"],
            "forced": item["forced"],
            "selection_reasons": item["reasons"],
            "price_contribution": item["price_contribution"],
            "earnings_contribution": item["earnings_contribution"],
            "selected_rank": index,
            "selection_data_as_of": "2026-06-30T00:00:00+00:00",
            "created_at": "2026-07-18T00:00:00+00:00",
            "warnings": [],
        }
        for index, item in enumerate(selection["selected"], start=1)
    ]
    return {
        "resolution_id": "componentresolution_test_bridge",
        "etf_symbol": "588870.SH",
        "selection_id": selection["selection_id"],
        "analysis_as_of": "2026-07-18T00:00:00+00:00",
        "selected_count": len(bindings),
        "reusable_count": 0,
        "partial_reusable_count": 0,
        "stale_count": 0,
        "missing_count": len(bindings),
        "conflicted_count": 0,
        "bindings": bindings,
        "digest_ids": [],
        "reuse_ratio": 0.0,
        "estimated_avoided_model_calls": 0,
        "estimated_avoided_input_tokens": 0,
        "estimated_avoided_output_tokens": 0,
        "estimation_basis": "fixture",
        "knowledge_fingerprint": "knowledge_fixture",
        "input_fingerprint": "resolution_fixture",
        "warnings": [],
        "cache_hit": False,
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }


def _submit_non_penetration_sections(service: DeepReportService, report_id: str) -> None:
    for section_id, _heading in get_report_profile("etf_deep_research")["required_sections"]:
        if section_id == "holding_penetration":
            continue
        service.submit_section(
            report_id,
            section_id=section_id,
            body_markdown="本节仅陈述已核验的产品研究结论，不包含额外数字。",
        )


def test_penetration_compiler_preserves_nested_fact_lineage_and_filters_naked_numbers() -> None:
    context = {
        "etf_component_selection": {
            "selection_id": "p4aselection_test",
            "selected_weight_coverage": 0.092,
            "explanation_coverage": 0.092,
            "concentration": {"observed_weight_coverage": 1.0},
            "selected": [{
                "symbol": "688256.SH",
                "name": "寒武纪",
                "weight": 0.092,
                "reasons": ["weight_at_least_8pct"],
            }],
        },
        "component_digest_resolution": {
            "bindings": [{
                "component_symbol": "688256.SH",
                "component_name": "寒武纪",
                "digest_id": "componentdigest_test",
                "digest_status": "partial_reusable",
            }],
        },
        "component_research_digests": {
            "componentdigest_test": {
                "summaries_by_dimension": {
                    "unsupported_guidance": "收入预计增长159.56%。",
                    "earnings_trend": "营业收入为28.85亿元。",
                    "cashflow": "经营现金流为5,131,729千元。",
                },
                "claim_ids_by_dimension": {
                    "unsupported_guidance": ["claim_unsupported"],
                    "earnings_trend": ["claim_supported"],
                    "cashflow": ["claim_cashflow"],
                },
            },
        },
        "component_research_claims": [
            {
                "claim_id": "claim_unsupported",
                "fact_ids": [],
                "evidence_ids": ["evidence_guidance"],
            },
            {
                "claim_id": "claim_supported",
                "fact_ids": ["fact_component_revenue"],
                "evidence_ids": ["evidence_earnings"],
            },
            {
                "claim_id": "claim_cashflow",
                "fact_ids": ["fact_component_cashflow"],
                "evidence_ids": ["evidence_earnings"],
            },
        ],
        "facts": [
            {
                "fact_id": "fact_component_revenue",
                "metric": "revenue",
                "value": "2885000000",
                "unit": "CNY",
            },
            {
                "fact_id": "fact_component_cashflow",
                "metric": "operating_cashflow",
                "value": "5131729",
                "unit": "CNY_thousand",
            },
        ],
    }
    modules = {
        "holding_penetration": ModuleResult(
            status="insufficient_evidence",
            details={
                "deterministic_analysis": {
                    "details": {
                        "fact_ids": {
                            "observed_weight_coverage": "fact_observed",
                            "selected_weight_coverage": "fact_selected",
                            "explanation_coverage": "fact_explained",
                            "component_weights": {"688256.SH": "fact_weight"},
                        }
                    }
                }
            },
        ),
        "component_research": ModuleResult(
            status="warning",
            details={
                "research_coverage": 1.0,
                "fully_supported_coverage": 0.0,
                "coverage_fact_ids": {
                    "research_coverage": "fact_research",
                    "fully_supported_coverage": "fact_supported",
                },
            },
        ),
    }

    rendered = _etf_penetration_markdown(_etf_penetration_view(context, modules))

    assert "100.00% [Fact:fact_observed]" in rendered
    assert "9.20% [Fact:fact_weight]" in rendered
    assert "核心高权重成分" in rendered
    assert "权重不低于8%" not in rendered
    assert "营业收入为28.85亿元。 [Fact:fact_component_revenue]" in rendered
    assert "经营现金流为51.32亿元。 [Fact:fact_component_cashflow]" in rendered
    assert "159.56%" not in rendered


def test_missing_component_digests_are_a_publishable_local_gap(tmp_path) -> None:
    service = DeepReportService(tmp_path / "reports")
    record = service.begin(
        session_id="session-etf-bridge",
        attempt_id="attempt-etf-bridge",
        request_content="研究 588870.SH",
        profile="etf_deep_research",
    )
    _attach_etf_snapshot(service, record.report_id)
    selection = _selection(selected=True)
    service.attach_etf_component_selection(record.report_id, selection)
    facts = [
        json.loads(line)
        for line in (
            tmp_path / "reports" / record.report_id / "analysis" / "facts.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    component_facts = {
        item["metadata"]["component_symbol"]: item
        for item in facts
        if item.get("metric") == "etf_component_weight"
    }
    assert component_facts["688256.SH"]["scope_key"] == "688256.SH"
    assert component_facts["688256.SH"]["metadata"]["scope_key"] == "688256.SH"
    assert component_facts["688041.SH"]["scope_key"] == "688041.SH"
    service.attach_component_digest_resolution(record.report_id, _resolution(selection))
    _submit_non_penetration_sections(service, record.report_id)

    inspected = service.inspect_workspace(record.report_id)
    assert inspected["etf_penetration"]["selected_count"] == 2
    assert inspected["etf_penetration"]["status_counts"]["missing"] == 2
    assert inspected["analysis_modules"]["component_research"]["status"] == "warning"

    evaluation = service.evaluate_workspace(record.report_id)
    assert evaluation["validation"]["quality_status"] == "passed_with_gaps"
    assert "关键持仓穿透" in evaluation["content"]
    assert "尚无可复用研究" in evaluation["content"]
    assert "权重较高" in evaluation["content"]
    assert "不会在本次报告生成过程中用推测内容补齐" in evaluation["content"]

    published = service.publish_workspace(record.report_id, evaluation)
    assert published.quality_status == "passed_with_gaps"
    assert published.status == "completed"
    assert any(item["artifact_id"] == "markdown" for item in published.artifacts)
    assert any(item["artifact_id"] == "monitoring_bundle" for item in published.artifacts)
    assert not any(item["artifact_id"] == "diagnostic" for item in published.artifacts)
    monitoring_bundle = json.loads(
        (tmp_path / "reports" / record.report_id / "monitoring_bundle.json").read_text(
            encoding="utf-8"
        )
    )
    assert monitoring_bundle["instrument_type"] == "etf"
    assert monitoring_bundle["horizon"] == "structural"
    assert monitoring_bundle["monitoring_status"] == "not_recommended"
    assert monitoring_bundle["candidates"] == []
    assert monitoring_bundle["trade_execution"] == "forbidden"
    resolution_file = (
        tmp_path / "reports" / record.report_id / "analysis"
        / "component_digest_resolution.json"
    )
    assert json.loads(resolution_file.read_text(encoding="utf-8"))["model_calls"] == 0


def test_zero_p4a_selection_is_valid_and_needs_no_component_prose(tmp_path) -> None:
    service = DeepReportService(tmp_path / "reports")
    record = service.begin(
        session_id="session-etf-zero",
        attempt_id="attempt-etf-zero",
        request_content="研究 588870.SH",
        profile="etf_deep_research",
    )
    _attach_etf_snapshot(service, record.report_id)
    selection = _selection(selected=False)
    service.attach_etf_component_selection(record.report_id, selection)
    service.attach_component_digest_resolution(record.report_id, _resolution(selection))
    _submit_non_penetration_sections(service, record.report_id)

    evaluation = service.evaluate_workspace(record.report_id)
    # A zero-component P4A selection is valid, while this legacy fixture has no
    # bound ETF product profile and is therefore published only with an identity gap.
    assert evaluation["validation"]["quality_status"] == "passed_with_gaps"
    assert "继续逐只穿透的边际解释增益不足" in evaluation["content"]
    assert "本章节尚未通过" not in evaluation["content"]


def test_prepare_tool_persists_p4_without_starting_generation(tmp_path, monkeypatch) -> None:
    snapshot = build_etf_snapshot(
        symbol="588870.SH",
        snapshot_type="universe",
        data_as_of="2026-06-30T00:00:00+00:00",
        payload={
            "provider_id": "official-index",
            "source_type": "official_index_weights",
            "source_urls": ["https://example.test/index.csv"],
            "mapping": {"index_code": "000688", "index_name": "科创50"},
        },
        coverage_ratio=1.0,
        source_ids=["official:index:000688"],
    )
    selection = ETFComponentSelection(
        selection_id="p4aselection_prepare_tool",
        etf_symbol="588870.SH",
        input_fingerprint="etfinput_prepare_tool",
        quality="complete",
        concentration=ETFConcentrationMetrics(
            concentration_class="highly_diversified",
            expected_component_count=50,
            observed_component_count=50,
            observed_weight_coverage=1.0,
            top1_weight=0.05,
            top3_weight=0.12,
            top5_weight=0.18,
            top10_weight=0.31,
            hhi_lower_bound=0.025,
            hhi_upper_bound=0.03,
            effective_component_count_lower_bound=33.0,
            min_penetration_count=0,
            max_penetration_count=3,
        ),
        selected=[],
        selected_weight_coverage=0.0,
        explanation_coverage=0.0,
        stop_reason="marginal_explanation_gain_below_5pct",
    )
    universe_result = ETFUniverseServiceResult(
        etf_symbol="588870.SH",
        snapshot=snapshot,
        selection=selection,
        cache_hit=True,
        snapshot_reused=True,
        p4a_cache_hit=True,
        network_fetched=False,
        provider_id="official-index",
        source_type="official_index_weights",
        fallback_used=False,
        cache_fallback=False,
        attempts=[],
        warnings=[],
    )
    resolution = ComponentDigestResolution(
        resolution_id="componentresolution_prepare_tool",
        etf_symbol="588870.SH",
        selection_id=selection.selection_id,
        analysis_as_of="2026-07-18T00:00:00+00:00",
        selected_count=0,
        reusable_count=0,
        partial_reusable_count=0,
        stale_count=0,
        missing_count=0,
        conflicted_count=0,
        bindings=[],
        digest_ids=[],
        reuse_ratio=0.0,
        estimated_avoided_model_calls=0,
        estimated_avoided_input_tokens=0,
        estimated_avoided_output_tokens=0,
        estimation_basis="fixture",
        knowledge_fingerprint="knowledge_prepare_tool",
        input_fingerprint="resolution_prepare_tool",
    )

    class _Universe:
        def get_or_refresh(self, *args, **kwargs):
            return universe_result

    class _Components:
        def resolve_selection(self, *args, **kwargs):
            return resolution

        def materialize_resolution(self, value):
            assert value.resolution_id == resolution.resolution_id
            return {
                "resolution_id": resolution.resolution_id,
                "digests": {},
                "claims": [],
                "facts": [],
                "evidence": [],
                "model_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
            }

    monkeypatch.setattr(
        "src.tools.etf_research_context_tool.get_etf_universe_service",
        lambda: _Universe(),
    )

    class _InstrumentProfiles:
        def refresh(self, symbol):
            return {
                "snapshot_id": "instrumentsnap_fixture",
                "symbol": symbol,
                "data_as_of": "2026-07-17T15:00:00+08:00",
                "metrics": [],
                "sources": [],
            }

        def latest_snapshot(self, symbol):
            return None

    class _ProductProfiles:
        def get_or_refresh(self, symbol, **kwargs):
            field = lambda value, source="etfsource_fixture": {
                "value": value, "status": "available", "unit": None,
                "data_as_of": "2026-07-17", "source_ids": [source],
                "semantics": "fixture", "note": None,
            }
            return {
                "profile_snapshot_id": "etfprofile_fixture",
                "symbol": symbol,
                "data_as_of": "2026-07-17",
                "retrieved_at": "2026-07-18T00:00:00+00:00",
                "snapshot_ids": {
                    "identity": "etfsnap_identity_fixture000000",
                    "index_methodology": "etfsnap_method_fixture00000000",
                    "product_metrics": "etfsnap_metrics_fixture000000",
                },
                "identity": {
                    "manager": field("汇添富基金管理股份有限公司"),
                    "custodian": field("中信证券股份有限公司"),
                    "exchange": field("上海证券交易所"),
                    "tracked_index_code": field("000688.SH"),
                    "tracked_index_name": field("上证科创板50成份指数"),
                },
                "index_methodology": {
                    "version": field("V1.1"),
                    "source_url": field("https://example.test/methodology.pdf"),
                },
                "product_metrics": {},
                "share_history": None,
                "peer_group": {"unit_change_coverage_ratio": 1.0},
                "sources": [{
                    "source_id": "etfsource_fixture", "kind": "fund_product",
                    "title": "ETF 产品资料", "publisher": "官方来源",
                    "url": "https://example.test/product", "content_hash": "abc",
                    "retrieved_at": "2026-07-18T00:00:00+00:00",
                    "verification_status": "official_primary", "body_status": "full_text",
                }],
                "hard_gate_status": "passed", "quality_status": "passed",
                "missing_hard_fields": [], "missing_optional_fields": [],
                "conflicts": [], "refresh_errors": [], "refresh_status": "completed",
            }

        def to_report_records(self, profile):
            return ([{
                "fact_id": "fact_manager_fixture", "symbol": "588870.SH",
                "metric": "manager", "value": "汇添富基金管理股份有限公司",
                "unit": "text", "period": "2026-07-17", "formula": None,
                "input_fact_ids": [], "evidence_ids": ["etfsource_fixture"],
                "calculation_version": "fixture", "validation_status": "pass",
                "metadata": {},
            }], [{
                "evidence_id": "etfsource_fixture", "symbol": "588870.SH",
                "domain": "fund_product", "source": "官方来源",
                "source_locator": "https://example.test/product",
                "retrieved_at": "2026-07-18T00:00:00+00:00",
                "published_at": None, "content_hash": "abc", "summary": "ETF 产品资料",
                "status": "verified", "metadata": {},
            }])
    monkeypatch.setattr(
        "src.tools.etf_research_context_tool.get_component_research_service",
        lambda: _Components(),
    )
    monkeypatch.setattr(
        "src.tools.etf_research_context_tool.VerifiedMarketDataTool.execute",
        lambda self, **kwargs: json.dumps({
            "status": "completed",
            "results": {
                "588870.SH": {
                    "status": "verified",
                    "consensus_close": 1.741,
                    "bar_time": "2026-07-17T15:00:00+08:00",
                    "verified_at": "2026-07-18T00:00:00+00:00",
                    "sources": ["eastmoney", "tencent"],
                    "retrieval": {"mode": "live_only"},
                }
            },
        }),
    )

    service = DeepReportService(tmp_path / "reports")
    record = service.begin(
        session_id="session-prepare-tool",
        attempt_id="attempt-prepare-tool",
        request_content="研究 588870.SH",
        profile="etf_deep_research",
    )

    def callback(event_type, data):
        assert persist_report_event(
            service,
            record.report_id,
            "etf_deep_research",
            event_type,
            data,
        )

    result = json.loads(PrepareETFResearchTool(
        event_callback=callback,
        product_profile_service=_ProductProfiles(),
        instrument_profile_service=_InstrumentProfiles(),
        research_store=ETFResearchStore(tmp_path / "research.sqlite3"),
    ).execute(
        symbol="588870.SH",
        security_name="科创50ETF",
        as_of="2026-07-18T00:00:00+00:00",
    ))
    assert result["status"] == "ok"
    assert result["p4b2_generation_started"] is False
    assert result["model_calls"] == 0
    assert result["subject_profile_snapshot_id"] == "etfprofile_fixture"
    inspected = service.inspect_workspace(record.report_id)
    assert inspected["analysis_available"] is True
    assert inspected["etf_penetration"]["selection_id"] == selection.selection_id
    assert inspected["etf_penetration"]["resolution_id"] == resolution.resolution_id
    assert inspected["subject_profile"]["identity"]["tracked_index_code"]["value"] == "000688.SH"
