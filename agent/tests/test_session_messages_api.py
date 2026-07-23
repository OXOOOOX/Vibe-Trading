"""Regression coverage for persisted session message metadata."""

from __future__ import annotations

from fastapi.testclient import TestClient

import api_server
from src.session.models import Message
from src.session.service import _confirmed_report_subject


def test_confirmed_report_subject_is_parsed_before_report_snapshot() -> None:
    assert _confirmed_report_subject(
        "研究对象已由用户确认：半导体设备ETF国泰（159516.SZ）。\n用户原始请求：159516"
    ) == ("159516.SZ", "半导体设备ETF国泰")


def test_session_messages_preserve_deep_report_metadata(monkeypatch) -> None:
    report_metadata = {
        "report_id": "report_example",
        "report_quality_status": "passed_with_gaps",
        "report_artifacts": [
            {
                "artifact_id": "markdown",
                "available": True,
                "previewable": True,
            }
        ],
    }

    class FakeSessionService:
        @staticmethod
        def get_messages(session_id: str, limit: int = 100) -> list[Message]:
            assert session_id == "session-example"
            assert limit == 100
            return [
                Message(
                    message_id="message-example",
                    session_id=session_id,
                    role="assistant",
                    content="穿透式深度研究已生成。",
                    created_at="2026-07-18T12:00:00",
                    linked_attempt_id="attempt-example",
                    metadata=report_metadata,
                )
            ]

    monkeypatch.setattr(api_server, "_get_session_service", lambda: FakeSessionService())
    client = TestClient(api_server.app, client=("127.0.0.1", 50000))

    response = client.get("/sessions/session-example/messages")

    assert response.status_code == 200
    assert response.json()[0]["metadata"] == report_metadata
    assert "metadata" not in api_server.SendReportArtifactToFeishuRequest.model_fields
