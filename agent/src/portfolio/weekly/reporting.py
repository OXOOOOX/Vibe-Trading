"""Render Markdown/PDF input exclusively from the structured weekly JSON."""

from __future__ import annotations

from typing import Any

from .presentation import (
    ACTIVATION_POLICY_LABELS,
    ADJUSTMENT_LABELS,
    AUTOMATION_STATUS_LABELS,
    CHANGE_TYPE_LABELS,
    CONDITION_COVERAGE_LABELS,
    CONDITION_ROLE_LABELS,
    COVERAGE_LABELS,
    DATA_GAP_LABELS,
    GATE_DECISION_LABELS,
    INTENT_LABELS,
    LEVEL_TYPE_LABELS,
    MARKET_DATA_STATUS_LABELS,
    MONITORING_STATUS_LABELS,
    OUTCOME_LABELS,
    QUALITY_LABELS,
    STRENGTH_LABELS,
    TRADE_EXECUTION_LABELS,
    UNIT_LABELS,
    bool_label,
    field_label,
    field_value_label,
    human_text,
    label,
    time_label,
    trigger_label,
)


def _cell(value: Any) -> str:
    return str(value if value not in (None, "") else "—").replace("|", "\\|").replace("\n", " ")


def _level_text(level: dict[str, Any]) -> str:
    if level.get("kind") == "zone":
        return f"{level.get('lower')}–{level.get('upper')}"
    return str(level.get("value") or "—")


def _gate_lines(gate: dict[str, Any]) -> list[str]:
    missing = [
        DATA_GAP_LABELS.get(str(item), "未分类数据缺口（详情见结构化数据文件）")
        for item in gate.get("missing_scopes") or []
    ]
    return [
        f"- 生成结论：{label(GATE_DECISION_LABELS, gate.get('decision'))}",
        f"- 市场数据状态：{label(MARKET_DATA_STATUS_LABELS, gate.get('market_data_status'))}",
        f"- 日线数据条数：{gate.get('daily_bar_count') if gate.get('daily_bar_count') is not None else '—'}",
        f"- 本周交易数据完整：{bool_label(gate.get('current_week_complete'))}",
        f"- 上一交易周数据可用：{bool_label(gate.get('previous_week_available'))}",
        f"- 上一份正式周报可用：{bool_label(gate.get('previous_weekly_report_available'))}",
        f"- 已尝试刷新数据：{bool_label(gate.get('refresh_attempted'))}",
        f"- 数据刷新成功：{bool_label(gate.get('refresh_succeeded'))}",
        f"- 本周是否为缩短交易周：{bool_label(gate.get('short_week'))}",
        f"- 已授权使用单一数据源：{bool_label(gate.get('single_source_authorized'))}",
        f"- 尚缺数据：{'；'.join(missing) if missing else '无'}",
    ]


_CONTEXT_SCOPE_LABELS = {
    "product_profile": "基金产品档案",
    "tracking_index": "跟踪指数",
    "index_relative_strength": "指数相对强弱",
    "fund_shares": "基金份额",
    "premium_discount": "折溢价",
    "nav_reference": "基金净值与盘中参考净值",
    "official_tracking_quality": "官方跟踪质量",
    "market_tracking_deviation": "市场价格跟踪偏离",
    "component_exposure": "成分与行业暴露",
    "component_research": "成分公司研究",
}
_AVAILABILITY_LABELS = {
    "complete": "可用",
    "partial": "部分可用",
    "missing": "缺失",
    "not_applicable": "不适用",
}
_SUPPORT_LABELS = {
    "verified": "已核验",
    "triangulated": "已由独立来源交叉验证",
}
_REUSE_EXCLUSION_LABELS = {
    "report_not_published": "候选报告尚未正式发布",
    "report_failed_validation": "候选报告未通过正式校验",
    "future_report_data": "候选报告的数据时点晚于本周截止日",
    "report_viewpoint_expired": "候选报告的观点已经过期",
}


