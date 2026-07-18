from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import sys
from types import ModuleType
from types import SimpleNamespace
from pathlib import Path

import pytest

from src.providers.openai_codex import _message_chunks_from_events
from src.usage import (
    UsageRecorder,
    UsageStore,
    aggregate_llm_costs,
    bind_usage_recorder,
    estimate_llm_cost,
    get_current_usage_recorder,
    normalize_usage,
    record_current_resource,
)


def test_normalize_usage_preserves_null_and_common_cache_fields() -> None:
    assert normalize_usage(
        {
            "input_tokens": 120,
            "output_tokens": 30,
            "input_token_details": {"cache_read": 75, "cache_creation": 12},
            "output_token_details": {"reasoning": 9},
        }
    ) == {
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
        "cache_read_input_tokens": 75,
        "cache_write_input_tokens": 12,
        "reasoning_tokens": 9,
    }
    assert normalize_usage({"prompt_tokens": 0, "completion_tokens": 0})["total_tokens"] == 0
    assert normalize_usage({"arbitrary": 10}) is None


def test_codex_completed_event_carries_real_usage() -> None:
    chunks = list(
        _message_chunks_from_events(
            [
                {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "input_tokens_details": {"cached_tokens": 80},
                            "output_tokens_details": {"reasoning_tokens": 7},
                        },
                    },
                }
            ]
        )
    )
    assert chunks[0].usage_metadata == {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "cache_read_input_tokens": 80,
        "cache_write_input_tokens": None,
        "reasoning_tokens": 7,
    }


def test_chat_provider_boundary_records_exactly_one_llm_call(monkeypatch, tmp_path) -> None:
    from src.providers.chat import ChatLLM

    class FakeLLM:
        @staticmethod
        def invoke(_messages, config):
            assert config == {}
            return SimpleNamespace(
                content="done",
                tool_calls=[],
                additional_kwargs={},
                response_metadata={"finish_reason": "stop"},
                usage_metadata={"input_tokens": 9, "output_tokens": 3, "total_tokens": 12},
            )

    llm = ChatLLM.__new__(ChatLLM)
    llm.model_name = "test-model"
    llm._llm = FakeLLM()
    monkeypatch.setenv("LANGCHAIN_PROVIDER", "test-provider")
    store = UsageStore(tmp_path / "sessions.db")
    recorder = UsageRecorder(store, "session", "provider", "provider", "attempt")

    with bind_usage_recorder(recorder):
        response = llm.chat([{"role": "user", "content": "hello"}])

    assert response.content == "done"
    summary = store.get_summary("session", "provider")["session"]
    assert summary["calls"]["llm_calls"] == 1
    assert summary["tokens"]["total_tokens"] == 12
    assert summary["models"][0]["key"] == "test-model"
    assert summary["models"][0]["count"] == 1


def test_deepseek_cost_uses_call_time_peak_window_and_reported_cache() -> None:
    base = {
        "kind": "llm_call",
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_read_input_tokens": 400_000,
    }

    standard = estimate_llm_cost({**base, "started_at": "2026-07-17T00:30:00Z"})
    assert standard["tier"] == "standard"  # 08:30 Asia/Shanghai
    assert standard["multiplier"] == 1.0
    assert standard["estimated_cost"] == 7.81

    peak = estimate_llm_cost({**base, "started_at": "2026-07-17T01:30:00Z"})
    assert peak["tier"] == "peak"  # 09:30 Asia/Shanghai
    assert peak["multiplier"] == 2.0
    assert peak["estimated_cost"] == 15.62
    assert peak["rates_per_million"] == {
        "input": 6.0,
        "cache_read_input": 0.05,
        "output": 12.0,
    }


