"""Structured, safety-aware summaries for research completion cards."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


_LOCAL_TZ = ZoneInfo("Asia/Shanghai")
_CONDITION_STATUSES = {"available", "not_recommended", "data_insufficient"}
_SCOPE_STATUSES = {"verified", "partial", "not_started", "unavailable", "not_requested"}


@dataclass(frozen=True)
class EvidenceScopeStatus:
    """Availability of one evidence domain used by a decision."""

    scope: str
    status: str
    as_of: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ConditionScenario:
    """One bounded, human-review-only condition-order observation."""

    trigger: str
    response: str
    confirmation: str | None = None
    invalidation: str | None = None


@dataclass(frozen=True)
class ResearchDecisionDigest:
    """Stable payload shared by report delivery and Feishu card rendering."""

    title: str
    symbol: str = ""
    report_time: str = ""
    market_as_of: str = ""
    trend_stage: str = "未标注"
    trend_direction: str = "待确认"
    trend_strength: str = "待确认"
    confidence: str = "low"
    trend_summary: str = "报告已生成，请查看 PDF 获取完整判断。"
    condition_status: str = "not_recommended"
    condition_summary: str = "本次未提取到新增条件单建议。"
    conditions: tuple[ConditionScenario, ...] = ()
    data_scopes: tuple[EvidenceScopeStatus, ...] = ()
    risk_notice: str = "仅供人工研究与决策，不会自动创建或提交订单。"
    report_version: int = 2
    fallback: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean_markdown(value: Any, *, limit: int = 360) -> str:
    text = str(value or "")
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"[`*_>#]", "", text)
    text = re.sub(r"\s+", " ", text).strip(" -|：:")
    return text[:limit]


def _title(report: str, fallback: str) -> str:
    match = re.search(r"(?m)^#\s+(.+?)\s*$", report)
    return _clean_markdown(match.group(1) if match else fallback, limit=120) or fallback


def _symbol(report: str) -> str:
    qualified = re.search(r"(?<![\d.])(\d{6}\.(?:SH|SZ|BJ))(?!\w)", report, re.I)
    if qualified:
        return qualified.group(1).upper()
    bare = re.search(r"(?<!\d)(\d{6})(?!\d)", report)
    return bare.group(1) if bare else ""


def _section(report: str, names: tuple[str, ...]) -> str:
    heading = "|".join(re.escape(name) for name in names)
    match = re.search(
        rf"(?ims)^##\s*(?:{heading})\s*$\n(.*?)(?=^##\s|\Z)",
        report,
    )
    return match.group(1).strip() if match else ""


def _summary_fields(report: str) -> dict[str, str]:
    summary = _section(report, ("决策摘要", "结论摘要", "飞书摘要"))
    fields: dict[str, str] = {}
    allowed = {
        "当前走势",
        "趋势阶段",
        "趋势方向",
        "趋势强弱",
        "置信度",
        "条件单",
        "条件单状态",
        "数据状态",
        "数据截至",
        "风险提示",
    }
    for raw in summary.splitlines():
        line = raw.strip().lstrip("-*• ")
        match = re.match(
            r"(当前走势|趋势阶段|趋势方向|趋势强弱|置信度|条件单|条件单状态|数据状态|数据截至|风险提示)\s*[:：]\s*(.+)",
            line,
        )
        if match:
            fields[match.group(1)] = _clean_markdown(match.group(2), limit=500)
            continue
        if raw.strip().startswith("|"):
            cells = [part.strip() for part in raw.strip().strip("|").split("|")]
            if len(cells) >= 2:
                key = _clean_markdown(cells[0], limit=40)
                if key in allowed:
                    fields[key] = _clean_markdown(cells[1], limit=500)
    return fields


def _condition_status(fields: dict[str, str], body: str, report: str) -> str:
    explicit = fields.get("条件单状态", "")
    mapping = {
        "可设置": "available",
        "暂不建议": "not_recommended",
        "数据不足": "data_insufficient",
    }
    if explicit in mapping:
        return mapping[explicit]
    text = f"{fields.get('条件单', '')} {body}"
    if re.search(r"数据不足|数据受限|无法判断|不可校核|尚未校核", text):
        return "data_insufficient"
    if re.search(r"无条件单|不设置|暂不建议|等待.{0,20}(信号|确认)", text):
        return "not_recommended"
    if text.strip():
        return "available"
    if re.search(r"数据受限模式", report[:800]):
        return "data_insufficient"
    return "not_recommended"


def _condition_scenarios(body: str) -> list[ConditionScenario]:
    lines = [line.strip() for line in body.splitlines() if line.strip().startswith("|")]
    if len(lines) < 3:
        return []
    headers = [part.strip() for part in lines[0].strip("|").split("|")]
    if not any("触发" in part for part in headers):
        return []

    def column(row: list[str], token: str) -> str:
        index = next((idx for idx, value in enumerate(headers) if token in value), -1)
        return _clean_markdown(row[index], limit=220) if 0 <= index < len(row) else ""

    scenarios: list[ConditionScenario] = []
    for line in lines[2:]:
        row = [part.strip() for part in line.strip("|").split("|")]
        trigger = column(row, "触发")
        response = column(row, "动作") or column(row, "响应") or column(row, "建议")
        if trigger and response:
            scenarios.append(
                ConditionScenario(
                    trigger=trigger,
                    response=response,
                    confirmation=column(row, "确认") or None,
                    invalidation=column(row, "失效") or column(row, "止损") or None,
                )
            )
        if len(scenarios) >= 2:
            break
    return scenarios


def _scope_statuses(fields: dict[str, str], report: str, market_as_of: str) -> list[EvidenceScopeStatus]:
    status_text = fields.get("数据状态", "")
    scopes: list[EvidenceScopeStatus] = []
    labels = {
        "daily": "日线",
        "intraday": "盘中",
        "fund_flow": "资金流",
        "news": "新闻",
        "fundamentals": "基本面",
    }
    for scope, label in labels.items():
        if not status_text:
            break
        segment = next((part.strip() for part in re.split(r"[｜|；;]", status_text) if label in part), "")
        if not segment:
            status = "not_requested"
            reason = "报告摘要未声明"
        elif re.search(r"未开盘|尚未开始", segment):
            status, reason = "not_started", segment
        elif re.search(r"部分可用|部分", segment):
            status, reason = "partial", segment
        elif re.search(r"已校核|可用|完整", segment):
            status, reason = "verified", None
        elif re.search(r"不足|受限|缺失|不可用|失败", segment):
            status, reason = "unavailable", segment
        else:
            status, reason = "partial", segment
        scopes.append(EvidenceScopeStatus(scope, status, market_as_of or None, reason))
    if scopes:
        return scopes
    limited = bool(re.search(r"数据受限模式|系统无法判断", report[:1200]))
    return [
        EvidenceScopeStatus("daily", "partial" if limited else "verified", market_as_of or None, "旧报告未提供分区状态"),
        EvidenceScopeStatus("intraday", "unavailable" if limited else "not_requested", market_as_of or None, "旧报告未提供分区状态"),
        EvidenceScopeStatus("fund_flow", "not_requested", None, None),
        EvidenceScopeStatus("news", "partial" if limited else "not_requested", None, "旧报告未提供分区状态"),
        EvidenceScopeStatus("fundamentals", "not_requested", None, None),
    ]


def build_research_digest(report: str, *, label: str = "研究报告") -> ResearchDecisionDigest:
    """Extract a validated card digest without making a second model call."""

    text = str(report or "").strip()
    fields = _summary_fields(text)
    trend_body = _section(text, ("当前走势", "走势判断", "核心判断", "技术面与趋势", "趋势与量价"))
    condition_body = _section(text, ("条件单观察位", "条件单观察清单", "条件建议", "条件单建议"))
    trend_summary = fields.get("当前走势") or _clean_markdown(trend_body, limit=360)
    if not trend_summary:
        paragraphs = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith(("#", "|", "-"))]
        trend_summary = _clean_markdown(paragraphs[0] if paragraphs else "", limit=360)
    condition_status = _condition_status(fields, condition_body, text)
    conditions = _condition_scenarios(condition_body) if condition_status == "available" else []
    condition_summary = fields.get("条件单") or _clean_markdown(condition_body, limit=320)
    if condition_status == "data_insufficient":
        condition_summary = "相关行情尚未完成校核，暂不提供精确条件价；请等待数据刷新后复核。"
    if not condition_summary:
        condition_summary = {
            "not_recommended": "本次暂不建议新增条件单；等待趋势和量价信号进一步确认。",
            "data_insufficient": "相关行情尚未完成校核，暂不提供精确条件价。",
        }.get(condition_status, "具体条件情景请查看 PDF。")

    market_as_of = fields.get("数据截至", "")
    now = datetime.now(_LOCAL_TZ).isoformat(timespec="seconds")
    return ResearchDecisionDigest(
        title=_title(text, label),
        symbol=_symbol(text),
        report_time=now,
        market_as_of=market_as_of,
        trend_stage=fields.get("趋势阶段", "未标注"),
        trend_direction=fields.get("趋势方向", "待确认"),
        trend_strength=fields.get("趋势强弱", "待确认"),
        confidence=fields.get("置信度", "low").lower(),
        trend_summary=trend_summary or "报告已生成，请查看 PDF 获取完整判断。",
        condition_status=condition_status,
        condition_summary=condition_summary,
        conditions=tuple(conditions[:2]),
        data_scopes=tuple(_scope_statuses(fields, text, market_as_of)),
        risk_notice=fields.get("风险提示") or "仅供人工研究与决策，不会自动创建或提交订单。",
        fallback=not bool(fields),
    )


def digest_from_brief(brief: dict[str, Any], *, title: str = "个股日报") -> ResearchDecisionDigest:
    """Create the same card contract from a structured daily holding brief."""

    condition_status = str(brief.get("condition_order_status") or "").strip()
    if condition_status not in _CONDITION_STATUSES:
        condition_status = "data_insufficient" if brief.get("data_limited") else (
            "available" if brief.get("condition_orders") else "not_recommended"
        )
    conditions: list[ConditionScenario] = []
    if condition_status == "available":
        for item in brief.get("condition_orders") or []:
            if not isinstance(item, dict):
                continue
            trigger = _clean_markdown(item.get("trigger"), limit=220)
            response = _clean_markdown(item.get("response"), limit=220)
            if trigger and response:
                conditions.append(
                    ConditionScenario(
                        trigger=trigger,
                        response=response,
                        confirmation=_clean_markdown(item.get("confirmation"), limit=180) or None,
                        invalidation=_clean_markdown(item.get("invalidation"), limit=180) or None,
                    )
                )
    scopes: list[EvidenceScopeStatus] = []
    raw_scopes = brief.get("data_scopes") or {}
    if isinstance(raw_scopes, dict):
        for scope, value in raw_scopes.items():
            item = value if isinstance(value, dict) else {"status": value}
            status = str(item.get("status") or "partial")
            scopes.append(
                EvidenceScopeStatus(
                    str(scope),
                    status if status in _SCOPE_STATUSES else "partial",
                    str(item.get("as_of") or "") or None,
                    str(item.get("reason") or "") or None,
                )
            )
    trend = brief.get("trend") if isinstance(brief.get("trend"), dict) else {}
    condition_summary = str(
        brief.get("condition_order_summary")
        or ("具体条件情景见下方。" if conditions else "本次暂不建议新增条件单。")
    )
    if condition_status == "data_insufficient":
        condition_summary = "相关行情尚未完成校核，暂不提供精确条件价；请等待数据刷新后复核。"
    return ResearchDecisionDigest(
        title=title,
        symbol=str(brief.get("symbol") or ""),
        report_time=str(brief.get("generated_at") or datetime.now(_LOCAL_TZ).isoformat(timespec="seconds")),
        market_as_of=str(brief.get("data_as_of") or ""),
        trend_stage=str(trend.get("stage") or "未标注"),
        trend_direction=str(trend.get("direction") or "待确认"),
        trend_strength=str(trend.get("strength") or "待确认"),
        confidence=str(brief.get("confidence") or "low"),
        trend_summary=str(trend.get("summary") or brief.get("summary") or "今日以观察为主。"),
        condition_status=condition_status,
        condition_summary=condition_summary,
        conditions=tuple(conditions[:2]),
        data_scopes=tuple(scopes),
        fallback=False,
    )
