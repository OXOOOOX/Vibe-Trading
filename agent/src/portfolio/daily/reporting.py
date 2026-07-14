"""Deterministic portfolio aggregation and report rendering."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


_ACTION_LABELS = {
    "observe": "观察",
    "add": "考虑加仓",
    "reduce": "考虑减仓",
    "exit": "考虑退出",
}
_CONFIDENCE_LABELS = {"low": "低", "medium": "中", "high": "高"}
_DATA_STATUS_LABELS = {
    "ok": "数据完整",
    "live": "数据完整",
    "limited": "部分数据受限",
    "partial": "部分数据受限",
    "offline": "离线数据",
}
_SLEEVE_STATUS_LABELS = {
    "below_min": "🔵 低于下限",
    "below_target": "🔵 低于目标",
    "in_band": "🟢 目标区间内",
    "above_target": "🟡 高于目标",
    "above_max": "🔴 高于上限",
    "unconfigured": "⚠️ 未配置",
}
_PRIORITY_LABELS = {
    "high": "🔴 高",
    "normal": "🔵 常规",
    "low": "🟢 低",
}


def _table_cell(value: Any) -> str:
    """Keep model-authored text inside one Markdown table cell."""

    return "；".join(str(value or "—").replace("|", "｜").splitlines()).strip() or "—"


def _security_label(item: dict[str, Any]) -> str:
    symbol = str(item.get("symbol") or "—")
    name = str(item.get("name") or "").strip()
    return f"{name}（{symbol}）" if name and name != symbol else symbol


def _priority_label(priority: Any) -> str:
    key = str(priority or "normal").strip().lower()
    return _PRIORITY_LABELS.get(key, f"🔵 {_table_cell(priority)}")


def _money(value: Any) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def holding_value(holding: dict[str, Any]) -> float:
    direct = _money(holding.get("market_value"))
    if direct:
        return direct
    return _money(holding.get("quantity")) * _money(holding.get("last_price") or holding.get("cost_price"))


def _has_holding_value(holding: dict[str, Any]) -> bool:
    if holding.get("market_value") is not None:
        return True
    return holding.get("quantity") is not None and (
        holding.get("last_price") is not None or holding.get("cost_price") is not None
    )


def _sleeve_status(current: float, sleeve: dict[str, Any]) -> str:
    minimum = _money(sleeve.get("min_amount"))
    target = _money(sleeve.get("target_amount"))
    maximum = sleeve.get("max_amount")
    if current < minimum:
        return "below_min"
    if maximum is not None and current > _money(maximum):
        return "above_max"
    if maximum is not None:
        return "in_band"
    if current < target:
        return "below_target"
    if current > target:
        return "above_target"
    return "in_band"


def aggregate_portfolio(
    *,
    portfolio: dict[str, Any],
    mandate: dict[str, Any],
    briefs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply portfolio constraints and produce a deterministic decision contract."""

    assignments = mandate.get("assignments") or {}
    sleeve_defs = {item["id"]: item for item in mandate.get("sleeves") or []}
    holdings_by_symbol = {
        str(item.get("symbol") or item.get("code") or "").upper(): item for item in portfolio.get("holdings") or []
    }
    sleeve_values: dict[str, float] = defaultdict(float)
    for symbol, holding in holdings_by_symbol.items():
        sleeve_id = str((assignments.get(symbol) or {}).get("active_sleeve_id") or "unassigned")
        sleeve_values[sleeve_id] += holding_value(holding)

    cash_known = portfolio.get("cash") is not None
    cash = _money(portfolio.get("cash"))
    cash_policy = mandate.get("cash_policy") or {}
    cash_floor = _money(cash_policy.get("min_amount")) if cash_policy.get("configured") else 0.0
    cash_available = max(0.0, cash - cash_floor)
    warnings: list[str] = []
    budget_checks: list[dict[str, Any]] = []
    missing_values = [symbol for symbol, holding in holdings_by_symbol.items() if not _has_holding_value(holding)]
    nav = cash + sum(holding_value(item) for item in holdings_by_symbol.values())
    configured_sleeves = [item for item in sleeve_defs.values() if item.get("configured") and not item.get("parent_id")]
    target_total = sum(_money(item.get("target_amount")) for item in configured_sleeves)
    if cash_policy.get("configured"):
        target_total += _money(cash_policy.get("target_amount"))
    target_feasible = target_total <= nav + 0.01
    quantitative_plan_enabled = cash_known and not missing_values and target_feasible
    if not cash_known:
        warnings.append("现金尚未手工维护；保留方向判断，但关闭全部定量金额建议。")
    if missing_values:
        warnings.append("以下持仓缺少可核验市值，已关闭定量金额建议：" + "、".join(missing_values))
    if not target_feasible:
        warnings.append(
            f"分区与现金目标合计 {target_total:.2f} 超过组合净值 {nav:.2f}；"
            "保留研究方向，但不自动缩减用户目标，也不输出定量金额。"
        )
    budget_checks.extend(
        [
            {"check": "cash_known", "passed": cash_known, "value": portfolio.get("cash")},
            {"check": "holding_values_complete", "passed": not missing_values, "missing": missing_values},
            {
                "check": "target_total_within_nav",
                "passed": target_feasible,
                "target_total": round(target_total, 2),
                "nav": round(nav, 2),
            },
        ]
    )

    prepared: list[dict[str, Any]] = []
    for raw in briefs:
        brief = dict(raw)
        symbol = str(brief.get("symbol") or "").upper()
        holding = holdings_by_symbol.get(symbol, {})
        current_value = holding_value(holding)
        assignment = assignments.get(symbol) or {}
        sleeve_id = str(assignment.get("active_sleeve_id") or "unassigned")
        sleeve = sleeve_defs.get(sleeve_id) or {}
        action = str(brief.get("action") or "observe")
        requested = _money(brief.get("suggested_amount"))
        if not requested and action in {"reduce", "exit"}:
            requested = current_value if action == "exit" else current_value * 0.25
        if not requested and action == "add":
            requested = max(0.0, current_value * 0.1)

        brief.update(
            {
                "raw_action": action,
                "action": action,
                "requested_amount": round(requested, 2) if requested else None,
                "constrained_amount": None,
                "funded_now_amount": None,
                "conditional_amount": None,
                "funding_status": None,
                "sleeve_id": sleeve_id,
                "constraint_notes": [],
                "current_value": round(current_value, 2),
            }
        )
        prepared.append(brief)

    reduction_pool = 0.0
    for brief in prepared:
        if brief["action"] not in {"reduce", "exit"}:
            continue
        if not quantitative_plan_enabled:
            brief["constraint_notes"].append("组合定量计划已关闭")
            continue
        sleeve_id = str(brief["sleeve_id"])
        sleeve = sleeve_defs.get(sleeve_id) or {}
        current_value = _money(brief["current_value"])
        cap = min(_money(brief["requested_amount"]), current_value)
        risk_override = brief["action"] == "exit" and brief.get("confidence") == "high"
        if sleeve.get("configured") and not risk_override:
            minimum = _money(sleeve.get("min_amount"))
            cap = min(cap, max(0.0, sleeve_values[sleeve_id] - minimum))
        if cap <= 0:
            brief["action"] = "observe"
            brief["constraint_notes"].append("分区下限不支持继续降低风险暴露")
            continue
        brief["constrained_amount"] = round(cap, 2)
        brief["funding_status"] = "expected_reduction"
        if risk_override:
            brief["constraint_notes"].append("高置信度退出结论覆盖分区低配状态")
        sleeve_values[sleeve_id] -= cap
        reduction_pool += cap

    for brief in prepared:
        if brief["action"] != "add":
            continue
        if not quantitative_plan_enabled:
            brief["constraint_notes"].append("组合定量计划已关闭")
            continue
        sleeve_id = str(brief["sleeve_id"])
        sleeve = sleeve_defs.get(sleeve_id) or {}
        current_value = _money(brief["current_value"])
        cap = _money(brief["requested_amount"])
        if sleeve.get("configured"):
            target = _money(sleeve.get("target_amount"))
            band = _money(sleeve.get("rebalance_band_amount"))
            gap = max(0.0, target - sleeve_values[sleeve_id])
            if gap <= band:
                cap = 0.0
                brief["constraint_notes"].append("分区偏差未超过再平衡带宽")
            else:
                max_amount = sleeve.get("max_amount")
                upper = _money(max_amount) if max_amount is not None else target
                if upper:
                    cap = min(cap, max(0.0, upper - sleeve_values[sleeve_id]))
                position_max = sleeve.get("single_position_max_amount")
                if position_max is not None:
                    cap = min(cap, max(0.0, _money(position_max) - current_value))
        funded_now = min(cap, cash_available)
        conditional = min(max(0.0, cap - funded_now), reduction_pool)
        total = funded_now + conditional
        if total <= 0:
            brief["action"] = "observe"
            brief["constraint_notes"].append("现金、预期减持回款或分区上限不支持新增风险暴露")
            continue
        cash_available -= funded_now
        reduction_pool -= conditional
        sleeve_values[sleeve_id] += total
        brief.update(
            {
                "constrained_amount": round(total, 2),
                "funded_now_amount": round(funded_now, 2) if funded_now else None,
                "conditional_amount": round(conditional, 2) if conditional else None,
                "funding_status": (
                    "conditional_on_reduction"
                    if conditional and not funded_now
                    else "partially_conditional_on_reduction"
                    if conditional
                    else "funded_now"
                ),
            }
        )

    constrained = prepared

    sleeve_summary = []
    for sleeve_id, sleeve in sleeve_defs.items():
        current = sum(
            holding_value(holding)
            for symbol, holding in holdings_by_symbol.items()
            if (assignments.get(symbol) or {}).get("active_sleeve_id") == sleeve_id
        )
        target = _money(sleeve.get("target_amount"))
        status = _sleeve_status(current, sleeve) if sleeve.get("configured") else "unconfigured"
        sleeve_summary.append(
            {
                "id": sleeve_id,
                "name": sleeve.get("name") or sleeve_id,
                "configured": bool(sleeve.get("configured")),
                "current_amount": round(current, 2),
                "target_amount": round(target, 2),
                "gap_amount": round(target - current, 2) if sleeve.get("configured") else None,
                "min_amount": _money(sleeve.get("min_amount")),
                "max_amount": (_money(sleeve.get("max_amount")) if sleeve.get("max_amount") is not None else None),
                "status": status,
                "eligible_increases": [
                    item["symbol"]
                    for item in constrained
                    if item.get("sleeve_id") == sleeve_id and item.get("action") == "add"
                ],
                "eligible_reductions": [
                    item["symbol"]
                    for item in constrained
                    if item.get("sleeve_id") == sleeve_id and item.get("action") in {"reduce", "exit"}
                ],
                "hold_or_observe": [
                    item["symbol"]
                    for item in constrained
                    if item.get("sleeve_id") == sleeve_id and item.get("action") == "observe"
                ],
            }
        )

    priority = {"exit": 0, "reduce": 1, "add": 2, "observe": 3}
    constrained.sort(key=lambda item: (priority.get(str(item.get("action")), 9), item.get("symbol", "")))
    counts = {
        action: sum(1 for item in constrained if item.get("action") == action)
        for action in ("exit", "reduce", "add", "observe")
    }
    observations = [
        {"symbol": item.get("symbol"), "watch_point": point}
        for item in constrained
        for point in item.get("watch_points") or []
    ][:12]
    decision = {
        "schema_version": 1,
        "portfolio_summary": {
            "nav": round(nav, 2),
            "holding_count": len(holdings_by_symbol),
            "target_total": round(target_total, 2),
        },
        "cash_summary": {
            "actual_cash": cash if cash_known else None,
            "minimum_cash": round(cash_floor, 2),
            "available_cash": round(max(0.0, cash - cash_floor), 2) if cash_known else None,
            "remaining_cash": round(cash_available, 2) if quantitative_plan_enabled else None,
            "unused_expected_reduction": round(reduction_pool, 2),
        },
        "sleeve_summaries": sleeve_summary,
        "today_observation_points": observations,
        "increase_candidates": [item for item in constrained if item.get("action") == "add"],
        "reduce_candidates": [item for item in constrained if item.get("action") == "reduce"],
        "exit_candidates": [item for item in constrained if item.get("action") == "exit"],
        "hold_items": [],
        "observe_items": [item for item in constrained if item.get("action") == "observe"],
        "conditional_order_observations": [
            {"symbol": item.get("symbol"), **condition}
            for item in constrained
            for condition in item.get("condition_orders") or []
        ],
        "budget_checks": budget_checks,
        "quantitative_plan_enabled": quantitative_plan_enabled,
        "data_gaps": missing_values,
    }
    return {
        "cash": cash if portfolio.get("cash") is not None else None,
        "cash_floor": cash_floor,
        "sleeves": sleeve_summary,
        "briefs": constrained,
        "warnings": warnings,
        "counts": counts,
        "quantitative_plan_enabled": quantitative_plan_enabled,
        "budget_checks": budget_checks,
        "decision": decision,
    }