def _percent(value: Any) -> str:
    try:
        return f"{float(value) * 100:+.2f}%"
    except (TypeError, ValueError):
        return "—"


def _limit_percent(value: Any) -> str:
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "—"


def _scope_time_label(value: Any) -> str:
    text = str(value or "").strip()
    return text if len(text) == 10 else time_label(text) if text else "未登记"


def _context_lines(context: dict[str, Any]) -> list[str]:
    lines = ["", "## 统一报告目录上下文", ""]
    if not context.get("catalog_available"):
        lines.append("- 统一报告目录当前不可用，本周结论没有继承历史报告文本。")
        return lines
    for scope_id, scope in (context.get("scopes") or {}).items():
        if not isinstance(scope, dict) or scope.get("legacy") is True:
            continue
        lines.append(
            f"- {_CONTEXT_SCOPE_LABELS.get(str(scope_id), '其他结构化数据')}："
            f"{_AVAILABILITY_LABELS.get(str(scope.get('availability')), '状态未明')}；"
            f"数据截至 {_scope_time_label(scope.get('data_as_of'))}"
        )
    tracking = context.get("tracking_metrics") or {}
    relative = (tracking.get("index_relative_strength") or {}).get("metrics") or {}
    if relative:
        lines.extend([
            "",
            "### ETF 与跟踪指数对照",
            "",
            f"- ETF 本周市场价格收益：{_percent(relative.get('etf_market_return_1w'))}",
            f"- 跟踪指数本周收益：{_percent(relative.get('tracked_index_return_1w'))}",
            f"- ETF 相对指数收益差：{_percent(relative.get('fund_index_return_gap_1w'))}",
        ])
    market_tracking = (tracking.get("market_tracking_deviation") or {}).get("metrics") or {}
    if market_tracking:
        lines.append(
            "- 二十日市场价格跟踪偏离（年化代理）："
            f"{_percent(market_tracking.get('market_tracking_deviation_20d'))}"
        )
        if market_tracking.get("market_tracking_deviation_60d") is not None:
            lines.append(
                "- 六十日市场价格跟踪偏离（年化代理）："
                f"{_percent(market_tracking.get('market_tracking_deviation_60d'))}"
            )
        lines.append("- 上述偏离由二级市场收盘价计算，不代表基金定期报告披露的官方跟踪误差。")
    official_facts = (
        ((context.get("scopes") or {}).get("official_tracking_quality") or {}).get("facts")
        or []
    )
    official_rows = {
        str(item.get("scope_key") or ""): item
        for item in official_facts
        if isinstance(item, dict) and item.get("metric") == "tracking_difference"
    }
    objective_limits = {
        str(item.get("metric") or ""): item.get("value")
        for item in official_facts
        if isinstance(item, dict) and str(item.get("scope_key") or "") == "contract_objective"
    }
    if official_rows or objective_limits:
        lines.extend(["", "### 官方跟踪质量披露", ""])
        for scope_key, period_label in (("3m", "近三个月"), ("6m", "近六个月")):
            fact = official_rows.get(scope_key)
            if fact:
                lines.append(
                    f"- {period_label}基金净值收益减业绩比较基准收益："
                    f"{_percent(fact.get('value'))}。"
                )
        daily_limit = objective_limits.get("daily_tracking_deviation_absolute_limit")
        annual_limit = objective_limits.get("annual_tracking_error_limit")
        if daily_limit is not None:
            lines.append(
                "- 合同目标：日均跟踪偏离度绝对值不超过 "
                f"{_limit_percent(daily_limit)}。"
            )
        if annual_limit is not None:
            lines.append(f"- 合同目标：年跟踪误差不超过 {_limit_percent(annual_limit)}。")
        lines.append("- 本节来自基金官方定期报告，与上方二级市场价格代理指标分开登记。")
    structural = (context.get("structured_claims") or {}).get("structural") or {}
    summary = structural.get("summary") if isinstance(structural, dict) else None
    if isinstance(summary, dict):
        lines.extend([
            "",
            "### 可复用的结构研究结论",
            "",
            f"- {_SUPPORT_LABELS.get(str(summary.get('support_status')), '支撑状态未明')}："
            f"{human_text(summary.get('text'))}",
        ])
    risks = structural.get("risks") if isinstance(structural, dict) else []
    for item in risks or []:
        if isinstance(item, dict):
            lines.append(
                f"- 结构研究风险（{_SUPPORT_LABELS.get(str(item.get('support_status')), '支撑状态未明')}）："
                f"{human_text(item.get('text'))}"
            )
    if not summary:
        lines.append("- 当前没有达到复用等级的结构研究结论。")
        reasons = list(dict.fromkeys(
            _REUSE_EXCLUSION_LABELS.get(str(item.get("reason") or ""), "候选结论未达到复用条件")
            for item in context.get("reuse_exclusions") or []
            if isinstance(item, dict)
        ))
        for reason in reasons:
            lines.append(f"- 未复用原因：{reason}。")
    return lines


