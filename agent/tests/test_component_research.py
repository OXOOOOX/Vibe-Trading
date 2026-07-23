"""P4B1 deterministic ComponentResearchDigest reuse tests."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.report_library_routes import register_report_library_routes
from src.reports import (
    ComponentResearchDigestService,
    ComponentResearchDigestStore,
    DeepReportService,
    ETFComponentSelection,
    ETFConcentrationMetrics,
    ETFResearchStore,
    ETFSelectedComponent,
    ReportEnvelope,
    ReportLibraryService,
    execute_p4a_selection,
    make_universe_fetch_result,
)
from src.reports.etf_research import build_etf_snapshot, stable_fingerprint
from src.research.knowledge import ResearchKnowledgeStore


ANALYSIS_AS_OF = "2026-07-20T00:00:00+00:00"


def _services(tmp_path: Path):
    knowledge = ResearchKnowledgeStore(
        path=tmp_path / "research_cache.sqlite3",
        object_dir=tmp_path / "objects",
    )
    library = ReportLibraryService(knowledge)
    component_store = ComponentResearchDigestStore(knowledge_store=knowledge)
    component = ComponentResearchDigestService(
        knowledge,
        store=component_store,
        now_provider=lambda: "2026-07-20T01:00:00+00:00",
    )
    return knowledge, library, component


def _concentration(count: int) -> ETFConcentrationMetrics:
    return ETFConcentrationMetrics(
        concentration_class="concentrated",
        expected_component_count=max(count, 1),
        observed_component_count=max(count, 1),
        observed_weight_coverage=1.0,
        top1_weight=1.0 if count else 0.0,
        top3_weight=1.0 if count else 0.0,
        top5_weight=1.0 if count else 0.0,
        top10_weight=1.0 if count else 0.0,
        hhi_lower_bound=1.0 if count == 1 else 0.5,
        hhi_upper_bound=1.0,
        effective_component_count_lower_bound=float(max(count, 1)),
        min_penetration_count=0,
        max_penetration_count=5,
    )


def _selection(
    etf_symbol: str,
    components: list[tuple[str, str]],
    *,
    reason: str = "structural_representative",
    quality: str = "complete",
    warnings: list[str] | None = None,
    forced: bool = False,
) -> ETFComponentSelection:
    selected = [
        ETFSelectedComponent(
            symbol=symbol,
            name=name,
            weight=round(0.1 - index * 0.01, 8),
            score=round(0.9 - index * 0.05, 8),
            marginal_explanation_gain=round(0.1 - index * 0.01, 8),
            forced=forced,
            reasons=[reason],
            price_contribution=0.06 if forced else None,
            earnings_contribution=0.07 if forced else None,
        )
        for index, (symbol, name) in enumerate(components)
    ]
    input_fingerprint = stable_fingerprint("testp4ainput", {
        "etf_symbol": etf_symbol,
        "selected": [item.to_dict() for item in selected],
        "quality": quality,
        "warnings": warnings or [],
    })
    return ETFComponentSelection(
        selection_id=stable_fingerprint("p4aselection", {
            "etf_symbol": etf_symbol,
            "input_fingerprint": input_fingerprint,
        }),
        etf_symbol=etf_symbol,
        input_fingerprint=input_fingerprint,
        quality=quality,  # type: ignore[arg-type]
        concentration=_concentration(len(selected)),
        selected=selected,
        selected_weight_coverage=round(sum(item.weight for item in selected), 8),
        explanation_coverage=round(
            sum(item.marginal_explanation_gain for item in selected), 8
        ),
        stop_reason="test_fixture",
        warnings=warnings or [],
        created_at="2026-07-18T00:00:00+00:00",
    )


def _register_research(
    knowledge: ResearchKnowledgeStore,
    library: ReportLibraryService,
    *,
    symbol: str,
    report_id: str,
    claims: list[tuple[str, str]],
    data_as_of: str = "2026-07-10T00:00:00+00:00",
    report_quality: str = "passed",
    coverage: str = "complete",
    fact_values: list[str] | None = None,
    valid_until: str | None = None,
) -> list[str]:
    document = knowledge.store_document(
        url=f"https://example.test/{report_id}",
        content=f"{symbol} {report_id} source body",
        title=report_id,
        publisher="Example Exchange",
        published_at=data_as_of,
    )
    evidence = []
    facts = []
    claim_rows = []
    claim_ids = []
    values = fact_values or [str(index + 1) for index in range(len(claims))]
    for index, ((section_id, text), value) in enumerate(zip(claims, values, strict=True)):
        evidence_id = f"evidence_{report_id}_{index}"
        fact_id = f"fact_{report_id}_{index}"
        claim_id = f"claim_{report_id}_{index}"
        evidence.append({
            "evidence_id": evidence_id,
            "symbol": symbol,
            "domain": "company_fundamentals",
            "status": "verified",
            "summary": text,
            "published_at": data_as_of,
            "metadata": {
                "document_ref": document.document_ref,
                "valid_until": valid_until,
            },
        })
        facts.append({
            "fact_id": fact_id,
            "symbol": symbol,
            "metric": f"metric_{section_id}" if len(values) == len(set(values)) else "conflict_metric",
            "value": value,
            "unit": "CNY",
            "period": "2026Q2",
            "evidence_ids": [evidence_id],
            "validation_status": "pass",
            "metadata": {"scope_key": "company", "currency": "CNY"},
        })
        claim_rows.append({
            "claim_id": claim_id,
            "section_id": section_id,
            "claim_type": "fact",
            "text": text,
            "fact_ids": [fact_id],
            "evidence_ids": [evidence_id],
        })
        claim_ids.append(claim_id)
    knowledge.link_report(
        report_id=report_id,
        revision=1,
        symbol=symbol,
        quality_status=report_quality,
        evidence=evidence,
        facts=facts,
        claims=claim_rows,
    )
    # Keep the fixture's knowledge-availability clock deterministic.  The
    # production store records ingestion time, but these historical cutoff
    # tests intentionally model the bundle as available at ``data_as_of``.
    with knowledge.connect() as connection:
        connection.execute(
            "UPDATE claim_records SET created_at=? WHERE origin_id=?",
            (data_as_of, report_id),
        )
        connection.executemany(
            "UPDATE fact_records SET created_at=? WHERE fact_id=?",
            [(data_as_of, item["fact_id"]) for item in facts],
        )
        connection.executemany(
            "UPDATE evidence_records SET created_at=? WHERE evidence_id=?",
            [(data_as_of, item["evidence_id"]) for item in evidence],
        )
        connection.execute(
            "UPDATE fact_conflicts SET created_at=? WHERE comparison_key LIKE ?",
            (data_as_of, f"{symbol}|%"),
        )
    library.register_report(ReportEnvelope(
        report_id=report_id,
        family_id=report_id,
        report_kind="deep_research",
        subject_type="symbol",
        subject_key=symbol,
        symbol=symbol,
        security_name=f"证券{symbol}",
        status="diagnostic" if report_quality == "failed_validation" else "published",
        report_quality_status=report_quality,  # type: ignore[arg-type]
        coverage_status=coverage,  # type: ignore[arg-type]
        generated_at=data_as_of,
        data_as_of=data_as_of,
        source_type="deep_report",
        source_id=report_id,
        source_revision=1,
    ))
    return claim_ids


FULL_CLAIMS = [
    ("business_position", "主营业务与行业暴露清晰"),
    ("financial_quality", "营收和利润趋势改善"),
    ("valuation", "长期折现框架具备可比口径"),
    ("catalyst_note", "订单与新产品构成催化"),
    ("accounting_review", "主要风险和反向证据"),
    ("holder_note", "重要股东与治理结构稳定"),
    ("event_note", "重大事件公告已经核验"),
]


def test_cross_etf_reuses_one_digest_but_keeps_distinct_bindings(tmp_path) -> None:
    knowledge, library, service = _services(tmp_path)
    _register_research(
        knowledge, library, symbol="600519.SH", report_id="report_moutai",
        claims=FULL_CLAIMS,
    )
    first = service.resolve_selection(
        _selection("510300.SH", [("600519.SH", "贵州茅台")], reason="weight_at_least_5pct"),
        ANALYSIS_AS_OF,
    )
    second = service.resolve_selection(
        _selection("999999.SH", [("600519.SH", "贵州茅台")], reason="major_event", forced=True),
        ANALYSIS_AS_OF,
    )

    assert first.digest_ids == second.digest_ids
    assert first.bindings[0].binding_id != second.bindings[0].binding_id
    assert first.bindings[0].selection_reasons != second.bindings[0].selection_reasons
    assert first.bindings[0].component_weight == second.bindings[0].component_weight
    with service.store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM component_research_digests").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM etf_component_digest_bindings").fetchone()[0] == 2
    assert service.store.metrics()["cross_etf_shared_digest_count"] == 1


def test_stable_ids_and_second_resolution_hit_real_cache(tmp_path) -> None:
    knowledge, library, service = _services(tmp_path)
    _register_research(
        knowledge, library, symbol="300750.SZ", report_id="report_catl",
        claims=FULL_CLAIMS,
    )
    selection = _selection("510300.SH", [("300750.SZ", "宁德时代")])

    first = service.resolve_selection(selection, ANALYSIS_AS_OF)
    second = service.resolve_selection(selection, ANALYSIS_AS_OF)

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert first.resolution_id == second.resolution_id
    assert first.digest_ids == second.digest_ids
    metrics = service.store.metrics()
    assert metrics["digest_builds"] == 1
    assert metrics["digest_cache_hits"] == 1
    assert metrics["model_calls"] == 0
    assert metrics["input_tokens"] == 0
    assert metrics["output_tokens"] == 0


def test_new_claim_changes_fingerprint_and_superseded_claim_becomes_stale(tmp_path) -> None:
    knowledge, library, service = _services(tmp_path)
    claim_ids = _register_research(
        knowledge, library, symbol="688256.SH", report_id="report_cambricon",
        claims=[("business_position", "主营业务与行业暴露")],
    )
    selection = _selection("588870.SH", [("688256.SH", "寒武纪")])
    first = service.resolve_selection(selection, ANALYSIS_AS_OF)
    assert first.partial_reusable_count == 1

    _register_research(
        knowledge, library, symbol="688256.SH", report_id="report_cambricon_risk",
        claims=[("accounting_review", "风险与反向证据")],
        data_as_of="2026-07-11T00:00:00+00:00",
    )
    changed = service.resolve_selection(selection, ANALYSIS_AS_OF)
    assert changed.knowledge_fingerprint != first.knowledge_fingerprint
    assert changed.digest_ids != first.digest_ids

    with knowledge.connect() as connection:
        connection.execute(
            "UPDATE claim_records SET superseded_by='claim_replacement' WHERE claim_id=?",
            (claim_ids[0],),
        )
    invalidated = service.resolve_selection(selection, ANALYSIS_AS_OF)
    assert invalidated.knowledge_fingerprint != changed.knowledge_fingerprint
    digest = service.store.get_digest(invalidated.digest_ids[0])
    assert digest is not None
    assert "business_exposure" in digest.stale_dimensions


def test_missing_partial_stale_and_reusable_are_explicit(tmp_path) -> None:
    knowledge, library, service = _services(tmp_path)
    _register_research(
        knowledge, library, symbol="688041.SH", report_id="report_partial",
        claims=[("business_position", "主营业务与行业暴露")],
    )
    _register_research(
        knowledge, library, symbol="688981.SH", report_id="report_stale",
        claims=[("business_position", "主营业务与行业暴露")],
        data_as_of="2025-01-01T00:00:00+00:00",
    )
    _register_research(
        knowledge, library, symbol="688008.SH", report_id="report_complete",
        claims=FULL_CLAIMS,
    )
    resolution = service.resolve_selection(
        _selection("588870.SH", [
            ("688041.SH", "海光信息"),
            ("688981.SH", "中芯国际"),
            ("688008.SH", "澜起科技"),
            ("688012.SH", "中微公司"),
        ]),
        ANALYSIS_AS_OF,
    )
    statuses = {item.component_symbol: item.digest_status for item in resolution.bindings}
    assert statuses == {
        "688041.SH": "partial_reusable",
        "688981.SH": "stale",
        "688008.SH": "reusable",
        "688012.SH": "missing",
    }
    assert resolution.reusable_count == 1
    assert resolution.partial_reusable_count == 1
    assert resolution.stale_count == 1
    assert resolution.missing_count == 1
    assert resolution.model_calls == resolution.input_tokens == resolution.output_tokens == 0
    repeated = service.resolve_selection(
        _selection("588870.SH", [
            ("688041.SH", "海光信息"),
            ("688981.SH", "中芯国际"),
            ("688008.SH", "澜起科技"),
            ("688012.SH", "中微公司"),
        ]),
        ANALYSIS_AS_OF,
    )
    assert repeated.cache_hit is True
    metrics = service.store.metrics()
    assert metrics["digest_builds"] == 4
    assert metrics["digest_cache_hits"] == 4


def test_unresolved_fact_conflict_is_retained(tmp_path) -> None:
    knowledge, library, service = _services(tmp_path)
    _register_research(
        knowledge, library, symbol="002558.SZ", report_id="report_conflict",
        claims=[
            ("financial_quality", "利润口径来源一"),
            ("financial_quality", "利润口径来源二"),
        ],
        fact_values=["100", "140"],
    )
    resolution = service.resolve_selection(
        _selection("516010.SH", [("002558.SZ", "巨人网络")]),
        ANALYSIS_AS_OF,
    )
    assert resolution.conflicted_count == 1
    binding = resolution.bindings[0]
    digest = service.store.get_digest(binding.digest_id or "")
    assert digest is not None
    assert digest.status == "conflicted"
    assert digest.conflict_ids
    with knowledge.connect() as connection:
        connection.execute(
            "UPDATE fact_conflicts SET resolution_status='resolved' WHERE conflict_id=?",
            (digest.conflict_ids[0],),
        )
    resolved = service.resolve_selection(
        _selection("516010.SH", [("002558.SZ", "巨人网络")]),
        ANALYSIS_AS_OF,
    )
    assert resolved.conflicted_count == 0
    assert resolved.digest_ids != resolution.digest_ids


def test_future_and_failed_knowledge_are_excluded(tmp_path) -> None:
    knowledge, library, service = _services(tmp_path)
    old_claims = _register_research(
        knowledge, library, symbol="002555.SZ", report_id="report_timebox",
        claims=[
            ("business_position", "主营业务与行业暴露"),
            ("accounting_review", "风险与反向证据"),
        ],
    )
    with knowledge.connect() as connection:
        connection.execute(
            "UPDATE claim_records SET created_at='2026-07-25T00:00:00+00:00' WHERE claim_id=?",
            (old_claims[1],),
        )
        linked = connection.execute(
            "SELECT fact_ids_json,evidence_ids_json FROM claim_records WHERE claim_id=?",
            (old_claims[0],),
        ).fetchone()
        fact_id = json.loads(linked["fact_ids_json"])[0]
        evidence_id = json.loads(linked["evidence_ids_json"])[0]
        connection.execute(
            "UPDATE fact_records SET created_at='2026-07-25T00:00:00+00:00' WHERE fact_id=?",
            (fact_id,),
        )
        connection.execute(
            "UPDATE evidence_records SET valid_from='2026-07-25T00:00:00+00:00' WHERE evidence_id=?",
            (evidence_id,),
        )
    _register_research(
        knowledge, library, symbol="002555.SZ", report_id="report_future",
        claims=[("financial_quality", "未来报告盈利趋势")],
        data_as_of="2026-07-25T00:00:00+00:00",
    )
    _register_research(
        knowledge, library, symbol="002555.SZ", report_id="report_failed",
        claims=[("financial_quality", "失败报告盈利趋势")],
        report_quality="failed_validation",
        coverage="insufficient",
    )
    resolution = service.resolve_selection(
        _selection("516010.SH", [("002555.SZ", "三七互娱")]),
        ANALYSIS_AS_OF,
    )
    digest = service.store.get_digest(resolution.digest_ids[0])
    assert digest is not None
    assert digest.source_report_ids == ["report_timebox"]
    assert old_claims[1] not in json.dumps(digest.claim_ids_by_dimension)
    assert digest.fact_ids == []
    assert digest.evidence_ids == []
    assert "future_reports_excluded:1" in digest.warnings
    assert "future_claims_excluded:1" in digest.warnings


def test_current_digest_defaults_to_now_and_cannot_be_shadowed_by_future_cutoff(
    tmp_path,
) -> None:
    knowledge, library, service = _services(tmp_path)
    _register_research(
        knowledge,
        library,
        symbol="002555.SZ",
        report_id="report_current_digest",
        claims=[
            ("business_position", "主营业务与行业暴露"),
            ("financial_quality", "经营趋势"),
            ("accounting_review", "风险与反向证据"),
        ],
    )
    resolution = service.resolve_selection(
        _selection("516010.SH", [("002555.SZ", "三七互娱")]),
        ANALYSIS_AS_OF,
    )
    current = service.store.get_digest(resolution.digest_ids[0])
    assert current is not None
    service.store.save_digest(
        replace(
            current,
            digest_id="componentdigest_future_shadow",
            analysis_as_of="2026-07-21T00:00:00+00:00",
            status="missing",
            input_fingerprint="future_shadow_input",
            created_at="2026-07-20T01:30:00+00:00",
        )
    )

    resolved = service.current_digest("002555.SZ")

    assert resolved is not None
    assert resolved.digest_id == current.digest_id
    assert resolved.status != "missing"


def test_empty_and_forced_p4a_only_create_selected_bindings(tmp_path) -> None:
    _knowledge, _library, service = _services(tmp_path)
    empty = service.resolve_selection(_selection("560010.SH", []), ANALYSIS_AS_OF)
    assert empty.selected_count == 0
    assert empty.bindings == []
    assert empty.digest_ids == []
    with service.store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM etf_component_digest_bindings").fetchone()[0] == 0

    forced = service.resolve_selection(
        _selection(
            "560010.SH", [("000001.SZ", "强制事件成分")],
            reason="major_event", forced=True,
        ),
        ANALYSIS_AS_OF,
    )
    assert [item.component_symbol for item in forced.bindings] == ["000001.SZ"]
    assert forced.bindings[0].forced is True
    assert forced.bindings[0].price_contribution == 0.06
    assert forced.bindings[0].earnings_contribution == 0.07


def test_partial_universe_warning_is_preserved(tmp_path) -> None:
    knowledge, library, service = _services(tmp_path)
    _register_research(
        knowledge, library, symbol="002517.SZ", report_id="report_partial_universe",
        claims=[("business_position", "主营业务与行业暴露")],
    )
    resolution = service.resolve_selection(
        _selection(
            "516010.SH", [("002517.SZ", "恺英网络")], quality="partial",
            warnings=["known_component_weight_coverage_below_50pct"],
        ),
        ANALYSIS_AS_OF,
    )
    assert "partial_component_universe" in resolution.bindings[0].warnings
    assert "known_component_weight_coverage_below_50pct" in resolution.bindings[0].warnings


def test_single_flight_concurrent_cross_etf_builds_one_digest(tmp_path) -> None:
    knowledge, library, service = _services(tmp_path)
    _register_research(
        knowledge, library, symbol="002624.SZ", report_id="report_perfect_world",
        claims=FULL_CLAIMS,
    )
    selections = [
        _selection("516010.SH", [("002624.SZ", "完美世界")]),
        _selection("999999.SH", [("002624.SZ", "完美世界")], reason="major_event"),
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda item: service.resolve_selection(item, ANALYSIS_AS_OF),
            selections,
        ))
    assert results[0].digest_ids == results[1].digest_ids
    with service.store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM component_research_digests").fetchone()[0] == 1


def test_hk_components_come_from_normalized_snapshot_contract(tmp_path) -> None:
    fetched = make_universe_fetch_result(
        etf_symbol="513120.SH",
        etf_name="港股创新药ETF",
        tracked_index_code="931787.CSI",
        tracked_index_name="中证香港创新药指数",
        provider_id="test_reliable_snapshot",
        source_type="official_index_weight",
        source_ids=["official:931787:20260718"],
        source_urls=["https://example.test/official-931787.xls"],
        data_as_of="2026-07-18",
        components=[
            {"symbol": "700", "exchange": "HK", "name": "腾讯", "weight": 30},
            {"symbol": "2269", "exchange": "香港", "name": "药明生物", "weight": 25},
            {"symbol": "6160", "exchange": "SEHK", "name": "百济神州", "weight": 20},
            {"symbol": "9926", "exchange": "HK", "name": "康方生物", "weight": 15},
            {"symbol": "1801", "exchange": "HK", "name": "信达生物", "weight": 10},
        ],
        expected_component_count=5,
        universe_complete=True,
        weight_scale="percent",
    )
    research_store = ETFResearchStore(tmp_path / "etf.sqlite3")
    snapshot = build_etf_snapshot(
        symbol=fetched.etf_symbol,
        snapshot_type="universe",
        data_as_of=fetched.data_as_of,
        payload=fetched.to_snapshot_payload(),
        coverage_ratio=fetched.required_field_coverage,
        source_ids=fetched.source_ids,
        freshness_expires_at="2026-08-31T00:00:00+00:00",
    )
    selection, _ = execute_p4a_selection(store=research_store, universe_snapshot=snapshot)
    assert selection.selected
    assert all(item.symbol.endswith(".HK") and len(item.symbol) == 8 for item in selection.selected)

    _knowledge, _library, component = _services(tmp_path / "component")
    resolution = component.resolve_selection(
        selection,
        ANALYSIS_AS_OF,
        selection_data_as_of=snapshot.data_as_of,
    )
    assert resolution.selected_count == len(selection.selected)
    assert resolution.missing_count == len(selection.selected)
    assert all(item.digest_id is None for item in resolution.bindings)


def test_deep_report_attachment_creates_no_artifact(tmp_path) -> None:
    _knowledge, _library, component = _services(tmp_path / "knowledge")
    selection = _selection("588870.SH", [("688012.SH", "中微公司")])
    resolution = component.resolve_selection(selection, ANALYSIS_AS_OF)
    reports = DeepReportService(tmp_path / "reports")
    record = reports.begin(
        session_id="session-p4b1",
        attempt_id="attempt-p4b1",
        request_content="研究 588870.SH",
        profile="etf_deep_research",
    )
    reports.attach_etf_analysis(record.report_id, {
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
    })
    reports.attach_etf_component_selection(record.report_id, selection.to_dict())
    attached = reports.attach_component_digest_resolution(
        record.report_id, resolution.to_dict()
    )

    assert attached.analysis_modules["component_research"].details["model_calls"] == 0
    report_dir = tmp_path / "reports" / record.report_id
    assert (report_dir / "analysis" / "component_digest_resolution.json").exists()
    assert not (report_dir / "report.md").exists()
    assert not list(report_dir.glob("*.pdf"))
    assert attached.artifacts == []


def test_subject_api_exposes_stable_component_research_profile(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_REPORT_LIBRARY_ENABLED", "1")
    knowledge, library, component = _services(tmp_path)
    _register_research(
        knowledge, library, symbol="600519.SH", report_id="report_api_component",
        claims=[("business_position", "主营业务与行业暴露")],
    )
    selection = _selection("510300.SH", [("600519.SH", "贵州茅台")])
    component.resolve_selection(selection, ANALYSIS_AS_OF)
    library.register_report(ReportEnvelope(
        report_id="report_etf_profile",
        family_id="report_etf_profile",
        report_kind="deep_research",
        subject_type="symbol",
        subject_key="510300.SH",
        symbol="510300.SH",
        security_name="沪深300ETF",
        status="published",
        report_quality_status="passed_with_gaps",
        coverage_status="partial",
        generated_at="2026-07-18T00:00:00+00:00",
        data_as_of="2026-07-18T00:00:00+00:00",
        source_type="deep_report",
        source_id="report_etf_profile",
    ))

    class _UniverseSnapshot:
        def to_dict(self):
            return {
                "snapshot_id": "etfsnap_subject_profile",
                "symbol": "510300.SH",
                "snapshot_type": "universe",
                "data_as_of": "2026-06-30T00:00:00+00:00",
                "retrieved_at": "2026-07-18T00:00:00+00:00",
                "freshness_expires_at": "2026-08-14T00:00:00+00:00",
                "quality_status": "passed",
                "source_ids": ["csi:000300:20260630:closeweight"],
                "payload": {
                    "etf_symbol": "510300.SH",
                    "etf_name": "沪深300ETF",
                    "tracked_index_code": "000300.SH",
                    "tracked_index_name": "沪深300",
                    "provider_id": "csi_official_close_weight",
                    "source_type": "official_index_weight",
                    "source_urls": ["https://example.test/000300closeweight.xls"],
                    "weight_scale": "fraction",
                    "quality": "complete",
                    "expected_component_count": 300,
                    "observed_component_count": 300,
                    "observed_weight_coverage": 1.0,
                    "required_field_coverage": 1.0,
                    "universe_complete": True,
                    "partial_components_are_top_ranked": False,
                    "warnings": [],
                    "components": [
                        {"symbol": "600519.SH", "name": "贵州茅台", "weight": 0.051},
                        {"symbol": "300750.SZ", "name": "宁德时代", "weight": 0.038},
                    ],
                },
            }

    class _UniverseService:
        def latest_snapshot(self, symbol):
            assert symbol == "510300.SH"
            return _UniverseSnapshot()

    app = FastAPI()
    register_report_library_routes(
        app,
        lambda: None,
        get_service=lambda: library,
        get_component_service=lambda: component,
        get_etf_universe_service=lambda: _UniverseService(),
    )
    client = TestClient(app)

    response = client.get("/report-library/subjects/510300.SH")
    assert response.status_code == 200
    profile = response.json()["profile"]["etf"]["component_research"]
    assert profile["selected_count"] == 1
    assert profile["components"][0]["symbol"] == "600519.SH"
    assert profile["components"][0]["research_data_as_of"]
    assert profile["model_calls"] == profile["input_tokens"] == profile["output_tokens"] == 0
    universe = response.json()["profile"]["etf"]["universe"]
    assert universe == response.json()["etf_universe"]
    assert universe["weight_semantics"] == "tracked_index_weight"
    assert universe["observed_component_count"] == 300
    assert universe["components"][0] == {
        "symbol": "600519.SH",
        "name": "贵州茅台",
        "weight": 0.051,
        "metadata": {},
    }

    resolution_id = profile["selection_id"]
    resolution = client.get(
        f"/report-library/component-research/resolutions/{resolution_id}"
    )
    assert resolution.status_code == 200
    metrics = client.get("/report-library/component-research/metrics")
    assert metrics.status_code == 200
    assert metrics.json()["model_calls"] == 0
    rerun = client.post(
        "/report-library/component-research/resolutions/resolve",
        json={"selection": selection.to_dict(), "analysis_as_of": ANALYSIS_AS_OF},
    )
    assert rerun.status_code == 200
    assert rerun.json()["cache_hit"] is True
    assert rerun.json()["model_calls"] == 0


def test_test_database_is_isolated_from_default_runtime_database(tmp_path, monkeypatch) -> None:
    default_runtime = Path.home() / ".vibe-trading" / "cache" / "research_cache.sqlite3"
    monkeypatch.setenv("VIBE_TRADING_RESEARCH_CACHE_DB", str(tmp_path / "isolated.sqlite3"))
    knowledge = ResearchKnowledgeStore(
        path=tmp_path / "isolated.sqlite3",
        object_dir=tmp_path / "objects",
    )
    service = ComponentResearchDigestService(knowledge)
    service.resolve_selection(
        _selection("560010.SH", [("300251.SZ", "光线传媒")]),
        ANALYSIS_AS_OF,
    )
    assert service.store.path.resolve() == (tmp_path / "isolated.sqlite3").resolve()
    assert service.store.path.resolve() != default_runtime.resolve()


def test_invalid_or_name_only_symbols_are_rejected(tmp_path) -> None:
    _knowledge, _library, service = _services(tmp_path)
    with pytest.raises(ValueError, match="market-qualified"):
        service.resolve_selection(
            _selection("510300.SH", [("贵州茅台", "贵州茅台")]),
            ANALYSIS_AS_OF,
        )
    with pytest.raises(ValueError, match="market-qualified"):
        service.resolve_selection(
            _selection("510300.SH", [("600519", "贵州茅台")]),
            ANALYSIS_AS_OF,
        )
