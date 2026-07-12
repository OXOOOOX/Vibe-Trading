from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src.portfolio.analysis import (
    build_analysis_prompt,
    build_analysis_title,
    resolve_market_analysis_phase,
)


def test_market_analysis_is_premarket_before_1130_shanghai() -> None:
    now = datetime(2026, 7, 13, 8, 15, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert resolve_market_analysis_phase(now) == "premarket"
    assert build_analysis_title("market", now=now) == "盘前分析 · 2026-07-13"
    prompt = build_analysis_prompt("market", now=now)
    assert "今天的盘前分析" in prompt
    assert "不得引用尚未发生的当日盘中行情" in prompt
    assert "2026-07-13 08:15 Asia/Shanghai" in prompt


def test_market_analysis_switches_to_intraday_at_1130_shanghai() -> None:
    before_cutoff_utc = datetime(2026, 7, 13, 3, 29, tzinfo=timezone.utc)
    at_cutoff_utc = datetime(2026, 7, 13, 3, 30, tzinfo=timezone.utc)

    assert resolve_market_analysis_phase(before_cutoff_utc) == "premarket"
    assert resolve_market_analysis_phase(at_cutoff_utc) == "intraday"
    assert build_analysis_title("market", now=at_cutoff_utc) == "盘中分析 · 2026-07-13"
    prompt = build_analysis_prompt("market", now=at_cutoff_utc)
    assert "今天午间的盘中分析" in prompt
    assert "下午盘中观察清单" in prompt


def test_market_analysis_returns_to_next_session_premarket_after_close_or_on_weekend() -> None:
    after_close = datetime(2026, 7, 13, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    weekend = datetime(2026, 7, 12, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert resolve_market_analysis_phase(after_close) == "premarket"
    assert resolve_market_analysis_phase(weekend) == "premarket"
    prompt = build_analysis_prompt("market", now=weekend)
    assert "下一交易日的盘前分析" in prompt
    assert "先核实交易日历" in prompt
