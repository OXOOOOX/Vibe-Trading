from __future__ import annotations

from datetime import datetime, timezone

from src.agent.context import DEFAULT_RUNTIME_TIME_ZONE, _SYSTEM_PROMPT, build_current_datetime_context


def test_runtime_clock_is_explicitly_grounded_in_shanghai_time() -> None:
    rendered = build_current_datetime_context(
        now=datetime(2026, 7, 11, 1, 2, 3, tzinfo=timezone.utc),
        timezone_name="Asia/Shanghai",
    )

    assert "Runtime date: 2026-07-11" in rendered
    assert "Runtime time: 09:02:03" in rendered
    assert "Runtime time zone: Asia/Shanghai (UTC+08:00)" in rendered
    assert "Interpret relative dates" in rendered


def test_runtime_clock_falls_back_from_invalid_timezone(caplog) -> None:
    rendered = build_current_datetime_context(
        now=datetime(2026, 7, 11, 1, 2, 3, tzinfo=timezone.utc),
        timezone_name="Not/A_Real_Zone",
    )

    assert f"Runtime time zone: {DEFAULT_RUNTIME_TIME_ZONE} (UTC+08:00)" in rendered
    assert "Invalid VIBE_TRADING_TIME_ZONE" in caplog.text


def test_portfolio_analysis_has_an_authoritative_state_first_tool_rule() -> None:
    assert 'FIRST tool call must be `portfolio_state(action="get")`' in _SYSTEM_PROMPT
