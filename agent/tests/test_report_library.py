from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.report_library_routes import register_report_library_routes
from src.reports.catalog import ReportLibraryService
from src.reports.contracts import DeepReportRecord, ModuleResult, ReportEnvelope, ReportViewpoint
from src.research.annual_report_jobs import (
    AnnualReportBackfillJobService,
    AnnualReportBackfillJobStore,
)
from src.research.knowledge import ResearchKnowledgeStore


def _service(tmp_path, *, now: datetime | None = None, summarizer=None) -> ReportLibraryService:
    store = ResearchKnowledgeStore(
        path=tmp_path / "research.sqlite3",
        object_dir=tmp_path / "objects",
    )
    clock = now or datetime(2026, 7, 18, 2, 0, tzinfo=timezone.utc)
    return ReportLibraryService(
        store,
        summarizer=summarizer,
        now_provider=lambda: clock,
    )


def _daily_run(run_id: str, market_date: str, *, warnings=None) -> dict:
    return {
        "run_id": run_id,
        "market_date": market_date,
        "status": "completed_with_warnings" if warnings else "completed",
        "created_at": f"{market_date}T08:00:00+08:00",
        "completed_at": f"{market_date}T09:00:00+08:00",
        "artifact_revision": 1,
        "warnings": warnings or [],
        "artifacts": [
            {
                "artifact_id": f"{run_id}-holding-md",
                "kind": "holding_daily_markdown",
                "symbol": "588870.SH",
                "security_name": "科创板新能源ETF",
                "filename": f"{market_date}_588870.md",
                "media_type": "text/markdown",
                "sha256": "a" * 64,
                "revision": 1,
            },
            {
                "artifact_id": f"{run_id}-master-md",
                "kind": "master_markdown",
                "filename": f"{market_date}_portfolio.md",
                "media_type": "text/markdown",
                "sha256": "b" * 64,
                "revision": 1,
            },
        ],
    }


def _aggregate(action: str, summary: str, *, warnings=None, data_as_of: str | None = None) -> dict:
    return {
        "briefs": [
            {
                "symbol": "588870.SH",
                "summary": summary,
                "action": action,
                "confidence": "medium",
                "reasons": ["等待趋势确认"],
                "risks": ["短线波动仍高"],
                "watch_points": ["观察关键条件"],
                "condition_orders": [],
                **({"data_as_of": data_as_of} if data_as_of else {}),
            }
        ],
        "counts": {action: 1},
        "warnings": warnings or [],
    }


def _weekly_run(run_id: str, week_end: str, revision: int = 1) -> tuple[dict, dict]:
    report_id = f"weekly-{run_id}"
    record = {
        "run_id": run_id,
        "report_id": report_id,
        "symbol": "588870.SH",
        "week_end": week_end,
        "revision": revision,
        "status": "completed_with_warnings",
        "artifacts": [
            {
                "artifact_id": f"{run_id}-json",
                "kind": "weekly_review_json",
                "symbol": "588870.SH",
                "security_name": "科创板新能源ETF",
                "filename": f"{week_end}_588870.SH_科创板新能源ETF_周度复盘.json",
                "media_type": "application/json",
                "sha256": "c" * 64,
                "revision": revision,
            }
        ],
    }
    brief = {
        "report_id": report_id,
        "run_id": run_id,
        "revision": revision,
        "symbol": "588870.SH",
        "security_name": "科创板新能源ETF",
        "quality_status": "passed_with_gaps",
        "coverage_status": "partial",
        "generated_at": f"{week_end}T16:00:00+08:00",
        "data_as_of": f"{week_end}T15:00:00+08:00",
        "valid_from": f"{week_end}T16:00:00+08:00",
        "valid_until": "2026-07-24T15:30:00+08:00",
        "review_due_at": "2026-07-24T15:30:00+08:00",
        "source_valid_until": "2026-07-24T15:30:00+08:00",
        "weekly_view": {"trend_direction": "向上"},
        "summary_claim_id": f"claim-{run_id}-summary",
        "reason_claim_ids": [f"claim-{run_id}-reason"],
        "risk_claim_ids": [f"claim-{run_id}-risk"],
        "monitoring_claims": [
            {
                "claim_id": f"claim-{run_id}-summary",
                "section_id": "weekly_summary",
                "claim_type": "inference",
                "text": "周度趋势向上但仍需人工复核。",
                "fact_ids": [],
                "evidence_ids": [],
            },
            {
                "claim_id": f"claim-{run_id}-reason",
                "section_id": "weekly_reason",
                "claim_type": "calculation",
                "text": "本周收盘高于上周。",
                "fact_ids": [],
                "evidence_ids": [],
            },
            {
                "claim_id": f"claim-{run_id}-risk",
                "section_id": "weekly_risk",
                "claim_type": "opinion",
                "text": "报告不自动激活监控。",
                "fact_ids": [],
                "evidence_ids": [],
            },
        ],
        "monitoring_bundle": {
            "schema_version": 1,
            "monitoring_status": "available",
            "candidates": [],
        },
        "previous_week_validation": [],
        "scenario_changes": [],
    }
    return record, brief


