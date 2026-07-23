"""API contracts for persisted equity Deep Reports."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

import api_server
from src.reports.contracts import ModuleResult
from src.reports.etf_research import ETFResearchStore
from src.reports.profile import get_report_profile
from src.reports.service import DeepReportService
from src.research.knowledge import ResearchKnowledgeStore


class _Dispatcher:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def submit(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {"message_id": "msg-1", "attempt_id": "attempt-1"}


def _install_runtime(monkeypatch, tmp_path):
    reports = DeepReportService(tmp_path / "reports")
    dispatcher = _Dispatcher()
    monkeypatch.setattr(api_server, "_session_service", SimpleNamespace(deep_reports=reports))
    monkeypatch.setattr(api_server, "_session_dispatcher", dispatcher)
    return reports, dispatcher, TestClient(api_server.app)


def test_deep_report_list_detail_and_markdown_artifact(monkeypatch, tmp_path) -> None:
    reports, dispatcher, client = _install_runtime(monkeypatch, tmp_path)
    record = reports.begin(
        session_id="session-1",
        attempt_id="attempt-1",
        request_content="研究301308.SZ",
        generation_source="portfolio_monitor_autopilot",
        generation_reason="原报告缺失",
    )
    record = reports.mark_failed(record.report_id, "测试诊断")

    listed = client.get("/reports")
    assert listed.status_code == 200
    assert listed.json()[0]["report_id"] == record.report_id
    assert listed.json()[0]["profile"] == "equity_deep_research"
    assert listed.json()[0]["generation_source"] == "portfolio_monitor_autopilot"
    assert listed.json()[0]["generation_reason"] == "原报告缺失"

    detail = client.get(f"/reports/{record.report_id}?include_content=true")
    assert detail.status_code == 200
    assert "测试诊断" not in detail.json()["content"]
    assert "内部审计文件" in detail.json()["content"]
    assert detail.json()["content_role"] == "diagnostic"
    assert detail.json()["quality_status"] == "failed_validation"
    assert detail.json()["subject_profile"] is None

    formal_artifact = client.get(f"/reports/{record.report_id}/artifacts/markdown")
    assert formal_artifact.status_code == 409

    diagnostic = client.get(
        f"/reports/{record.report_id}/artifacts/diagnostic?download=0"
    )
    assert diagnostic.status_code == 200
    assert diagnostic.headers["content-disposition"].startswith("inline;")
    assert "测试诊断" not in diagnostic.content.decode("utf-8")
    assert "内部审计文件" in diagnostic.content.decode("utf-8")
    diagnostic_download = client.get(
        f"/reports/{record.report_id}/artifacts/diagnostic?download=1"
    )
    assert diagnostic_download.headers["content-disposition"].startswith("attachment;")

    unavailable_pdf = client.get(f"/reports/{record.report_id}/artifacts/pdf")
    assert unavailable_pdf.status_code == 409
    assert "did not pass validation" in unavailable_pdf.json()["detail"]

    rejected_archive = client.post(f"/reports/{record.report_id}/archive")
    assert rejected_archive.status_code == 409

    repair = client.post(
        f"/reports/{record.report_id}/repair",
        json={"instructions": "复用现有证据修复标准章节"},
    )
    assert repair.status_code == 409
    assert "用新数据更新" in repair.json()["detail"]
    assert dispatcher.calls == []


def test_etf_report_detail_returns_revision_bound_subject_profile(monkeypatch, tmp_path) -> None:
    reports, _dispatcher, client = _install_runtime(monkeypatch, tmp_path)
    record = reports.begin(
        session_id="session-etf-profile",
        attempt_id="attempt-etf-profile",
        request_content="研究 588870.SH",
        profile="etf_deep_research",
    )
    bound_profile = {
        "profile_snapshot_id": "etfprofile_bound_revision",
        "symbol": "588870.SH",
        "identity": {
            key: {
                "value": value, "status": "available", "source_ids": ["source-official"],
                "data_as_of": "2026-07-17", "semantics": "fixture",
            }
            for key, value in {
                "manager": "汇添富基金管理股份有限公司",
                "exchange": "上海证券交易所",
                "tracked_index_code": "000688.SH",
                "tracked_index_name": "上证科创板50成份指数",
            }.items()
        },
        "index_methodology": {}, "product_metrics": {}, "sources": [],
    }
    reports.attach_etf_analysis(record.report_id, {
        "profile": "etf_deep_research", "symbol": "588870.SH",
        "security_name": "科创50ETF汇添富", "data_as_of": "2026-07-17T15:00:00+08:00",
        "snapshot": {
            "symbol": "588870.SH", "data_as_of": "2026-07-17T15:00:00+08:00",
            "snapshot_ids": {
                "identity": "etfsnap_identity_bound000000",
                "universe": "etfsnap_universe_bound000000",
                "market": "etfsnap_market_bound00000000",
            },
            "coverage_ratio": 1.0, "price_verified": True,
            "subject_profile": bound_profile,
        },
        "module_statuses": {
            "identity": {"status": "passed", "coverage": 1.0},
            "universe": {"status": "passed", "coverage": 1.0},
            "market_data": {"status": "passed", "coverage": 1.0},
        },
    })

    detail = client.get(f"/reports/{record.report_id}")
    assert detail.status_code == 200
    assert detail.json()["subject_profile"] == bound_profile
    assert detail.json()["subject_profile"]["profile_snapshot_id"] == "etfprofile_bound_revision"


def test_message_and_revision_routes_preserve_explicit_deep_report_metadata(monkeypatch, tmp_path) -> None:
    reports, dispatcher, client = _install_runtime(monkeypatch, tmp_path)

    legacy = client.post(
        "/sessions/session-1/messages",
        json={"content": "普通聊天"},
    )
    assert legacy.status_code == 200
    assert dispatcher.calls[-1]["source_metadata"]["response_mode"] == "chat"
    assert dispatcher.calls[-1]["source_metadata"]["report_profile"] is None

    sent = client.post(
        "/sessions/session-1/messages",
        json={
            "content": "深度研究江波龙",
            "response_mode": "deep_report",
            "report_profile": "equity_deep_research",
            "routing_decision_id": "route-1",
        },
    )
    assert sent.status_code == 200
    assert dispatcher.calls[-1]["source_metadata"] == {
        "response_mode": "deep_report",
        "report_profile": "equity_deep_research",
        "routing_decision_id": "route-1",
    }

    etf_sent = client.post(
        "/sessions/session-1/messages",
        json={
            "content": "深度研究 588870.SH",
            "response_mode": "deep_report",
            "report_profile": "etf_deep_research",
        },
    )
    assert etf_sent.status_code == 200
    assert dispatcher.calls[-1]["source_metadata"] == {
        "response_mode": "deep_report",
        "report_profile": "etf_deep_research",
        "routing_decision_id": None,
    }

    record = reports.begin(
        session_id="session-1",
        attempt_id="attempt-existing",
        request_content="研究301308.SZ",
    )
    repairable = reports.begin(
        session_id="session-1",
        attempt_id="attempt-repairable",
        request_content="修复章节",
    )
    repairable.status = "completed"
    repairable.quality_status = "failed_validation"
    repairable.analysis_modules["executive_summary"] = ModuleResult(
        status="failed_validation",
        reason="missing section: 核心结论",
    )
    reports._write_manifest(repairable)
    repaired = client.post(
        f"/reports/{repairable.report_id}/repair",
        json={"instructions": "复用现有证据修复标准章节"},
    )
    assert repaired.status_code == 200
    repair_call = dispatcher.calls[-1]
    assert repair_call["source_metadata"] == {
        "response_mode": "deep_report",
        "report_profile": "equity_deep_research",
        "parent_report_id": repairable.report_id,
        "revision_sections": [
            section_id
            for section_id, _heading in get_report_profile("equity_deep_research")["required_sections"]
        ],
        "revision_mode": "repair",
    }
    assert "[PARENT_REPORT_VALIDATION_REPAIR]" in repair_call["content"]
    assert "章节局部 status=passed 不代表" in repair_call["content"]

    revised = client.post(
        f"/reports/{record.report_id}/revisions",
        json={"section_ids": ["counter_thesis"], "instructions": "更新风险与催化剂"},
    )
    assert revised.status_code == 200
    metadata = dispatcher.calls[-1]["source_metadata"]
    assert metadata["parent_report_id"] == record.report_id
    assert metadata["revision_sections"] == ["counter_thesis"]
    assert metadata["revision_mode"] == "section_revision"
    assert metadata["report_profile"] == "equity_deep_research"

    refreshed = client.post(
        f"/reports/{record.report_id}/refresh",
        json={"instructions": "使用最新数据更新"},
    )
    assert refreshed.status_code == 200
    assert dispatcher.calls[-1]["source_metadata"]["revision_mode"] == "full_refresh"

    followed_up = client.post(
        f"/reports/{record.report_id}/followups",
        json={"content": "解释当前报告的风险"},
    )
    assert followed_up.status_code == 200
    assert dispatcher.calls[-1]["source_metadata"] == {
        "response_mode": "chat",
        "linked_report_id": record.report_id,
    }

    legacy_resume = client.post(
        f"/reports/{record.report_id}/resume",
        json={"content": "继续解释"},
    )
    assert legacy_resume.status_code == 200
    assert dispatcher.calls[-1]["source_metadata"] == {
        "response_mode": "chat",
        "linked_report_id": record.report_id,
    }

    invalid = client.post(
        f"/reports/{record.report_id}/revisions",
        json={"section_ids": ["unknown"], "instructions": "bad"},
    )
    assert invalid.status_code == 400


def test_extended_evidence_enrichment_requires_consent_and_records_scope(monkeypatch, tmp_path) -> None:
    reports, dispatcher, client = _install_runtime(monkeypatch, tmp_path)
    record = reports.begin(
        session_id="session-1",
        attempt_id="attempt-existing",
        request_content="研究588870.SH",
        profile="etf_deep_research",
    )
    record.status = "completed"
    record.quality_status = "passed_with_gaps"
    record.symbol = "588870.SH"
    record.security_name = "科创50ETF"
    record.analysis_modules = {
        key: ModuleResult(status="passed")
        for key, _ in get_report_profile("etf_deep_research")["required_sections"]
    }
    record.analysis_modules["holding_penetration"] = ModuleResult(
        status="insufficient_evidence",
        reason="缺少可比期间数据",
    )
    reports._write_manifest(record)

    denied = client.post(
        f"/reports/{record.report_id}/refresh",
        json={
            "instructions": "补齐往年数据",
            "research_depth": "extended",
            "consent_to_extended_research": False,
        },
    )
    assert denied.status_code == 400
    assert dispatcher.calls == []

    accepted = client.post(
        f"/reports/{record.report_id}/refresh",
        json={
            "instructions": "补齐往年数据",
            "research_depth": "extended",
            "consent_to_extended_research": True,
        },
    )

    assert accepted.status_code == 200
    assert accepted.json()["research_depth"] == "extended"
    assert accepted.json()["token_notice_acknowledged"] is True
    call = dispatcher.calls[-1]
    assert call["source_metadata"] == {
        "response_mode": "deep_report",
        "report_profile": "etf_deep_research",
        "parent_report_id": record.report_id,
        "revision_mode": "full_refresh",
        "research_depth": "extended",
        "extended_research_consent": True,
        "generation_reason": "用户同意补齐缺失资料后重新生成",
    }
    assert "已知悉这会增加研究耗时和 Token 消耗" in call["content"]
    assert "关键持仓穿透" in call["content"]
    assert "扩展搜集后仍无法核实" in call["content"]


def test_equity_extended_refresh_emits_auditable_enrichment_plan(monkeypatch, tmp_path) -> None:
    reports, dispatcher, client = _install_runtime(monkeypatch, tmp_path)
    record = reports.begin(
        session_id="session-1",
        attempt_id="attempt-existing",
        request_content="研究000651.SZ",
        profile="equity_deep_research",
    )
    record.status = "completed"
    record.quality_status = "passed_with_gaps"
    record.symbol = "000651.SZ"
    record.security_name = "格力电器"
    record.analysis_modules = {
        "financial_quality": ModuleResult(status="passed"),
        "business_position": ModuleResult(status="insufficient_evidence"),
        "implied_expectations": ModuleResult(status="insufficient_evidence"),
        "terminal_narrative": ModuleResult(status="insufficient_evidence"),
        "terminal_scenarios": ModuleResult(status="not_requested"),
    }
    reports._write_manifest(record)

    response = client.post(
        f"/reports/{record.report_id}/refresh",
        json={
            "instructions": "补齐行业、一致预期与往年年报",
            "research_depth": "extended",
            "consent_to_extended_research": True,
            "historical_annual_years": 5,
        },
    )

    assert response.status_code == 200
    metadata = dispatcher.calls[-1]["source_metadata"]
    plan = metadata["research_enrichment_plan"]
    assert plan["research_depth"] == "extended"
    assert [item["task_id"] for item in plan["tasks"]] == [
        "annual_filings",
        "business_position",
        "consensus",
        "terminal_inputs",
    ]
    assert len(plan["tasks"][0]["target_years"]) == 5
    assert "record_research_attempt" in dispatcher.calls[-1]["content"]


def test_research_knowledge_search_source_and_history_routes(monkeypatch, tmp_path) -> None:
    _reports, _dispatcher, client = _install_runtime(monkeypatch, tmp_path)
    store = ResearchKnowledgeStore(
        path=tmp_path / "research.sqlite3",
        object_dir=tmp_path / "objects",
    )
    import src.research.knowledge as knowledge_module

    monkeypatch.setenv("VIBE_TRADING_RESEARCH_KNOWLEDGE_ENABLED", "1")
    monkeypatch.setattr(knowledge_module, "_shared_store", store)
    document = store.store_document(
        url="https://example.test/filing",
        content="# 正式公告\n\n2025年度营业收入为10亿元。",
        publisher="交易所",
        published_at="2026-03-01",
    )
    evidence = {
        "evidence_id": "ev-api", "symbol": "603738.SH", "domain": "announcement",
        "published_at": "2026-03-01", "summary": "正式公告披露营业收入。",
        "metadata": {
            "document_ref": document.document_ref,
            "chunk_refs": [document.chunk_catalog[0]["chunk_ref"]],
            "source_strength": "A",
        },
    }
    fact = {
        "fact_id": "fact-api", "symbol": "603738.SH", "metric": "revenue",
        "value": "10", "unit": "亿元", "period": "2025",
        "evidence_ids": ["ev-api"], "validation_status": "pass",
        "metadata": {"currency": "CNY", "scope_key": "consolidated"},
    }
    store.link_report(
        report_id="report_aaaaaaaaaaaaaaaa", revision=1, symbol="603738.SH",
        quality_status="passed", evidence=[evidence], facts=[fact], claims=[],
    )

    searched = client.get("/research/knowledge/search", params={"symbol": "603738.SH"})
    assert searched.status_code == 200
    assert searched.json()["facts"][0]["fact_id"] == "fact-api"

    source = client.get(f"/research/sources/{document.document_ref}")
    assert source.status_code == 200
    assert source.json()["chunks"][0]["text"].startswith("2025")

    history = client.get("/research/symbols/603738.SH/history")
    assert history.status_code == 200
    assert history.json()["reports"][0]["report_id"] == "report_aaaaaaaaaaaaaaaa"


def test_etf_reuse_metrics_route_uses_shared_research_database(monkeypatch, tmp_path) -> None:
    _reports, _dispatcher, client = _install_runtime(monkeypatch, tmp_path)
    import src.reports.etf_research as etf_module

    store = ETFResearchStore(tmp_path / "research.sqlite3")
    monkeypatch.setattr(etf_module, "_shared_store", store)

    response = client.get("/research/etf/588870.SH/reuse-metrics")

    assert response.status_code == 200
    assert response.json() == {
        "symbol": "588870.SH",
        "requests": 0,
        "cache_hits": 0,
        "cache_hit_ratio": 0.0,
        "module_runs": 0,
        "model_runs": 0,
        "deterministic_runs": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "saved_tokens": 0,
        "decision_counts": {},
    }


def test_repair_rejects_deterministic_hard_gate_and_requires_refresh(monkeypatch, tmp_path) -> None:
    reports, dispatcher, client = _install_runtime(monkeypatch, tmp_path)
    record = reports.begin(
        session_id="session-1",
        attempt_id="attempt-existing",
        request_content="研究603738.SH",
    )
    record.status = "completed"
    record.quality_status = "failed_validation"
    record.analysis_modules["market_data"] = ModuleResult(
        status="failed_validation",
        reason="timestamped_price_and_market_cap_required",
    )
    reports._write_manifest(record)

    response = client.post(
        f"/reports/{record.report_id}/repair",
        json={"instructions": "修复报告"},
    )

    assert response.status_code == 409
    assert "用新数据更新" in response.json()["detail"]
    assert dispatcher.calls == []


def test_archive_publishes_existing_formal_markdown_without_agent_run(monkeypatch, tmp_path) -> None:
    reports, dispatcher, client = _install_runtime(monkeypatch, tmp_path)
    record = reports.begin(
        session_id="session-1",
        attempt_id="attempt-existing",
        request_content="研究301308.SZ",
    )
    report_dir = tmp_path / "reports" / record.report_id
    markdown_path = report_dir / "report.md"
    markdown_path.write_text("# 江波龙（301308.SZ）穿透式深度研究\n\n正式正文。\n", encoding="utf-8")
    record.status = "completed"
    record.quality_status = "passed"
    record.delivery_kind = "report"
    record.symbol = "301308.SZ"
    record.security_name = "江波龙"
    record.artifacts = [{
        "artifact_id": "markdown",
        "artifact_type": "text/markdown",
        "artifact_role": "report",
        "filename": "2026-07-17_江波龙（301308.SZ）_穿透式深度研究.md",
        "path": str(markdown_path),
        "available": True,
        "previewable": True,
    }]
    reports._write_manifest(record)

    from src.tools.obsidian_publish_tool import PublishObsidianNoteTool

    monkeypatch.setattr(
        PublishObsidianNoteTool,
        "execute",
        lambda self, **kwargs: json.dumps({
            "status": "ok",
            "path": kwargs["path"],
            "bytes_written": len(kwargs["content"].encode("utf-8")),
        }, ensure_ascii=False),
    )

    response = client.post(f"/reports/{record.report_id}/archive")

    assert response.status_code == 200
    assert response.json()["path"].endswith("_穿透式深度研究.md")
    assert dispatcher.calls == []


def test_monitor_autopilot_queues_one_durable_deep_report_with_visible_origin(
    monkeypatch, tmp_path,
) -> None:
    reports = DeepReportService(tmp_path / "auto-reports")

    class AutoSessionService:
        def __init__(self) -> None:
            self.deep_reports = reports
            self.sessions: list[SimpleNamespace] = []

        def create_session(self, *, title: str, config: dict):
            session = SimpleNamespace(
                session_id=f"auto-session-{len(self.sessions) + 1}",
                title=title,
                config=config,
            )
            self.sessions.append(session)
            return session

    class QueueStore:
        def __init__(self) -> None:
            self.item = None

        def get_by_source_event(self, source: str, source_event_id: str):
            assert source == "api"
            assert source_event_id == "auto-equity-deep-report:2026-07-16:301308.SZ"
            return self.item

    class AutoDispatcher:
        def __init__(self) -> None:
            self.store = QueueStore()
            self.calls: list[dict] = []

        async def submit(self, session_id: str, content: str, **kwargs):
            self.calls.append({"session_id": session_id, "content": content, **kwargs})
            self.store.item = SimpleNamespace(
                status="pending",
                job_id="dispatch-1",
                session_id=session_id,
                attempt_id=None,
            )
            return {
                "status": "queued",
                "job_id": "dispatch-1",
                "attempt_id": None,
                "deduplicated": False,
            }

    service = AutoSessionService()
    dispatcher = AutoDispatcher()
    monkeypatch.setattr(api_server, "_get_session_service", lambda: service)
    monkeypatch.setattr(api_server, "_get_session_dispatcher", lambda: dispatcher)
    payload = {
        "job_id": "planner-1",
        "symbol": "301308.SZ",
        "security_name": "江波龙",
        "research_date": "2026-07-16",
        "research_reasons": ["report_stale", "report_data_limited"],
        "trigger_type": "scheduled_refresh",
    }

    created = asyncio.run(api_server._queue_monitor_auto_deep_report(payload))
    assert created["status"] == "queued"
    assert created["session_id"] == "auto-session-1"
    assert service.sessions[0].title == "江波龙（301308.SZ）穿透式深度研究 · AI自主监控"
    call = dispatcher.calls[0]
    assert "穿透式单股深度研究报告" in call["content"]
    assert call["source"] == "api"
    assert call["source_event_id"] == "auto-equity-deep-report:2026-07-16:301308.SZ"
    assert call["include_shell_tools"] is False
    assert call["source_metadata"] == {
        "response_mode": "deep_report",
        "report_profile": "equity_deep_research",
        "generation_source": "portfolio_monitor_autopilot",
        "generation_reason": "report_stale、report_data_limited",
        "monitor_planner_job_id": "planner-1",
        "monitor_trigger_type": "scheduled_refresh",
    }

    deduplicated = asyncio.run(api_server._queue_monitor_auto_deep_report(payload))
    assert deduplicated["deduplicated"] is True
    assert deduplicated["job_id"] == "dispatch-1"
    assert len(service.sessions) == 1
    assert len(dispatcher.calls) == 1


def test_monitor_autopilot_routes_etf_to_etf_deep_research(monkeypatch, tmp_path) -> None:
    reports = DeepReportService(tmp_path / "auto-etf-reports")

    class AutoSessionService:
        deep_reports = reports

        def __init__(self) -> None:
            self.session = None

        def create_session(self, *, title: str, config: dict):
            self.session = SimpleNamespace(
                session_id="auto-etf-session",
                title=title,
                config=config,
            )
            return self.session

    class QueueStore:
        def get_by_source_event(self, source: str, source_event_id: str):
            assert source == "api"
            assert source_event_id == "auto-etf-deep-report:2026-07-20:588870.SH"
            return None

    class AutoDispatcher:
        store = QueueStore()

        def __init__(self) -> None:
            self.call = None

        async def submit(self, session_id: str, content: str, **kwargs):
            self.call = {"session_id": session_id, "content": content, **kwargs}
            return {
                "status": "queued",
                "job_id": "dispatch-etf",
                "attempt_id": None,
                "deduplicated": False,
            }

    service = AutoSessionService()
    dispatcher = AutoDispatcher()
    monkeypatch.setattr(api_server, "_get_session_service", lambda: service)
    monkeypatch.setattr(api_server, "_get_session_dispatcher", lambda: dispatcher)

    result = asyncio.run(
        api_server._queue_monitor_auto_deep_report(
            {
                "job_id": "planner-etf",
                "symbol": "588870.SH",
                "security_name": "科创50ETF汇添富",
                "research_date": "2026-07-20",
                "research_reasons": ["report_stale"],
                "trigger_type": "scheduled_refresh",
            }
        )
    )

    assert result["status"] == "queued"
    assert service.session.config["research_session"]["kind"] == "etf_deep_research"
    assert "穿透式ETF深度研究报告" in dispatcher.call["content"]
    assert dispatcher.call["source_event_id"] == "auto-etf-deep-report:2026-07-20:588870.SH"
    assert dispatcher.call["source_metadata"]["report_profile"] == "etf_deep_research"


def test_monitor_autopilot_refreshes_inadequate_structural_report_as_one_revision(
    monkeypatch, tmp_path,
) -> None:
    reports = DeepReportService(tmp_path / "auto-structural-refresh-reports")
    parent = reports.begin(
        session_id="structural-parent-session",
        attempt_id="structural-parent-attempt",
        request_content="research 513120.SH",
        profile="etf_deep_research",
    )
    parent.symbol = "513120.SH"
    parent.security_name = "HK创新药"
    parent.status = "completed"
    parent.quality_status = "passed_with_gaps"
    reports._write_manifest(parent)

    class AutoSessionService:
        deep_reports = reports

        def create_session(self, **_kwargs):
            raise AssertionError("structural refresh must reuse the parent report session")

    class QueueStore:
        def __init__(self) -> None:
            self.item = None

        def get_by_source_event(self, source: str, source_event_id: str):
            assert source == "api"
            assert source_event_id == (
                f"auto-etf-deep-report-structural-refresh:513120.SH:{parent.report_id}"
            )
            return self.item

    class AutoDispatcher:
        def __init__(self) -> None:
            self.store = QueueStore()
            self.calls: list[dict] = []

        async def submit(self, session_id: str, content: str, **kwargs):
            self.calls.append({"session_id": session_id, "content": content, **kwargs})
            self.store.item = SimpleNamespace(
                status="pending",
                job_id="dispatch-structural-refresh",
                session_id=session_id,
                attempt_id="attempt-structural-refresh",
            )
            return {
                "status": "queued",
                "job_id": "dispatch-structural-refresh",
                "attempt_id": "attempt-structural-refresh",
                "deduplicated": False,
            }

    dispatcher = AutoDispatcher()
    monkeypatch.setattr(api_server, "_get_session_service", lambda: AutoSessionService())
    monkeypatch.setattr(api_server, "_get_session_dispatcher", lambda: dispatcher)
    payload = {
        "job_id": "planner-structural-refresh",
        "symbol": "513120.SH",
        "security_name": "HK创新药",
        "research_date": "2026-07-20",
        "research_reasons": ["structural_monitoring_not_recommended"],
        "trigger_type": "report_ready",
        "structural_refresh": True,
        "parent_report_id": parent.report_id,
        "source_bundle_sha256": "a" * 64,
    }

    created = asyncio.run(api_server._queue_monitor_auto_deep_report(payload))
    assert created["status"] == "queued"
    assert created["parent_report_id"] == parent.report_id
    assert created["revision_mode"] == "full_refresh"
    call = dispatcher.calls[0]
    assert call["session_id"] == parent.session_id
    assert call["source_metadata"] == {
        "response_mode": "deep_report",
        "report_profile": "etf_deep_research",
        "parent_report_id": parent.report_id,
        "revision_mode": "full_refresh",
        "generation_source": "portfolio_monitor_structural_refresh",
        "generation_reason": "structural_monitoring_not_recommended",
        "monitor_planner_job_id": "planner-structural-refresh",
        "monitor_trigger_type": "report_ready",
        "monitor_source_bundle_sha256": "a" * 64,
    }
    assert "candidates=[]" in call["content"]

    deduplicated = asyncio.run(api_server._queue_monitor_auto_deep_report(payload))
    assert deduplicated["deduplicated"] is True
    assert len(dispatcher.calls) == 1

    dispatcher.store.item.status = "completed"
    terminal = asyncio.run(api_server._queue_monitor_auto_deep_report(payload))
    assert terminal["status"] == "refresh_already_attempted"
    assert terminal["refresh_outcome"] == "completed"
    assert terminal["parent_report_id"] == parent.report_id
    assert len(dispatcher.calls) == 1

    parent.generation_source = "portfolio_monitor_structural_refresh"
    reports._write_manifest(parent)
    bounded = asyncio.run(
        api_server._queue_monitor_auto_deep_report({
            **payload,
            "parent_report_id": parent.report_id,
        })
    )
    assert bounded["status"] == "refresh_already_attempted"
    assert len(dispatcher.calls) == 1


def test_monitor_structural_refresh_retries_one_infrastructure_failure(
    monkeypatch, tmp_path,
) -> None:
    reports = DeepReportService(tmp_path / "auto-structural-refresh-retry-reports")
    parent = reports.begin(
        session_id="retry-parent-session",
        attempt_id="retry-parent-attempt",
        request_content="research 000651.SZ",
        profile="equity_deep_research",
    )
    parent.symbol = "000651.SZ"
    parent.security_name = "Gree Electric"
    parent.status = "completed"
    parent.quality_status = "passed_with_gaps"
    reports._write_manifest(parent)

    class AutoSessionService:
        deep_reports = reports

        def create_session(self, **_kwargs):
            raise AssertionError("structural refresh must reuse the parent report session")

    base_event_id = (
        f"auto-equity-deep-report-structural-refresh:000651.SZ:{parent.report_id}"
    )

    class QueueStore:
        def __init__(self) -> None:
            self.items = {
                base_event_id: SimpleNamespace(
                    status="failed",
                    job_id="dispatch-base-failed",
                    session_id=parent.session_id,
                    attempt_id="base-failed-attempt",
                    error="database is locked",
                )
            }

        def get_by_source_event(self, source: str, source_event_id: str):
            assert source == "api"
            return self.items.get(source_event_id)

    class AutoDispatcher:
        def __init__(self) -> None:
            self.store = QueueStore()
            self.calls: list[dict] = []

        async def submit(self, session_id: str, content: str, **kwargs):
            self.calls.append({"session_id": session_id, "content": content, **kwargs})
            self.store.items[kwargs["source_event_id"]] = SimpleNamespace(
                status="pending",
                job_id="dispatch-retry-1",
                session_id=session_id,
                attempt_id="retry-attempt-1",
                error=None,
            )
            return {
                "status": "queued",
                "job_id": "dispatch-retry-1",
                "attempt_id": "retry-attempt-1",
                "deduplicated": False,
            }

    dispatcher = AutoDispatcher()
    monkeypatch.setattr(api_server, "_get_session_service", lambda: AutoSessionService())
    monkeypatch.setattr(api_server, "_get_session_dispatcher", lambda: dispatcher)
    payload = {
        "job_id": "planner-structural-retry",
        "symbol": "000651.SZ",
        "security_name": "Gree Electric",
        "research_date": "2026-07-22",
        "research_reasons": ["structural_monitoring_not_recommended"],
        "trigger_type": "holdings_changed",
        "structural_refresh": True,
        "parent_report_id": parent.report_id,
        "source_bundle_sha256": "b" * 64,
    }

    created = asyncio.run(api_server._queue_monitor_auto_deep_report(payload))
    assert created["status"] == "queued"
    assert created["retry_attempt"] == 1
    assert len(dispatcher.calls) == 1
    assert dispatcher.calls[0]["source_event_id"] == f"{base_event_id}:retry-1"
    assert dispatcher.calls[0]["source_metadata"]["monitor_refresh_retry"] == 1

    pending = asyncio.run(api_server._queue_monitor_auto_deep_report(payload))
    assert pending["status"] == "pending"
    assert pending["deduplicated"] is True
    assert len(dispatcher.calls) == 1

    dispatcher.store.items[f"{base_event_id}:retry-1"].status = "failed"
    dispatcher.store.items[f"{base_event_id}:retry-1"].error = "database is locked"
    exhausted = asyncio.run(api_server._queue_monitor_auto_deep_report(payload))
    assert exhausted["status"] == "refresh_already_attempted"
    assert exhausted["refresh_outcome"] == "failed"
    assert exhausted["retry_exhausted"] is True
    assert exhausted["retry_attempt"] == 1
    assert len(dispatcher.calls) == 1


def test_equity_resolution_requires_one_explicit_supported_listing(monkeypatch, tmp_path) -> None:
    _reports, _dispatcher, client = _install_runtime(monkeypatch, tmp_path)
    from src.tools.symbol_search_tool import SymbolSearchTool

    monkeypatch.setattr(
        SymbolSearchTool,
        "execute",
        lambda self, **kwargs: json.dumps({
            "ok": True,
            "data": {
                "candidates": [{
                    "symbol": "301308.SZ",
                    "name": "江波龙",
                    "market": "cn",
                    "source": "eastmoney",
                }],
                "sources": {"eastmoney": "ok", "yahoo": "ok"},
            },
        }, ensure_ascii=False),
    )

    response = client.post("/reports/resolve-equity", json={"query": "深度研究江波龙"})

    assert response.status_code == 200
    assert response.json()["status"] == "resolved"
    assert response.json()["symbol"] == "301308.SZ"
    assert response.json()["security_name"] == "江波龙"
    assert response.json()["instrument_type"] == "company_equity"


def test_equity_resolution_returns_fuzzy_candidates_for_user_confirmation(monkeypatch, tmp_path) -> None:
    _reports, _dispatcher, client = _install_runtime(monkeypatch, tmp_path)
    from src.tools.symbol_search_tool import SymbolSearchTool

    monkeypatch.setattr(
        SymbolSearchTool,
        "execute",
        lambda self, **kwargs: json.dumps({
            "ok": True,
            "data": {
                "candidates": [
                    {
                        "symbol": "601857.SH",
                        "name": "中国石油",
                        "market": "cn",
                        "source": "tencent",
                    },
                    {
                        "symbol": "600028.SH",
                        "name": "中国石化",
                        "market": "cn",
                        "source": "tencent",
                    },
                ],
                "sources": {"tencent": "ok"},
            },
        }, ensure_ascii=False),
    )

    response = client.post("/reports/resolve-equity", json={"query": "研究中国"})

    assert response.status_code == 200
    assert response.json()["status"] == "ambiguous"
    assert response.json()["options"] == [
        {
            "symbol": "601857.SH",
            "security_name": "中国石油",
            "market": "cn",
            "source": "tencent",
            "instrument_type": "company_equity",
        },
        {
            "symbol": "600028.SH",
            "security_name": "中国石化",
            "market": "cn",
            "source": "tencent",
            "instrument_type": "company_equity",
        },
    ]


def test_instrument_resolution_recognizes_exact_cn_etf_without_equity_hit(
    monkeypatch, tmp_path,
) -> None:
    _reports, _dispatcher, client = _install_runtime(monkeypatch, tmp_path)
    from src.tools.symbol_search_tool import SymbolSearchTool
    from src.reports import etf_universe_provider

    monkeypatch.setattr(
        SymbolSearchTool,
        "execute",
        lambda self, **kwargs: json.dumps({
            "ok": True,
            "data": {"candidates": [], "sources": {"eastmoney": "no_match"}},
        }),
    )

    class FakeETFUniverseService:
        def status(self, symbol):
            assert symbol == "588870.SH"
            return {"mapping": {"index_name": "科创板新能源指数"}}

    monkeypatch.setattr(
        etf_universe_provider,
        "get_etf_universe_service",
        lambda: FakeETFUniverseService(),
    )

    response = client.post("/reports/resolve-instrument", json={"query": "588870"})

    assert response.status_code == 200
    assert response.json()["status"] == "resolved"
    assert response.json()["symbol"] == "588870.SH"
    assert response.json()["security_name"] == "科创板新能源指数ETF"
    assert response.json()["instrument_type"] == "etf"
