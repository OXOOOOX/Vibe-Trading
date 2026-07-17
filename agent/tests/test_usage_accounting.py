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
