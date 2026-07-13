from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from src.agent.loop import AgentLoop
from src.portfolio.answer_guard import (
    build_portfolio_conflict_fallback,
    compact_portfolio_tool_result,
    find_portfolio_answer_conflict,
)


def _portfolio_tool_message(holdings: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": "call_portfolio",
        "name": "portfolio_state",
        "content": json.dumps(
            {"status": "ok", "state": {"holdings": holdings}},
            ensure_ascii=False,
        ),
    }


def test_nonempty_portfolio_rejects_false_empty_claim() -> None:
    messages = [
        _portfolio_tool_message(
            [{"name": "招商银行", "symbol": "600036.SH", "quantity": 200, "cost_price": 36.597}]
        )
    ]

    conflict = find_portfolio_answer_conflict(
        messages,
        "⚠️ 持仓状态已清空，以下仅分析关注列表。",
    )

    assert conflict is not None
    assert conflict.matched_text == "持仓状态已清空"
    assert conflict.holdings[0]["quantity"] == 200


def test_empty_portfolio_allows_empty_claim() -> None:
    messages = [_portfolio_tool_message([])]

    assert find_portfolio_answer_conflict(messages, "当前没有持仓。") is None


def test_conflict_fallback_contains_structured_snapshot() -> None:
    messages = [
        _portfolio_tool_message(
            [{"name": "招商银行", "symbol": "600036.SH", "quantity": 200, "cost_price": 36.597}]
        )
    ]
    conflict = find_portfolio_answer_conflict(messages, "当前持仓为空。")

    assert conflict is not None
    fallback = build_portfolio_conflict_fallback(conflict)
    assert "600036.SH" in fallback
    assert "200" in fallback
    assert "36.597" in fallback
    assert "错误分析没有发送" in fallback


def test_portfolio_tool_compaction_keeps_valid_complete_holding_facts() -> None:
    holdings = [
        {
            "name": f"标的{i}",
            "symbol": f"{i:06d}.SH",
            "quantity": i + 100,
            "cost_price": i + 1.25,
            "market_cache_path": "C:/very/long/cache/path/" + ("x" * 500),
            "market_sources": ["eastmoney", "tencent"],
        }
        for i in range(10)
    ]
    raw = json.dumps(
        {
            "status": "ok",
            "path": "C:/portfolio_state.json",
            "state": {"holdings": holdings, "recent_trades": [], "cash": 123.0},
        },
        ensure_ascii=False,
    )

    compact = compact_portfolio_tool_result(raw, limit=10_000)
    payload = json.loads(compact)

    assert len(raw) > len(compact)
    assert len(compact) <= 10_000
    assert len(payload["state"]["holdings"]) == 10
    assert payload["state"]["holdings"][3]["quantity"] == 103
    assert "market_cache_path" not in payload["state"]["holdings"][0]


class _NoToolRegistry:
    def get_definitions(self) -> list[Any]:
        return []


class _RetryingLLM:
    model_name = "answer-guard-test"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    def stream_chat(self, messages, **_kwargs):
        self.calls.append([dict(message) for message in messages])
        content = (
            "持仓状态已清空，以下是近期关注标的。"
            if len(self.calls) == 1
            else "已读取真实持仓：招商银行 200 股，成本 36.597 元。"
        )
        return SimpleNamespace(
            content=content,
            tool_calls=[],
            reasoning_content=None,
            has_tool_calls=False,
            usage_metadata=None,
        )


class _AlwaysWrongLLM(_RetryingLLM):
    def stream_chat(self, messages, **_kwargs):
        self.calls.append([dict(message) for message in messages])
        return SimpleNamespace(
            content="当前没有持仓，只能分析近期关注标的。",
            tool_calls=[],
            reasoning_content=None,
            has_tool_calls=False,
            usage_metadata=None,
        )


def test_agent_loop_retries_a_portfolio_contradiction(monkeypatch, tmp_path) -> None:
    from src.agent import loop as loop_module

    holdings = [
        {"name": "招商银行", "symbol": "600036.SH", "quantity": 200, "cost_price": 36.597}
    ]
    seeded_messages = [
        {"role": "system", "content": "test"},
        {"role": "user", "content": "盘前分析"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_portfolio",
                    "type": "function",
                    "function": {"name": "portfolio_state", "arguments": '{"action":"get"}'},
                }
            ],
        },
        _portfolio_tool_message(holdings),
    ]
    monkeypatch.setattr(
        loop_module.ContextBuilder,
        "build_messages",
        lambda _self, _user_message, _history: [dict(message) for message in seeded_messages],
    )
    monkeypatch.setattr(loop_module, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(loop_module, "SESSIONS_DIR", tmp_path / "sessions")

    llm = _RetryingLLM()
    agent = AgentLoop(registry=_NoToolRegistry(), llm=llm, max_iterations=3)  # type: ignore[arg-type]

    result = agent.run("盘前分析", session_id="session-guard")

    assert result["status"] == "success"
    assert result["content"] == "已读取真实持仓：招商银行 200 股，成本 36.597 元。"
    assert len(llm.calls) == 2
    assert any(
        "系统事实校验发现" in str(message.get("content") or "")
        for message in llm.calls[1]
    )
    assert any(
        item.get("type") == "portfolio_answer_conflict" and item.get("action") == "retry"
        for item in result["react_trace"]
    )


def test_agent_loop_falls_back_after_repeated_portfolio_contradiction(
    monkeypatch, tmp_path
) -> None:
    from src.agent import loop as loop_module

    holdings = [
        {"name": "招商银行", "symbol": "600036.SH", "quantity": 200, "cost_price": 36.597}
    ]
    seeded_messages = [
        {"role": "system", "content": "test"},
        {"role": "user", "content": "盘前分析"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_portfolio",
                    "type": "function",
                    "function": {"name": "portfolio_state", "arguments": '{"action":"get"}'},
                }
            ],
        },
        _portfolio_tool_message(holdings),
    ]
    monkeypatch.setattr(
        loop_module.ContextBuilder,
        "build_messages",
        lambda _self, _user_message, _history: [dict(message) for message in seeded_messages],
    )
    monkeypatch.setattr(loop_module, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(loop_module, "SESSIONS_DIR", tmp_path / "sessions")

    llm = _AlwaysWrongLLM()
    agent = AgentLoop(registry=_NoToolRegistry(), llm=llm, max_iterations=3)  # type: ignore[arg-type]

    result = agent.run("盘前分析", session_id="session-fallback")

    assert result["status"] == "success"
    assert "系统已拦截错误分析" in result["content"]
    assert "600036.SH" in result["content"]
    assert "200" in result["content"]
    assert len(llm.calls) == 2
    assert any(
        item.get("type") == "portfolio_answer_conflict" and item.get("action") == "fallback"
        for item in result["react_trace"]
    )
