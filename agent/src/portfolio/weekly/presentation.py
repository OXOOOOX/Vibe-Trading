"""Chinese presentation labels for the human-facing weekly report.

The structured JSON contract deliberately keeps stable English enum values for
machines.  Markdown and PDF must use this module so those implementation codes
never leak into the reader-facing report.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.reports.data_gaps import DATA_GAP_LABELS


SHANGHAI = ZoneInfo("Asia/Shanghai")

QUALITY_LABELS = {
    "passed": "通过",
    "passed_with_gaps": "通过（存在数据缺口）",
    "failed_validation": "未通过校验",
}

COVERAGE_LABELS = {
    "complete": "完整覆盖",
    "partial": "部分覆盖",
    "insufficient": "数据不足",
}

TRADE_EXECUTION_LABELS = {
    "forbidden": "禁止自动交易",
}

MONITORING_STATUS_LABELS = {
    "available": "可生成监控候选",
    "not_recommended": "不建议生成监控候选",
    "data_insufficient": "数据不足，无法生成监控候选",
}

ACTIVATION_POLICY_LABELS = {
    "manual_confirmation_required": "须经人工确认后启用",
}

AUTOMATION_STATUS_LABELS = {
    "action_ready": "条件已映射，可供人工启用",
    "watch_only": "仅观察",
}

CONDITION_ROLE_LABELS = {
    "required": "必要条件",
    "supportive": "辅助条件",
    "invalidation": "失效条件",
}

CONDITION_COVERAGE_LABELS = {
    "mapped": "已映射",
    "awaiting_data": "等待所需数据",
    "ambiguous": "条件含义待澄清",
    "unsupported": "当前不支持",
}

LEVEL_TYPE_LABELS = {
    "support": "支撑区间",
    "resistance": "阻力区间",
    "breakout": "突破观察位",
    "breakdown": "跌破观察位",
    "reclaim": "重新站上观察位",
    "invalidation": "趋势失效位",
    "watch_zone": "观察区间",
}

STRENGTH_LABELS = {
    "low": "低",
    "weak": "弱",
    "medium": "中",
    "normal": "普通",
    "high": "高",
    "strong": "强",
}

OUTCOME_LABELS = {
    "confirmed": "已确认",
    "invalidated": "已失效",
    "approached": "曾接近",
    "not_triggered": "未触发",
    "unresolved": "尚未判定",
    "insufficient_data": "数据不足",
    "expired": "已到期",
}

CHANGE_TYPE_LABELS = {
    "new": "新增",
    "unchanged": "未变化",
    "raised": "上调",
    "lowered": "下调",
    "modified": "已修改",
    "withdrawn": "已撤回",
    "expired": "已到期",
}

FIELD_LABELS = {
    "original_level.value": "原始观察价位",
    "original_level.lower": "原始区间下沿",
    "original_level.upper": "原始区间上沿",
    "intent": "场景意图",
    "trigger.kind": "触发方式",
    "trigger.interval": "检查周期",
    "trigger.confirmation_count": "连续确认次数",
    "volume_confirmation.metric": "成交量确认指标",
    "volume_confirmation.threshold": "成交量确认门槛",
    "invalidation.kind": "失效判定方式",
    "invalidation.level": "失效价位",
    "action_template.action": "建议动作",
    "action_template.sizing.kind": "仓位口径",
    "action_template.sizing.value": "仓位数值",
    "automation_status": "自动化准备状态",
}

INTENT_LABELS = {
    "buy_point": "买点观察",
    "add_position": "加仓观察",
    "stop_loss": "止损风险观察",
    "take_profit": "止盈观察",
    "watch": "观察",
    "breakout": "突破观察",
}

TRIGGER_KIND_LABELS = {
    "price_cross_above": "价格向上穿越",
    "price_cross_below": "价格向下穿越",
    "price_zone_enter": "价格进入区间",
    "price_zone_exit": "价格离开区间",
}

ACTION_LABELS = {
    "observe": "继续观察",
    "add": "考虑加仓",
    "reduce": "考虑减仓",
    "exit": "考虑退出",
}

SIZING_LABELS = {
    "units": "按份额",
    "position_fraction": "按持仓比例",
    "cash_amount": "按资金金额",
    "target_position_units": "按目标持仓份额",
    "default_policy": "采用默认仓位规则",
}

INTERVAL_LABELS = {
    "1m": "1分钟",
    "5m": "5分钟",
    "30m": "30分钟",
    "1d": "日线",
    "1w": "周线",
}

VOLUME_METRIC_LABELS = {
    "same_bucket_5m_volume_ratio": "同一时间段的5分钟成交量比",
    "same_clock_cumulative_volume_ratio": "同一时刻的累计成交量比",
    "absolute_cumulative_volume": "累计成交量",
}

ADJUSTMENT_LABELS = {
    "raw": "原始不复权价格",
}

UNIT_LABELS = {
    "CNY": "人民币元",
    "ratio": "倍",
    "shares": "股",
    "lots": "手",
}

GATE_DECISION_LABELS = {
    "proceed": "通过，可生成正式周报",
    "stop": "未通过，停止生成正式周报",
}

MARKET_DATA_STATUS_LABELS = {
    "verified": "已交叉验证",
    "single_source": "仅有单一数据源",
    "limited": "数据受限",
    "unavailable": "不可用",
}

_OPAQUE_PREFIXES = (
    "weekly_",
    "scenario_",
    "candidate_",
    "claim_",
    "condition_",
    "level_",
)
_TECHNICAL_CODE = re.compile(r"^[a-z][a-z0-9_.-]*$")


def label(mapping: dict[str, str], value: Any, *, unknown: str = "未识别") -> str:
    """Translate a finite contract value without leaking an unknown code."""

    if value in (None, ""):
        return "—"
    return mapping.get(str(value), unknown)


def bool_label(value: Any) -> str:
    if value is True:
        return "是"
    if value is False:
        return "否"
    return "—"


def identifier(value: Any) -> str:
    """Keep traceability while removing English implementation prefixes."""

    text = str(value or "").strip()
    for prefix in _OPAQUE_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text or "—"


def time_label(value: Any) -> str:
    """Render ISO timestamps as reader-friendly Beijing time."""

    text = str(value or "").strip()
    if not text:
        return "—"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.strftime("%Y-%m-%d %H:%M")
    return f"{parsed.astimezone(SHANGHAI):%Y-%m-%d %H:%M}（北京时间）"


def human_text(value: Any) -> str:
    """Translate technical fragments embedded in otherwise human prose."""

    if value in (None, ""):
        return "—"
    text = str(value).strip()
    if text in DATA_GAP_LABELS:
        return DATA_GAP_LABELS[text]
    if _TECHNICAL_CODE.fullmatch(text) and "_" in text:
        return "未分类技术项（详情见结构化数据文件）"
    return (
        text.replace("ATR14 的", "14日平均真实波幅的")
        .replace("ATR14 缓冲", "14日平均真实波幅缓冲")
        .replace("ATR14", "14日平均真实波幅")
        .replace(" CNY", " 元")
        .replace("classify_only", "仅用于分类")
    )


def field_label(field: Any) -> str:
    return FIELD_LABELS.get(str(field or ""), "其他结构化字段")


def field_value_label(field: Any, value: Any) -> str:
    """Translate a scenario-delta value according to its field semantics."""

    if value in (None, ""):
        return "未设置"
    name = str(field or "")
    mappings = {
        "intent": INTENT_LABELS,
        "trigger.kind": TRIGGER_KIND_LABELS,
        "trigger.interval": INTERVAL_LABELS,
        "volume_confirmation.metric": VOLUME_METRIC_LABELS,
        "invalidation.kind": TRIGGER_KIND_LABELS,
        "action_template.action": ACTION_LABELS,
        "action_template.sizing.kind": SIZING_LABELS,
        "automation_status": AUTOMATION_STATUS_LABELS,
    }
    if name in mappings:
        return label(mappings[name], value)
    if name == "trigger.confirmation_count":
        return f"{value}次"
    if name == "volume_confirmation.threshold":
        return f"{value}倍"
    if name.startswith("original_level.") or name == "invalidation.level":
        return f"{value}元"
    return human_text(value)


def trigger_label(trigger: dict[str, Any]) -> str:
    """Render the structured trigger as a concise Chinese sentence."""

    kind = str(trigger.get("kind") or "")
    interval = label(INTERVAL_LABELS, trigger.get("interval"))
    confirmations = trigger.get("confirmation_count")
    confirmation_text = f"，连续确认{confirmations}次" if confirmations else ""
    if kind == "price_zone_enter":
        return (
            f"每隔{interval}检查价格是否进入 {trigger.get('lower')} 至 "
            f"{trigger.get('upper')} 元区间{confirmation_text}"
        )
    if kind == "price_zone_exit":
        return (
            f"每隔{interval}检查价格是否离开 {trigger.get('lower')} 至 "
            f"{trigger.get('upper')} 元区间{confirmation_text}"
        )
    if kind in {"price_cross_above", "price_cross_below"}:
        direction = "向上突破" if kind == "price_cross_above" else "向下跌破"
        return (
            f"每隔{interval}检查价格是否{direction} {trigger.get('threshold')} 元"
            f"{confirmation_text}"
        )
    return "触发条件尚未形成可读说明（详情见结构化数据文件）"
