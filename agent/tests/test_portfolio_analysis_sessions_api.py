from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

import api_server
from src.session.dispatcher import DispatchJob
from src.session.models import Session


class _FakeJobStore:
    def __init__(self) -> None:
        self.jobs: dict[str, DispatchJob] = {}

    def get(self, job_id: str):
        return self.jobs.get(job_id)


class _FakeDispatcher:
    def __init__(self, *, fail: bool = False) -> None:
        self.store = _FakeJobStore()
        self.fail = fail
        self.calls: list[dict] = []

    async def submit(self, session_id: str, content: str, **kwargs):
        self.calls.append({"session_id": session_id, "content": content, **kwargs})
        if self.fail:
            raise RuntimeError("queue unavailable")
        job = DispatchJob(
            job_id=f"analysis-{len(self.calls)}",
            session_id=session_id,
            content=content,
            source=kwargs["source"],
            source_metadata=dict(kwargs["source_metadata"]),
            status="pending",
        )
        self.store.jobs[job.job_id] = job
        return {
            "job_id": job.job_id,
            "message_id": job.message_id,
            "attempt_id": job.attempt_id,
            "status": "queued",
            "queue_position": 1,
        }


class _FakeService:
    def __init__(self) -> None:
        self.sessions: list[Session] = []
        self.deleted: list[str] = []

    def create_session(self, title: str = "", config=None) -> Session:
        session = Session(session_id=f"session-{len(self.sessions) + 1}", title=title, config=dict(config or {}))
        self.sessions.append(session)
        return session

    def delete_session(self, session_id: str) -> bool:
        self.deleted.append(session_id)
        return True


def _client(tmp_path, monkeypatch, *, dispatcher: _FakeDispatcher | None = None):
    monkeypatch.setenv("VIBE_TRADING_PORTFOLIO_STATE_PATH", str(tmp_path / "portfolio_state.json"))
    service = _FakeService()
    dispatcher = dispatcher or _FakeDispatcher()
    monkeypatch.setattr(api_server, "_get_session_service", lambda: service)
    monkeypatch.setattr(api_server, "_get_session_dispatcher", lambda: dispatcher)
    return TestClient(api_server.app, client=("127.0.0.1", 50000)), service, dispatcher


def _seed_holdings() -> None:
    from src.portfolio.state import update_holdings

    update_holdings(raw_text="科创50ETF 588870 2100 1.975\n券商ETF 159842 1300 0.750")


def test_holding_analysis_creates_research_only_background_session(tmp_path, monkeypatch) -> None:
    client, service, dispatcher = _client(tmp_path, monkeypatch)
    _seed_holdings()

    response = client.post("/portfolio/analysis-sessions", json={"scope": "holding", "symbol": "588870"})

    assert response.status_code == 202
    payload = response.json()
    assert payload == {
        "analysis_id": "analysis-1",
        "session_id": "session-1",
        "scope": "holding",
        "symbol": "588870.SH",
        "analysis_phase": None,
        "status": "queued",
        "queue_position": 1,
        "created_at": payload["created_at"],
        "started_at": None,
        "completed_at": None,
        "error": None,
    }
    assert service.sessions[0].title == "持仓分析 · 科创50ETF (588870.SH)"
    assert service.sessions[0].config["portfolio_analysis"] == {
        "scope": "holding",
        "symbol": "588870.SH",
        "research_only": True,
    }
    dispatched = dispatcher.calls[0]
    assert dispatched["include_shell_tools"] is False
    assert dispatched["source"] == "portfolio_analysis"
    assert dispatched["source_metadata"] == {"scope": "holding", "symbol": "588870.SH"}
    assert 'portfolio_state(action="get")' in dispatched["content"]
    assert "最近 30 天" in dispatched["content"]
    assert "最近 7 天" in dispatched["content"]
    assert "不得创建、提交、修改、取消真实订单或条件单" in dispatched["content"]


def test_portfolio_analysis_status_and_scope_validation(tmp_path, monkeypatch) -> None:
    client, _, dispatcher = _client(tmp_path, monkeypatch)

    assert client.post("/portfolio/analysis-sessions", json={"scope": "portfolio"}).status_code == 400
    _seed_holdings()
    assert client.post("/portfolio/analysis-sessions", json={"scope": "holding"}).status_code == 400
    assert client.post("/portfolio/analysis-sessions", json={"scope": "holding", "symbol": "000001.SZ"}).status_code == 404

    started = client.post("/portfolio/analysis-sessions", json={"scope": "portfolio"})
    assert started.status_code == 202
    job = dispatcher.store.get(started.json()["analysis_id"])
    job.status = "running"
    job.started_at = "2026-07-11T10:00:00+08:00"

    status = client.get(f"/portfolio/analysis-sessions/{job.job_id}")
    assert status.status_code == 200
    assert status.json()["status"] == "running"
    assert status.json()["scope"] == "portfolio"
    assert status.json()["symbol"] is None


def test_market_analysis_uses_lunch_prompt_after_1130_shanghai(tmp_path, monkeypatch) -> None:
    client, service, dispatcher = _client(tmp_path, monkeypatch)
    _seed_holdings()
    trigger_time = datetime(2026, 7, 13, 11, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr("src.portfolio.analysis.current_market_analysis_time", lambda: trigger_time)

    response = client.post("/portfolio/analysis-sessions", json={"scope": "market"})

    assert response.status_code == 202
    assert response.json()["scope"] == "market"
    assert response.json()["analysis_phase"] == "intraday"
    assert service.sessions[0].title == "盘中分析 · 2026-07-13"
    assert service.sessions[0].config["portfolio_analysis"] == {
        "scope": "market",
        "symbol": None,
        "research_only": True,
        "analysis_phase": "intraday",
    }
    dispatched = dispatcher.calls[0]
    assert dispatched["source_metadata"] == {
        "scope": "market",
        "symbol": None,
        "analysis_phase": "intraday",
    }
    assert "今天午间的盘中分析" in dispatched["content"]
    assert "今天上午截至午休" in dispatched["content"]
    assert "2026-07-13 11:30 Asia/Shanghai" in dispatched["content"]


def test_enqueue_failure_removes_orphan_session(tmp_path, monkeypatch) -> None:
    client, service, _ = _client(tmp_path, monkeypatch, dispatcher=_FakeDispatcher(fail=True))
    _seed_holdings()

    response = client.post("/portfolio/analysis-sessions", json={"scope": "portfolio"})

    assert response.status_code == 503
    assert service.deleted == ["session-1"]