def test_formal_weekly_registration_is_idempotent_and_does_not_replace_daily(tmp_path) -> None:
    service = _service(tmp_path)
    daily = service.register_daily_run(
        _daily_run("daily-weekly-baseline", "2026-07-17"),
        _aggregate("observe", "日度观点"),
    )[0]
    record, brief = _weekly_run("weekly-a", "2026-07-17")

    first = service.register_weekly_run(record, brief)
    second = service.register_weekly_run(record, brief)
    subject = service.subject("588870.SH")

    assert first is not None and second is not None
    assert first["report_id"] == second["report_id"]
    assert subject["current"]["weekly"]["latest"]["report_id"] == first["report_id"]
    assert subject["current"]["daily"]["latest"]["report_id"] == daily["report_id"]
    assert first["knowledge_link"]["monitoring_bundle_source_locator"].startswith("weekly-run:")
    assert first["artifacts"][0]["source_locator"].startswith("weekly-run:")


def test_new_weekly_report_supersedes_only_prior_weekly_report(tmp_path) -> None:
    service = _service(tmp_path)
    first_record, first_brief = _weekly_run("weekly-a", "2026-07-10")
    first_brief["valid_until"] = first_brief["review_due_at"] = first_brief["source_valid_until"] = "2026-07-17T15:30:00+08:00"
    first = service.register_weekly_run(first_record, first_brief)
    second_record, second_brief = _weekly_run("weekly-b", "2026-07-17")
    second = service.register_weekly_run(second_record, second_brief)

    assert first is not None and second is not None
    assert len(second["relations"]) == 1
    assert second["relations"][0]["from_report_id"] == second["report_id"]
    assert second["relations"][0]["to_report_id"] == first["report_id"]
    assert second["relations"][0]["relation_type"] == "supersedes"
    assert second["relations"][0]["horizon"] == "weekly"


def test_schema_v2_and_daily_report_registration_are_idempotent(tmp_path) -> None:
    service = _service(
        tmp_path,
        now=datetime(2026, 7, 18, 2, 0, tzinfo=timezone.utc),
    )
    record = _daily_run("daily-1", "2026-07-18")
    aggregate = _aggregate("observe", "当日保持观察")

    first = service.register_daily_run(record, aggregate)
    second = service.register_daily_run(record, aggregate)

    assert [item["report_kind"] for item in first] == ["daily_holding", "daily_portfolio"]
    assert [item["report_id"] for item in first] == [item["report_id"] for item in second]
    with service.knowledge.connect() as conn:
        versions = {row[0] for row in conn.execute("SELECT version FROM research_knowledge_schema")}
        report_count = conn.execute("SELECT COUNT(*) FROM report_catalog_entries").fetchone()[0]
        claim_count = conn.execute("SELECT COUNT(*) FROM claim_records").fetchone()[0]
    assert versions == {1, 2, 3, 4, 5, 6}
    assert report_count == 2
    assert claim_count >= 5

    dossier = service.subject("588870.SH")
    current = dossier["current"]["daily"]
    assert current["latest"]["viewpoint"]["action"] == "observe"
    assert current["latest_complete"]["report_id"] == current["latest"]["report_id"]
    assert dossier["timeline"][0]["artifacts"][0]["source_locator"].startswith(
        "daily-run:daily-1:"
    )


def test_report_period_is_distinct_from_real_data_cutoff(tmp_path) -> None:
    service = _service(tmp_path)
    holding, portfolio = service.register_daily_run(
        _daily_run("daily-period", "2026-07-18"),
        _aggregate(
            "observe",
            "非交易日沿用最近收盘数据",
            data_as_of="2026-07-17T15:00:00+08:00",
        ),
    )

    assert holding["report_period"] == {
        "start_date": "2026-07-18",
        "end_date": "2026-07-18",
        "label": "2026-07-18",
    }
    assert holding["data_as_of"] == "2026-07-17T07:00:00+00:00"
    assert portfolio["report_period"]["label"] == "2026-07-18"
    assert portfolio["data_as_of"] == "2026-07-17T07:00:00+00:00"