def render_holding_markdown(
    *, market_date: str, holding: dict[str, Any], brief: dict[str, Any], data_status: str
) -> str:
    name = str(holding.get("name") or brief.get("symbol") or "个股")
    label = _security_label({"name": name, "symbol": brief.get("symbol")})
    lines = [
        f"# {name}每日更新｜{market_date}",
        "",
        f"- 标的：{label}",
        f"- 数据状态：{_DATA_STATUS_LABELS.get(data_status, data_status)}",
        f"- 今日结论：{_ACTION_LABELS.get(brief.get('action'), '观察')}",
        f"- 置信度：{_CONFIDENCE_LABELS.get(str(brief.get('confidence', 'low')), brief.get('confidence', '低'))}",
        f"- 受约束建议金额：{brief.get('constrained_amount') or '—'}",
        "",
        "## 核心判断",
        "",
        str(brief.get("summary") or "今日以观察为主。"),
        "",
        "## 依据",
        "",
        *[f"- {item}" for item in brief.get("reasons") or []],
        "",
        "## 今日观察点",
        "",
        *([f"- {item}" for item in brief.get("watch_points") or []] or ["- 暂无新增观察点。"]),
        "",
        "## 条件建议",
        "",
    ]
    conditions = brief.get("condition_orders") or []
    if conditions:
        lines.extend(["| 优先级 | 触发条件 | 建议响应 |", "|---|---|---|"])
        lines.extend(
            f"| {_priority_label(item.get('priority'))} | {_table_cell(item.get('trigger'))} | {_table_cell(item.get('response'))} |"
            for item in conditions
        )
    else:
        lines.append("- 今日不设置新增条件建议。")
    if brief.get("constraint_notes"):
        lines.extend(["", "## 组合约束修正", "", *[f"- {item}" for item in brief["constraint_notes"]]])
    return "\n".join(lines).strip() + "\n"


