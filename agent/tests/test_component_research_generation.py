"""P4B2 controlled generation, budget, publication, and isolation tests."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.component_research_generation_routes import (
    register_component_research_generation_routes,
)
from src.reports.component_research import (
    ComponentResearchDigestService,
    ComponentResearchDigestStore,
    normalize_component_symbol,
)
from src.reports.component_research_generation import (
    PILOT_AUTHORIZATION_TEXT,
    PILOT_CODEX_SOFT_LIMIT_AUTHORIZATION_TEXT,
    PILOT_COMPONENT_SYMBOLS,
    PILOT_EXPANDED_OUTPUT_AUTHORIZATION_TEXT,
    PILOT_FEATURE_FIRST_AUTHORIZATION_TEXT,
    ComponentResearchEvidencePackBuilder,
    ComponentResearchGenerationService,
    ComponentResearchGenerationStore,
    component_research_generation_policy,
    prepare_component_research_live_database,
    validate_pilot_authorization,
)
from src.reports.contracts import (
    ComponentResearchGenerationJob,
    ETFComponentSelection,
    ETFConcentrationMetrics,
    ETFSelectedComponent,
)
from src.reports.etf_research import stable_fingerprint
from src.research.knowledge import ResearchKnowledgeStore


ANALYSIS_AS_OF = "2026-07-18T23:00:00+00:00"
NOW = "2026-07-18T14:00:00+00:00"
SELECTION_DATA_AS_OF = "2026-07-18T10:00:00+00:00"
NAMES = {
    "688256.SH": "寒武纪",
    "688041.SH": "海光信息",
    "688981.SH": "中芯国际",
    "688008.SH": "澜起科技",
}


def _authorization() -> dict[str, object]:
    return {
        "authorization_text": PILOT_AUTHORIZATION_TEXT,
        "etf_symbol": "588870.SH",
        "component_symbols": list(PILOT_COMPONENT_SYMBOLS),
        "max_model_calls": 3,
        "max_input_tokens": 18000,
        "max_output_tokens": 1800,
        "max_auto_repairs": 0,
    }


def _expanded_authorization() -> dict[str, object]:
    return {
        **_authorization(),
        "authorization_text": PILOT_EXPANDED_OUTPUT_AUTHORIZATION_TEXT,
        "max_output_tokens": 3000,
    }


def _codex_soft_authorization() -> dict[str, object]:
    return {
        **_expanded_authorization(),
        "authorization_text": PILOT_CODEX_SOFT_LIMIT_AUTHORIZATION_TEXT,
    }


def _feature_first_authorization() -> dict[str, object]:
    return {
        **_expanded_authorization(),
        "authorization_text": PILOT_FEATURE_FIRST_AUTHORIZATION_TEXT,
        "max_model_calls": -1,
        "max_output_tokens": -1,
    }


def _concentration(count: int) -> ETFConcentrationMetrics:
    return ETFConcentrationMetrics(
        concentration_class="concentrated",
        expected_component_count=max(count, 1),
        observed_component_count=max(count, 1),
        observed_weight_coverage=1.0 if count else 0.0,
        top1_weight=0.3 if count else 0.0,
        top3_weight=0.6 if count else 0.0,
        top5_weight=1.0 if count else 0.0,
        top10_weight=1.0 if count else 0.0,
        hhi_lower_bound=0.2 if count else 0.0,
        hhi_upper_bound=0.3 if count else 0.0,
        effective_component_count_lower_bound=float(max(count, 1)),
        min_penetration_count=0,
        max_penetration_count=5,
    )


def _selection(
    symbols: tuple[str, ...] = PILOT_COMPONENT_SYMBOLS,
    *,
    etf_symbol: str = "588870.SH",
) -> ETFComponentSelection:
    selected = [
        ETFSelectedComponent(
            symbol=symbol,
            name=NAMES.get(symbol, symbol),
            weight=round(0.12 - index * 0.01, 6),
            score=round(0.9 - index * 0.05, 6),
            marginal_explanation_gain=round(0.12 - index * 0.01, 6),
            forced=True,
            reasons=["p4a_forced_high_weight"],
        )
        for index, symbol in enumerate(symbols)
    ]
    fingerprint = stable_fingerprint(
        "p4b2testselection", [etf_symbol, [item.to_dict() for item in selected]]
    )
    return ETFComponentSelection(
        selection_id=stable_fingerprint("p4aselection", fingerprint),
        etf_symbol=etf_symbol,
        input_fingerprint=fingerprint,
        quality="complete",
        concentration=_concentration(len(selected)),
        selected=selected,
        selected_weight_coverage=sum(item.weight for item in selected),
        explanation_coverage=sum(item.marginal_explanation_gain for item in selected),
        stop_reason="fixture",
        created_at=SELECTION_DATA_AS_OF,
    )


def _seed_evidence(
    knowledge: ResearchKnowledgeStore,
    symbol: str,
    *,
    include_business: bool = True,
    include_earnings: bool = True,
    include_risk: bool = True,
    published_at: str = "2026-07-18T09:00:00+00:00",
) -> None:
    rows = []
    if include_business:
        rows.append(("identity_market", "主营业务、产品和行业暴露已经由公司披露核验"))
    if include_earnings:
        rows.append(("financial_statements", "最近报告期营收、利润和经营趋势已经核验"))
    if include_risk:
        rows.append(("catalysts_risks", "主要风险、反向证据和失效条件已经披露"))
    for ordinal, (domain, summary) in enumerate(rows):
        document = knowledge.store_document(
            url=f"https://www.sse.com.cn/{symbol}/{domain}/{ordinal}",
            content=summary,
            title=f"{symbol}-{domain}",
            publisher="上海证券交易所",
            published_at=published_at,
        )
        evidence_id = f"evidence_{symbol.replace('.', '_')}_{domain}"
        knowledge.register_bundle(
            {
                "evidence": [
                    {
                        "evidence_id": evidence_id,
                        "symbol": symbol,
                        "domain": domain,
                        "status": "verified",
                        "summary": summary,
                        "published_at": published_at,
                        "metadata": {
                            "document_ref": document.document_ref,
                            "source_strength": "A",
                        },
                    }
                ],
                "facts": [
                    {
                        "fact_id": f"fact_{symbol.replace('.', '_')}_{domain}",
                        "symbol": symbol,
                        "metric": (
                            "revenue_trend" if domain == "financial_statements" else domain
                        ),
                        "value": "verified",
                        "unit": "",
                        "period": "2026Q2",
                        "evidence_ids": [evidence_id],
                        "validation_status": "pass",
                    }
                ],
            }
        )
        # Publication and ledger creation are both frozen before ANALYSIS_AS_OF;
        # otherwise this historical fixture becomes "future" as wall time moves on.
        with knowledge.connect() as connection:
            connection.execute(
                "UPDATE evidence_records SET created_at=? WHERE evidence_id=?",
                (published_at, evidence_id),
            )
            connection.execute(
                "UPDATE fact_records SET created_at=? WHERE fact_id=?",
                (published_at, f"fact_{symbol.replace('.', '_')}_{domain}"),
            )
            connection.commit()


def _fake_runner(counter: dict[str, int], *, omit_evidence: bool = False):
    def runner(**kwargs):
        counter["calls"] = counter.get("calls", 0) + 1
        payload = json.loads(kwargs["messages"][1]["content"])
        context = payload["allowlisted_context"]
        pack = payload["evidence_pack"]
        by_domain = {item["domain"]: item["evidence_id"] for item in context["evidence"]}

        def claim(text: str, dimension: str, evidence_id: str) -> dict[str, object]:
            return {
                "text": text,
                "dimension": dimension,
                "stance": "neutral",
                "confidence": "medium",
                "evidence_ids": [] if omit_evidence else [evidence_id],
                "fact_ids": [],
                "valid_until": None,
                "invalidation_conditions": ["新披露与当前证据相反"],
            }

        output = {
            "component_symbol": context["component_symbol"],
            "analysis_as_of": pack["analysis_as_of"],
            "research_data_as_of": "2026-07-18T09:00:00+00:00",
            "business_exposure_summary": claim(
                "业务暴露来自正式披露",
                "business_exposure",
                by_domain["identity_market"],
            ),
            "earnings_trend_summary": claim(
                "经营趋势来自最近有效报告期",
                "earnings_trend",
                by_domain["financial_statements"],
            ),
            "catalyst_claims": [],
            "risk_claims": [
                claim("风险和反向证据已保留", "risks", by_domain["catalysts_risks"])
            ],
            "material_event_claims": [],
            "valuation_claims": [],
            "holder_governance_claims": [],
            "invalidation_conditions": ["核心证据失效"],
            "coverage_dimensions": ["business_exposure", "earnings_trend", "risks"],
            "missing_dimensions": [],
            "warnings": [],
        }
        return {
            "content": json.dumps(output, ensure_ascii=False),
            "usage": {"input_tokens": 500, "output_tokens": 120},
        }

    return runner


def _services(
    tmp_path: Path,
    *,
    symbols: tuple[str, ...] = PILOT_COMPONENT_SYMBOLS,
    seed: bool = True,
    live: bool = False,
    model_runner=None,
):
    knowledge = ResearchKnowledgeStore(
        path=tmp_path / "research_cache.sqlite3",
        object_dir=tmp_path / "objects",
    )
    if seed:
        for symbol in symbols:
            _seed_evidence(knowledge, symbol)
    digest_store = ComponentResearchDigestStore(knowledge_store=knowledge)
    digest_service = ComponentResearchDigestService(
        knowledge,
        store=digest_store,
        now_provider=lambda: NOW,
    )
    selection = _selection(symbols)
    resolution = digest_service.resolve_selection(
        selection,
        ANALYSIS_AS_OF,
        selection_data_as_of=SELECTION_DATA_AS_OF,
    )
    generation_store = ComponentResearchGenerationStore(
        knowledge_store=knowledge,
        auto_initialize=True,
    )
    policy = component_research_generation_policy({})
    if live:
        policy = replace(policy, enabled=True, live_run_enabled=True)
    generation = ComponentResearchGenerationService(
        knowledge_store=knowledge,
        store=generation_store,
        digest_service=digest_service,
        policy=policy,
        model_runner=model_runner,
        model_id="fake-component-model",
        now_provider=lambda: NOW,
    )
    return knowledge, digest_service, generation, selection, resolution


def test_policy_defaults_fail_closed_and_hard_limits_are_clamped() -> None:
    policy = component_research_generation_policy(
        {
            "ETF_COMPONENT_RESEARCH_GENERATION_ENABLED": "0",
            "ETF_COMPONENT_RESEARCH_LIVE_RUN_ENABLED": "0",
            "ETF_COMPONENT_RESEARCH_MAX_COMPONENTS_PER_DAY": "99",
            "ETF_COMPONENT_RESEARCH_MAX_INPUT_TOKENS_PER_COMPONENT": "99999",
            "ETF_COMPONENT_RESEARCH_MAX_OUTPUT_TOKENS_PER_COMPONENT": "99999",
        }
    )
    assert policy.enabled is False
    assert policy.live_run_enabled is False
    assert policy.max_components_per_etf_run == 3
    assert policy.max_components_per_day == 5
    assert policy.max_model_calls_per_component == 1
    assert policy.max_input_tokens_per_component == 6000
    assert policy.max_output_tokens_per_component == 1000
    assert policy.max_auto_repairs == 0


def test_exact_pilot_authorization_rejects_any_scope_or_budget_drift() -> None:
    from src.reports.component_research_generation import ComponentResearchAuthorization

    valid, reasons = validate_pilot_authorization(
        ComponentResearchAuthorization.from_value(_authorization())
    )
    assert valid is True
    assert reasons == []
    changed = _authorization()
    changed["component_symbols"] = ["688256.SH"]
    valid, reasons = validate_pilot_authorization(
        ComponentResearchAuthorization.from_value(changed)
    )
    assert valid is False
    assert "authorization_component_scope_mismatch" in reasons


def test_expanded_output_authorization_is_exact_and_keeps_input_scope_fixed() -> None:
    from src.reports.component_research_generation import ComponentResearchAuthorization

    valid, reasons = validate_pilot_authorization(
        ComponentResearchAuthorization.from_value(_expanded_authorization())
    )
    assert valid is True
    assert reasons == []

    mismatched = _expanded_authorization()
    mismatched["max_output_tokens"] = 1800
    valid, reasons = validate_pilot_authorization(
        ComponentResearchAuthorization.from_value(mismatched)
    )
    assert valid is False
    assert "authorization_output_token_limit_mismatch" in reasons


def test_codex_soft_limit_authorization_is_exact() -> None:
    from src.reports.component_research_generation import ComponentResearchAuthorization

    valid, reasons = validate_pilot_authorization(
        ComponentResearchAuthorization.from_value(_codex_soft_authorization())
    )
    assert valid is True
    assert reasons == []

    changed = _codex_soft_authorization()
    changed["max_model_calls"] = 2
    valid, reasons = validate_pilot_authorization(
        ComponentResearchAuthorization.from_value(changed)
    )
    assert valid is False
    assert "authorization_model_call_limit_mismatch" in reasons


def test_feature_first_authorization_uses_explicit_unlimited_sentinels() -> None:
    from src.reports.component_research_generation import ComponentResearchAuthorization

    valid, reasons = validate_pilot_authorization(
        ComponentResearchAuthorization.from_value(_feature_first_authorization())
    )
    assert valid is True
    assert reasons == []

    changed = _feature_first_authorization()
    changed["max_output_tokens"] = 3000
    valid, reasons = validate_pilot_authorization(
        ComponentResearchAuthorization.from_value(changed)
    )
    assert valid is False
    assert "authorization_output_token_limit_mismatch" in reasons


def test_p4b2_reresolution_reconstructs_only_authorized_plan_scope(tmp_path) -> None:
    symbols = (*PILOT_COMPONENT_SYMBOLS, "688008.SH")
    _, _, generation, _, resolution = _services(tmp_path, symbols=symbols)

    reconstructed = generation._selection_from_resolution(
        resolution,
        allowed_symbols=set(PILOT_COMPONENT_SYMBOLS),
    )

    assert [item.symbol for item in reconstructed.selected] == list(
        PILOT_COMPONENT_SYMBOLS
    )
    assert "688008.SH" not in {
        item.symbol for item in reconstructed.selected
    }


def test_dry_run_is_stable_has_zero_calls_and_only_uses_p4a_scope(tmp_path) -> None:
    calls: dict[str, int] = {}
    _, _, generation, _, resolution = _services(
        tmp_path, model_runner=_fake_runner(calls)
    )
    first = generation.create_plan(
        resolution,
        requested_components=list(PILOT_COMPONENT_SYMBOLS),
        dry_run=True,
    )
    second = generation.create_plan(
        resolution,
        requested_components=list(PILOT_COMPONENT_SYMBOLS),
        dry_run=True,
    )
    assert first.plan_id == second.plan_id
    assert first.authorized is False
    assert "dry_run_only" in first.warnings
    assert first.planned_count == 3
    assert first.estimated_model_calls == 3
    assert calls.get("calls", 0) == 0
    with pytest.raises(ValueError, match="P4A did not select"):
        generation.create_plan(
            resolution,
            requested_components=["688008.SH"],
            dry_run=True,
        )


def test_incomplete_or_future_evidence_blocks_model_call(tmp_path) -> None:
    knowledge, _, generation, _, resolution = _services(tmp_path, seed=False)
    _seed_evidence(knowledge, "688256.SH", include_risk=False)
    _seed_evidence(
        knowledge,
        "688041.SH",
        published_at="2026-07-19T09:00:00+00:00",
    )
    plan = generation.create_plan(
        resolution,
        requested_components=["688256.SH", "688041.SH"],
        dry_run=True,
    )
    assert plan.planned_count == 0
    assert all(job.status == "blocked" for job in plan.jobs)
    packs = [generation.get_evidence_pack(job.evidence_pack_id) for job in plan.jobs]
    assert packs[0] is not None and "missing_risk_counterevidence" in packs[0].warnings
    assert packs[1] is not None and any(
        item.startswith("future_evidence_excluded") for item in packs[1].warnings
    )


def test_name_matching_is_rejected_and_hk_remains_code_isolated(tmp_path) -> None:
    with pytest.raises(ValueError):
        normalize_component_symbol("寒武纪")
    knowledge = ResearchKnowledgeStore(
        path=tmp_path / "research_cache.sqlite3", object_dir=tmp_path / "objects"
    )
    _seed_evidence(knowledge, "06160.HK")
    hk_pack, _ = ComponentResearchEvidencePackBuilder(knowledge).build(
        component_symbol="06160.HK",
        security_name="百济神州",
        analysis_as_of=ANALYSIS_AS_OF,
        selection_id="selection_hk",
        resolution_id="resolution_hk",
    )
    a_pack, _ = ComponentResearchEvidencePackBuilder(knowledge).build(
        component_symbol="688235.SH",
        security_name="百济神州",
        analysis_as_of=ANALYSIS_AS_OF,
        selection_id="selection_a",
        resolution_id="resolution_a",
    )
    assert hk_pack.evidence_ids
    assert a_pack.evidence_ids == []


def test_empty_p4a_selection_creates_zero_jobs_and_zero_budget(tmp_path) -> None:
    knowledge, digest, generation, _, _ = _services(tmp_path, symbols=(), seed=False)
    empty = _selection((), etf_symbol="560010.SH")
    resolution = digest.resolve_selection(
        empty, ANALYSIS_AS_OF, selection_data_as_of=SELECTION_DATA_AS_OF
    )
    plan = generation.create_plan(resolution, requested_components=[], dry_run=True)
    assert plan.candidate_count == 0
    assert plan.planned_count == 0
    assert plan.estimated_model_calls == 0
    assert plan.estimated_input_tokens == 0
    assert generation.store.budget_usage()["model_calls"] == 0
    assert knowledge.path == tmp_path / "research_cache.sqlite3"


def test_live_plan_requires_gates_authorization_and_exact_three_component_scope(tmp_path) -> None:
    _, _, generation, _, resolution = _services(tmp_path)
    with pytest.raises(PermissionError):
        generation.create_plan(
            resolution,
            requested_components=list(PILOT_COMPONENT_SYMBOLS),
            dry_run=False,
            authorization=None,
        )
    _, _, live_generation, _, live_resolution = _services(
        tmp_path / "live", live=True, model_runner=_fake_runner({})
    )
    with pytest.raises(PermissionError, match="scope"):
        live_generation.create_plan(
            live_resolution,
            requested_components=["688256.SH"],
            dry_run=False,
            authorization=_authorization(),
        )


def test_successful_pilot_publishes_unified_knowledge_and_reresolves_p4b1(tmp_path) -> None:
    calls: dict[str, int] = {}
    knowledge, _, generation, _, resolution = _services(
        tmp_path,
        live=True,
        model_runner=_fake_runner(calls),
    )
    plan = generation.create_plan(
        resolution,
        requested_components=list(PILOT_COMPONENT_SYMBOLS),
        dry_run=False,
        authorization=_authorization(),
    )
    results = generation.execute_plan(plan.plan_id, authorization=_authorization())
    assert len(results) == 3
    assert calls["calls"] == 3
    assert all(item.p4b1_digest_status_after == "partial_reusable" for item in results)
    completed_plan = generation.get_plan(plan.plan_id)
    assert completed_plan.status == "completed"
    assert all(job.status == "published" for job in completed_plan.jobs)
    assert generation.store.budget_usage() == {
        "components": 3,
        "model_calls": 3,
        "input_tokens": 1500,
        "output_tokens": 360,
    }
    with knowledge.connect() as conn:
        reports = conn.execute(
            "SELECT report_kind,subject_key,coverage_status FROM report_catalog_entries WHERE report_kind='component_research'"
        ).fetchall()
        assert len(reports) == 3
        assert {row["subject_key"] for row in reports} == set(PILOT_COMPONENT_SYMBOLS)
        assert conn.execute("SELECT COUNT(*) FROM report_artifact_links").fetchone()[0] == 0
        claim_count = conn.execute(
            "SELECT COUNT(*) FROM claim_records WHERE origin_id IN (SELECT report_id FROM report_catalog_entries WHERE report_kind='component_research')"
        ).fetchone()[0]
        assert claim_count == 9
        assert conn.execute(
            "SELECT COUNT(*) FROM report_knowledge_links WHERE report_id IN (SELECT report_id FROM report_catalog_entries WHERE report_kind='component_research')"
        ).fetchone()[0] == 3


def test_feature_first_mode_publishes_despite_output_and_daily_call_budgets(tmp_path) -> None:
    calls: dict[str, int] = {}
    base_runner = _fake_runner(calls)

    def long_output_runner(**kwargs):
        response = base_runner(**kwargs)
        response["usage"]["output_tokens"] = 1500
        return response

    knowledge, _, generation, _, resolution = _services(
        tmp_path,
        live=True,
        model_runner=long_output_runner,
    )
    constrained = replace(
        generation.policy,
        max_components_per_day=1,
        max_model_calls_per_day=1,
        max_output_tokens_per_component=1000,
        max_output_tokens_per_day=1000,
    )
    generation.policy = constrained
    plan = generation.create_plan(
        resolution,
        requested_components=list(PILOT_COMPONENT_SYMBOLS),
        dry_run=False,
        authorization=_feature_first_authorization(),
    )

    assert plan.planned_count == 3
    assert plan.budget_remaining["components"] == -1
    assert plan.budget_remaining["model_calls"] == -1
    assert plan.budget_remaining["output_tokens"] == -1
    results = generation.execute_plan(
        plan.plan_id,
        authorization=_feature_first_authorization(),
    )

    assert len(results) == 3
    assert calls["calls"] == 3
    assert generation.store.budget_usage()["output_tokens"] == 4500
    with knowledge.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM report_catalog_entries WHERE report_kind='component_research'"
        ).fetchone()[0] == 3


def test_second_identical_execution_reuses_publish_without_model_call(tmp_path) -> None:
    calls: dict[str, int] = {}
    _, _, generation, _, resolution = _services(
        tmp_path, live=True, model_runner=_fake_runner(calls)
    )
    plan = generation.create_plan(
        resolution,
        requested_components=list(PILOT_COMPONENT_SYMBOLS),
        dry_run=False,
        authorization=_authorization(),
    )
    first = generation.execute_plan(plan.plan_id, authorization=_authorization())
    second = generation.execute_plan(plan.plan_id, authorization=_authorization())
    assert [item.publish_id for item in second] == [item.publish_id for item in first]
    assert calls["calls"] == 3


def test_missing_evidence_id_fails_validation_and_publishes_nothing(tmp_path) -> None:
    calls: dict[str, int] = {}
    knowledge, _, generation, _, resolution = _services(
        tmp_path,
        live=True,
        model_runner=_fake_runner(calls, omit_evidence=True),
    )
    plan = generation.create_plan(
        resolution,
        requested_components=list(PILOT_COMPONENT_SYMBOLS),
        dry_run=False,
        authorization=_authorization(),
    )
    with pytest.raises(ValueError, match="Evidence IDs"):
        generation.execute_job(plan.jobs[0].job_id, authorization=_authorization())
    assert calls["calls"] == 1
    assert generation.get_job(plan.jobs[0].job_id).status == "failed"
    with knowledge.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM report_catalog_entries WHERE report_kind='component_research'"
        ).fetchone()[0] == 0


def test_publish_transaction_rolls_back_half_written_claims(tmp_path) -> None:
    knowledge, _, generation, _, resolution = _services(tmp_path, live=True)
    plan = generation.create_plan(
        resolution,
        requested_components=list(PILOT_COMPONENT_SYMBOLS),
        dry_run=False,
        authorization=_authorization(),
    )
    job = plan.jobs[0]
    pack = generation.get_evidence_pack(job.evidence_pack_id)
    assert pack is not None
    context = generation._contexts[pack.evidence_pack_id]
    response = _fake_runner({})(
        model_id="fake",
        messages=generation._messages(generation._model_payload(pack, context)),
        max_output_tokens=600,
    )
    output, claims = generation.validate_output(
        generation._extract_json(response["content"]), job=job, pack=pack
    )
    with pytest.raises(RuntimeError, match="injected"):
        generation._publish_transaction(
            job=job,
            pack=pack,
            output=output,
            claims=claims,
            actual_input_tokens=500,
            actual_output_tokens=120,
            fail_after_claims=True,
        )
    with knowledge.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM report_catalog_entries WHERE report_kind='component_research'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM claim_records WHERE origin_id LIKE 'componentreport_%'"
        ).fetchone()[0] == 0


def test_database_single_flight_and_daily_budget_reservation_are_atomic(tmp_path) -> None:
    _, _, generation, _, resolution = _services(tmp_path)
    plan = generation.create_plan(
        resolution,
        requested_components=list(PILOT_COMPONENT_SYMBOLS),
        dry_run=True,
    )
    base = plan.jobs[0]
    alternate = replace(
        base,
        job_id=stable_fingerprint("p4b2job", "alternate"),
        idempotency_key=stable_fingerprint("p4b2idempotency", "alternate"),
    )
    alternate_plan = replace(
        plan,
        plan_id=stable_fingerprint("p4b2plan", "alternate"),
        jobs=[alternate],
    )
    pack = generation.get_evidence_pack(base.evidence_pack_id)
    assert pack is not None
    generation.store.save_plan(alternate_plan, {pack.evidence_pack_id: pack})

    def reserve(job: ComponentResearchGenerationJob):
        return generation.store.reserve_budget(job, generation.policy)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(reserve, (base, alternate)))
    outcomes = [first, second]
    assert sum(item[0] for item in outcomes) == 1
    assert any(item[1] == "component_single_flight_active" for item in outcomes)
    assert generation.store.budget_usage()["components"] == 1


def test_provider_usage_is_mandatory_and_no_auto_repair_occurs(tmp_path) -> None:
    calls = {"count": 0}

    def no_usage(**_kwargs):
        calls["count"] += 1
        return {"content": "{}", "usage": {}}

    _, _, generation, _, resolution = _services(
        tmp_path, live=True, model_runner=no_usage
    )
    plan = generation.create_plan(
        resolution,
        requested_components=list(PILOT_COMPONENT_SYMBOLS),
        dry_run=False,
        authorization=_authorization(),
    )
    with pytest.raises(RuntimeError, match="actual token usage"):
        generation.execute_job(plan.jobs[0].job_id, authorization=_authorization())
    assert calls["count"] == 1
    assert generation.policy.max_auto_repairs == 0
    assert generation.get_job(plan.jobs[0].job_id).model_calls == 1


def test_generation_api_exposes_exact_dry_plan_job_cancel_and_budget(tmp_path) -> None:
    _, _, generation, _, resolution = _services(tmp_path)
    app = FastAPI()
    register_component_research_generation_routes(
        app,
        lambda: None,
        lambda: None,
        get_service=lambda: generation,
    )
    client = TestClient(app)
    created = client.post(
        "/report-library/component-research-generation/plans",
        json={
            "resolution_id": resolution.resolution_id,
            "component_symbols": list(PILOT_COMPONENT_SYMBOLS),
            "dry_run": True,
        },
    )
    assert created.status_code == 200
    payload = created.json()
    assert payload["dry_run"] is True
    assert payload["authorized"] is False
    assert len(payload["jobs"]) == 3
    plan_id = payload["plan_id"]
    job_id = payload["jobs"][0]["job_id"]
    assert client.get(
        f"/report-library/component-research-generation/plans/{plan_id}"
    ).status_code == 200
    assert client.get(
        f"/report-library/component-research-generation/jobs/{job_id}"
    ).status_code == 200
    cancelled = client.post(
        f"/report-library/component-research-generation/jobs/{job_id}/cancel"
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    budget = client.get(
        "/report-library/component-research-generation/budget/today"
    )
    assert budget.status_code == 200
    assert budget.json()["used"]["model_calls"] == 0


def test_authorized_prepare_backs_up_before_idempotent_p4b1_p4b2_migration(
    tmp_path,
) -> None:
    knowledge = ResearchKnowledgeStore(
        path=tmp_path / "research_cache.sqlite3", object_dir=tmp_path / "objects"
    )
    with knowledge.connect() as conn:
        conn.execute(
            "INSERT INTO report_library_meta(key,value,updated_at) VALUES ('sentinel','keep',?)",
            (NOW,),
        )
    with pytest.raises(PermissionError):
        prepare_component_research_live_database(
            knowledge.path,
            authorization={**_authorization(), "authorization_text": "not authorized"},
            backup_dir=tmp_path / "backups",
        )
    assert not (tmp_path / "backups").exists()

    result = prepare_component_research_live_database(
        knowledge.path,
        authorization=_authorization(),
        backup_dir=tmp_path / "backups",
    )
    backup = Path(result["backup_path"])
    assert backup.is_file()
    assert result["backup_size"] > 0
    assert len(result["backup_sha256"]) == 64
    assert result["p4b1_initialized"] is True
    assert result["p4b2_initialized"] is True
    with knowledge.connect() as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "component_digest_resolutions" in names
        assert "component_research_generation_jobs" in names
        assert conn.execute(
            "SELECT value FROM report_library_meta WHERE key='sentinel'"
        ).fetchone()[0] == "keep"