def render_weekly_markdown(brief: dict[str, Any]) -> str:
    """Produce the human report without inventing facts outside ``brief``."""

    view = brief.get("weekly_view") or {}
    stats = brief.get("weekly_statistics") or {}
    gate = brief.get("analysis_gate") or {}
    bundle = brief.get("monitoring_bundle") or {}
    method_snapshot = brief.get("analysis_method_snapshot") or {}
    agent_analysis = brief.get("agent_analysis") or {}
    lines = [
        f"# {brief['week_end']} {brief.get('security_name') or brief['symbol']}（{brief['symbol']}）周度复盘",
        "",
        f"- 生成时间：{time_label(brief.get('generated_at'))}",
        f"- 数据截至：{time_label(brief.get('data_as_of'))}",
        f"- 适用交易周：{brief.get('week_start')} 至 {brief.get('week_end')}",
        f"- 有效期：{time_label(brief.get('valid_from'))} 至 {time_label(brief.get('valid_until'))}",
        f"- 复核时间：{time_label(brief.get('review_due_at'))}",
        f"- 报告质量 / 数据覆盖：{label(QUALITY_LABELS, brief.get('quality_status'))} / "
        f"{label(COVERAGE_LABELS, brief.get('coverage_status'))}",
        f"- 交易执行：{label(TRADE_EXECUTION_LABELS, brief.get('trade_execution'))}",
        "",
        "## 本周结论",
        "",
        str(brief.get("summary") or view.get("summary") or "本周仅保留结构化观察。"),
        "",
        "## 周度观点",
        "",
        f"- 趋势阶段：{view.get('trend_stage')}",
        f"- 趋势方向 / 强度：{view.get('trend_direction')} / {view.get('trend_strength')}",
        f"- 本周涨跌：{view.get('week_return_pct')}%",
        f"- 相对强弱：{view.get('relative_strength')}",
        f"- 量能 / 波动：{view.get('volume_state')} / {view.get('volatility_state')}",
        f"- 结构位置：{view.get('location_context')}",
        f"- 解释：{view.get('summary')}",
    ]
    lines.extend(_context_lines(dict(brief.get("weekly_context") or {})))
    regime = method_snapshot.get("regime") or {}
    methods_by_id = {
        str(item.get("method_id")): str(item.get("label") or item.get("method_id"))
        for item in method_snapshot.get("methods") or []
        if isinstance(item, dict)
    }
    levels_by_id = {
        str(item.get("candidate_id")): item
        for item in method_snapshot.get("level_candidates") or []
        if isinstance(item, dict)
    }
    lines.extend(
        [
            "",
            "## 分析方法与反证审查",
            "",
            f"- 确定性市场状态：{regime.get('stage') or '待确认'}；"
            f"方向 {regime.get('direction') or '待确认'}；"
            f"强度 {regime.get('strength') or '待确认'}；"
            f"波动 {regime.get('volatility_state') or '待确认'}。",
        ]
    )
    if agent_analysis.get("status") == "completed":
        selected_methods = [
            methods_by_id.get(str(item), "已登记分析方法")
            for item in agent_analysis.get("selected_methods") or []
        ]
        lines.extend(
            [
                f"- 分析智能体采用的方法：{'、'.join(selected_methods) or '未选择'}。",
                f"- 市场状态解释：{agent_analysis.get('regime_interpretation')}",
                f"- 跨周期结论：{agent_analysis.get('cross_horizon_conclusion')}",
                "- 支持证据："
                + "；".join(agent_analysis.get("evidence_for") or ["未形成合格支持证据。"]),
                "- 反对证据："
                + "；".join(agent_analysis.get("counter_evidence") or ["未形成合格反对证据。"]),
                "- 失效条件："
                + "；".join(agent_analysis.get("invalidation_conditions") or ["等待下一次复核。"]),
                "- 反证审查："
                + (
                    "通过"
                    if (agent_analysis.get("critic") or {}).get("verdict") == "pass"
                    else "需要修订"
                    if (agent_analysis.get("critic") or {}).get("verdict") == "revise"
                    else "证据不足"
                )
                + "；"
                + "；".join((agent_analysis.get("critic") or {}).get("issues") or ["未发现新增问题。"]),
            ]
        )
        selected_levels = [
            levels_by_id[str(item)]
            for item in agent_analysis.get("selected_level_ids") or []
            if str(item) in levels_by_id
        ]
        for item in selected_levels:
            level_name = "支撑候选" if item.get("level_type") == "support" else "阻力候选"
            confidence = {"high": "高", "medium": "中", "low": "低"}.get(
                str(item.get("confidence") or ""), "待确认"
            )
            lines.append(
                f"- 分析智能体选中的{level_name}：{item.get('lower')}–{item.get('upper')}；"
                f"证据强度 {confidence}。"
            )
    else:
        lines.append(
            "- 分析智能体的方法分析未运行；本报告只采用可复核的确定性方法结果，不把机械边界包装成主观判断。"
        )
    lines.extend([
        "", "## 量价事实", "",
        "| 开盘 | 最高 | 最低 | 收盘 | 周振幅 | 总成交量 | 较上周量比 | 14日平均真实波幅 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
        "| " + " | ".join(
            _cell(value)
            for value in (
                stats.get("open"),
                stats.get("high"),
                stats.get("low"),
                stats.get("close"),
                f"{stats.get('max_amplitude_pct')}%",
                stats.get("volume"),
                stats.get("volume_ratio_vs_previous_week"),
                stats.get("atr14"),
            )
        ) + " |",
        "", "## 关键点位", "",
        "| 类型 | 点位 | 价格口径 | 强度 | 计算依据 |",
        "|---|---:|---|---|---|",
    ])
    for level in brief.get("key_levels") or []:
        basis = level.get("calculation_basis") or {}
        lines.append(
            "| " + " | ".join(
                [
                    _cell(label(LEVEL_TYPE_LABELS, level.get("level_type"))),
                    _cell(_level_text(level)),
                    _cell(
                        f"{label(ADJUSTMENT_LABELS, level.get('adjustment'))} / "
                        f"{label(UNIT_LABELS, level.get('unit'))}"
                    ),
                    _cell(label(STRENGTH_LABELS, level.get("strength"))),
                    _cell(human_text(basis.get("summary") or basis.get("method"))),
                ]
            ) + " |"
        )

    lines.extend(["", "## 上一周场景验证", ""])
    validations = brief.get("previous_week_validation") or []
    if validations:
        lines.extend(
            [
                "| 场景族 | 结果 | 首次接近 | 首次触发 | 失效 | 观察高低 | 说明 |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for ordinal, item in enumerate(validations, start=1):
            lines.append(
                "| " + " | ".join(
                    [
                        _cell(f"场景 {ordinal}"),
                        _cell(label(OUTCOME_LABELS, item.get("outcome"))),
                        _cell(time_label(item.get("first_approach_at"))),
                        _cell(time_label(item.get("first_trigger_at"))),
                        _cell(time_label(item.get("invalidation_at"))),
                        _cell(f"{item.get('observed_low')}–{item.get('observed_high')}"),
                        _cell(human_text(item.get("summary"))),
                    ]
                ) + " |"
            )
    else:
        lines.append("- 这是该标的首份正式周报，没有上一周正式场景可验证。")

    lines.extend(["", "## 与上一份正式周报的场景变化", ""])
    changes = brief.get("scenario_changes") or []
    candidate_intents = {
        str(item.get("candidate_id") or item.get("scenario_id") or ""): item.get("intent")
        for item in bundle.get("candidates") or []
        if isinstance(item, dict)
    }
    if changes:
        for change in changes:
            change_type = str(change.get("change_type") or "")
            raw_intent = candidate_intents.get(str(change.get("candidate_id") or ""))
            intent = f"{label(INTENT_LABELS, raw_intent)}：" if raw_intent else ""
            lines.append(
                f"- 场景变化：**{label(CHANGE_TYPE_LABELS, change_type)}**；"
                f"{intent}{human_text((change.get('change_details') or {}).get('summary'))}"
            )
            # A newly added or removed scenario has no meaningful before/after
            # comparison. Listing every field as “未设置 → …” buries the
            # actual weekly conclusion and exposes implementation structure.
            if change_type in {"new", "added", "removed", "expired", "withdrawn"}:
                continue
            for field in change.get("field_changes") or []:
                lines.append(
                    f"  - {field_label(field.get('field'))}："
                    f"{_cell(field_value_label(field.get('field'), field.get('before')))} → "
                    f"{_cell(field_value_label(field.get('field'), field.get('after')))}"
                )
    else:
        lines.append("- 没有上一份正式周报，当前候选均按首次建立登记。")

    lines.extend(["", "## 结构化监控候选", ""])
    lines.append(
        f"- 状态：{label(MONITORING_STATUS_LABELS, bundle.get('monitoring_status'))}；"
        f"启用规则：{label(ACTIVATION_POLICY_LABELS, bundle.get('activation_policy'))}；"
        f"交易执行：{label(TRADE_EXECUTION_LABELS, bundle.get('trade_execution'))}"
    )
    lines.append(
        f"- 来源有效期：{time_label(bundle.get('source_valid_until'))}；"
        f"复核时间：{time_label(bundle.get('review_due_at'))}"
    )
    for candidate in bundle.get("candidates") or []:
        trigger = candidate.get("trigger") or {}
        lines.extend(
            [
                "",
                f"### {candidate.get('label')}",
                "",
                f"- 准备状态：{label(AUTOMATION_STATUS_LABELS, candidate.get('automation_status'))}"
                "（仍需人工确认，不会自动启用）",
                f"- 价格提醒：{trigger_label(trigger)}",
                f"- 点位依据：{human_text((candidate.get('calculation_basis') or {}).get('summary'))}",
            ]
        )
        for condition in candidate.get("source_conditions") or []:
            lines.append(
                f"- 原始条件（{label(CONDITION_ROLE_LABELS, condition.get('role'))} / "
                f"{label(CONDITION_COVERAGE_LABELS, condition.get('coverage_status'))}）："
                f"{human_text(condition.get('source_text'))}；{human_text(condition.get('reason'))}"
            )

    lines.extend(["", "## 数据门控与缺口", ""])
    lines.extend(_gate_lines(gate))
    for gap in brief.get("data_gaps") or []:
        lines.append(f"- 数据缺口：{human_text(gap)}")
    for risk in brief.get("risks") or []:
        lines.append(f"- 风险：{human_text(risk)}")
    lines.extend(
        [
            "",
            "> 本周报只生成研究与监控候选，不自动激活监控、不发送提醒、不执行交易。",
        ]
    )
    return "\n".join(lines).strip() + "\n"