def test_deepseek_cost_keeps_cache_null_as_an_estimate_range() -> None:
    estimate = estimate_llm_cost(
        {
            "kind": "llm_call",
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "started_at": "2026-07-17T06:00:00Z",  # 14:00 Asia/Shanghai
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "cache_read_input_tokens": None,
        }
    )
    assert estimate["status"] == "partial"
    assert estimate["tier"] == "peak"
    assert estimate["minimum_estimated_cost"] == 12.05
    assert estimate["maximum_estimated_cost"] == 18.0
    assert estimate["estimated_cost"] == 18.0


def test_kimi_k2_6_cost_uses_official_cache_rate() -> None:
    estimate = estimate_llm_cost(
        {
            "kind": "llm_call",
            "provider": "moonshot",
            "model": "kimi-k2.6",
            "started_at": "2026-07-17T06:00:00Z",
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "cache_read_input_tokens": 250_000,
        }
    )

    assert estimate["status"] == "complete"
    assert estimate["currency"] == "USD"
    assert estimate["estimated_cost"] == 4.7525
    assert estimate["rates_per_million"] == {
        "input": 0.95,
        "cache_read_input": 0.16,
        "output": 4.0,
    }


def test_cost_aggregate_keeps_native_currencies_separate() -> None:
    costs = aggregate_llm_costs(
        [
            {
                "kind": "llm_call",
                "provider": "deepseek",
                "model": "deepseek-v4-pro",
                "started_at": "2026-07-17T04:00:00Z",
                "input_tokens": 1_000_000,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            {
                "kind": "llm_call",
                "provider": "openai",
                "model": "gpt-5.5-instant",
                "started_at": "2026-07-17T04:00:00Z",
                "input_tokens": 1_000_000,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        ]
    )
    assert costs["coverage"] == "complete"
    assert costs["currencies"] == [
        {
            "currency": "CNY",
            "estimated_cost": 3.0,
            "minimum_estimated_cost": 3.0,
            "maximum_estimated_cost": 3.0,
            "calls": 1,
            "peak_calls": 0,
        },
        {
            "currency": "USD",
            "estimated_cost": 5.0,
            "minimum_estimated_cost": 5.0,
            "maximum_estimated_cost": 5.0,
            "calls": 1,
            "peak_calls": 0,
        },
    ]


def test_monitor_job_includes_linked_deep_report_session_without_global_duplication(
    tmp_path,
) -> None:
    store = UsageStore(tmp_path / "sessions.db")
    monitor = UsageRecorder(store, "monitor_job", "job-1", attempt_id="600000.SH:1")
    monitor.record_llm(
        {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        provider="deepseek",
        model="deepseek-v4-pro",
        status="ok",
        elapsed_ms=10,
        started_at="2026-07-17T01:00:00Z",
    )
    store.start_scope("session", "deep-report-session")
    assert store.link_scope(
        "monitor_job",
        "job-1",
        "session",
        "deep-report-session",
        relationship="auto_deep_report",
        child_attempt_id="attempt-1",
    )

    child = UsageRecorder(
        store,
        "session",
        "deep-report-session",
        "deep-report-session",
        "attempt-1",
    )
    child.record_llm(
        {"input_tokens": 20, "output_tokens": 3, "total_tokens": 23},
        provider="deepseek",
        model="deepseek-v4-pro",
        status="ok",
        elapsed_ms=20,
        started_at="2026-07-17T01:01:00Z",
    )

    job = store.get_summary("monitor_job", "job-1")
    assert job["session"]["tokens"]["total_tokens"] == 35
    assert job["direct"]["tokens"]["total_tokens"] == 12
    assert job["linked_scopes"] == [{
        "scope_type": "session",
        "scope_id": "deep-report-session",
        "attempt_id": "attempt-1",
        "relationship": "auto_deep_report",
    }]
    assert len(store.list_events("monitor_job", "job-1")["items"]) == 2

    # A later human follow-up in the same session is not part of the monitor
    # task that created the Deep Report.
    UsageRecorder(
        store,
        "session",
        "deep-report-session",
        "deep-report-session",
        "attempt-2",
    ).record_llm(
        {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        provider="deepseek",
        model="deepseek-v4-pro",
        status="ok",
        elapsed_ms=10,
        started_at="2026-07-17T01:02:00Z",
    )
    assert store.get_summary("monitor_job", "job-1")["session"]["tokens"]["total_tokens"] == 35

    # A deduplicated child may be shared by two monitor jobs. The global total
    # counts the underlying events once rather than once per link.
    store.link_scope(
        "monitor_job",
        "job-2",
        "session",
        "deep-report-session",
        child_attempt_id="attempt-1",
    )
    global_summary = store.get_type_summary(
        "monitor_job",
        started_at="2020-01-01T00:00:00Z",
        completed_at="2030-01-01T00:00:00Z",
    )
    assert global_summary["session"]["tokens"]["total_tokens"] == 35
    assert global_summary["scope_count"] == 2
    assert global_summary["linked_scope_count"] == 1


def test_usage_store_migrates_legacy_scope_links_for_attempt_scoping(tmp_path) -> None:
    path = tmp_path / "sessions.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE usage_scopes (
                scope_type TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0,
                recording_started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (scope_type, scope_id)
            );
            CREATE TABLE usage_scope_links (
                parent_scope_type TEXT NOT NULL,
                parent_scope_id TEXT NOT NULL,
                child_scope_type TEXT NOT NULL,
                child_scope_id TEXT NOT NULL,
                relationship TEXT NOT NULL DEFAULT 'child',
                created_at TEXT NOT NULL,
                PRIMARY KEY (
                    parent_scope_type, parent_scope_id,
                    child_scope_type, child_scope_id
                )
            );
            """
        )

    store = UsageStore(path)
    with store._connect() as connection:
        info = connection.execute("PRAGMA table_info(usage_scope_links)").fetchall()
        columns = {str(row[1]) for row in info}
        primary_key = [
            str(row[1])
            for row in sorted(info, key=lambda item: int(item[5] or 0))
            if int(row[5] or 0) > 0
        ]
    assert "child_attempt_id" in columns
    assert primary_key[-1] == "child_attempt_id"


def test_monitor_planner_item_binds_model_and_resource_calls_to_one_attempt(
    tmp_path,
) -> None:
    from src.portfolio.monitoring.service import MonitoringService

    usage_store = UsageStore(tmp_path / "sessions.db")

    class FakeMonitorStore:
        @staticmethod
        def get_planner_job(_job_id: str) -> dict[str, object]:
            return {"items": [{"symbol": "600000.SH", "status": "ready"}]}

    service = MonitoringService.__new__(MonitoringService)
    service.store = FakeMonitorStore()
    service.usage_store = usage_store

    def run_item(_job, _item) -> None:
        recorder = get_current_usage_recorder()
        assert recorder is not None
        recorder.record_llm(
            {"input_tokens": 50, "output_tokens": 10, "total_tokens": 60},
            provider="deepseek",
            model="deepseek-v4-pro",
            status="ok",
            elapsed_ms=5,
        )
        record_current_resource(
            provider="eastmoney",
            category="market",
            status="ok",
            elapsed_ms=7,
            cache_mode="network",
            query={"code": "600000.SH"},
        )

    service._run_planner_item = run_item
    service._run_recorded_planner_item(
        {
            "job_id": "job-boundary",
            "activation_mode": "autonomous",
            "trigger_type": "report_ready",
        },
        {"symbol": "600000.SH", "attempt": 2},
    )

    summary = usage_store.get_summary("monitor_job", "job-boundary")
    assert summary["session"]["tokens"]["total_tokens"] == 60
    assert summary["session"]["calls"] == {
        "llm_calls": 1,
        "agent_tools": 1,
        "external_requests": 1,
        "cache_accesses": 0,
        "failures": 0,
        "running": 0,
    }
    events = usage_store.list_events("monitor_job", "job-boundary")["items"]
    planner_event = next(item for item in events if item["kind"] == "tool_call")
    assert {item["attempt_id"] for item in events} == {"600000.SH:2"}
    assert {
        item["parent_tool_call_id"]
        for item in events
        if item["kind"] in {"llm_call", "resource_call"}
    } == {planner_event["event_id"]}


def test_usage_store_aggregates_tokens_tools_network_and_cache(tmp_path) -> None:
    store = UsageStore(tmp_path / "sessions.db")
    assert store.get_summary("session", "old")["recording_status"] == "unrecorded"

    recorder = UsageRecorder(
        store=store,
        scope_type="session",
        scope_id="s1",
        session_id="s1",
        attempt_id="a1",
    )
    recorder.record_llm(
        {
            "input_tokens": 200,
            "output_tokens": 50,
            "total_tokens": 250,
            "input_token_details": {"cache_read": 100},
        },
        provider="openai",
        model="gpt-test",
        status="ok",
        elapsed_ms=25,
    )
    tool_event = recorder.start_tool("call-1", "web_search", {"query": "AAPL earnings"})
    recorder.finish_tool(tool_event, status="ok", elapsed_ms=30)
    revision_after_finish = store.get_summary("session", "s1")["revision"]
    recorder.finish_tool(tool_event, status="ok", elapsed_ms=30)
    assert store.get_summary("session", "s1")["revision"] == revision_after_finish

    recorder.record_resource(
        provider="duckduckgo",
        category="web",
        status="error",
        elapsed_ms=80,
        cache_mode="network",
        query={"query": "AAPL earnings with a very long suffix that must be safely shortened"},
    )
    recorder.record_resource(
        provider="verified_market_cache",
        category="market",
        status="ok",
        elapsed_ms=1,
        cache_mode="cache_hit",
        query={"code": "AAPL", "interval": "1D", "source": "yahoo"},
    )

    summary = store.get_summary("session", "s1", current_attempt_id="a1")
    assert summary["recording_status"] == "recording"
    assert summary["session"]["tokens"]["total_tokens"] == 250
    assert summary["session"]["tokens"]["cache_read_input_tokens"] == 100
    assert summary["session"]["tokens"]["cache_hit_rate"] == 0.5
    assert summary["session"]["calls"] == {
        "llm_calls": 1,
        "agent_tools": 1,
        "external_requests": 1,
        "cache_accesses": 1,
        "failures": 1,
        "running": 0,
    }
    assert summary["current_attempt"] == summary["session"]

    page = store.list_events("session", "s1", kind="resource_call", limit=1)
    assert len(page["items"]) == 1
    assert page["next_cursor"] is not None
    assert len(page["items"][0]["query_summary"]) <= 80
    next_page = store.list_events(
        "session", "s1", kind="resource_call", cursor=page["next_cursor"], limit=1
    )
    assert len(next_page["items"]) == 1

    store.delete_scope("session", "s1")
    assert store.get_summary("session", "s1")["recording_status"] == "unrecorded"


def test_parallel_resource_writes_are_durable(tmp_path) -> None:
    store = UsageStore(tmp_path / "sessions.db")
    recorder = UsageRecorder(store, "session", "parallel", "parallel", "attempt")

    def write(index: int) -> None:
        recorder.record_resource(
            provider=f"provider-{index % 2}",
            category="market",
            status="ok",
            elapsed_ms=index,
            cache_mode="network",
            query={"code": f"CODE{index}", "interval": "1D"},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(24)))

    summary = store.get_summary("session", "parallel")
    assert summary["session"]["calls"]["external_requests"] == 24
    assert summary["revision"] == 24


def test_market_dual_source_refresh_and_stale_fallback_classification(tmp_path) -> None:
    store = UsageStore(tmp_path / "sessions.db")
    recorder = UsageRecorder(store, "session", "market", "market", "attempt")
    for provider in ("eastmoney", "tencent"):
        recorder.record_resource(
            provider=provider,
            category="market",
            status="ok",
            elapsed_ms=10,
            cache_mode="network",
            query={"code": "600000.SH", "interval": "1D", "source": provider},
        )
    recorder.record_resource(
        provider="yahoo",
        category="market",
        status="ok",
        elapsed_ms=15,
        cache_mode="cache_refresh",
        query={"code": "AAPL", "interval": "1D", "source": "yahoo"},
    )
    recorder.record_resource(
        provider="verified_market_cache",
        category="market",
        status="ok",
        elapsed_ms=0,
        cache_mode="stale_fallback",
        query={"code": "AAPL", "interval": "1D"},
    )

    calls = store.get_summary("session", "market")["session"]["calls"]
    assert calls["external_requests"] == 3
    assert calls["cache_accesses"] == 2


def test_web_search_fallback_is_one_tool_and_multiple_provider_requests(
    monkeypatch, tmp_path
) -> None:
    from src.tools import web_search_tool

    fake_ddgs = ModuleType("ddgs")

    class FailingDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def text(self, *_args, **_kwargs):
            raise ConnectionError("unreachable")

    fake_ddgs.DDGS = FailingDDGS
    monkeypatch.setitem(sys.modules, "ddgs", fake_ddgs)
    monkeypatch.setenv("ALIYUN_IQS_API_KEY", "configured-for-test")
    monkeypatch.setattr(web_search_tool, "_aliyun_iqs_search", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(web_search_tool, "_sogou_search", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        web_search_tool,
        "_bing_cn_search",
        lambda *_args, **_kwargs: [
            {"title": "result", "href": "https://example.com", "body": "snippet"}
        ],
    )

    store = UsageStore(tmp_path / "sessions.db")
    recorder = UsageRecorder(store, "session", "search", "search", "attempt")
    tool_event = recorder.start_tool("call-search", "web_search", {"query": "market news"})
    with bind_usage_recorder(recorder, "call-search"):
        payload = json.loads(web_search_tool.WebSearchTool().execute(query="market news"))
    recorder.finish_tool(tool_event, status="ok", elapsed_ms=10)

    assert payload["status"] == "ok"
    summary = store.get_summary("session", "search")["session"]
    assert summary["calls"]["agent_tools"] == 1
    assert summary["calls"]["external_requests"] == 4
    assert {row["key"] for row in summary["providers"]} == {
        "aliyun_iqs",
        "ddgs:duckduckgo, google, bing, brave, mojeek, yahoo",
        "sogou",
        "bing_cn",
    }


def test_session_delete_cleans_usage_and_fork_starts_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from src.session.events import EventBus
    from src.session.models import Message
    from src.session.service import SessionService
    from src.session.store import SessionStore

    class DummyIndex:
        def index_session(self, *_args, **_kwargs) -> None:
            return None

        def index_message(self, *_args, **_kwargs) -> None:
            return None

    monkeypatch.setattr("src.session.service.get_shared_index", lambda: DummyIndex())
    usage_store = UsageStore(tmp_path / "sessions.db")
    service = SessionService(
        SessionStore(tmp_path / "sessions"),
        EventBus(),
        tmp_path / "runs",
        usage_store=usage_store,
    )
    source = service.create_session("source")
    assistant = Message(session_id=source.session_id, role="assistant", content="answer")
    service.store.append_message(assistant)
    UsageRecorder(
        usage_store, "session", source.session_id, source.session_id, "a1"
    ).record_resource(
        provider="yahoo",
        category="market",
        status="ok",
        elapsed_ms=1,
        cache_mode="network",
        query={"code": "AAPL"},
    )

    fork = service.fork_session(source.session_id, assistant.message_id)
    fork_summary = usage_store.get_summary("session", fork.session_id)
    assert fork_summary["recording_status"] == "recording"
    assert fork_summary["session"]["calls"]["external_requests"] == 0

    assert service.delete_session(source.session_id) is True
    assert usage_store.get_summary("session", source.session_id)["recording_status"] == "unrecorded"
