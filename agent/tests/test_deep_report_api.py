"""API contracts for persisted equity Deep Reports."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

import api_server
from src.reports.contracts import ModuleResult
from src.reports.service import DeepReportService


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

    formal_artifact = client.get(f"/reports/{record.report_id}/artifacts/markdown")
    assert formal_artifact.status_code == 409

    diagnostic = client.get(f"/reports/{record.report_id}/artifacts/diagnostic")
    assert diagnostic.status_code == 200
    assert "测试诊断" not in diagnostic.content.decode("utf-8")
    assert "内部审计文件" in diagnostic.content.decode("utf-8")

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
    assert dispatcher.calls[-1]["source_metadata"] == {
        "response_mode": "deep_report",
        "report_profile": "equity_deep_research",
        "parent_report_id": repairable.report_id,
        "revision_mode": "repair",
    }

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
        },
        {
            "symbol": "600028.SH",
            "security_name": "中国石化",
            "market": "cn",
            "source": "tencent",
        },
    ]
