from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src.portfolio.analysis import (
    build_analysis_prompt,
    build_analysis_title,
    build_custom_stock_prompt,
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
    assert "market.bars_handles[]" in prompt
    assert "数据受限模式" in prompt
    assert "不得再对其子集重复请求 context" in prompt
    assert "actionability=analysis_only" in prompt
    assert "不得绕过 selected_quote=null" in prompt
    assert "精确买卖价、仓位比例、加减仓数量" in prompt


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


def test_condition_order_prompts_are_optional_and_trend_gated() -> None:
    holding = {"name": "招商银行", "symbol": "600036.SH"}
    prompts = [
        build_analysis_prompt("holding", holding),
        build_analysis_prompt("portfolio"),
        build_analysis_prompt("market", market_phase="premarket"),
        build_custom_stock_prompt("600036"),
    ]

    for prompt in prompts:
        assert "条件单观察建议不是报告必填项" in prompt
        assert "下降趋势只能讨论风险控制、减仓或退出观察" in prompt
        assert "结构突破、量价确认" in prompt
        assert "本次无条件单建议" in prompt
        assert "不得为了填满表格而编造价位" in prompt
