from __future__ import annotations

import asyncio
import json

import api_server

from src.tools import build_registry
from src.agent.tools import ToolRegistry
from src.session.events import EventBus
from src.session.models import Attempt
from src.session.service import SessionService
from src.session.store import SessionStore
from src.tools.weekly_report_tool import WeeklyReportTool


def test_weekly_report_tool_starts_only_through_injected_handler() -> None:
    calls: list[tuple[str, dict]] = []
    events: list[tuple[str, dict]] = []

    def handler(command: str, payload: dict) -> dict:
        calls.append((command, payload))
        return {
            "status": "accepted",
            "report_audience": "user",
            "runs": [{"run_id": "weekly-1", "status": "queued"}],
        }

    result = json.loads(
        WeeklyReportTool(handler=handler, event_callback=lambda *args: events.append(args)).execute(
            command="start",
            symbols=["588870.SH"],
            refresh_policy="ensure_fresh",
        )
    )

    assert result["report_audience"] == "user"
    assert calls == [
        (
            "start",
            {
                "command": "start",
                "symbols": ["588870.SH"],
                "refresh_policy": "ensure_fresh",
            },
        )
    ]
    assert events[0][0] == "weekly_report.started"
    assert events[0][1]["run_ids"] == ["weekly-1"]


def test_weekly_report_tool_fails_closed_without_service() -> None:
    result = json.loads(WeeklyReportTool().execute(command="start", symbols=["588870.SH"]))

    assert result == {
        "status": "error",
        "error": "weekly report service is unavailable",
    }


def test_registry_exposes_weekly_report_only_when_session_handler_is_injected() -> None:
    without_handler = build_registry(include_shell_tools=False)
    with_handler = build_registry(
        include_shell_tools=False,
        weekly_report_handler=lambda _command, _payload: {"status": "ok"},
    )

    assert "weekly_report" not in without_handler.tool_names
    assert "weekly_report" in with_handler.tool_names


def test_session_weekly_bridge_starts_user_report_and_hides_local_paths(monkeypatch) -> None:
    class Service:
        start_kwargs: dict | None = None

        async def start(self, **kwargs):
            self.start_kwargs = kwargs
            return [
                {
                    "run_id": "weekly-1",
                    "status": "queued",
                    "report_audience": "user",
                    "artifacts": [
                        {
                            "artifact_id": "markdown",
                            "filename": "weekly.md",
                            "path": "C:/private/weekly.md",
                        }
                    ],
                }
            ]

    service = Service()
    monkeypatch.setattr(api_server, "_get_portfolio_weekly_service", lambda: service)

    result = asyncio.run(
        api_server._handle_session_weekly_report(
            "start",
            {
                "symbols": ["588870.SH"],
                "week_end": "2026-07-17",
                "refresh_policy": "ensure_fresh",
            },
        )
    )

    assert result["status"] == "accepted"
    assert result["report_audience"] == "user"
    assert service.start_kwargs["report_audience"] == "user"
    assert service.start_kwargs["report_profile"] == "weekly_review_v1"
    assert service.start_kwargs["trigger"] == "session"
    assert "path" not in result["runs"][0]["artifacts"][0]


def test_normal_session_registry_bridges_weekly_tool_to_async_service(
    tmp_path, monkeypatch
) -> None:
    calls: list[tuple[str, dict]] = []

    async def weekly_handler(command: str, payload: dict) -> dict:
        calls.append((command, payload))
        return {
            "status": "accepted",
            "report_audience": "user",
            "runs": [{"run_id": "weekly-session-1", "status": "queued"}],
        }

    def registry_builder(**kwargs) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(WeeklyReportTool(handler=kwargs["weekly_report_handler"]))
        return registry

    class AgentLoop:
        def __init__(self, *, registry, **_kwargs) -> None:
            self.registry = registry

        def run(self, **_kwargs) -> dict:
            payload = json.loads(
                self.registry.execute(
                    "weekly_report",
                    {"command": "start", "symbols": ["588870.SH"]},
                )
            )
            return {"status": "completed", "weekly": payload}

    monkeypatch.setattr("src.session.service.get_shared_index", lambda: object())
    monkeypatch.setattr("src.tools.build_registry", registry_builder)
    monkeypatch.setattr("src.providers.chat.ChatLLM", lambda: object())
    monkeypatch.setattr("src.memory.persistent.PersistentMemory", lambda: object())
    monkeypatch.setattr("src.agent.loop.AgentLoop", AgentLoop)
    monkeypatch.setattr(
        "src.config.loader.load_runtime_agent_config", lambda overrides=None: object()
    )
    monkeypatch.setattr(
        "src.config.loader.sanitize_session_overrides", lambda overrides: dict(overrides)
    )

    session_service = SessionService(
        store=SessionStore(tmp_path / "sessions"),
        event_bus=EventBus(),
        runs_dir=tmp_path / "runs",
        weekly_report_handler=weekly_handler,
    )
    attempt = Attempt(session_id="session-weekly", prompt="生成 588870 的正式周报")

    result = asyncio.run(
        session_service._run_with_agent(attempt, messages=[], session_config={})
    )

    assert result["weekly"]["runs"][0]["run_id"] == "weekly-session-1"
    assert calls == [
        ("start", {"command": "start", "symbols": ["588870.SH"]})
    ]
