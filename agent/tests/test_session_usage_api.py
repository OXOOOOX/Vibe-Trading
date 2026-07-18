from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

import api_server
from src.usage import UsageRecorder, UsageStore


def test_session_usage_summary_events_filters_and_404(monkeypatch, tmp_path) -> None:
    store = UsageStore(tmp_path / "sessions.db")
    recorder = UsageRecorder(store, "session", "s1", "s1", "a1")
    recorder.record_llm(
        {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        provider="deepseek",
        model="deepseek-v4-pro",
        status="ok",
        elapsed_ms=10,
    )
    recorder.record_resource(
        provider="yahoo",
        category="market",
        status="ok",
        elapsed_ms=20,
        cache_mode="network",
        query={"code": "AAPL", "interval": "1D"},
    )

    class FakeService:
        usage_store = store

        @staticmethod
        def get_session(session_id: str):
            if session_id != "s1":
                return None
            return SimpleNamespace(last_attempt_id="a1")

    monkeypatch.setattr(api_server, "_get_session_service", lambda: FakeService())
    client = TestClient(api_server.app, client=("127.0.0.1", 50000))

    response = client.get("/sessions/s1/usage")
    assert response.status_code == 200
    assert response.json()["session"]["tokens"]["total_tokens"] == 15
    assert response.json()["session"]["cost"]["priced_calls"] == 1
    assert response.json()["session"]["cost"]["currencies"][0]["currency"] == "CNY"
    assert response.json()["current_attempt"]["calls"]["external_requests"] == 1

    response = client.get("/sessions/s1/usage/events?kind=resource_call&category=market&limit=1")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["provider"] == "yahoo"

    assert client.get("/sessions/s1/usage/events?cursor=bad").status_code == 400
    assert client.get("/sessions/missing/usage").status_code == 404
