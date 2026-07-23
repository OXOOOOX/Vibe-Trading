"""Deterministic acquisition plans for consented Deep Report enrichment.

The plan is deliberately separate from report prose.  It turns a user's
``extended`` consent into bounded, auditable acquisition tasks instead of a
larger free-form prompt.  The report workspace stores attempts and derives the
task status; an Agent cannot declare a search exhausted without receipts.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


_SHANGHAI = ZoneInfo("Asia/Shanghai")

ENRICHMENT_OUTCOMES = {
    "evidence_accepted",
    "no_results",
    "retrieval_failed",
    "evidence_rejected",
    "source_unavailable",
}

TERMINAL_ENRICHMENT_STATUSES = {"satisfied", "exhausted", "not_applicable"}

SECTION_TASKS = {
    "business_position": ("business_position",),
    "financial_quality": ("annual_filings",),
    "implied_expectations": ("consensus",),
    "terminal_narrative": ("terminal_inputs",),
}


def _module_status(record: Any, module_id: str) -> str:
    modules = getattr(record, "analysis_modules", {}) or {}
    value = modules.get(module_id)
    if value is None:
        return "pending"
    return str(getattr(value, "status", None) or (value.get("status") if isinstance(value, dict) else "pending"))


def _stable_plan_id(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return f"enrichment_{hashlib.sha256(encoded).hexdigest()[:20]}"


def build_extended_research_plan(
    record: Any,
    *,
    historical_years: int = 8,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a bounded plan from the parent report's unresolved modules."""

    current = now or datetime.now(_SHANGHAI)
    last_complete_year = current.year - 1
    year_count = max(2, min(int(historical_years), 12))
    annual_years = list(range(last_complete_year, last_complete_year - year_count, -1))
    profile = str(getattr(record, "profile", "") or "")
    instrument_type = str(getattr(record, "instrument_type", "") or "")
    symbol = str(getattr(record, "symbol", "") or "").upper()

    tasks: list[dict[str, Any]] = []
    is_company_equity = profile == "equity_deep_research" or instrument_type == "company_equity"
    if is_company_equity:
        tasks.append({
            "task_id": "annual_filings",
            "domain": "financial_statements",
            "section_ids": ["financial_quality"],
            "intent": "补齐并结构化往年官方年报；资料归档到标的档案，不创建独立研究报告。",
            "executor": "get_official_filings",
            "target_years": annual_years,
            "minimum_attempts": 1,
            "minimum_independent_sources": 1,
            "required_fact_metrics": [],
            "required_metric_periods": 0,
            "query_templates": [],
            "status": "planned",
            "reason_code": None,
            "attempts": [],
        })

    if is_company_equity and _module_status(record, "business_position") != "passed":
        tasks.append({
            "task_id": "business_position",
            "domain": "industry_tam_competition",
            "section_ids": ["business_position"],
            "intent": "取得公司业务结构、行业规模、竞争格局和可比市场份额的独立证据。",
            "executor": "web_search+read_url+record_report_evidence",
            "target_years": [],
            "minimum_attempts": 3,
            "minimum_independent_sources": 2,
            "required_fact_metrics": [],
            "required_metric_periods": 0,
            "query_templates": [
                "{company} {year} 年报 分部收入 客户 供应链",
                "{industry} {year} 行业规模 市场份额 行业协会",
                "{company} 竞争对手 市占率 连续历史 数据来源",
            ],
            "status": "planned",
            "reason_code": None,
            "attempts": [],
        })

    if is_company_equity and _module_status(record, "implied_expectations") != "passed":
        tasks.append({
            "task_id": "consensus",
            "domain": "consensus",
            "section_ids": ["implied_expectations"],
            "intent": "取得连续三个预测财年的可追溯一致预期或已发布券商预测。",
            "executor": "analyze_financial_snapshot+web_search+read_url+record_report_evidence",
            "target_years": [],
            "minimum_attempts": 3,
            "minimum_independent_sources": 1,
            "required_fact_metrics": ["consensus_eps"],
            "required_metric_periods": 3,
            "query_templates": [
                "{company} 一致预期 EPS 预测 {forward_years}",
                "{company} 券商研报 盈利预测 {forward_years}",
                "{symbol} analyst consensus earnings forecast {forward_years}",
            ],
            "status": "planned",
            "reason_code": None,
            "attempts": [],
        })

    if is_company_equity and (
        _module_status(record, "terminal_scenarios") not in {"passed", "warning"}
        or _module_status(record, "terminal_narrative") != "passed"
    ):
        tasks.append({
            "task_id": "terminal_inputs",
            "domain": "industry_tam_competition",
            "section_ids": ["terminal_narrative"],
            "intent": "取得带年份、口径和币种的 TAM、长期份额与稳态净利率证据。",
            "executor": "web_search+read_url+record_report_evidence",
            "target_years": [],
            "minimum_attempts": 4,
            "minimum_independent_sources": 2,
            "required_fact_metrics": ["tam", "long_term_market_share", "steady_net_margin"],
            "required_metric_periods": 1,
            "query_templates": [
                "{industry} TAM 市场规模 {year} 官方 统计",
                "{industry} 市场规模 预测 行业协会",
                "{company} 市场份额 独立来源",
                "{industry} 稳态净利率 可比公司 历史",
            ],
            "status": "planned",
            "reason_code": None,
            "attempts": [],
        })

    base = {
        "schema_version": 1,
        "research_depth": "extended",
        "symbol": symbol,
        "parent_report_id": str(getattr(record, "report_id", "") or ""),
        "created_at": current.isoformat(),
        "status": "planned" if tasks else "not_applicable",
        "tasks": tasks,
    }
    base["plan_id"] = _stable_plan_id(base)
    return base


def render_extended_research_plan(plan: dict[str, Any]) -> str:
    """Render a compact machine-owned checklist for the research prompt."""

    tasks = list(plan.get("tasks") or [])
    if not tasks:
        return ""
    lines = [
        "[EXTENDED_RESEARCH_PLAN]",
        f"plan_id={plan.get('plan_id')}",
        "这是服务端强制补齐计划，不是写作建议。先完成或穷尽任务，再提交对应章节。",
        "历史年报工具会自动登记结果；其他每次检索、读取或证据登记后，调用 "
        "report_workspace(command=\"record_research_attempt\", ...)。",
        "只有服务端把任务标记为 satisfied 或 exhausted，相关章节才允许提交；不得用一句‘未找到’提前结束。",
    ]
    for task in tasks:
        lines.append(
            f"- {task['task_id']}: {task['intent']} "
            f"最低尝试 {task['minimum_attempts']} 次，独立来源 {task['minimum_independent_sources']} 个。"
        )
        years = task.get("target_years") or []
        if years:
            lines.append(
                "  调用 get_official_filings 时传 annual_years="
                f"{json.dumps(years, ensure_ascii=False)}；返回资料自动归档到标的档案。"
            )
        for query in task.get("query_templates") or []:
            lines.append(f"  查询模板：{query}")
    lines.append("[/EXTENDED_RESEARCH_PLAN]")
    return "\n".join(lines)


__all__ = [
    "ENRICHMENT_OUTCOMES",
    "SECTION_TASKS",
    "TERMINAL_ENRICHMENT_STATUSES",
    "build_extended_research_plan",
    "render_extended_research_plan",
]
