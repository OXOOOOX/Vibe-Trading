"""SessionService regressions for remote MCP startup paths."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from src.session.events import EventBus
from src.session.models import Attempt, AttemptStatus
from src.session.service import (
    _EQUITY_DEEP_REPORT_MAX_ITERATIONS,
    _MONITOR_STRUCTURAL_REFRESH_MAX_ITERATIONS,
    _MONITOR_STRUCTURAL_REFRESH_MAX_TOTAL_TOKENS,
    _STANDARD_AGENT_MAX_ITERATIONS,
    SessionService,
)
from src.reports.execution_policy import resolve_agent_execution_limits
from src.session.store import SessionStore


class _DummyIndex:
    def index_session(self, session_id: str, title: str) -> None:
        del session_id, title

    def index_message(self, session_id: str, role: str, content: str) -> None:
        del session_id, role, content


class _DummyAgentLoop:
    observed_max_iterations: list[int] = []
    observed_max_total_tokens: list[int | None] = []

    def __init__(
        self,
        *,
        registry,
        llm,
        event_callback,
        max_iterations,
        max_total_tokens,
        persistent_memory,
        usage_recorder,
        **kwargs,
    ) -> None:
        del registry, llm, event_callback, persistent_memory, usage_recorder, kwargs
        self.observed_max_iterations.append(max_iterations)
        self.observed_max_total_tokens.append(max_total_tokens)

    def run(self, *, user_message: str, history, session_id: str) -> dict[str, str]:
        del user_message, history, session_id
        return {"status": "completed"}


def test_run_with_agent_keeps_event_loop_responsive_during_registry_build(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def _slow_build_registry(**kwargs):
        del kwargs
        time.sleep(0.25)
        return object()

    monkeypatch.setattr("src.session.service.get_shared_index", lambda: _DummyIndex())
    monkeypatch.setattr("src.tools.build_registry", _slow_build_registry)
    monkeypatch.setattr("src.providers.chat.ChatLLM", lambda: object())
    monkeypatch.setattr("src.memory.persistent.PersistentMemory", lambda: object())
    monkeypatch.setattr("src.agent.loop.AgentLoop", _DummyAgentLoop)
    monkeypatch.setattr("src.config.loader.load_runtime_agent_config", lambda overrides=None: object())
    monkeypatch.setattr("src.config.loader.sanitize_session_overrides", lambda overrides: dict(overrides))

    service = SessionService(
        store=SessionStore(tmp_path / "sessions"),
        event_bus=EventBus(),
        runs_dir=tmp_path / "runs",
    )
    attempt = Attempt(session_id="session-1", prompt="hello")

    async def _ticker(events: list[float], start: float) -> None:
        await asyncio.sleep(0.05)
        events.append(time.perf_counter() - start)

    async def _exercise() -> tuple[list[float], dict[str, str]]:
        events: list[float] = []
        start = time.perf_counter()
        asyncio.create_task(_ticker(events, start))
        result = await service._run_with_agent(attempt, messages=[], session_config={})
        await asyncio.sleep(0.01)
        return events, result

    tick_times, result = asyncio.run(_exercise())

    assert result["status"] == "completed"
    assert tick_times, "Expected the event loop ticker to run while registry build was pending"
    assert tick_times[0] < 0.18, f"Registry build blocked the event loop for too long: {tick_times[0]:.3f}s"
    assert _DummyAgentLoop.observed_max_iterations[-1] == _STANDARD_AGENT_MAX_ITERATIONS
    assert _DummyAgentLoop.observed_max_total_tokens[-1] is None


def test_equity_deep_report_has_dedicated_iteration_budget() -> None:
    assert _EQUITY_DEEP_REPORT_MAX_ITERATIONS > _STANDARD_AGENT_MAX_ITERATIONS


def test_monitor_structural_refresh_has_bounded_agent_budget() -> None:
    limits = resolve_agent_execution_limits(
        is_deep_report=True,
        generation_source="portfolio_monitor_structural_refresh",
    )

    assert limits.max_iterations == _MONITOR_STRUCTURAL_REFRESH_MAX_ITERATIONS == 20
    assert limits.max_total_tokens == _MONITOR_STRUCTURAL_REFRESH_MAX_TOTAL_TOKENS == 240_000
    assert limits.max_iterations < _EQUITY_DEEP_REPORT_MAX_ITERATIONS


def test_deep_report_gate_defaults_to_disabled(monkeypatch) -> None:
    monkeypatch.delenv("VIBE_TRADING_DEEP_REPORT_ENABLED", raising=False)

    assert SessionService._deep_report_enabled() is False


def test_completed_attempt_persists_react_trace(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("src.session.service.get_shared_index", lambda: _DummyIndex())
    service = SessionService(
        store=SessionStore(tmp_path / "sessions"),
        event_bus=EventBus(),
        runs_dir=tmp_path / "runs",
    )
    session = service.create_session("trace persistence")
    attempt = Attempt(session_id=session.session_id, prompt="trace this")
    service.store.create_attempt(attempt)

    async def _fake_run_with_agent(*_args, **_kwargs):
        return {
            "status": "success",
            "content": "done",
            "run_dir": str(tmp_path / "runs" / "trace-run"),
            "react_trace": [{"type": "tool_call", "tool": "get_data_context"}],
        }

    monkeypatch.setattr(service, "_run_with_agent", _fake_run_with_agent)
    asyncio.run(service._run_attempt(session, attempt))

    persisted = service.store.get_attempt(session.session_id, attempt.attempt_id)
    assert persisted is not None
    assert persisted.status == AttemptStatus.COMPLETED
    assert persisted.react_trace == [{"type": "tool_call", "tool": "get_data_context"}]
