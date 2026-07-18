from __future__ import annotations

from src.portfolio.research_digest import build_research_digest, digest_from_brief


def test_digest_extracts_decision_summary_and_two_condition_scenarios() -> None:
    report = """# 科创50ETF（588870.SH）持仓分析

## 决策摘要

- 当前走势：日线仍处震荡修复，盘中重新站回关键支撑，但资金流分化。
- 趋势阶段：震荡
- 趋势方向：横盘
- 趋势强弱：中
- 置信度：medium
- 条件单状态：可设置
- 条件单：仅观察突破确认和回踩确认两个情景。
- 数据状态：日线已校核｜盘中已校核｜新闻部分可用
- 数据截至：2026-07-16 09:47
- 风险提示：重新跌回支撑下方将推翻修复判断。

## 条件单观察位

| 情景 | 触发条件 | 确认条件 | 失效位 | 建议动作 |
|---|---|---|---|---|
| 突破 | 放量站稳关键位 | 连续确认 | 跌回关键位下方 | 人工复核后再决定 |
| 回踩 | 缩量回踩支撑 | 再次转强 | 支撑失守 | 继续观察 |
"""

    digest = build_research_digest(report)

    assert digest.symbol == "588870.SH"
    assert digest.condition_status == "available"
    assert len(digest.conditions) == 2
    assert digest.conditions[0].confirmation == "连续确认"
    assert digest.market_as_of == "2026-07-16 09:47"
    assert {item.scope: item.status for item in digest.data_scopes}["daily"] == "verified"
    assert {item.scope: item.status for item in digest.data_scopes}["news"] == "partial"


def test_digest_never_keeps_conditions_when_scope_is_data_insufficient() -> None:
    report = """# 科创50ETF（588870.SH）分析

## 决策摘要

- 当前走势：日线趋势可判断，盘中行情尚未校核。
- 条件单状态：数据不足
- 条件单：盘中行情尚未校核，暂不提供精确触发价。
- 数据状态：日线已校核｜盘中尚未开盘｜新闻部分可用
- 数据截至：2026-07-15 15:00

## 条件建议

| 触发条件 | 建议动作 |
|---|---|
| 1.90 | 加仓 |
"""

    digest = build_research_digest(report)

    assert digest.condition_status == "data_insufficient"
    assert digest.conditions == ()
    assert "1.90" not in digest.condition_summary
    assert "暂不提供精确条件价" in digest.condition_summary
    assert {item.scope: item.status for item in digest.data_scopes}["intraday"] == "not_started"


def test_digest_accepts_decision_summary_table_without_model_fallback() -> None:
    report = """# 军工ETF（512680.SH）持仓分析

## 决策摘要

| 字段 | 内容 |
|---|---|
| **当前走势** | 日线下降趋势仍未扭转，盘中价格已完成校核。 |
| **趋势阶段** | 下降 |
| **趋势方向** | 向下 |
| **趋势强弱** | 中 |
| **置信度** | medium |
| **条件单状态** | 暂不建议 |
| **条件单** | 等待结构突破和量价确认。 |
| **数据状态** | 日线已校核｜盘中已校核｜新闻部分可用 |
| **数据截至** | 2026-07-16 15:00 |
| **风险提示** | 支撑失守会延续下行风险。 |
"""

    digest = build_research_digest(report)

    assert digest.fallback is False
    assert digest.trend_stage == "下降"
    assert digest.condition_status == "not_recommended"
    assert digest.market_as_of == "2026-07-16 15:00"
    assert {item.scope: item.status for item in digest.data_scopes}["intraday"] == "verified"


def test_daily_brief_uses_same_digest_contract() -> None:
    digest = digest_from_brief(
        {
            "symbol": "588870.SH",
            "summary": "日线震荡修复。",
            "confidence": "medium",
            "condition_order_status": "not_recommended",
            "condition_order_summary": "等待量价确认。",
            "trend": {
                "summary": "日线震荡修复。",
                "stage": "震荡",
                "direction": "横盘",
                "strength": "中",
            },
            "data_scopes": {
                "daily": {"status": "verified", "as_of": "2026-07-15"},
                "news": {"status": "partial", "reason": "来源延迟"},
            },
        },
        title="科创50ETF · 个股日报",
    )

    assert digest.title == "科创50ETF · 个股日报"
    assert digest.trend_stage == "震荡"
    assert digest.condition_status == "not_recommended"
    assert {item.scope: item.status for item in digest.data_scopes}["news"] == "partial"