def test_large_catalog_subject_and_report_cursors_are_complete_and_stable(tmp_path) -> None:
    service = _service(
        tmp_path,
        now=datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc),
    )
    base = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    for index in range(105):
        generated_at = (base + timedelta(minutes=index)).isoformat()
        report_id = f"cursor-report-{index:03d}"
        service.register_report(
            ReportEnvelope(
                report_id=report_id,
                family_id=report_id,
                report_kind="component_research" if index == 0 else "daily_holding",
                subject_type="symbol",
                subject_key="600000.SH",
                symbol="600000.SH",
                security_name="浦发银行",
                status="published",
                report_quality_status="passed",
                coverage_status="complete",
                generated_at=generated_at,
                data_as_of=generated_at,
                source_type="cursor_test",
                source_id=report_id,
            )
        )
    latest_id = "cursor-other-latest"
    service.register_report(
        ReportEnvelope(
            report_id=latest_id,
            family_id=latest_id,
            report_kind="weekly_review",
            subject_type="symbol",
            subject_key="000001.SZ",
            symbol="000001.SZ",
            security_name="平安银行",
            status="published",
            report_quality_status="passed",
            coverage_status="complete",
            generated_at="2026-07-18T10:00:00+00:00",
            data_as_of="2026-07-18T09:30:00+00:00",
            source_type="cursor_test",
            source_id=latest_id,
        )
    )

    seen: list[str] = []
    cursor = None
    while True:
        page = service.list_reports(subject_type="symbol", limit=17, cursor=cursor)
        assert page["total_count"] == 106
        seen.extend(item["report_id"] for item in page["reports"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 106

    subject_seen: list[str] = []
    cursor = None
    while True:
        page = service.list_subject_reports("600000.SH", limit=19, cursor=cursor)
        assert page["total_count"] == 105
        subject_seen.extend(item["report_id"] for item in page["reports"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(subject_seen) == len(set(subject_seen)) == 105

    subjects = service.list_subjects(limit=10)
    assert subjects["total_count"] == 2
    assert [item["subject_key"] for item in subjects["subjects"]] == ["000001.SZ", "600000.SH"]
    bank = subjects["subjects"][1]
    assert bank["report_count"] == 105
    assert bank["report_kinds"] == ["component_research", "daily_holding"]
    assert bank["latest_report"]["report_id"] == "cursor-report-104"
    assert "knowledge_link" not in bank["latest_report"]
    assert "viewpoints" not in bank["latest_report"]
    shell = service.subject("600000.SH", include_timeline=False)
    assert shell["report_count"] == 105
    assert shell["timeline"] == []


def test_deep_report_registers_internal_reference_and_structural_bundle(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_REPORT_LIBRARY_ENABLED", "1")
    service = _service(tmp_path)
    artifact_dir = tmp_path / "deep"
    artifact_dir.mkdir()
    monitoring_path = artifact_dir / "monitoring_bundle.json"
    monitoring_path.write_text(json.dumps({
        "schema_version": 1,
        "monitoring_status": "not_recommended",
        "candidates": [],
    }), encoding="utf-8")
    record = DeepReportRecord(
        report_id="report_aaaaaaaaaaaaaaaa",
        profile="etf_deep_research",
        symbol="588870.SH",
        security_name="科创板新能源ETF",
        report_date="2026-07-18",
        data_as_of="2026-07-18T10:00:00+08:00",
        status="completed",
        quality_status="passed_with_gaps",
        revision=2,
        analysis_modules={
            "holding_penetration": ModuleResult(
                status="passed",
                coverage=0.42,
                details={"selected_count": 4, "explanation_coverage": 0.51},
            ),
            "component_research": ModuleResult(
                status="warning",
                coverage=0.5,
                details={
                    "research_coverage": 0.31,
                    "fully_supported_coverage": 0.18,
                    "reusable_count": 2,
                    "missing_count": 2,
                },
            ),
        },
        artifacts=[{
            "artifact_id": "monitoring_bundle",
            "artifact_type": "application/json",
            "artifact_role": "monitoring",
            "filename": monitoring_path.name,
            "path": str(monitoring_path),
            "available": True,
        }],
    )

    registered = service.register_deep_report(record)
    assert registered is not None
    link = registered["knowledge_link"]
    code = link["internal_reference_code"]
    assert code.startswith("VT-ETF-588870SH-20260718-R02-")
    assert link["monitoring_bundle_status"] == "not_recommended"
    assert link["monitoring_candidate_count"] == 0
    assert link["etf_penetration"]["selected_count"] == 4
    assert link["etf_penetration"]["research_coverage"] == 0.31
    assert service.get_report_by_reference_code(code)["report_id"] == record.report_id

    app = FastAPI()
    register_report_library_routes(app, lambda: None, get_service=lambda: service)
    resolved = TestClient(app).get(f"/report-library/references/{code}")
    assert resolved.status_code == 200
    assert resolved.json()["report_id"] == record.report_id


def test_deep_report_refresh_inherits_family_and_compact_timeline(tmp_path) -> None:
    service = _service(tmp_path)
    parent = DeepReportRecord(
        report_id="report_1111111111111111",
        profile="equity_deep_research",
        symbol="000651.SZ",
        security_name="格力电器",
        report_date="2026-07-10",
        data_as_of="2026-07-10T15:00:00+08:00",
        status="completed",
        quality_status="passed",
    )
    child = DeepReportRecord(
        report_id="report_2222222222222222",
        parent_report_id=parent.report_id,
        profile="equity_deep_research",
        symbol="000651.SZ",
        security_name="格力电器",
        report_date="2026-07-20",
        data_as_of="2026-07-20T15:00:00+08:00",
        status="completed",
        quality_status="passed",
        revision=2,
        revision_mode="full_refresh",
    )

    first = service.register_deep_report(parent)
    second = service.register_deep_report(child)
    compact = service.subject("000651.SZ")
    full = service.subject("000651.SZ", history_mode="full")

    assert first is not None and second is not None
    assert second["family_id"] == first["family_id"]
    assert [item["report_id"] for item in compact["timeline"]] == [child.report_id]
    assert [item["report_id"] for item in full["timeline"]] == [
        child.report_id,
        parent.report_id,
    ]


def test_latest_partial_report_keeps_recent_complete_baseline(tmp_path) -> None:
    service = _service(
        tmp_path,
        now=datetime(2026, 7, 21, 2, 0, tzinfo=timezone.utc),
    )
    complete = service.register_daily_run(
        _daily_run("daily-complete", "2026-07-20"),
        _aggregate("observe", "完整观点"),
    )[0]
    partial = service.register_daily_run(
        _daily_run("daily-partial", "2026-07-21", warnings=["部分研究缺口"]),
        _aggregate("reduce", "最新但有缺口", warnings=["部分研究缺口"]),
    )[0]

    current = service.subject("588870.SH")["current"]["daily"]

    assert current["latest"]["report_id"] == partial["report_id"]
    assert current["latest_complete"]["report_id"] == complete["report_id"]
    assert current["latest"]["coverage_status"] == "partial"


def test_cross_horizon_is_not_a_conflict_and_same_horizon_can_diverge(tmp_path) -> None:
    service = _service(tmp_path)
    daily = service.register_daily_run(
        _daily_run("daily-a", "2026-07-18"),
        _aggregate("add", "日度偏多"),
    )[0]
    weekly_id = "weekly_588870"
    weekly = service.register_report(
        ReportEnvelope(
            report_id=weekly_id,
            family_id=weekly_id,
            report_kind="weekly_review",
            subject_type="symbol",
            subject_key="588870.SH",
            symbol="588870.SH",
            security_name="科创板新能源ETF",
            status="published",
            report_quality_status="passed",
            coverage_status="complete",
            generated_at="2026-07-18T10:00:00+08:00",
            data_as_of="2026-07-18T10:00:00+08:00",
            source_type="weekly_review",
            source_id=weekly_id,
            viewpoints=[
                ReportViewpoint(
                    viewpoint_id="weekly-view",
                    report_id=weekly_id,
                    horizon="weekly",
                    stance="bearish",
                    action="reduce",
                    confidence="medium",
                )
            ],
        )
    )

    cross = service.compare(
        [
            {"report_id": daily["report_id"], "horizon": "daily"},
            {"report_id": weekly["report_id"], "horizon": "weekly"},
        ]
    )

    assert cross["deltas"][0]["relation"] == "different_horizon"

    second_daily = service.register_daily_run(
        _daily_run("daily-b", "2026-07-21"),
        _aggregate("reduce", "日度观点转弱"),
    )[0]
    same = service.compare(
        [
            {"report_id": daily["report_id"], "horizon": "daily"},
            {"report_id": second_daily["report_id"], "horizon": "daily"},
        ]
    )
    assert same["deltas"][0]["relation"] == "diverged"


class _Summarizer:
    prompt_version = "test-v1"

    def __init__(self) -> None:
        self.payload = None

    def summarize(self, payload):
        self.payload = payload
        citation = payload["allowlisted_claims"][0]
        return {
            "summary": "观点随新报告发生变化。",
            "items": [{"text": "行动意见变化。", "citations": [citation]}],
        }


def test_ai_comparison_receives_only_structured_deltas_and_allowlisted_claims(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("VIBE_TRADING_REPORT_VIEWPOINT_AI_ENABLED", "1")
    summarizer = _Summarizer()
    service = _service(tmp_path, summarizer=summarizer)
    first = service.register_daily_run(
        _daily_run("daily-a", "2026-07-18"),
        _aggregate("observe", "旧观点"),
    )[0]
    second = service.register_daily_run(
        _daily_run("daily-b", "2026-07-21"),
        _aggregate("reduce", "新观点"),
    )[0]

    result = service.compare(
        [
            {"report_id": first["report_id"], "horizon": "daily"},
            {"report_id": second["report_id"], "horizon": "daily"},
        ],
        include_ai_summary=True,
    )

    assert result["ai_summary"]["status"] == "completed"
    encoded = json.dumps(summarizer.payload, ensure_ascii=False)
    assert "markdown" not in encoded.casefold()
    assert "allowlisted_claims" in summarizer.payload
    assert result["ai_summary"]["items"][0]["citations"][0]["claim_id"]


def test_report_library_api_filters_subjects_and_compares(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_REPORT_LIBRARY_ENABLED", "1")
    service = _service(tmp_path)
    first = service.register_daily_run(
        _daily_run("daily-a", "2026-07-18"),
        _aggregate("observe", "观察"),
    )[0]
    second = service.register_daily_run(
        _daily_run("daily-b", "2026-07-21"),
        _aggregate("reduce", "减仓"),
    )[0]
    source = service.knowledge.store_document(
        url="https://www.sse.com.cn/disclosure/annual.html",
        content="Official annual filing full text with revenue, profit and cash-flow evidence.",
        title="2025 annual report",
        publisher="Shanghai Stock Exchange",
        source_class="regulatory_filing",
    )
    chunk_ref = service.knowledge.read_document(source.document_ref, limit=1)["chunks"][0][
        "chunk_ref"
    ]
    service.knowledge.link_report(
        report_id=second["report_id"],
        revision=1,
        symbol="588870.SH",
        quality_status="passed",
        evidence=[{
            "evidence_id": "ev-report-library-api",
            "symbol": "588870.SH",
            "domain": "financial_statements",
            "summary": "Annual filing evidence",
            "status": "verified",
            "metadata": {
                "document_ref": source.document_ref,
                "chunk_refs": [chunk_ref],
                "source_strength": "A",
            },
        }],
        facts=[],
        claims=[{
            "claim_id": "claim-report-library-api",
            "section_id": "financial_quality",
            "text": "Annual filing supports the report conclusion",
            "evidence_ids": ["ev-report-library-api"],
        }],
    )
    note_id = service.knowledge.index_research_session(
        session_id="research-session-api",
        symbol="588870.SH",
        role="assistant",
        content="Unverified research note for the subject dossier.",
        message_id="research-message-api",
    )

    class _OfficialService:
        def refresh(self, symbol: str, *, force: bool):
            return {"symbol": symbol, "force": force, "ingested": 1}

        def annual_report_coverage(self, symbol: str, *, years: list[int]):
            return {
                "symbol": symbol,
                "requested_years": years,
                "covered_years": years[:1],
                "missing_years": years[1:],
                "coverage_ratio": 1 / len(years),
                "documents_by_year": {},
            }

        def backfill_annual_reports(
            self,
            symbol: str,
            *,
            years: list[int],
            force: bool,
            progress_callback=None,
        ):
            if progress_callback is not None:
                for year in years:
                    for stage in (
                        "discovering", "discovered", "downloading", "parsing", "validating", "completed",
                    ):
                        progress_callback({"stage": stage, "year": year, "document_ref": f"doc-{year}"})
            return {
                "symbol": symbol,
                "status": "completed",
                "collection_scope": "historical_annual_reports",
                "refreshed": len(years),
                "failed": 0,
                "document_refs": [f"doc-{year}" for year in years],
                "provider_attempts": [],
                "coverage": {
                    "symbol": symbol,
                    "requested_years": years,
                    "covered_years": years,
                    "missing_years": [],
                    "coverage_ratio": 1.0,
                    "documents_by_year": {},
                },
                "structured": {},
            }

    class _ExtractionService:
        def extract_subject(self, symbol: str, *, force: bool, repair_only: bool = False):
            return {
                "subject_key": symbol,
                "force": force,
                "repair_only": repair_only,
                "documents": 0,
                "validated": 0,
            }

    class _DataService:
        def get_context(self, **kwargs):
            assert kwargs == {
                "symbols": ["588870.SH"],
                "purpose": "holding",
                "include": ["reports"],
                "force_live": True,
            }
            return {
                "research": {
                    "reports": {
                        "items": {
                            "588870.SH": {
                                "mode": "live",
                                "documents": [
                                    {"url": "https://data.eastmoney.com/report/one"},
                                    {"url": "https://data.eastmoney.com/report/two"},
                                ],
                            }
                        }
                    }
                }
            }

    app = FastAPI()
    annual_jobs = AnnualReportBackfillJobService(
        AnnualReportBackfillJobStore(tmp_path / "annual-report-jobs.sqlite3")
    )
    register_report_library_routes(
        app,
        lambda: None,
        get_service=lambda: service,
        get_official_service=lambda: _OfficialService(),
        get_data_service=lambda: _DataService(),
        get_financial_extraction_service=lambda: _ExtractionService(),
        get_annual_backfill_job_service=lambda: annual_jobs,
    )
    client = TestClient(app)

    listed = client.get("/report-library/reports", params={"query": "588870"})
    assert listed.status_code == 200
    assert len(listed.json()["reports"]) == 2
    assert listed.json()["reports"][0]["artifacts"][0]["url"].startswith(
        "/portfolio/daily-runs/"
    )

    status = client.get("/report-library/status")
    assert status.status_code == 200
    assert status.json()["report_count"] == 4
    assert status.json()["subject_count"] == 2
    assert status.json()["index_failure_count"] == 0
    assert status.json()["source_archive"]["orphan_report_sources"] == 0

    subject = client.get("/report-library/subjects/588870.SH")
    assert subject.status_code == 200
    assert subject.json()["current"]["daily"]["latest"]["report_id"] == second["report_id"]

    sources = client.get("/report-library/subjects/588870.SH/sources")
    assert sources.status_code == 200
    assert sources.json()["sources"][0]["document_ref"] == source.document_ref

    report_sources = client.get(
        f"/report-library/reports/{second['report_id']}/sources"
    )
    assert report_sources.status_code == 200
    assert report_sources.json()["sources"][0]["section_ids"] == ["financial_quality"]

    report = client.get(f"/report-library/reports/{second['report_id']}")
    assert report.status_code == 200
    assert report.json()["sources"][0]["document_ref"] == source.document_ref

    notes = client.get("/report-library/subjects/588870.SH/research-notes")
    assert notes.status_code == 200
    assert notes.json()["notes"][0]["note_claim_id"] == note_id
    assert notes.json()["notes"][0]["derived_status"] == "unverified"

    snapshots = client.get(
        "/report-library/subjects/588870.SH/financial-snapshots"
    )
    assert snapshots.status_code == 200
    assert snapshots.json()["count"] == 0

    rebuilt = client.post(
        "/report-library/subjects/588870.SH/financial-snapshots/rebuild"
    )
    assert rebuilt.status_code == 200
    assert rebuilt.json()["validated"] == 0
    assert rebuilt.json()["repair_only"] is True

    refreshed = client.post("/report-library/subjects/588870.SH/sources/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["ingested"] == 1

    annual_coverage = client.get(
        "/report-library/subjects/588870.SH/annual-reports/coverage",
        params={"start_year": 2024, "end_year": 2025},
    )
    assert annual_coverage.status_code == 200
    assert annual_coverage.json()["missing_years"] == [2024]

    annual_backfill = client.post(
        "/report-library/subjects/588870.SH/annual-reports/backfill",
        json={"years": [2025, 2024], "force": False},
    )
    assert annual_backfill.status_code == 200
    assert annual_backfill.json()["coverage"]["covered_years"] == [2025, 2024]
    accepted = client.post(
        "/report-library/subjects/588870.SH/annual-reports/backfill-jobs",
        json={"years": [2025, 2024], "force": False},
    )
    assert accepted.status_code == 202
    job_id = accepted.json()["job_id"]
    job = client.get(
        f"/report-library/subjects/588870.SH/annual-reports/backfill-jobs/{job_id}"
    )
    assert job.status_code == 200
    assert job.json()["years"] == [2025, 2024]
    latest_job = client.get(
        "/report-library/subjects/588870.SH/annual-reports/backfill-jobs/latest"
    )
    assert latest_job.status_code == 200
    assert latest_job.json()["job"]["job_id"] == job_id
    assert refreshed.json()["broker_research"] == {
        "status": "live",
        "linked": 2,
        "documents": 2,
    }

    compared = client.post(
        "/report-library/comparisons",
        json={
            "items": [
                {"report_id": first["report_id"], "horizon": "daily"},
                {"report_id": second["report_id"], "horizon": "daily"},
            ]
        },
    )
    assert compared.status_code == 200
    assert compared.json()["deltas"][0]["relation"] == "updated"


def test_etf_subject_exposes_product_profile_and_unified_refresh(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_REPORT_LIBRARY_ENABLED", "1")
    service = _service(tmp_path)
    service.register_daily_run(
        _daily_run("daily-etf-product", "2026-07-17"),
        _aggregate("observe", "ETF 产品档案测试"),
    )
    product = {
        "profile_snapshot_id": "etfprofile_api_fixture",
        "symbol": "588870.SH",
        "identity": {
            "manager": {"value": "汇添富基金管理股份有限公司", "status": "available"},
            "tracked_index_code": {"value": "000688.SH", "status": "available"},
        },
        "index_methodology": {
            "version": {"value": "V1.1", "status": "available"},
        },
        "product_metrics": {}, "sources": [], "peer_group": {"member_count": 8},
        "refresh_status": "completed", "refresh_errors": [],
    }
    valuation_percentile = {
        "snapshot_id": "etfvaluation_api_fixture",
        "symbol": "588870.SH",
        "tracked_index_code": "000688",
        "tracked_index_name": "科创50",
        "status": "available",
        "metrics": [{
            "key": "pe", "label": "PE · 市盈率", "value": 209.94,
            "percentile": 98.6, "temperature": "极热",
        }],
    }

    class _Products:
        def latest_profile(self, symbol):
            return product

        def get_or_refresh(self, symbol, **kwargs):
            return product

        def source_plan(self, symbol, **kwargs):
            return {
                "registry_version": "etf-source-rules-v1",
                "symbol": symbol,
                "phases": {
                    "product_profile": {
                        "rules": [{"rule_id": "sse.etf_product_catalog.v1"}]
                    },
                    "share_flow": {
                        "rules": [{"rule_id": "sse.etf_share_scale_history.v1"}]
                    },
                },
            }

    class _Instrument:
        def latest_snapshot(self, symbol):
            return {"snapshot_id": "instrument", "symbol": symbol, "identity": {}}

        def refresh(self, symbol):
            return self.latest_snapshot(symbol)

    class _ValuationPercentile:
        def latest_snapshot(self, symbol):
            assert symbol == "588870.SH"
            return valuation_percentile

        def refresh(self, symbol, *, tracked_index_code, tracked_index_name):
            assert symbol == "588870.SH"
            assert tracked_index_code == "000688.SH"
            assert tracked_index_name == ""
            return valuation_percentile

    universe_snapshot = SimpleNamespace(
        to_dict=lambda: {
            "snapshot_id": "universe", "symbol": "588870.SH",
            "data_as_of": "2026-06-30", "retrieved_at": "2026-07-18",
            "quality_status": "passed", "source_ids": ["index"],
            "payload": {
                "etf_symbol": "588870.SH", "tracked_index_code": "000688.SH",
                "tracked_index_name": "上证科创板50成份指数", "components": [],
                "source_type": "official_index_weights", "source_urls": [],
            },
        }
    )

    class _Universe:
        def latest_snapshot(self, symbol):
            return universe_snapshot

        def get_or_refresh(self, symbol, **kwargs):
            return SimpleNamespace(snapshot=universe_snapshot)

    app = FastAPI()
    register_report_library_routes(
        app,
        lambda: None,
        get_service=lambda: service,
        get_etf_product_profile_service=lambda: _Products(),
        get_etf_valuation_percentile_service=lambda: _ValuationPercentile(),
        get_instrument_profile_service=lambda: _Instrument(),
        get_etf_universe_service=lambda: _Universe(),
    )
    client = TestClient(app)

    subject = client.get("/report-library/subjects/588870.SH")
    assert subject.status_code == 200
    assert subject.json()["etf_product"]["profile_snapshot_id"] == "etfprofile_api_fixture"
    assert subject.json()["profile"]["etf"]["product"]["identity"]["manager"]["value"] == "汇添富基金管理股份有限公司"
    assert subject.json()["profile"]["etf"]["valuation_percentile"]["metrics"][0]["percentile"] == 98.6
    assert subject.json()["source_bundle"] is not None

    rules = client.get(
        "/report-library/subjects/588870.SH/etf-profile/source-rules"
    )
    assert rules.status_code == 200
    assert rules.json()["registry_version"] == "etf-source-rules-v1"
    assert rules.json()["phases"]["share_flow"]["rules"][0]["rule_id"] == (
        "sse.etf_share_scale_history.v1"
    )

    refreshed = client.post("/report-library/subjects/588870.SH/etf-profile/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["status"] == "completed"
    assert refreshed.json()["profile"]["peer_group"]["member_count"] == 8
    assert refreshed.json()["valuation_percentile"]["tracked_index_code"] == "000688"
    assert refreshed.json()["sources"]["valuation_percentile"] == "available"


def test_report_library_exposes_and_refreshes_equity_historical_percentile(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("VIBE_TRADING_REPORT_LIBRARY_ENABLED", "1")
    service = _service(tmp_path)
    service.register_report(ReportEnvelope(
        report_id="equity-percentile-report",
        family_id="equity-percentile-report",
        report_kind="daily_holding",
        subject_type="symbol",
        subject_key="600036.SH",
        symbol="600036.SH",
        security_name="招商银行",
        status="published",
        report_quality_status="passed",
        coverage_status="complete",
        generated_at="2026-07-19T09:00:00+08:00",
        data_as_of="2026-07-17T15:00:00+08:00",
        source_type="test",
        source_id="equity-percentile-report",
    ))
    instrument = {
        "snapshot_id": "instrument-equity",
        "symbol": "600036.SH",
        "instrument_type": "company_equity",
        "identity": {"name": "招商银行", "currency": "CNY"},
    }
    percentile = {
        "schema_version": 2,
        "snapshot_id": "historicalpct-equity",
        "symbol": "600036.SH",
        "instrument_type": "company_equity",
        "valuation_basis": "company_valuation",
        "scope_label": "招商银行 · 公司估值",
        "status": "available",
        "metrics": [{
            "key": "pb_mrq", "label": "PB · 市净率", "value": 0.85,
            "percentile": 12.3, "temperature": "偏冷",
        }],
    }

    class _Instrument:
        def latest_snapshot(self, symbol):
            assert symbol == "600036.SH"
            return instrument

    class _HistoricalPercentile:
        supports_all_instruments = True

        def latest_snapshot(self, symbol):
            assert symbol == "600036.SH"
            return percentile

        def refresh(self, symbol, **kwargs):
            assert symbol == "600036.SH"
            assert kwargs["instrument_type"] == "company_equity"
            assert kwargs["instrument_name"] == "招商银行"
            assert kwargs["currency"] == "CNY"
            return percentile

    app = FastAPI()
    register_report_library_routes(
        app,
        lambda: None,
        get_service=lambda: service,
        get_instrument_profile_service=lambda: _Instrument(),
        get_instrument_historical_percentile_service=lambda: _HistoricalPercentile(),
    )
    client = TestClient(app)

    subject = client.get("/report-library/subjects/600036.SH")
    assert subject.status_code == 200
    assert subject.json()["historical_percentile"]["metrics"][0]["percentile"] == 12.3
    assert subject.json()["profile"]["equity"]["historical_percentile"]["scope_label"] == (
        "招商银行 · 公司估值"
    )
    assert subject.json()["etf_valuation_percentile"] is None

    refreshed = client.post(
        "/report-library/subjects/600036.SH/historical-percentile/refresh"
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["valuation_basis"] == "company_valuation"


def test_report_library_expands_monitoring_bundle_from_the_single_daily_json_artifact(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("VIBE_TRADING_REPORT_LIBRARY_ENABLED", "1")
    service = _service(tmp_path)
    bundle = {
        "schema_version": 1,
        "symbol": "588870.SH",
        "instrument_type": "etf",
        "horizon": "daily",
        "generated_at": "2026-07-18T09:00:00+08:00",
        "data_as_of": "2026-07-18T08:55:00+08:00",
        "valid_from": "2026-07-18T09:00:00+08:00",
        "valid_until": "2026-07-25T09:00:00+08:00",
        "review_due_at": "2026-07-19T09:00:00+08:00",
        "price_basis": {"adjustment": "raw", "currency": "CNY", "tick_size": 0.001},
        "monitoring_status": "data_insufficient",
        "price_volume_context": {},
        "candidates": [],
        "scenario_changes": [],
        "validation_errors": ["test gap"],
        "source": "structured_daily_report",
        "activation_policy": "manual_confirmation_required",
        "trade_execution": "forbidden",
    }
    artifact_path = tmp_path / "holding.json"
    artifact_path.write_text(
        json.dumps({"monitoring_bundle": bundle}, ensure_ascii=False), encoding="utf-8"
    )
    record = _daily_run("daily-structured", "2026-07-18", warnings=["test gap"])
    json_artifact = {
        "artifact_id": "daily-structured-holding-json",
        "kind": "holding_daily_json",
        "symbol": "588870.SH",
        "security_name": "科创板新能源ETF",
        "filename": "2026-07-18_588870.json",
        "media_type": "application/json",
        "sha256": "c" * 64,
        "revision": 1,
        "path": str(artifact_path),
    }
    record["artifacts"].append(json_artifact)
    aggregate = _aggregate("observe", "数据不足", warnings=["test gap"])
    aggregate["briefs"][0]["monitoring_bundle"] = bundle
    aggregate["briefs"][0]["monitoring_claims"] = [
        {
            "claim_id": "claim_daily_level",
            "section_id": "daily_level",
            "claim_type": "fact",
            "text": "关注1.85原始价格",
            "fact_ids": [],
            "evidence_ids": [],
        }
    ]
    registered = service.register_daily_run(record, aggregate)[0]
    assert registered["knowledge_link"]["monitoring_bundle_artifact_id"] == json_artifact["artifact_id"]
    with service.knowledge.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM claim_records WHERE claim_id='claim_daily_level'"
        ).fetchone()[0] == 1

    class _DailyStore:
        @staticmethod
        def resolve_artifact(run_id, artifact_id):
            assert (run_id, artifact_id) == (
                "daily-structured",
                "daily-structured-holding-json",
            )
            return json_artifact, artifact_path

    class _DailyService:
        store = _DailyStore()

    app = FastAPI()
    register_report_library_routes(
        app,
        lambda: None,
        get_service=lambda: service,
        get_daily_service=lambda: _DailyService(),
    )
    response = TestClient(app).get(f"/report-library/reports/{registered['report_id']}")

    assert response.status_code == 200
    assert response.json()["monitoring_bundle"]["source"] == "structured_daily_report"
    assert response.json()["monitoring_bundle"]["candidates"] == []


def test_index_failure_metric_is_persisted(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_REPORT_LIBRARY_ENABLED", "1")
    service = _service(tmp_path)

    service.record_index_failure(
        source_type="daily_run",
        source_id="daily-broken",
        error="aggregate unavailable",
    )

    status = service.status()
    assert status["enabled"] is True
    assert status["report_count"] == 0
    assert status["index_failure_count"] == 1
    assert status["last_index_failure"]["source_id"] == "daily-broken"


def test_v2_migration_creates_backup_for_existing_knowledge_database(tmp_path) -> None:
    path = tmp_path / "research.sqlite3"
    ResearchKnowledgeStore(path=path, object_dir=tmp_path / "objects")
    backup = path.with_suffix(path.suffix + ".pre-report-library-v2.bak")
    backup.unlink(missing_ok=True)
    with sqlite3.connect(path) as conn:
        for table in (
            "viewpoint_delta_cache",
            "report_relations",
            "report_artifact_links",
            "report_viewpoints",
            "report_catalog_entries",
            "report_library_meta",
        ):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("DELETE FROM research_knowledge_schema WHERE version=2")

    ResearchKnowledgeStore(path=path, object_dir=tmp_path / "objects")

    assert backup.exists()