def render_master_markdown(
    *, market_date: str, portfolio: dict[str, Any], mandate: dict[str, Any], aggregate: dict[str, Any]
) -> str:
    lines = [
        f"# 组合盘前综合报告｜{market_date}",
        "",
        "## 今日结论速览",
        "",
        f"- 持仓数：{len(portfolio.get('holdings') or [])}",
        f"- 现金：{aggregate.get('cash') if aggregate.get('cash') is not None else '未维护'}",
        f"- 定量金额计划：{'启用' if aggregate.get('quantitative_plan_enabled') else '关闭'}",
        f"- 退出/减仓/加仓/观察：{aggregate['counts']['exit']} / {aggregate['counts']['reduce']} / {aggregate['counts']['add']} / {aggregate['counts']['observe']}",
        "",
        "## 分组目标与偏离",
        "",
        "| 分组 | 当前金额 | 目标金额 | 偏离 | 状态 |",
        "|---|---:|---:|---:|---|",
    ]
    for sleeve in aggregate["sleeves"]:
        target = f"{sleeve['target_amount']:.2f}" if sleeve["configured"] else "未设置"
        gap = f"{sleeve['gap_amount']:.2f}" if sleeve["gap_amount"] is not None else "—"
        status = _SLEEVE_STATUS_LABELS.get(str(sleeve.get("status") or ""), _table_cell(sleeve.get("status")))
        lines.append(f"| {sleeve['name']} | {sleeve['current_amount']:.2f} | {target} | {gap} | {status} |")
    lines.extend(["", "## 今日行动顺序", ""])
    for item in aggregate["briefs"]:
        amount = f"，建议金额不超过 {item['constrained_amount']:.2f}" if item.get("constrained_amount") else ""
        funding = {
            "funded_now": "（现有现金可覆盖）",
            "conditional_on_reduction": "（依赖减持回款）",
            "partially_conditional_on_reduction": "（部分依赖减持回款）",
        }.get(str(item.get("funding_status") or ""), "")
        lines.append(
            f"- **{_security_label(item)}**：{_ACTION_LABELS.get(item['action'], '观察')}{amount}{funding}。{item.get('summary', '')}"
        )
    lines.extend(["", "## 条件建议", ""])
    condition_count = 0
    condition_rows: list[str] = []
    for item in aggregate["briefs"]:
        conditions = item.get("condition_orders") or []
        for condition_index, condition in enumerate(conditions):
            condition_rows.append(
                "| "
                + " | ".join(
                    [
                        _priority_label(condition.get("priority")),
                        _table_cell(_security_label(item)) if condition_index == 0 else "",
                        _table_cell(condition.get("trigger")),
                        _table_cell(condition.get("response")),
                    ]
                )
                + " |"
            )
            condition_count += 1
    if condition_count:
        lines.extend(
            [
                "| 优先级 | 标的 | 触发条件 | 建议响应 |",
                "|---|---|---|---|",
                *condition_rows,
            ]
        )
    else:
        lines.append("- 今日无新增条件建议。")
    if aggregate.get("warnings"):
        lines.extend(["", "## 数据与约束提示", "", *[f"- {item}" for item in aggregate["warnings"]]])
    lines.extend(["", "## 个股附录索引", ""])
    lines.extend([f"- {_security_label(item)}：详见同名个股每日更新 PDF。" for item in aggregate["briefs"]])
    return "\n".join(lines).strip() + "\n"
