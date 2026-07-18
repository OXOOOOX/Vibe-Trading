"""Deep report lifecycle, validation, artifact persistence, and reuse."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import shutil
import threading
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from .contracts import ClaimItem, DeepReportRecord, ModuleResult, ReportSection, utc_now
from .profile import EQUITY_DEEP_RESEARCH_PROFILE

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SYMBOL_RE = re.compile(r"(?<![A-Z0-9])((?:\d{6}\.(?:SH|SZ|BJ))|(?:\d{5}\.HK)|(?:[A-Z][A-Z0-9.-]{0,14}\.US))(?![A-Z0-9])", re.I)
_TITLE_RE = re.compile(r"^#\s*(.+?)[（(]([^（）()]+)[）)]穿透式深度研究\s*$", re.M)
_DATA_AS_OF_RE = re.compile(r"数据截至(?:时间)?\s*[：:]\s*([^\n]+)")
_QUALITY_RE = re.compile(r"质量状态\s*[：:]\s*(passed_with_gaps|failed_validation|passed)\b", re.I)
_FACT_RE = re.compile(r"\[Fact:([A-Za-z0-9_-]+)\]")
_EVIDENCE_RE = re.compile(r"\[Evidence:([A-Za-z0-9_-]+)\]")
_READER_FACT_RE = re.compile(r"〔数据(\d+)〕")
_READER_EVIDENCE_RE = re.compile(r"〔来源(\d+)〕")
_MATERIAL_NUMBER_RE = re.compile(
    r"(?:[¥￥$]\s*\d|\d+(?:\.\d+)?\s*(?:%|倍|[xX]\b|元|万元|亿元|亿|万|million|billion))",
    re.I,
)
_TARGET_VALUE_TERMS = ("目标价", "目标股价", "合理估值", "合理市值", "三情景估值")
_TARGET_VALUE_GUARDS = ("不是目标价", "并非目标价", "非目标价", "不构成目标价", "不生成目标价", "不提供目标价")
_DETERMINISTIC_COMMANDS = {"implied_terminal_earnings", "validate_terminal_scenarios"}
_UNSUPPORTED_FORECAST_PROVENANCE_MARKERS = (
    "内部估计",
    "内部预测",
    "内部测算",
    "自行估计",
    "自行预测",
    "自行测算",
    "管理层指引外推",
    "无一致预期",
    "没有一致预期",
    "未给出具体eps预测",
    "未给出具体盈利预测",
    "internal estimate",
    "internal forecast",
    "internal projection",
    "assistant estimate",
    "model estimate",
    "management guidance extrapolation",
    "no consensus",
    "extrapolat",
)
_VALUATION_DIRECTION_TERMS = (
    "显著高估",
    "明显高估",
    "严重高估",
    "高估结论",
    "估值处于极端区间",
    "安全边际不存在",
    "极端乐观预期",
    "极高溢价",
    "显著低估",
    "明显低估",
    "严重低估",
    "低估结论",
    "materially overvalued",
    "materially undervalued",
)
_SECTION_HEADINGS = dict(EQUITY_DEEP_RESEARCH_PROFILE["required_sections"])
_SECTION_IDS = set(_SECTION_HEADINGS)
_SECTION_ALIASES = {
    "核心结论": "executive_summary",
    "投资判断摘要": "executive_summary",
    "公司业务与产业位置": "business_position",
    "公司业务与行业定位": "business_position",
    "财务诊断": "financial_quality",
    "三张报表与财务质量": "financial_quality",
    "会计科目异常与核查清单": "accounting_review",
    "核心财务疑点": "accounting_review",
    "估值分析": "implied_expectations",
    "市值隐含预期": "implied_expectations",
    "长期经营情景": "terminal_narrative",
    "长期经营情景与叙事阶段": "terminal_narrative",
    "反方论证": "counter_thesis",
    "风险清单": "counter_thesis",
    "催化剂与时间窗口": "counter_thesis",
    "反方论证、风险与催化剂": "counter_thesis",
    "结论与跟踪框架": "conclusion_watchlist",
}
_NUMBER_CAPTURE_RE = re.compile(
    r"(?P<prefix>[¥￥$])?\s*(?P<value>-?\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>%|％|倍|[xX×]|元|万元|亿元|亿|万|股|shares?|million|billion)?",
    re.I,
)
_COMPILER_METHOD_HEADING = "数据缺口与方法说明"

_MODULE_READER_LABELS = {
    "executive_summary": "核心结论",
    "business_position": "公司业务与产业位置",
    "financial_quality": "财务质量",
    "accounting_review": "会计科目核查",
    "implied_expectations": "市场隐含预期",
    "terminal_narrative": "长期经营情景",
    "terminal_scenarios": "长期经营情景",
    "counter_thesis": "反方论证与风险",
    "conclusion_watchlist": "结论与跟踪框架",
    "report_gate": "报告完整性",
    "market_data": "价格与市值数据",
    "symbol_identity": "股票身份",
    "latest_quarter": "最新季度数据",
}

_METRIC_READER_LABELS = {
    "current_price": "最新价格",
    "market_cap": "总市值",
    "total_shares": "总股本",
    "revenue": "营业收入",
    "revenue_yoy": "营业收入同比增速",
    "gross_profit": "毛利润",
    "gross_margin": "毛利率",
    "net_profit_parent": "归母净利润",
    "net_profit_parent_yoy": "归母净利润同比增速",
    "net_margin": "净利率",
    "cfo": "经营现金流",
    "cfo_to_net_income": "经营现金流与净利润之比",
    "capex": "资本开支",
    "dividends": "现金分红",
    "total_assets": "总资产",
    "total_equity": "净资产",
    "debt_ratio": "资产负债率",
    "nonrecurring_profit_to_net_income": "非经常性损益占净利润比例",
    "company_global_market_share": "公司全球市场份额",
    "global_crystal_oscillator_tam_2025": "全球晶振市场规模",
    "global_crystal_oscillator_tam_2030": "全球晶振市场规模预测",
    "crystal_oscillator_tam_cagr_2025_2030": "全球晶振市场预计复合增速",
    "ultra_high_freq_oscillator_global_suppliers": "全球具备超高频差分晶振量产能力的厂商数量",
    "net_profit_2026H1_low": "2026年上半年归母净利润预告下限",
    "net_profit_2026H1_high": "2026年上半年归母净利润预告上限",
}


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _atomic_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    tmp.replace(path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _safe_component(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", value.strip()).strip(" ._")
    return cleaned[:120] or fallback


def report_pdf_filename(record: DeepReportRecord) -> str:
    date_text = record.report_date or datetime.now(_SHANGHAI).date().isoformat()
    name = _safe_component(record.security_name or record.symbol, "上市公司")
    symbol = _safe_component(record.symbol, "UNKNOWN")
    return f"{date_text}_{name}（{symbol}）_穿透式深度研究.pdf"


def _reader_datetime(value: Any) -> str:
    """Format machine timestamps as a concise China-market reader timestamp."""

    raw = str(value or "").strip()
    if not raw or raw == "未明确":
        return "尚未明确"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_SHANGHAI)
    local = parsed.astimezone(_SHANGHAI)
    if local.hour == 0 and local.minute == 0 and local.second == 0:
        return local.strftime("%Y年%m月%d日")
    return local.strftime("%Y年%m月%d日 %H:%M（北京时间）")


def _ordered_matches(pattern: re.Pattern[str], content: str) -> list[str]:
    return list(dict.fromkeys(pattern.findall(content)))


def _reader_gap_labels(module_values: dict[str, Any]) -> list[str]:
    """Collapse internal module states into the few reader-relevant evidence gaps."""

    gap_ids = [
        key for key, value in module_values.items()
        if str((value or {}).get("status"))
        in {"warning", "failed_validation", "insufficient_evidence", "not_requested"}
    ]
    # These sections normally inherit a more specific gap from the valuation or
    # scenario modules. Repeating them makes a complete report look half empty.
    inherited = {"executive_summary", "counter_thesis", "conclusion_watchlist"}
    labels: list[str] = []
    for module_id in gap_ids:
        if module_id in inherited:
            continue
        label = _MODULE_READER_LABELS.get(module_id, "部分研究结论")
        if label not in labels:
            labels.append(label)
    if not labels and gap_ids:
        labels.append("部分研究结论")
    return labels


def _reader_quality_label(value: str) -> str:
    return {
        "passed": "研究已完成，证据与校验均通过",
        "passed_with_gaps": "研究已完成；部分判断因公开证据不足而保留",
        "failed_validation": "仅生成诊断，尚不能发布正式研究结论",
    }.get(value, "研究结果正在校验")


def _reader_fact_value(item: dict[str, Any]) -> str:
    metric = str(item.get("metric") or "").strip()
    raw_value = str(item.get("value") or "").strip()
    value = _decimal(item.get("value"))
    if value is None:
        if raw_value and metric == "ultra_high_freq_oscillator_global_suppliers":
            return f"{raw_value}家"
        return raw_value or "数值未登记"
    unit = str(item.get("unit") or "").strip()
    normalized = unit.casefold()
    if normalized in {"ratio", "decimal"}:
        if metric in {"cfo_to_net_income"}:
            return f"{value:.2f}倍"
        return f"{value * Decimal('100'):.2f}%"
    if normalized in {"percent", "%"}:
        return f"{value:.2f}%"
    if normalized in {"cny", "rmb", "yuan", "元"}:
        absolute = abs(value)
        if absolute >= Decimal("100000000"):
            return f"{value / Decimal('100000000'):.2f}亿元"
        if absolute >= Decimal("10000"):
            return f"{value / Decimal('10000'):.2f}万元"
        return f"{value:.2f}元"
    if normalized in {"shares", "share", "股"}:
        if abs(value) >= Decimal("100000000"):
            return f"{value / Decimal('100000000'):.2f}亿股"
        if abs(value) >= Decimal("10000"):
            return f"{value / Decimal('10000'):.2f}万股"
        return f"{value:.0f}股"
    if normalized == "usd_billion":
        return f"{value.normalize()}十亿美元"
    if normalized in {"count", "个"}:
        return f"{value.normalize()}个"
    if normalized in {"multiple", "times"}:
        return f"{value:.2f}倍"
    rendered = format(value.normalize(), "f")
    return f"{rendered}{unit}" if unit else rendered


def _reader_fact_description(item: dict[str, Any]) -> str:
    metric = str(item.get("metric") or "").strip()
    label = _METRIC_READER_LABELS.get(metric, metric.replace("_", " ") or "已核实数据")
    period = str(item.get("period") or "期间未明").strip()
    return f"{period} · {label}：{_reader_fact_value(item)}"


def _readerize_report_text(
    content: str,
    fact_labels: dict[str, str],
    evidence_labels: dict[str, str],
    facts: dict[str, dict[str, Any]],
) -> str:
    """Convert workspace notation into reader-facing Markdown language."""

    rendered = _FACT_RE.sub(lambda match: f"〔{fact_labels[match.group(1)]}〕", content)
    rendered = _EVIDENCE_RE.sub(lambda match: f"〔{evidence_labels[match.group(1)]}〕", rendered)
    reader_facts = {fact_labels[fact_id]: facts.get(fact_id) or {} for fact_id in fact_labels}

    def replace_money(match: re.Match[str]) -> str:
        fact = reader_facts.get(f"数据{match.group('alias')}") or {}
        unit = str(fact.get("unit") or "").casefold()
        if unit not in {"cny", "rmb", "yuan", "元"}:
            return match.group(0)
        return f"{_reader_fact_value(fact)} {match.group('citation')}"

    def replace_shares(match: re.Match[str]) -> str:
        fact = reader_facts.get(f"数据{match.group('alias')}") or {}
        unit = str(fact.get("unit") or "").casefold()
        if unit not in {"shares", "share", "股"}:
            return match.group(0)
        return f"{_reader_fact_value(fact)} {match.group('citation')}"

    rendered = re.sub(
        r"[¥￥]\s*-?[\d,]+(?:\.\d+)?\s*(?P<citation>〔数据(?P<alias>\d+)〕)",
        replace_money,
        rendered,
    )
    rendered = re.sub(
        r"-?[\d,]+(?:\.\d+)?\s*股\s*(?P<citation>〔数据(?P<alias>\d+)〕)",
        replace_shares,
        rendered,
    )
    rendered = re.sub(r"(?m)^\[data_gap\]\s*", "**证据说明：** ", rendered)
    rendered = rendered.replace("[data_gap]", "（当前证据不足）")
    rendered = rendered.replace("[inference]", "（研究判断）")
    replacements = {
        "insufficient_evidence": "证据不足",
        "not_requested": "本次未执行",
        "implied_terminal_earnings": "市值隐含长期利润反推",
        "validate_terminal_scenarios": "长期经营情景校验",
        "net_income_proxy": "净利润近似口径",
        "确定性分析模块（市值隐含长期利润反推）返回 证据不足：": "目前无法完成市值隐含长期利润反推：",
        "确定性 Ledger": "已核验数据记录",
        "Ledger": "数据记录",
        "data_gap": "证据不足",
        "Evidence": "资料",
        "Fact": "数据",
        "CAGR": "复合年增长率",
        "CFO": "经营现金流",
        "Capex": "资本开支",
        "FCF": "自由现金流",
    }
    for source, target in replacements.items():
        rendered = rendered.replace(source, target)
    # Do not surface a duplicate English implementation explanation after the
    # Chinese conclusion. The structured reason remains in validation.json.
    rendered = re.sub(r"[（(][A-Za-z][A-Za-z0-9 ,./_\-]{40,}[）)]", "", rendered)
    return rendered


def _section_body(content: str, heading: str) -> str:
    match = re.search(rf"^##\s+{re.escape(heading)}\s*$", content, re.M)
    if not match:
        return ""
    rest = content[match.end():]
    next_heading = re.search(r"^##\s+", rest, re.M)
    return rest[: next_heading.start()] if next_heading else rest


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _unit_multiplier(unit: str) -> Decimal:
    normalized = unit.strip().lower().replace(" ", "_")
    if "100million" in normalized or "亿元" in normalized:
        return Decimal("100000000")
    if "million" in normalized or "百万元" in normalized:
        return Decimal("1000000")
    if "万元" in normalized or normalized.endswith("_10000"):
        return Decimal("10000")
    return Decimal("1")


def _same_value(left: Any, right: Any, *, tolerance: Decimal = Decimal("0.01")) -> bool:
    lhs = _decimal(left)
    rhs = _decimal(right)
    if lhs is None or rhs is None:
        return False
    denominator = max(abs(rhs), Decimal("1e-28"))
    return abs(lhs - rhs) / denominator <= tolerance


def _has_unsupported_forecast_provenance(*values: Any) -> bool:
    descriptor = " ".join(str(value or "") for value in values).casefold()
    return any(marker.casefold() in descriptor for marker in _UNSUPPORTED_FORECAST_PROVENANCE_MARKERS)


def _period_year(value: Any) -> int | None:
    match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", str(value or ""))
    return int(match.group(1)) if match else None


def _validation_issue_reader_message(issue: str) -> str:
    """Translate internal gate codes into an actionable sentence for readers."""

    if "timestamped_price_and_market_cap_required" in issue:
        return "缺少同一时点、可核验的最新价格和总市值，无法形成正式结论。"
    if issue.startswith("missing_required_section:"):
        return f"报告章节“{issue.split(':', 1)[1]}”尚未生成完整。"
    if issue.startswith("workspace_section_not_ready:"):
        module_id = issue.split(":", 2)[1]
        return f"“{_MODULE_READER_LABELS.get(module_id, '相关章节')}”尚未通过内容校验。"
    if issue.startswith("unknown_fact_reference:"):
        return "报告引用了一项无法在本次数据快照中核对的数据。"
    if issue.startswith("unknown_evidence_reference:"):
        return "报告引用了一项无法在本次资料库中核对的来源。"
    if issue.startswith("numeric_") or issue == "service_numeric_audit_failed":
        return "报告中的部分数字未能与已核实数据逐项对应。"
    if issue.startswith("uncited_material_numbers:"):
        return "报告中仍有重要数字缺少同段数据依据。"
    if issue == "valuation_direction_without_implied_expectations":
        return "现有证据不足以支持高估或低估判断，相关方向性结论已被拦截。"
    if issue == "target_price_or_reasonable_value_detected":
        return "报告包含未经确定性模型支持的目标价或合理价值表述。"
    if issue.startswith("module_failed_validation:"):
        parts = issue.split(":", 2)
        module_id = parts[1] if len(parts) > 1 else ""
        return f"“{_MODULE_READER_LABELS.get(module_id, '关键数据')}”未达到正式发布要求。"
    if issue in {"quality_status_invariant_failed", "unknown_validation_failure"}:
        return "报告质量状态与校验结果不一致，需要重新生成。"
    return "有一项报告校验未通过；详细技术记录已保留在内部审计文件中。"


def _diagnostic_markdown(record: DeepReportRecord, issues: list[str], error: str | None = None) -> str:
    details = [f"- {_validation_issue_reader_message(issue)}" for issue in issues]
    recovery_error = str(error or "").strip()
    if recovery_error and "用新数据更新" in recovery_error:
        details.append(f"- {recovery_error}")
    elif error and error not in issues:
        details.append("- 报告生成过程中发生技术异常，请稍后重新运行。")
    details = list(dict.fromkeys(details))
    refresh_required = (
        "用新数据更新" in recovery_error
        or any(
            "timestamped_price_and_market_cap_required" in issue
            or issue.startswith("module_failed_validation:symbol_identity:")
            or issue.startswith("module_failed_validation:financial_quality:")
            for issue in issues
        )
    )
    next_action = (
        "\n\n### 建议操作\n\n"
        "当前问题来自基础数据或股票身份校验，单独重写章节无法解决。请点击“用新数据更新”重新获取资料。"
        if refresh_required
        else ""
    )
    return (
        f"# {record.security_name or record.symbol or '单股'}穿透式深度研究诊断\n\n"
        "> **当前状态：尚未形成可发布的正式报告**\n"
        f"> - 股票：{record.symbol or '尚未明确'}\n"
        f"> - 数据更新至：{_reader_datetime(record.data_as_of)}\n"
        "> - 本次只保留诊断结果，不会生成正式 PDF。\n\n"
        "## 为什么没有发布正式报告\n\n"
        "本次研究已完成运行，但关键数据或报告内容没有通过发布前校验。系统因此没有给出投资结论。\n\n"
        "### 需要处理的问题\n\n"
        + ("\n".join(details) if details else "- 报告质量状态与校验结果不一致，需要重新生成。")
        + next_action
        + "\n"
    )


_HARD_VALIDATION_MODULE_IDS = {
    "report_gate", "market_data", "symbol_identity", "financial_quality",
}


def _ensure_failed_validation_issues(validation: dict[str, Any]) -> None:
    """Make every failed quality result explain which deterministic gate failed."""

    if validation.get("quality_status") != "failed_validation":
        return
    issues = [str(value) for value in (validation.get("issues") or []) if str(value)]
    modules = dict(validation.get("analysis_modules") or {})
    for module_id, payload in modules.items():
        module = dict(payload or {})
        if str(module.get("status") or "") != "failed_validation":
            continue
        reason = str(module.get("reason") or "unspecified_module_failure").replace("\n", " ")
        issues.append(f"module_failed_validation:{module_id}:{reason}")
    if not issues:
        issues.append("quality_status_invariant_failed")
    validation["issues"] = list(dict.fromkeys(issues))


def _normalized_heading(value: str) -> str:
    value = re.sub(r"^[一二三四五六七八九十\d]+[、.．]\s*", "", value.strip())
    return re.sub(r"\s+", "", value)


def _display_decimals(raw_value: str) -> int:
    normalized = raw_value.replace(",", "")
    return len(normalized.rsplit(".", 1)[1]) if "." in normalized else 0


def _fact_display_value(fact: dict[str, Any], display_unit: str) -> Decimal | None:
    value = _decimal(fact.get("value"))
    if value is None:
        return None
    fact_unit = str(fact.get("unit") or "").strip().lower()
    unit = display_unit.strip().lower()
    if unit in {"%", "％"}:
        if fact_unit in {"ratio", "decimal"}:
            return value * Decimal("100")
        return value
    if unit in {"亿元", "亿"}:
        return value * _unit_multiplier(fact_unit) / Decimal("100000000")
    if unit in {"万元", "万"}:
        return value * _unit_multiplier(fact_unit) / Decimal("10000")
    if unit in {"million"}:
        return value * _unit_multiplier(fact_unit) / Decimal("1000000")
    if unit in {"billion"}:
        return value * _unit_multiplier(fact_unit) / Decimal("1000000000")
    return value * _unit_multiplier(fact_unit)


def _display_matches_fact(raw_value: str, display_unit: str, fact: dict[str, Any]) -> bool:
    reported = _decimal(raw_value.replace(",", ""))
    expected = _fact_display_value(fact, display_unit)
    if reported is None or expected is None:
        return False
    decimals = _display_decimals(raw_value)
    tolerance = Decimal("0.5") * (Decimal("10") ** -decimals)
    tolerance += max(abs(expected), Decimal("1")) * Decimal("0.00000001")
    return abs(reported - expected) <= tolerance


def _line_material_numbers(line: str) -> list[re.Match[str]]:
    matches: list[re.Match[str]] = []
    for match in _NUMBER_CAPTURE_RE.finditer(line):
        prefix = match.group("prefix") or ""
        unit = match.group("unit") or ""
        if not prefix and not unit:
            continue
        matches.append(match)
    return matches


class DeepReportService:
    """Own the report state machine independently from the LLM attempt."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @staticmethod
    def _knowledge_store():
        try:
            from src.research import get_research_knowledge_store, knowledge_enabled

            return get_research_knowledge_store() if knowledge_enabled() else None
        except Exception:
            return None

    def _prior_report_for(self, record: DeepReportRecord) -> DeepReportRecord | None:
        if record.parent_report_id:
            parent = self.get(record.parent_report_id)
            if parent is not None and parent.quality_status != "failed_validation":
                return parent
        for candidate in self.list(limit=500):
            if candidate.report_id == record.report_id:
                continue
            if (
                candidate.symbol.upper() == record.symbol.upper()
                and candidate.status == "completed"
                and candidate.quality_status != "failed_validation"
            ):
                return candidate
        return None

    def _refresh_knowledge_state(self, record: DeepReportRecord) -> None:
        store = self._knowledge_store()
        if store is None or not record.symbol:
            return
        prior = self._prior_report_for(record)
        analysis = self._analysis_context(record.report_id)
        facts = list(analysis.get("facts") or [])
        evidence = list(analysis.get("evidence") or [])
        if not record.research_coverage:
            record.research_coverage = store.create_coverage_plan(
                symbol=record.symbol,
                profile=record.profile,
                as_of=record.data_as_of or record.updated_at,
                report_id=record.report_id,
                prior_report_id=prior.report_id if prior else None,
            )
        domain_counts: dict[str, int] = {}
        for item in evidence:
            domain = str(item.get("domain") or "other")
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
        for domain in record.research_coverage.get("domains") or []:
            domain_id = str(domain.get("domain") or "")
            if domain_id in {"identity_market", "financial_statements"} and facts:
                domain["status"] = "covered"
            elif domain_id == "industry_tam_competition" and sum(
                domain_counts.get(key, 0) for key in ("industry", "tam", "competition")
            ) >= int(domain.get("minimum_independent_sources") or 1):
                domain["status"] = "covered"
            elif domain_counts.get(domain_id, 0):
                domain["status"] = "covered"
        record.history_delta = store.preview_delta(
            facts,
            base_report_id=prior.report_id if prior else None,
        )
        record.research_coverage["reused_fact_count"] = len(record.history_delta.get("confirmed") or [])
        record.research_coverage["refreshed_fact_count"] = (
            len(record.history_delta.get("added") or [])
            + len(record.history_delta.get("updated") or [])
        )

    def _dir(self, report_id: str) -> Path:
        if not re.fullmatch(r"report_[a-f0-9]{16}", report_id):
            raise ValueError("invalid report_id")
        return self.base_dir / report_id

    def _manifest_path(self, report_id: str) -> Path:
        return self._dir(report_id) / "manifest.json"

    def _workspace_dir(self, report_id: str) -> Path:
        return self._dir(report_id) / "workspace"

    def _workspace_manifest_path(self, report_id: str) -> Path:
        return self._workspace_dir(report_id) / "workspace.json"

    def _section_path(self, report_id: str, section_id: str) -> Path:
        if section_id not in _SECTION_IDS:
            raise ValueError(f"unknown report section: {section_id}")
        return self._workspace_dir(report_id) / "sections" / f"{section_id}.json"

    def _read_section(self, report_id: str, section_id: str) -> ReportSection | None:
        path = self._section_path(report_id, section_id)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return ReportSection.from_dict(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _write_section(self, report_id: str, section: ReportSection) -> None:
        _atomic_json(self._section_path(report_id, section.section_id), section.to_dict())

    def _write_workspace_manifest(self, record: DeepReportRecord) -> None:
        sections = {}
        for section_id in _SECTION_IDS:
            section = self._read_section(record.report_id, section_id)
            sections[section_id] = {
                "status": section.status if section else "missing",
                "content_hash": section.content_hash if section else None,
                "source_report_id": section.source_report_id if section else None,
                "issues": section.validation_issues if section else ["workspace_missing_section"],
            }
        _atomic_json(
            self._workspace_manifest_path(record.report_id),
            {
                "schema_version": 1,
                "report_id": record.report_id,
                "parent_report_id": record.parent_report_id,
                "revision": record.revision,
                "revision_mode": record.revision_mode,
                "revision_sections": record.revision_sections,
                "pipeline_state": record.pipeline_state,
                "sections": sections,
                "updated_at": utc_now(),
            },
        )

    def _legacy_sections(self, report_id: str) -> dict[str, str]:
        report_dir = self._dir(report_id)
        source_path = report_dir / "rejected_draft.md"
        if not source_path.exists():
            source_path = report_dir / "report.md"
        if not source_path.exists():
            return {}
        content = source_path.read_text(encoding="utf-8")
        matches = list(re.finditer(r"^##\s+(.+?)\s*$", content, re.M))
        collected: dict[str, list[str]] = {}
        for index, match in enumerate(matches):
            heading = _normalized_heading(match.group(1))
            section_id = _SECTION_ALIASES.get(heading)
            if not section_id:
                continue
            end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
            body = content[match.end():end].strip()
            if body:
                collected.setdefault(section_id, []).append(body)
        return {key: "\n\n".join(values) for key, values in collected.items()}

    def _copy_parent_workspace(
        self,
        record: DeepReportRecord,
        parent: DeepReportRecord,
    ) -> None:
        parent_sections = {
            section_id: self._read_section(parent.report_id, section_id)
            for section_id in _SECTION_IDS
        }
        if not any(parent_sections.values()):
            legacy = self._legacy_sections(parent.report_id)
            parent_sections = {
                section_id: (
                    ReportSection(
                        section_id=section_id,
                        body_markdown=body,
                        source_report_id=parent.report_id,
                        source_revision=parent.revision,
                        content_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                        fact_ids=sorted(set(_FACT_RE.findall(body))),
                        evidence_ids=sorted(set(_EVIDENCE_RE.findall(body))),
                        status="stale",
                        validation_issues=["legacy_section_requires_revalidation"],
                    )
                    if (body := legacy.get(section_id))
                    else None
                )
                for section_id in _SECTION_IDS
            }

        selected = set(record.revision_sections)
        for section_id, source in parent_sections.items():
            if source is None:
                continue
            should_stale = (
                record.revision_mode == "full_refresh"
                or (
                    record.revision_mode == "repair"
                    and source.status != "passed"
                )
                or section_id in selected
                or source.status != "passed"
            )
            copied = ReportSection(
                section_id=section_id,
                body_markdown=source.body_markdown,
                source_report_id=parent.report_id,
                source_revision=parent.revision,
                content_hash=source.content_hash,
                fact_ids=list(source.fact_ids),
                evidence_ids=list(source.evidence_ids),
                status="stale" if should_stale else "passed",
                validation_issues=(
                    ["section_requires_refresh"] if should_stale else []
                ),
            )
            self._write_section(record.report_id, copied)

    def begin(
        self,
        *,
        session_id: str,
        attempt_id: str,
        request_content: str,
        profile: str = "equity_deep_research",
        parent_report_id: str | None = None,
        generation_source: str | None = None,
        generation_reason: str | None = None,
        revision_mode: str = "initial",
        revision_sections: list[str] | None = None,
    ) -> DeepReportRecord:
        if profile != "equity_deep_research":
            raise ValueError(f"unsupported report profile: {profile}")
        with self._lock:
            existing = self.find_by_attempt(session_id, attempt_id)
            if existing is not None:
                return existing
            revision = 1
            parent: DeepReportRecord | None = None
            if parent_report_id:
                parent = self.get(parent_report_id)
                if parent is None:
                    raise ValueError(f"parent report not found: {parent_report_id}")
                revision = parent.revision + 1
            if revision_mode not in {"initial", "full_refresh", "section_revision", "repair"}:
                raise ValueError(f"unsupported revision mode: {revision_mode}")
            requested_sections = list(dict.fromkeys(revision_sections or []))
            invalid_sections = sorted(set(requested_sections) - _SECTION_IDS)
            if invalid_sections:
                raise ValueError(f"unknown revision sections: {', '.join(invalid_sections)}")
            record = DeepReportRecord(
                session_id=session_id,
                attempt_id=attempt_id,
                profile=profile,
                request_content=request_content,
                report_date=datetime.now(_SHANGHAI).date().isoformat(),
                symbol=parent.symbol if parent is not None else "",
                security_name=parent.security_name if parent is not None else "",
                data_as_of=parent.data_as_of if parent is not None else "",
                parent_report_id=parent_report_id,
                revision=revision,
                generation_source=(
                    generation_source
                    or (parent.generation_source if parent is not None else "manual")
                ),
                generation_reason=(
                    generation_reason
                    if generation_reason is not None
                    else (parent.generation_reason if parent is not None else "")
                ),
                revision_mode=revision_mode,  # type: ignore[arg-type]
                revision_sections=requested_sections,
                analysis_modules=(
                    {
                        key: ModuleResult(**asdict(value))
                        for key, value in parent.analysis_modules.items()
                    }
                    if parent is not None
                    else {
                        key: ModuleResult(status="pending")
                        for key, _ in EQUITY_DEEP_RESEARCH_PROFILE["required_sections"]
                    }
                ),
            )
            record.latest_revision_id = record.report_id
            report_dir = self._dir(record.report_id)
            report_dir.mkdir(parents=True, exist_ok=False)
            if parent is not None:
                parent_analysis = self._dir(parent.report_id) / "analysis"
                if parent_analysis.exists() and revision_mode != "full_refresh":
                    shutil.copytree(parent_analysis, report_dir / "analysis")
                self._copy_parent_workspace(record, parent)
                parent.latest_revision_id = record.report_id
                parent.updated_at = utc_now()
                self._write_manifest(parent)
            self._write_workspace_manifest(record)
            if record.symbol:
                self._refresh_knowledge_state(record)
            self._write_manifest(record)
            return record

    @staticmethod
    def _module_result(raw: Any) -> ModuleResult:
        payload = dict(raw) if isinstance(raw, dict) else {"status": str(raw or "pending")}
        status = str(payload.get("status") or "pending")
        status = {
            "pass": "passed",
            "fail": "failed_validation",
            "insufficient_data": "insufficient_evidence",
            "not_comparable": "warning",
        }.get(status, status)
        allowed = {
            "pending", "running", "passed", "warning", "failed_validation",
            "insufficient_evidence", "not_requested",
        }
        if status not in allowed:
            status = "warning"
        coverage = payload.get("coverage")
        if coverage is None:
            coverage = payload.get("coverage_ratio")
        details = dict(payload.get("details") or {})
        details.update({
            key: value for key, value in payload.items()
            if key not in {"status", "coverage", "coverage_ratio", "reason", "details"}
        })
        return ModuleResult(
            status=status,  # type: ignore[arg-type]
            coverage=float(coverage) if isinstance(coverage, (int, float)) else None,
            reason=str(payload.get("reason")) if payload.get("reason") else None,
            details=details,
        )

    def attach_analysis(self, report_id: str, analysis: dict[str, Any]) -> DeepReportRecord:
        """Persist the normalized financial snapshot and its complete ledgers."""

        with self._lock:
            record = self.require(report_id)
            if analysis.get("profile") not in {None, "equity_deep_research"}:
                raise ValueError("analysis profile does not match report profile")
            if analysis.get("status") not in {None, "ok"}:
                raise ValueError("cannot attach an unsuccessful financial analysis")

            analysis_dir = self._dir(report_id) / "analysis"
            snapshot = dict(analysis.get("snapshot") or {})
            facts = [dict(item) for item in (analysis.get("facts") or []) if isinstance(item, dict)]
            evidence = [dict(item) for item in (analysis.get("evidence") or []) if isinstance(item, dict)]
            if not snapshot or not facts or not evidence:
                raise ValueError("financial analysis must contain snapshot, facts, and evidence")

            # A newly attached snapshot starts a new evidence/calculation generation.
            # Parent report artifacts may have been copied for revision reuse, but an
            # audit receipt or deterministic result bound to the old snapshot must
            # never survive a full data refresh.
            shutil.rmtree(analysis_dir / "deterministic", ignore_errors=True)
            (analysis_dir / "report_audit.json").unlink(missing_ok=True)

            module_statuses = dict(analysis.get("module_statuses") or {})
            implied_expectations = dict(analysis.get("implied_expectations") or {})
            quality_status = analysis.get("quality_status")
            if implied_expectations.get("applicability") == "applicable":
                facts_by_id = {
                    str(item.get("fact_id")): item for item in facts if item.get("fact_id")
                }
                evidence_by_id = {
                    str(item.get("evidence_id")): item
                    for item in evidence
                    if item.get("evidence_id")
                }
                try:
                    self._validate_implied_expectations_lineage(
                        implied_expectations,
                        facts_by_id,
                        evidence_by_id,
                    )
                except (TypeError, ValueError) as exc:
                    rejected_fact_ids = {
                        str(value) for value in (implied_expectations.get("result_fact_ids") or [])
                    }
                    facts = [
                        item for item in facts
                        if str(item.get("fact_id") or "") not in rejected_fact_ids
                        and str(item.get("metric") or "") != "implied_terminal_earnings"
                    ]
                    reason = f"lineage_validation_failed: {exc}"
                    implied_expectations = {
                        "applicability": "not_applicable",
                        "reason": reason,
                    }
                    module_statuses["implied_expectations"] = {
                        "status": "insufficient_evidence",
                        "reason": reason,
                    }
                    if quality_status == "passed":
                        quality_status = "passed_with_gaps"

            _atomic_json(analysis_dir / "snapshot.json", snapshot)
            _atomic_jsonl(analysis_dir / "facts.jsonl", facts)
            _atomic_jsonl(analysis_dir / "evidence.jsonl", evidence)
            _atomic_json(analysis_dir / "reconciliations.json", analysis.get("reconciliations") or [])
            _atomic_json(analysis_dir / "alerts.json", analysis.get("alerts") or [])

            module_statuses.setdefault("financial_quality", analysis.get("financial_gate") or {})
            module_statuses.setdefault("latest_quarter", analysis.get("latest_quarter") or {})
            index = {
                "profile": "equity_deep_research",
                "symbol": analysis.get("symbol") or snapshot.get("symbol"),
                "security_name": analysis.get("security_name") or snapshot.get("security_name"),
                "data_as_of": analysis.get("data_as_of") or snapshot.get("data_as_of"),
                "quality_status": quality_status,
                "financial_gate": analysis.get("financial_gate") or {},
                "latest_quarter": analysis.get("latest_quarter") or {},
                "module_statuses": module_statuses,
                "source_statuses": analysis.get("source_statuses") or {},
                "research_status": analysis.get("research_status") or {},
                "implied_expectations": implied_expectations,
                "fact_count": len(facts),
                "evidence_count": len(evidence),
                "attached_at": utc_now(),
            }
            _atomic_json(analysis_dir / "index.json", index)

            record.symbol = str(index.get("symbol") or record.symbol).upper()
            record.security_name = str(index.get("security_name") or record.security_name)
            record.data_as_of = str(index.get("data_as_of") or record.data_as_of)
            raw_quality = str(index.get("quality_status") or "")
            if raw_quality in {"passed", "passed_with_gaps", "failed_validation"}:
                record.quality_status = raw_quality  # type: ignore[assignment]
            for key, raw in dict(index["module_statuses"]).items():
                record.analysis_modules[str(key)] = self._module_result(raw)
            self._refresh_knowledge_state(record)
            record.updated_at = utc_now()
            self._write_manifest(record)
            return record

    def attach_deterministic_result(
        self,
        report_id: str,
        command: str,
        result: dict[str, Any],
    ) -> DeepReportRecord:
        """Attach a deterministic post-snapshot calculation to the report ledger."""

        with self._lock:
            record = self.require(report_id)
            analysis_dir = self._dir(report_id) / "analysis"
            if not (analysis_dir / "snapshot.json").exists():
                raise ValueError("attach the financial snapshot before deterministic results")
            if command not in _DETERMINISTIC_COMMANDS:
                raise ValueError(f"deterministic command is not allowed for {record.profile}: {command}")

            facts_path = analysis_dir / "facts.jsonl"
            evidence_path = analysis_dir / "evidence.jsonl"
            by_id = {
                str(item.get("fact_id")): item
                for item in _read_jsonl(facts_path)
                if item.get("fact_id")
            }
            evidence_by_id = {
                str(item.get("evidence_id")): item
                for item in _read_jsonl(evidence_path)
                if item.get("evidence_id")
            }
            if command == "implied_terminal_earnings" and result.get("applicability") == "applicable":
                self._validate_implied_expectations_lineage(result, by_id, evidence_by_id)
            if command == "validate_terminal_scenarios":
                self._validate_terminal_scenario_lineage(result, by_id, evidence_by_id)
                implied_path = analysis_dir / "deterministic" / "implied_terminal_earnings.json"
                if implied_path.exists():
                    implied = json.loads(implied_path.read_text(encoding="utf-8"))
                    implied_year = implied.get("derived_steady_year")
                    scenario_year = result.get("steady_year")
                    if implied_year is not None and scenario_year != implied_year:
                        raise ValueError("terminal scenarios and implied expectations must use the same steady year")

            safe_command = _safe_component(command, "calculation")
            _atomic_json(analysis_dir / "deterministic" / f"{safe_command}.json", result)

            derived = [dict(item) for item in (result.get("derived_facts") or []) if isinstance(item, dict)]
            if derived:
                known_fact_ids = set(by_id)
                if (
                    command == "validate_terminal_scenarios"
                    and result.get("currency")
                    and result.get("tam_currency")
                    and result.get("currency") != result.get("tam_currency")
                ):
                    for scenario in result.get("scenarios") or []:
                        fx_fact_id = str((scenario or {}).get("fx_fact_id") or "")
                        fx_fact = by_id.get(fx_fact_id)
                        metric = str((fx_fact or {}).get("metric") or "").lower()
                        if not fx_fact or not any(token in metric for token in ("fx", "exchange_rate", "汇率")):
                            raise ValueError("cross-currency scenarios require a registered FX-rate Fact")
                for item in derived:
                    fact_id = str(item.get("fact_id") or "")
                    if fact_id:
                        inputs = {str(value) for value in (item.get("input_fact_ids") or [])}
                        if not inputs or not inputs.issubset(known_fact_ids):
                            raise ValueError("derived facts must reference existing input Fact IDs")
                        by_id[fact_id] = item
                _atomic_jsonl(facts_path, by_id.values())

            index_path = analysis_dir / "index.json"
            index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
            modules = dict(index.get("module_statuses") or {})
            if command == "validate_terminal_scenarios":
                passed = result.get("validation_status") == "pass"
                modules["terminal_scenarios"] = {
                    "status": "passed" if passed else "failed_validation",
                    "reason": None if passed else "terminal scenario validation failed",
                }
            elif command == "implied_terminal_earnings":
                applicable = result.get("applicability") == "applicable"
                modules["implied_expectations"] = {
                    "status": "passed" if applicable else "insufficient_evidence",
                    "reason": result.get("reason"),
                }
            index["module_statuses"] = modules
            index["fact_count"] = len(_read_jsonl(analysis_dir / "facts.jsonl"))
            index["updated_at"] = utc_now()
            _atomic_json(index_path, index)
            for key, raw in modules.items():
                record.analysis_modules[str(key)] = self._module_result(raw)
            self._refresh_knowledge_state(record)
            record.updated_at = utc_now()
            self._write_manifest(record)
            return record

    @staticmethod
    def _validate_implied_expectations_lineage(
        result: dict[str, Any],
        facts_by_id: dict[str, dict[str, Any]],
        evidence_by_id: dict[str, dict[str, Any]],
    ) -> None:
        """Fail closed unless market cap and E1-E3 replay from registered facts."""

        currency = str(result.get("currency") or "").strip().upper()
        years = [int(value) for value in (result.get("forecast_years") or [])]
        earnings = list(result.get("forecast_earnings_exact") or [])
        base_year = result.get("base_year")
        source_ids = [str(value) for value in (result.get("source_fact_ids") or [])]
        if not currency:
            raise ValueError("implied expectations require an explicit currency")
        if len(years) != 3 or len(earnings) != 3 or any(
            right != left + 1 for left, right in zip(years, years[1:])
        ):
            raise ValueError("implied expectations require three consecutive forecast years")
        if base_year is None or years[0] != int(base_year) + 1:
            raise ValueError("implied expectations forecast years must follow the latest actual year")
        if not source_ids or any(fact_id not in facts_by_id for fact_id in source_ids):
            raise ValueError("implied expectations must reference registered input Facts")

        source_facts = [facts_by_id[fact_id] for fact_id in source_ids]
        market_facts = [
            fact for fact in source_facts
            if (
                str(fact.get("metric") or "").strip().lower() == "market_cap"
                or str(fact.get("metric") or "").strip().lower().startswith("market_cap_")
            )
        ]
        if not market_facts:
            raise ValueError("implied expectations source_fact_ids must include the timestamped market-cap Fact")
        market_fact = market_facts[0]
        market_value = _decimal(market_fact.get("value"))
        if market_value is None or not _same_value(
            market_value * _unit_multiplier(str(market_fact.get("unit") or "")),
            result.get("market_cap_exact"),
        ):
            raise ValueError("implied expectations market cap does not replay from its input Fact")
        if currency not in str(market_fact.get("unit") or "").upper():
            raise ValueError("implied expectations market-cap currency is inconsistent")

        shares_facts = [
            fact for fact in source_facts
            if str(fact.get("metric") or "").lower() in {"total_shares", "total_shares_market"}
        ]
        shares = _decimal(shares_facts[0].get("value")) if shares_facts else None
        if shares is not None:
            shares *= _unit_multiplier(str(shares_facts[0].get("unit") or ""))

        for year, expected in zip(years, earnings):
            candidates = [
                fact for fact in source_facts
                if _period_year(fact.get("period")) == year
                and any(
                    token in str(fact.get("metric") or "").lower()
                    for token in ("consensus", "forecast", "estimate", "net_profit", "earnings", "eps")
                )
            ]
            if not candidates:
                raise ValueError(f"missing registered forecast Fact for {year}")
            matched = False
            unsupported_provenance = False
            for fact in candidates:
                fact_metadata = dict(fact.get("metadata") or {})
                if _has_unsupported_forecast_provenance(
                    fact.get("metric"),
                    fact_metadata.get("scope"),
                    fact_metadata.get("forecast_kind"),
                    fact_metadata.get("provenance"),
                ):
                    unsupported_provenance = True
                    continue
                raw = _decimal(fact.get("value"))
                unit = str(fact.get("unit") or "")
                metric = str(fact.get("metric") or "").lower()
                if raw is None or currency not in unit.upper():
                    continue
                replayed = raw * _unit_multiplier(unit)
                if "eps" in metric or "/share" in unit.lower():
                    if shares is None:
                        continue
                    replayed *= shares
                if not _same_value(replayed, expected):
                    continue
                linked_evidence = [
                    evidence_by_id.get(str(evidence_id))
                    for evidence_id in (fact.get("evidence_ids") or [])
                ]
                linked_evidence = [item for item in linked_evidence if item]
                if not linked_evidence:
                    continue
                valid_coverage = False
                for evidence in linked_evidence:
                    metadata = dict(evidence.get("metadata") or {})
                    coverage = metadata.get("coverage_count")
                    forecast_kind = str(metadata.get("forecast_kind") or "").strip().casefold()
                    if _has_unsupported_forecast_provenance(
                        evidence.get("source"),
                        evidence.get("source_locator"),
                        evidence.get("summary"),
                        metadata.get("provenance"),
                    ):
                        unsupported_provenance = True
                        continue
                    coverage_matches_kind = (
                        (forecast_kind == "single_broker" and coverage == 1)
                        or (
                            forecast_kind == "consensus"
                            and isinstance(coverage, int)
                            and coverage >= 2
                        )
                    )
                    if (
                        str(evidence.get("domain") or "") == "consensus"
                        and evidence.get("retrieved_at")
                        and evidence.get("source")
                        and evidence.get("source_locator")
                        and coverage_matches_kind
                    ):
                        valid_coverage = True
                        break
                if valid_coverage:
                    matched = True
                    break
            if not matched:
                if unsupported_provenance:
                    raise ValueError(
                        f"forecast Fact for {year} must come from a timestamped consensus or "
                        "broker forecast; internal estimates and extrapolations are not allowed"
                    )
                raise ValueError(
                    f"forecast Fact for {year} must replay the input and include identifiable "
                    "coverage consistent with forecast_kind"
                )

    @staticmethod
    def _validate_terminal_scenario_lineage(
        result: dict[str, Any],
        facts_by_id: dict[str, dict[str, Any]],
        evidence_by_id: dict[str, dict[str, Any]],
    ) -> None:
        if result.get("validation_status") != "pass":
            raise ValueError("terminal scenarios must pass deterministic validation before attachment")
        scenarios = list(result.get("scenarios") or [])
        if len(scenarios) != 4:
            raise ValueError("terminal scenarios require exactly four unweighted scenarios")
        for scenario in scenarios:
            source_ids = {str(value) for value in (scenario.get("source_fact_ids") or [])}
            if not source_ids or not source_ids.issubset(facts_by_id):
                raise ValueError("terminal scenario inputs must reference registered Facts")
            facts = [facts_by_id[fact_id] for fact_id in source_ids]
            metrics = [str(fact.get("metric") or "").lower() for fact in facts]
            required = {
                "tam": any("tam" in metric or "市场规模" in metric for metric in metrics),
                "share": any("share" in metric or "份额" in metric for metric in metrics),
                "margin": any("margin" in metric or "利润率" in metric for metric in metrics),
            }
            if not all(required.values()):
                raise ValueError("each terminal scenario must cite distinct TAM, market-share, and margin Facts")
            tam_facts = [
                fact for fact, metric in zip(facts, metrics)
                if "tam" in metric or "市场规模" in metric
            ]
            share_facts = [
                fact for fact, metric in zip(facts, metrics)
                if "share" in metric or "份额" in metric
            ]
            margin_facts = [
                fact for fact, metric in zip(facts, metrics)
                if "margin" in metric or "利润率" in metric
            ]
            tam_value = _decimal(tam_facts[0].get("value"))
            share_value = _decimal(share_facts[0].get("value"))
            margin_value = _decimal(margin_facts[0].get("value"))
            if tam_value is None or share_value is None or margin_value is None:
                raise ValueError("terminal scenario source Facts must contain numeric inputs")
            tam_value *= _unit_multiplier(str(tam_facts[0].get("unit") or ""))
            if "percent" in str(share_facts[0].get("unit") or "").lower() or "%" in str(share_facts[0].get("unit") or ""):
                share_value /= Decimal("100")
            if "percent" in str(margin_facts[0].get("unit") or "").lower() or "%" in str(margin_facts[0].get("unit") or ""):
                margin_value /= Decimal("100")
            if not _same_value(tam_value, scenario.get("tam_exact")):
                raise ValueError("terminal scenario TAM does not replay from its input Fact")
            if not _same_value(share_value, scenario.get("market_share_exact")):
                raise ValueError("terminal scenario market share does not replay from its input Fact")
            if not _same_value(margin_value, scenario.get("net_margin_exact")):
                raise ValueError("terminal scenario net margin does not replay from its input Fact")
            for fact in facts:
                if not fact.get("period") or not fact.get("unit"):
                    raise ValueError("terminal scenario Facts require period and unit")
                linked = [
                    evidence_by_id.get(str(evidence_id))
                    for evidence_id in (fact.get("evidence_ids") or [])
                ]
                if not any(item and item.get("source_locator") and item.get("retrieved_at") for item in linked):
                    raise ValueError("terminal scenario Facts require timestamped source Evidence")

    def attach_external_evidence(
        self,
        report_id: str,
        bundle: dict[str, Any],
    ) -> DeepReportRecord:
        """Merge opened-source evidence and extracted raw facts into the ledger."""

        with self._lock:
            record = self.require(report_id)
            analysis_dir = self._dir(report_id) / "analysis"
            if not (analysis_dir / "snapshot.json").exists():
                raise ValueError("attach the financial snapshot before external evidence")
            new_evidence = [
                dict(item) for item in (bundle.get("evidence") or [])
                if isinstance(item, dict) and item.get("evidence_id")
            ]
            new_facts = [
                dict(item) for item in (bundle.get("facts") or [])
                if isinstance(item, dict) and item.get("fact_id")
            ]
            if not new_evidence or not new_facts:
                raise ValueError("external evidence bundle must include evidence and facts")

            evidence_path = analysis_dir / "evidence.jsonl"
            evidence_by_id = {
                str(item["evidence_id"]): item
                for item in _read_jsonl(evidence_path)
                if item.get("evidence_id")
            }
            for item in new_evidence:
                evidence_by_id[str(item["evidence_id"])] = item

            known_evidence_ids = set(evidence_by_id)
            facts_path = analysis_dir / "facts.jsonl"
            facts_by_id = {
                str(item["fact_id"]): item
                for item in _read_jsonl(facts_path)
                if item.get("fact_id")
            }
            for item in new_facts:
                linked = {str(value) for value in (item.get("evidence_ids") or [])}
                if not linked or not linked.issubset(known_evidence_ids):
                    raise ValueError("every external fact must reference registered evidence")
                facts_by_id[str(item["fact_id"])] = item
            _atomic_jsonl(evidence_path, evidence_by_id.values())
            _atomic_jsonl(facts_path, facts_by_id.values())

            index_path = analysis_dir / "index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index["fact_count"] = len(facts_by_id)
            index["evidence_count"] = len(evidence_by_id)
            index["updated_at"] = utc_now()
            _atomic_json(index_path, index)
            self._refresh_knowledge_state(record)
            record.updated_at = utc_now()
            self._write_manifest(record)
            return record

    def attach_audit_result(self, report_id: str, result: dict[str, Any]) -> DeepReportRecord:
        """Persist a complete numeric audit receipt for the exact final draft."""

        with self._lock:
            record = self.require(report_id)
            if (
                result.get("audit_status") != "complete"
                or result.get("verdict") != "PASS"
                or result.get("content_binding_verified") is not True
                or not result.get("report_sha256")
                or int(result.get("expected_sample_size") or 0) <= 0
                or int(result.get("total") or 0) != int(result.get("expected_sample_size") or 0)
            ):
                raise ValueError("numeric audit must pass every sample and bind the exact report content")
            analysis_dir = self._dir(report_id) / "analysis"
            _atomic_json(analysis_dir / "report_audit.json", result)
            record.updated_at = utc_now()
            self._write_manifest(record)
            return record

    def _analysis_context(self, report_id: str) -> dict[str, Any]:
        analysis_dir = self._dir(report_id) / "analysis"
        index_path = analysis_dir / "index.json"
        index: dict[str, Any] = {}
        if index_path.exists():
            try:
                parsed = json.loads(index_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    index = parsed
            except (OSError, json.JSONDecodeError):
                index = {}
        facts = _read_jsonl(analysis_dir / "facts.jsonl")
        evidence = _read_jsonl(analysis_dir / "evidence.jsonl")
        audit: dict[str, Any] = {}
        audit_path = analysis_dir / "report_audit.json"
        if audit_path.exists():
            try:
                parsed_audit = json.loads(audit_path.read_text(encoding="utf-8"))
                if isinstance(parsed_audit, dict):
                    audit = parsed_audit
            except (OSError, json.JSONDecodeError):
                audit = {}
        return {
            "available": bool(index and (analysis_dir / "snapshot.json").exists()),
            "index": index,
            "facts": facts,
            "evidence": evidence,
            "fact_ids": {str(item.get("fact_id")) for item in facts if item.get("fact_id")},
            "evidence_ids": {str(item.get("evidence_id")) for item in evidence if item.get("evidence_id")},
            "audit": audit,
        }

    def inspect_workspace(
        self,
        report_id: str,
        *,
        section_ids: list[str] | None = None,
        fact_metrics: list[str] | None = None,
        evidence_domains: list[str] | None = None,
        include_module_statuses: bool = True,
        include_section_bodies: bool | None = None,
    ) -> dict[str, Any]:
        """Return a bounded, active-revision view for the report-writing Agent."""

        record = self.require(report_id)
        requested_sections = section_ids or list(_SECTION_HEADINGS)
        invalid = sorted(set(requested_sections) - _SECTION_IDS)
        if invalid:
            raise ValueError(f"unknown report sections: {', '.join(invalid)}")
        context = self._analysis_context(report_id)
        metric_filters = [value.strip().casefold() for value in (fact_metrics or []) if value.strip()]
        domain_filters = {value.strip().casefold() for value in (evidence_domains or []) if value.strip()}
        facts = [
            item for item in context["facts"]
            if not metric_filters
            or any(value in str(item.get("metric") or "").casefold() for value in metric_filters)
        ][:240]
        evidence = [
            item for item in context["evidence"]
            if not domain_filters
            or str(item.get("domain") or "").casefold() in domain_filters
        ][:120]
        if include_section_bodies is None:
            include_section_bodies = record.revision_mode in {"repair", "section_revision"}
        sections = {}
        for section_id in requested_sections:
            section = self._read_section(report_id, section_id)
            section_payload = section.to_dict() if section else {
                "section_id": section_id,
                "status": "missing",
                "body_markdown": "",
                "validation_issues": ["workspace_missing_section"],
            }
            section_is_parent_copy = (
                record.revision_mode == "full_refresh"
                and (
                    section_payload.get("source_report_id") != record.report_id
                    or section_payload.get("status") == "stale"
                )
            )
            if not include_section_bodies or section_is_parent_copy:
                body = str(section_payload.pop("body_markdown", ""))
                section_payload["body_available"] = bool(body)
                section_payload["body_char_count"] = len(body)
                if section_is_parent_copy:
                    fact_ids = list(section_payload.pop("fact_ids", []) or [])
                    evidence_ids = list(section_payload.pop("evidence_ids", []) or [])
                    section_payload["fact_ref_count"] = len(fact_ids)
                    section_payload["evidence_ref_count"] = len(evidence_ids)
                    section_payload["body_blocked_reason"] = (
                        "parent_section_unavailable_in_full_refresh"
                    )
            sections[section_id] = section_payload
        payload: dict[str, Any] = {
            "report_id": record.report_id,
            "parent_report_id": record.parent_report_id,
            "revision": record.revision,
            "revision_mode": record.revision_mode,
            "revision_sections": record.revision_sections,
            "symbol": record.symbol,
            "security_name": record.security_name,
            "data_as_of": record.data_as_of,
            "sections": sections,
            "facts": facts,
            "evidence": evidence,
            "fact_catalog": sorted({str(item.get("metric") or "") for item in context["facts"]}),
            "evidence_domains": sorted({str(item.get("domain") or "") for item in context["evidence"]}),
            "analysis_available": context["available"],
        }
        if include_module_statuses:
            payload["analysis_modules"] = {
                key: asdict(value) for key, value in record.analysis_modules.items()
            }
        return payload

    def _validate_section_body(
        self,
        report_id: str,
        section_id: str,
        body_markdown: str,
    ) -> tuple[list[str], dict[str, Any]]:
        context = self._analysis_context(report_id)
        facts_by_id = {
            str(item.get("fact_id")): item
            for item in context["facts"]
            if item.get("fact_id")
        }
        evidence_ids = context["evidence_ids"]
        issues: list[str] = []
        if not body_markdown.strip():
            issues.append("empty_section_body")
        if re.search(r"^#{1,2}\s+", body_markdown, re.M):
            issues.append("compiler_owned_heading_detected")
        fact_refs = set(_FACT_RE.findall(body_markdown))
        evidence_refs = set(_EVIDENCE_RE.findall(body_markdown))
        for fact_id in sorted(fact_refs - set(facts_by_id)):
            issues.append(f"unknown_fact_reference:{fact_id}")
        for evidence_id in sorted(evidence_refs - evidence_ids):
            issues.append(f"unknown_evidence_reference:{evidence_id}")
        if any(term in body_markdown for term in _TARGET_VALUE_TERMS):
            target_lines = [
                line for line in body_markdown.splitlines()
                if any(term in line for term in _TARGET_VALUE_TERMS)
                and not any(guard in line for guard in _TARGET_VALUE_GUARDS)
            ]
            if target_lines:
                issues.append("target_price_or_reasonable_value_detected")
        if re.search(r"(?:手动|人工).{0,12}(?:计算|反推|估值)", body_markdown):
            issues.append("manual_deterministic_substitution_detected")

        module_statuses = dict(context["index"].get("module_statuses") or {})
        implied_module = self._module_result(module_statuses.get("implied_expectations") or {})
        if implied_module.status != "passed" and any(
            term.casefold() in body_markdown.casefold()
            for term in _VALUATION_DIRECTION_TERMS
        ):
            issues.append("valuation_direction_without_implied_expectations")
        if section_id == "implied_expectations":
            if implied_module.status != "passed" and _line_material_numbers(body_markdown):
                issues.append("implied_expectations_numbers_without_deterministic_result")
        if section_id == "terminal_narrative" and "TAM" in body_markdown.upper():
            deterministic = self._module_result(module_statuses.get("terminal_scenarios") or {})
            if deterministic.status != "passed" and any(
                _line_material_numbers(line) for line in body_markdown.splitlines()
            ):
                issues.append("terminal_scenario_numbers_without_deterministic_result")

        numeric_rows: list[dict[str, Any]] = []
        for line_number, line in enumerate(body_markdown.splitlines(), start=1):
            matches = _line_material_numbers(line)
            if not matches:
                continue
            cited_ids = _FACT_RE.findall(line)
            cited_facts = [facts_by_id[value] for value in cited_ids if value in facts_by_id]
            if not cited_facts:
                issues.append(f"uncited_material_number:{line_number}")
                continue
            for match in matches:
                display_unit = match.group("unit") or (match.group("prefix") and "元") or ""
                matched_fact_id = next(
                    (
                        str(fact.get("fact_id"))
                        for fact in cited_facts
                        if _display_matches_fact(match.group("value"), display_unit, fact)
                    ),
                    None,
                )
                numeric_rows.append({
                    "line_number": line_number,
                    "reported_value": match.group("value"),
                    "unit": display_unit,
                    "matched_fact_id": matched_fact_id,
                })
                if matched_fact_id is None:
                    issues.append(f"numeric_fact_mismatch:{line_number}:{match.group(0).strip()}")

        for fact_id in sorted(fact_refs & set(facts_by_id)):
            fact = facts_by_id[fact_id]
            if fact.get("validation_status") not in {None, "pass"}:
                issues.append(f"fact_not_validated:{fact_id}")
            input_ids = {str(value) for value in (fact.get("input_fact_ids") or [])}
            if fact.get("formula") and (not input_ids or not input_ids.issubset(facts_by_id)):
                issues.append(f"derived_fact_lineage_incomplete:{fact_id}")
        return list(dict.fromkeys(issues)), {
            "fact_ids": sorted(fact_refs),
            "evidence_ids": sorted(evidence_refs),
            "numeric_rows": numeric_rows,
        }

    def submit_section(
        self,
        report_id: str,
        *,
        section_id: str,
        body_markdown: str,
    ) -> ReportSection:
        """Validate and atomically persist one compiler-owned report section."""

        with self._lock:
            if section_id not in _SECTION_IDS:
                raise ValueError(f"unknown report section: {section_id}")
            record = self.require(report_id)
            issues, details = self._validate_section_body(report_id, section_id, body_markdown)
            if issues:
                rejected_path = self._workspace_dir(report_id) / "rejected_sections" / f"{section_id}.json"
                _atomic_json(rejected_path, {
                    "section_id": section_id,
                    "body_markdown": body_markdown,
                    "validation_issues": issues,
                    "updated_at": utc_now(),
                })
                raise ValueError("; ".join(issues))
            section = ReportSection(
                section_id=section_id,
                body_markdown=body_markdown.strip(),
                source_report_id=record.report_id,
                source_revision=record.revision,
                content_hash=hashlib.sha256(body_markdown.strip().encode("utf-8")).hexdigest(),
                fact_ids=details["fact_ids"],
                evidence_ids=details["evidence_ids"],
                status="passed",
                validation_issues=[],
            )
            self._write_section(report_id, section)
            record.pipeline_state = "drafting_sections"
            record.updated_at = utc_now()
            self._write_workspace_manifest(record)
            self._write_manifest(record)
            return section

    def _workspace_content(self, record: DeepReportRecord) -> tuple[str, list[str]]:
        lines = [
            f"# {record.security_name or record.symbol or '上市公司'}（{record.symbol or 'UNKNOWN'}）穿透式深度研究",
        ]
        delta = dict(record.history_delta or {})
        lines.extend(["", "## 与上次研究相比", ""])
        if not delta.get("base_report_id"):
            lines.append("这是知识库中的首次正式研究，暂无可比较的历史正式报告。")
        else:
            changed_metrics = [
                str((item.get("after") or {}).get("metric") or "")
                for item in (delta.get("updated") or [])
                if isinstance(item, dict)
            ]
            added_metrics = [
                str(item.get("metric") or "")
                for item in (delta.get("added") or [])
                if isinstance(item, dict)
            ]
            confirmed_metrics = [
                str(item.get("metric") or "")
                for item in (delta.get("confirmed") or [])
                if isinstance(item, dict)
            ]
            stale_metrics = [
                str(item.get("metric") or "")
                for item in (delta.get("stale") or [])
                if isinstance(item, dict)
            ]

            def labels(values: list[str]) -> str:
                mapped = [_METRIC_READER_LABELS.get(value, value) for value in values if value]
                return "、".join(list(dict.fromkeys(mapped))[:8]) or "无重大项目"

            lines.extend([
                f"- 新增或发生变化：{labels([*added_metrics, *changed_metrics])}。",
                f"- 由新一轮资料再次确认：{labels(confirmed_metrics)}。",
                f"- 尚待复核或本次未覆盖：{labels(stale_metrics)}。",
                "- 历史结论仅作为上次判断展示，本次事实仍以当前 Evidence 与 Fact 为准。",
            ])
        workspace_issues: list[str] = []
        context = self._analysis_context(record.report_id)
        deterministic_modules = dict(context["index"].get("module_statuses") or {})
        for section_id, heading in EQUITY_DEEP_RESEARCH_PROFILE["required_sections"]:
            section = self._read_section(record.report_id, section_id)
            lines.extend(["", f"## {heading}", ""])
            if section is not None and section.status == "passed":
                lines.append(section.body_markdown)
                continue
            implied_gap = (
                section_id == "implied_expectations"
                and self._module_result(deterministic_modules.get("implied_expectations") or {}).status
                in {"insufficient_evidence", "not_requested", "warning"}
            )
            if implied_gap:
                lines.append(
                    "[data_gap] 当前缺少连续三个预测年度、可识别覆盖信息或可重放的市值 Fact，"
                    "因此不运行市值隐含长期利润反推，也不提供目标价。"
                )
                continue
            status = section.status if section else "missing"
            workspace_issues.append(f"workspace_section_not_ready:{section_id}:{status}")
            lines.append("[data_gap] 本章节尚未通过 Report Workspace 校验，未发布研究判断。")

        lines.extend([
            "",
            f"## {_COMPILER_METHOD_HEADING}",
            "",
            "本报告分别核验公司披露、市场数据和外部研究资料；重要数字均与已登记数据逐项对应。",
            "财务异常仅表示需要进一步核查，不等同于财务造假判断。",
            "市值隐含长期利润反推采用净利润近似口径，不是完整现金流折现模型，也不是目标价模型。",
            f"数据更新至：{_reader_datetime(record.data_as_of)}。",
            f"尚待补充：{('、'.join(_reader_gap_labels({key: asdict(value) for key, value in record.analysis_modules.items()})) or '无')}。",
        ])
        return "\n".join(lines).rstrip() + "\n", workspace_issues

    @staticmethod
    def _service_numeric_audit(
        content: str,
        facts: list[dict[str, Any]],
        *,
        reader_fact_ids: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        facts_by_id = {
            str(item.get("fact_id")): item for item in facts if item.get("fact_id")
        }
        rows: list[dict[str, Any]] = []
        issues: list[str] = []
        inside_reference_index = False
        for line_number, line in enumerate(content.splitlines(), start=1):
            if line.startswith("### 数据依据") or line.startswith("### 引用索引"):
                inside_reference_index = True
            if inside_reference_index or line.startswith("> -"):
                continue
            matches = _line_material_numbers(line)
            if not matches:
                continue
            cited_ids = _FACT_RE.findall(line)
            cited_ids.extend(
                (reader_fact_ids or {}).get(alias, "")
                for alias in _READER_FACT_RE.findall(line)
            )
            cited_ids = [value for value in cited_ids if value]
            cited_facts = [facts_by_id[value] for value in cited_ids if value in facts_by_id]
            for match in matches:
                unit = match.group("unit") or (match.group("prefix") and "元") or ""
                matched = next(
                    (
                        str(fact.get("fact_id"))
                        for fact in cited_facts
                        if _display_matches_fact(match.group("value"), unit, fact)
                    ),
                    None,
                )
                row = {
                    "line_number": line_number,
                    "reported_value": match.group("value"),
                    "unit": unit,
                    "matched_fact_id": matched,
                }
                rows.append(row)
                if matched is None:
                    issues.append(f"line {line_number}: {match.group(0).strip()}")
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return {
            "audit_mode": "service_full",
            "audit_status": "complete",
            "verdict": "PASS" if not issues else "FAIL",
            "content_binding_verified": True,
            "report_sha256": content_hash,
            "expected_sample_size": len(rows),
            "total": len(rows),
            "matched_count": len(rows) - len(issues),
            "unmatched_count": len(issues),
            "issues": issues,
            "rows": rows,
            "created_at": utc_now(),
        }

    def evaluate_workspace(self, report_id: str) -> dict[str, Any]:
        """Compile and validate the active workspace without publishing it."""

        record = self.require(report_id)
        record.pipeline_state = "compiling"
        self._write_manifest(record)
        context = self._analysis_context(report_id)
        raw_content, workspace_issues = self._workspace_content(record)
        desired_modules = {
            key: asdict(value) for key, value in record.analysis_modules.items()
        }
        desired_quality = (
            "passed_with_gaps"
            if _reader_gap_labels(desired_modules)
            else "passed"
        )
        reader_fact_ids = {
            str(index): fact_id
            for index, fact_id in enumerate(_ordered_matches(_FACT_RE, raw_content), start=1)
        }
        reader_evidence_ids = {
            str(index): evidence_id
            for index, evidence_id in enumerate(_ordered_matches(_EVIDENCE_RE, raw_content), start=1)
        }
        compiled = ""
        audit: dict[str, Any] = {}
        validation: dict[str, Any] = {}

        # Quality metadata is part of the published bytes. Recompile after
        # validation until the badge and missing-module summary agree with the
        # result, and rerun the audit against those exact final bytes.
        for _ in range(3):
            compiled = self._compile_report(
                record,
                raw_content,
                context,
                quality_status=desired_quality,
                analysis_modules=desired_modules,
            )
            audit = self._service_numeric_audit(
                compiled,
                context["facts"],
                reader_fact_ids=reader_fact_ids,
            )
            validation = self.validate(
                compiled,
                analysis_required=True,
                analysis_available=bool(context["available"]),
                available_fact_ids=context["fact_ids"],
                available_evidence_ids=context["evidence_ids"],
                deterministic_modules=dict(context["index"].get("module_statuses") or {}),
                audit_result=audit,
                referenced_fact_ids=set(reader_fact_ids.values()),
                referenced_evidence_ids=set(reader_evidence_ids.values()),
            )
            if workspace_issues:
                validation["issues"] = list(dict.fromkeys([
                    *validation["issues"], *workspace_issues,
                ]))
                validation["quality_status"] = "failed_validation"
            if audit["verdict"] != "PASS":
                validation["issues"] = list(dict.fromkeys([
                    *validation["issues"], "service_numeric_audit_failed",
                ]))
                validation["quality_status"] = "failed_validation"
            _ensure_failed_validation_issues(validation)

            next_quality = str(validation.get("quality_status") or "failed_validation")
            next_modules = dict(validation.get("analysis_modules") or {})
            current_missing = {
                key for key, value in desired_modules.items()
                if str((value or {}).get("status"))
                in {"warning", "failed_validation", "insufficient_evidence", "not_requested"}
            }
            next_missing = {
                key for key, value in next_modules.items()
                if str((value or {}).get("status"))
                in {"warning", "failed_validation", "insufficient_evidence", "not_requested"}
            }
            if next_quality == desired_quality and next_missing == current_missing:
                break
            desired_quality = next_quality
            desired_modules = next_modules
        return {
            "content": compiled,
            "validation": validation,
            "audit": audit,
            "workspace_issues": workspace_issues,
            "citation_map": {
                "reader_fact_ids": reader_fact_ids,
                "reader_evidence_ids": reader_evidence_ids,
            },
        }

    @staticmethod
    def _hard_module_failed(module_id: str, raw: Any) -> bool:
        if isinstance(raw, ModuleResult):
            status = raw.status
            details = raw.details
        else:
            payload = dict(raw or {})
            status = str(payload.get("status") or "")
            details = dict(payload.get("details") or {})
        if status != "failed_validation":
            return False
        if module_id == "financial_quality":
            deterministic = dict(details.get("deterministic_analysis") or {})
            if deterministic:
                return str(deterministic.get("status") or "") == "failed_validation"
        return True

    @staticmethod
    def _recoverable_validation(issues: list[str], modules: dict[str, Any]) -> bool:
        if any(
            DeepReportService._hard_module_failed(key, modules.get(key))
            for key in _HARD_VALIDATION_MODULE_IDS
        ):
            return False
        recoverable_prefixes = (
            "workspace_section_not_ready", "missing_required_section", "unknown_fact_reference",
            "unknown_evidence_reference", "uncited_material_numbers", "numeric_",
        )
        recoverable_exact = {
            "missing_fact_references", "target_price_or_reasonable_value_detected",
            "service_numeric_audit_failed", "numeric_audit_missing_incomplete_or_content_mismatch",
            "valuation_direction_without_implied_expectations",
        }
        return bool(issues) and all(
            issue in recoverable_exact or issue.startswith(recoverable_prefixes)
            for issue in issues
        )

    def should_auto_repair(self, report_id: str, evaluation: dict[str, Any]) -> bool:
        record = self.require(report_id)
        validation = evaluation["validation"]
        return (
            record.repair_round < 1
            and validation.get("quality_status") == "failed_validation"
            and self._recoverable_validation(
                list(validation.get("issues") or []),
                dict(validation.get("analysis_modules") or {}),
            )
        )

    def repair_blockers(self, report_id: str) -> list[str]:
        """Return deterministic hard gates that a section-only repair cannot resolve."""

        record = self.require(report_id)
        blockers: list[str] = []
        for module_id in sorted(_HARD_VALIDATION_MODULE_IDS):
            module = record.analysis_modules.get(module_id)
            if module is None or not self._hard_module_failed(module_id, module):
                continue
            blockers.append(f"{module_id}:{module.reason or 'unspecified_module_failure'}")
        return blockers

    def mark_repairing(self, report_id: str) -> DeepReportRecord:
        with self._lock:
            record = self.require(report_id)
            record.repair_round = 1
            record.pipeline_state = "repairing"
            record.updated_at = utc_now()
            self._write_workspace_manifest(record)
            self._write_manifest(record)
            return record

    def _revision_diff(self, record: DeepReportRecord) -> str | None:
        if not record.parent_report_id:
            return None
        parent = self.get(record.parent_report_id)
        if parent is None:
            return None
        lines = [
            f"# Revision {record.revision} 与 Revision {parent.revision} 的差异",
            "",
            f"当前报告：`{record.report_id}`",
            f"父报告：`{parent.report_id}`",
            "",
        ]
        for section_id, heading in EQUITY_DEEP_RESEARCH_PROFILE["required_sections"]:
            current = self._read_section(record.report_id, section_id)
            previous = self._read_section(parent.report_id, section_id)
            if current and previous and current.content_hash == previous.content_hash:
                lines.append(f"- {heading}：未变化，复用父版本。")
                continue
            lines.extend(["", f"## {heading}", ""])
            before = (previous.body_markdown if previous else "").splitlines()
            after = (current.body_markdown if current else "").splitlines()
            diff = list(difflib.unified_diff(
                before,
                after,
                fromfile=f"revision-{parent.revision}",
                tofile=f"revision-{record.revision}",
                lineterm="",
            ))
            lines.extend(["```diff", *(diff or ["（无可比较正文）"]), "```"])
        return "\n".join(lines).rstrip() + "\n"

    def publish_workspace(self, report_id: str, evaluation: dict[str, Any] | None = None) -> DeepReportRecord:
        """Publish only compiler output; Agent final text is never a report source."""

        with self._lock:
            record = self.require(report_id)
            result = evaluation or self.evaluate_workspace(report_id)
            content = str(result["content"])
            validation = dict(result["validation"])
            audit = dict(result["audit"])
            citation_map = dict(result.get("citation_map") or {})
            reader_fact_ids = dict(citation_map.get("reader_fact_ids") or {})
            reader_evidence_ids = dict(citation_map.get("reader_evidence_ids") or {})
            _ensure_failed_validation_issues(validation)
            record.validation_issues = list(validation.get("issues") or [])
            record.quality_status = validation.get("quality_status", "failed_validation")
            record.analysis_modules = {
                key: ModuleResult(**value)
                for key, value in dict(validation.get("analysis_modules") or {}).items()
            }
            record.status = "completed"
            record.updated_at = utc_now()
            report_dir = self._dir(report_id)
            _atomic_json(report_dir / "validation.json", validation)
            _atomic_json(report_dir / "numeric_audit.json", audit)

            if record.quality_status == "failed_validation":
                record.pipeline_state = "diagnostic"
                record.delivery_kind = "diagnostic"
                (report_dir / "rejected_draft.md").write_text(content, encoding="utf-8")
                diagnostic = _diagnostic_markdown(record, record.validation_issues)
                diagnostic_path = report_dir / "diagnostic.md"
                diagnostic_path.write_text(diagnostic, encoding="utf-8")
                (report_dir / "report.md").unlink(missing_ok=True)
                (report_dir / "report.pdf").unlink(missing_ok=True)
                record.artifacts = [{
                    "artifact_id": "diagnostic",
                    "artifact_type": "text/markdown",
                    "artifact_role": "diagnostic",
                    "filename": report_pdf_filename(record).removesuffix(".pdf") + "_诊断.md",
                    "path": str(diagnostic_path),
                    "available": True,
                    "previewable": True,
                }]
            else:
                record.pipeline_state = "published"
                record.delivery_kind = "report"
                (report_dir / "diagnostic.md").unlink(missing_ok=True)
                markdown_path = report_dir / "report.md"
                # The numeric audit hashes the compiler's exact UTF-8 bytes.
                # Disable Windows newline translation so the persisted
                # artifact has the same SHA-256 as the audited content.
                markdown_path.write_text(content, encoding="utf-8", newline="")
                artifacts = [{
                    "artifact_id": "markdown",
                    "artifact_type": "text/markdown",
                    "artifact_role": "report",
                    "filename": report_pdf_filename(record).removesuffix(".pdf") + ".md",
                    "path": str(markdown_path),
                    "available": True,
                    "previewable": True,
                }]
                diff_content = self._revision_diff(record)
                if diff_content is not None:
                    diff_path = report_dir / "revision_diff.md"
                    diff_path.write_text(diff_content, encoding="utf-8")
                    artifacts.append({
                        "artifact_id": "diff",
                        "artifact_type": "text/markdown",
                        "artifact_role": "diff",
                        "filename": report_pdf_filename(record).removesuffix(".pdf") + "_版本差异.md",
                        "path": str(diff_path),
                        "available": True,
                        "previewable": True,
                    })
                artifacts.append({
                    "artifact_id": "pdf",
                    "artifact_type": "application/pdf",
                    "artifact_role": "pdf",
                    "filename": report_pdf_filename(record),
                    "path": str(report_dir / "report.pdf"),
                    "available": True,
                    "materialized": (report_dir / "report.pdf").exists(),
                    "previewable": False,
                })
                record.artifacts = artifacts

            references = {
                "fact_ids": sorted(set(reader_fact_ids.values()) or set(_FACT_RE.findall(content))),
                "evidence_ids": sorted(
                    set(reader_evidence_ids.values()) or set(_EVIDENCE_RE.findall(content))
                ),
                "reader_fact_ids": reader_fact_ids,
                "reader_evidence_ids": reader_evidence_ids,
                "compiled_content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "audit_mode": audit.get("audit_mode"),
            }
            _atomic_json(report_dir / "references.json", references)
            self._write_claims(
                report_dir / "claims.jsonl",
                content,
                reader_fact_ids=reader_fact_ids,
                reader_evidence_ids=reader_evidence_ids,
            )
            knowledge = self._knowledge_store()
            if knowledge is not None and record.symbol:
                analysis = self._analysis_context(report_id)
                prior = self._prior_report_for(record)
                claims = _read_jsonl(report_dir / "claims.jsonl")
                coverage_id = str(record.research_coverage.get("coverage_snapshot_id") or "") or None
                record.history_delta = knowledge.link_report(
                    report_id=record.report_id,
                    revision=record.revision,
                    symbol=record.symbol,
                    quality_status=record.quality_status,
                    evidence=list(analysis.get("evidence") or []),
                    facts=list(analysis.get("facts") or []),
                    claims=claims,
                    coverage_snapshot_id=coverage_id,
                    base_report_id=prior.report_id if prior else None,
                )
            self._write_workspace_manifest(record)
            self._write_manifest(record)
            return record

    @staticmethod
    def _compile_report(
        record: DeepReportRecord,
        content: str,
        analysis_context: dict[str, Any],
        *,
        quality_status: str | None = None,
        analysis_modules: dict[str, Any] | None = None,
    ) -> str:
        """Compile the audited workspace into reader-facing Markdown."""

        module_values = analysis_modules or {
            key: asdict(value) for key, value in record.analysis_modules.items()
        }
        reader_gaps = _reader_gap_labels(module_values)
        effective_quality = quality_status or record.quality_status
        reader_block = "\n".join([
            "",
            "> **阅读提示**",
            f"> - 当前状态：{_reader_quality_label(effective_quality)}",
            f"> - 报告版本：第 {record.revision} 版",
            f"> - 数据更新至：{_reader_datetime(record.data_as_of)}",
            f"> - 尚待补充：{('、'.join(reader_gaps) if reader_gaps else '无')}",
            "> - 使用说明：证据不足的部分会明确保留，不会用推测数字补齐。",
            "",
        ])
        lines = content.splitlines()
        # Workspace metadata is for the state machine. The public report gets a
        # concise reader block above and must not expose enum values or IDs.
        first_section = next((index for index, line in enumerate(lines) if line.startswith("## ")), len(lines))
        internal_prefixes = (
            "报告类型：", "股票：", "数据截至时间：", "质量状态：",
            "Revision：", "父版本：", "缺失模块：",
        )
        lines = [
            line for index, line in enumerate(lines)
            if index >= first_section or not line.startswith(internal_prefixes)
        ]
        if lines and lines[0].startswith("# "):
            compiled = "\n".join([lines[0], reader_block, *lines[1:]]).rstrip()
        else:
            compiled = (reader_block + "\n" + "\n".join(lines)).rstrip()

        fact_refs = _ordered_matches(_FACT_RE, content)
        evidence_refs = _ordered_matches(_EVIDENCE_RE, content)
        fact_labels = {fact_id: f"数据{index}" for index, fact_id in enumerate(fact_refs, start=1)}
        evidence_labels = {
            evidence_id: f"来源{index}" for index, evidence_id in enumerate(evidence_refs, start=1)
        }
        facts = {
            str(item.get("fact_id")): item
            for item in analysis_context.get("facts") or []
            if item.get("fact_id")
        }
        evidence = {
            str(item.get("evidence_id")): item
            for item in analysis_context.get("evidence") or []
            if item.get("evidence_id")
        }
        compiled = _readerize_report_text(compiled, fact_labels, evidence_labels, facts)
        index_lines = ["", "---"]
        if fact_refs:
            index_lines.extend(["", "### 数据依据", ""])
            for fact_id in fact_refs:
                item = facts.get(fact_id) or {}
                index_lines.append(
                    f"- 〔{fact_labels[fact_id]}〕 {_reader_fact_description(item)}"
                )
        if evidence_refs:
            index_lines.extend(["", "### 资料来源", ""])
            for evidence_id in evidence_refs:
                item = evidence.get(evidence_id) or {}
                source = str(item.get("source") or "来源名称未登记")
                locator = str(item.get("source_locator") or "").strip()
                source_label = f"[{source}]({locator})" if locator.startswith(("http://", "https://")) else source
                published = item.get("published_at") or item.get("retrieved_at") or "时间未明"
                index_lines.append(
                    f"- 〔{evidence_labels[evidence_id]}〕 {source_label}，发布或获取于 {published}"
                )
        if fact_refs or evidence_refs:
            return compiled + "\n" + "\n".join(index_lines).rstrip() + "\n"
        return compiled.rstrip() + "\n"

    def finalize(
        self,
        report_id: str,
        content: str,
        *,
        status: str = "completed",
        error: str | None = None,
    ) -> DeepReportRecord:
        with self._lock:
            record = self.require(report_id)
            analysis_context = self._analysis_context(report_id)
            validation = self.validate(
                content,
                analysis_required=error is None,
                analysis_available=bool(analysis_context["available"]),
                available_fact_ids=analysis_context["fact_ids"],
                available_evidence_ids=analysis_context["evidence_ids"],
                deterministic_modules=dict(analysis_context["index"].get("module_statuses") or {}),
                audit_result=analysis_context.get("audit"),
            )
            title = _TITLE_RE.search(content)
            if title:
                record.security_name = title.group(1).strip()
                record.symbol = title.group(2).strip().upper()
            if not record.symbol:
                symbol_match = _SYMBOL_RE.search(content) or _SYMBOL_RE.search(record.request_content)
                if symbol_match:
                    record.symbol = symbol_match.group(1).upper()
            if not record.security_name:
                record.security_name = record.symbol or "上市公司"
            data_match = _DATA_AS_OF_RE.search(content)
            if data_match:
                record.data_as_of = data_match.group(1).strip()[:120]
            elif not record.data_as_of:
                record.data_as_of = "未明确"
            record.validation_issues = list(validation["issues"])
            record.quality_status = validation["quality_status"]
            if error:
                record.validation_issues.append(error)
                record.quality_status = "failed_validation"
            record.analysis_modules = {
                key: ModuleResult(**value)
                for key, value in validation["analysis_modules"].items()
            }
            validation_failed = record.quality_status == "failed_validation"
            if status == "cancelled":
                record.status = "cancelled"
                record.pipeline_state = "cancelled"
            elif error or status != "completed":
                record.status = "failed"
                record.pipeline_state = "technical_failed"
            else:
                record.status = "completed"
                record.pipeline_state = "diagnostic" if validation_failed else "published"
            record.updated_at = utc_now()

            report_dir = self._dir(report_id)
            published_content = content
            if validation_failed:
                if error is None:
                    (report_dir / "rejected_draft.md").write_text(content, encoding="utf-8")
                published_content = _diagnostic_markdown(record, record.validation_issues, error)
                pdf_path = report_dir / "report.pdf"
                if pdf_path.exists():
                    pdf_path.unlink()
            compiled_content = self._compile_report(record, published_content, analysis_context)
            markdown_path = report_dir / ("diagnostic.md" if validation_failed else "report.md")
            markdown_path.write_text(compiled_content, encoding="utf-8")
            if validation_failed:
                (report_dir / "report.md").unlink(missing_ok=True)
            validation_path = report_dir / "validation.json"
            _atomic_json(validation_path, validation)
            references = {
                "fact_ids": sorted(set(_FACT_RE.findall(published_content))),
                "evidence_ids": sorted(set(_EVIDENCE_RE.findall(published_content))),
                "source_content_hash": hashlib.sha256(published_content.encode("utf-8")).hexdigest(),
                "compiled_content_hash": hashlib.sha256(compiled_content.encode("utf-8")).hexdigest(),
                "rejected_draft_hash": (
                    hashlib.sha256(content.encode("utf-8")).hexdigest()
                    if validation_failed and error is None
                    else None
                ),
            }
            _atomic_json(report_dir / "references.json", references)
            self._write_claims(
                report_dir / "claims.jsonl",
                compiled_content,
                reader_fact_ids={
                    str(index): fact_id
                    for index, fact_id in enumerate(
                        _ordered_matches(_FACT_RE, published_content), start=1
                    )
                },
                reader_evidence_ids={
                    str(index): evidence_id
                    for index, evidence_id in enumerate(
                        _ordered_matches(_EVIDENCE_RE, published_content), start=1
                    )
                },
            )

            if validation_failed:
                record.delivery_kind = "diagnostic"
                record.artifacts = [{
                    "artifact_id": "diagnostic",
                    "artifact_type": "text/markdown",
                    "artifact_role": "diagnostic",
                    "filename": report_pdf_filename(record).removesuffix(".pdf") + "_诊断.md",
                    "path": str(markdown_path),
                    "available": True,
                    "previewable": True,
                }]
            else:
                record.delivery_kind = "report"
                record.artifacts = [{
                    "artifact_id": "markdown",
                    "artifact_type": "text/markdown",
                    "artifact_role": "report",
                    "filename": report_pdf_filename(record).removesuffix(".pdf") + ".md",
                    "path": str(markdown_path),
                    "available": True,
                    "previewable": True,
                }, {
                    "artifact_id": "pdf",
                    "artifact_type": "application/pdf",
                    "artifact_role": "pdf",
                    "filename": report_pdf_filename(record),
                    "path": str(report_dir / "report.pdf"),
                    "available": True,
                    "materialized": (report_dir / "report.pdf").exists(),
                    "previewable": False,
                }]
            self._write_manifest(record)
            return record

    def mark_failed(self, report_id: str, error: str, *, cancelled: bool = False) -> DeepReportRecord:
        record = self.require(report_id)
        diagnostic = (
            f"# {record.security_name or record.symbol or '单股'}穿透式深度研究诊断\n\n"
            f"质量状态：failed_validation\n\n## 数据缺口与方法说明\n\n{error}\n"
        )
        return self.finalize(
            report_id,
            diagnostic,
            status="cancelled" if cancelled else "failed",
            error=error,
        )

    def validate(
        self,
        content: str,
        *,
        analysis_required: bool = False,
        analysis_available: bool = False,
        available_fact_ids: set[str] | None = None,
        available_evidence_ids: set[str] | None = None,
        deterministic_modules: dict[str, Any] | None = None,
        audit_result: dict[str, Any] | None = None,
        referenced_fact_ids: set[str] | None = None,
        referenced_evidence_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        issues: list[str] = []
        modules: dict[str, dict[str, Any]] = {}
        for section_id, heading in EQUITY_DEEP_RESEARCH_PROFILE["required_sections"]:
            body = _section_body(content, heading)
            if not body.strip():
                modules[section_id] = {
                    "status": "failed_validation",
                    "reason": f"missing section: {heading}",
                    "coverage": 0.0,
                    "details": {},
                }
                issues.append(f"missing_required_section:{heading}")
                continue
            lowered = body.lower()
            gap = any(marker in lowered for marker in (
                "[data_gap]", "insufficient_evidence", "数据不足", "证据不足", "证据说明", "不可用",
            ))
            modules[section_id] = {
                "status": "insufficient_evidence" if gap else "passed",
                "reason": "section contains an explicit evidence gap" if gap else None,
                "coverage": None,
                "details": {},
            }

        method_body = _section_body(content, "数据缺口与方法说明")
        if not method_body.strip():
            issues.append("missing_method_and_data_gap_section")
        if not _TITLE_RE.search(content):
            issues.append("title_must_include_company_and_symbol")
        fact_references = (
            set(referenced_fact_ids)
            if referenced_fact_ids is not None
            else set(_FACT_RE.findall(content))
        )
        evidence_references = (
            set(referenced_evidence_ids)
            if referenced_evidence_ids is not None
            else set(_EVIDENCE_RE.findall(content))
        )
        if not fact_references:
            issues.append("missing_fact_references")
        if analysis_required and not analysis_available:
            issues.append("financial_analysis_snapshot_missing")
        if analysis_required:
            audit = dict(audit_result or {})
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            service_full_audit = audit.get("audit_mode") == "service_full"
            audit_complete = (
                audit.get("audit_status") == "complete"
                and audit.get("verdict") == "PASS"
                and audit.get("content_binding_verified") is True
                and audit.get("report_sha256") == content_hash
                and int(audit.get("total") or 0) == int(audit.get("expected_sample_size") or 0)
                and (
                    int(audit.get("unmatched_count") or 0) == 0
                    if service_full_audit
                    else int(audit.get("total") or 0) > 0
                )
            )
            if not audit_complete:
                issues.append("numeric_audit_missing_incomplete_or_content_mismatch")
            elif int(audit.get("warn_count") or 0) > 0:
                issues.append("numeric_audit_contains_warnings")
        if analysis_available and available_fact_ids is not None:
            for fact_id in sorted(fact_references - available_fact_ids):
                issues.append(f"unknown_fact_reference:{fact_id}")
        if analysis_available and available_evidence_ids is not None:
            for evidence_id in sorted(evidence_references - available_evidence_ids):
                issues.append(f"unknown_evidence_reference:{evidence_id}")
        if "___" in content or re.search(r"\{(?:最新|公司|年份|数据|股票)[^}]*\}", content):
            issues.append("template_placeholder_detected")
        if re.search(r"(?:概率加权目标价|加权目标价|加权后的目标价)", content):
            issues.append("probability_weighted_target_detected")
        target_lines = [
            line.strip()
            for line in content.splitlines()
            if any(term in line for term in _TARGET_VALUE_TERMS)
            and not any(guard in line for guard in _TARGET_VALUE_GUARDS)
            and (re.search(r"[¥￥$]|\d", line) or "情景" in line)
        ]
        if target_lines:
            issues.append("target_price_or_reasonable_value_detected")
        implied_module = self._module_result(
            dict(deterministic_modules or {}).get("implied_expectations") or {}
        )
        if implied_module.status != "passed" and any(
            term.casefold() in content.casefold()
            for term in _VALUATION_DIRECTION_TERMS
        ):
            issues.append("valuation_direction_without_implied_expectations")
        uncited_material_lines: list[int] = []
        inside_body = False
        inside_reference_index = False
        for line_number, raw_line in enumerate(content.splitlines(), start=1):
            if raw_line.startswith("### 数据依据") or raw_line.startswith("### 引用索引"):
                inside_reference_index = True
            if inside_reference_index or raw_line.startswith("> -"):
                continue
            if raw_line.startswith("## "):
                inside_body = True
            if not inside_body or not _MATERIAL_NUMBER_RE.search(raw_line):
                continue
            if (
                _FACT_RE.search(raw_line)
                or _EVIDENCE_RE.search(raw_line)
                or _READER_FACT_RE.search(raw_line)
                or _READER_EVIDENCE_RE.search(raw_line)
            ):
                continue
            uncited_material_lines.append(line_number)
        if uncited_material_lines:
            preview = ",".join(str(value) for value in uncited_material_lines[:12])
            issues.append(f"uncited_material_numbers:{preview}")
        if "DCF" in content.upper() and not any(
            guard in content for guard in ("不是完整", "非完整", "不构成完整", "并非完整")
        ):
            issues.append("dcf_limitation_not_disclosed")
        if "市值隐含预期" in content and not any(
            guard in content for guard in ("不是目标价", "非目标价", "不构成目标价")
        ):
            issues.append("implied_expectations_not_target_price_guard_missing")

        for key, raw in dict(deterministic_modules or {}).items():
            deterministic = self._module_result(raw)
            narrative = modules.get(str(key))
            if narrative is None:
                modules[str(key)] = asdict(deterministic)
                continue
            narrative_status = str(narrative.get("status") or "pending")
            rank = {
                "failed_validation": 5,
                "insufficient_evidence": 4,
                "warning": 3,
                "not_requested": 2,
                "pending": 1,
                "running": 1,
                "passed": 0,
            }
            deterministic_payload = asdict(deterministic)
            if rank.get(deterministic.status, 3) > rank.get(narrative_status, 3):
                deterministic_payload["details"] = {
                    **deterministic_payload.get("details", {}),
                    "narrative_section": narrative,
                }
                modules[str(key)] = deterministic_payload
            else:
                narrative["details"] = {
                    **dict(narrative.get("details") or {}),
                    "deterministic_analysis": deterministic_payload,
                }

        header_block = re.split(r"^##\s+", content, maxsplit=1, flags=re.M)[0]
        if any(
            value["status"] in {"insufficient_evidence", "warning", "not_requested"}
            for value in modules.values()
        ) and not any(label in header_block for label in ("缺失模块", "尚待补充")):
            issues.append("missing_modules_summary_missing")

        declared = _QUALITY_RE.search(content)
        hard_fail = any(
            issue.startswith("missing_required_section")
            or issue.startswith("unknown_fact_reference")
            or issue.startswith("unknown_evidence_reference")
            or issue in {
                "title_must_include_company_and_symbol",
                "missing_fact_references",
                "template_placeholder_detected",
                "probability_weighted_target_detected",
                "target_price_or_reasonable_value_detected",
                "financial_analysis_snapshot_missing",
                "numeric_audit_missing_incomplete_or_content_mismatch",
            }
            for issue in issues
        ) or any(issue.startswith("uncited_material_numbers") for issue in issues) or any(
            modules.get(key, {}).get("status") == "failed_validation"
            for key in ("report_gate", "financial_quality", "market_data", "symbol_identity")
        )
        has_gaps = any(
            value["status"] in {"insufficient_evidence", "warning", "not_requested"}
            for value in modules.values()
        )
        if hard_fail:
            quality_status = "failed_validation"
        elif has_gaps or issues:
            quality_status = "passed_with_gaps"
        else:
            quality_status = "passed"
        if declared and declared.group(1).lower() == "failed_validation":
            quality_status = "failed_validation"
        elif declared and declared.group(1).lower() == "passed_with_gaps" and quality_status == "passed":
            quality_status = "passed_with_gaps"

        return {
            "quality_status": quality_status,
            "issues": issues,
            "analysis_modules": modules,
            "fact_reference_count": len(fact_references),
            "evidence_reference_count": len(evidence_references),
        }

    def get(self, report_id: str) -> DeepReportRecord | None:
        try:
            path = self._manifest_path(report_id)
        except ValueError:
            return None
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return DeepReportRecord.from_dict(payload)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None

    def require(self, report_id: str) -> DeepReportRecord:
        record = self.get(report_id)
        if record is None:
            raise KeyError(report_id)
        return record

    def find_by_attempt(self, session_id: str, attempt_id: str) -> DeepReportRecord | None:
        for record in self.list(limit=500):
            if record.session_id == session_id and record.attempt_id == attempt_id:
                return record
        return None

    def list(self, *, limit: int = 100) -> list[DeepReportRecord]:
        records: list[DeepReportRecord] = []
        if not self.base_dir.exists():
            return records
        for directory in self.base_dir.iterdir():
            if not directory.is_dir() or not directory.name.startswith("report_"):
                continue
            record = self.get(directory.name)
            if record is not None:
                records.append(record)
        records.sort(key=lambda item: item.updated_at, reverse=True)
        return records[: max(1, min(int(limit), 500))]

    def latest_for_symbol(
        self,
        symbol: str,
        *,
        report_date: str | None = None,
        session_id: str | None = None,
    ) -> DeepReportRecord | None:
        normalized = symbol.strip().upper()
        for record in self.list(limit=500):
            if (
                record.symbol.upper() != normalized
                or record.status != "completed"
                or record.quality_status == "failed_validation"
            ):
                continue
            if report_date and record.report_date != report_date:
                continue
            if session_id and record.session_id != session_id:
                continue
            return record
        return None

    def read_markdown(self, report_id: str) -> str:
        record = self.require(report_id)
        path = self._dir(report_id) / (
            "diagnostic.md" if record.delivery_kind == "diagnostic" else "report.md"
        )
        if record.delivery_kind == "diagnostic" and not path.exists():
            # Compatibility for immutable pre-v2 validation failures.
            path = self._dir(report_id) / "report.md"
        if not path.exists():
            raise FileNotFoundError(path)
        return path.read_text(encoding="utf-8")

    def content_role(self, report_id: str) -> str:
        return self.require(report_id).delivery_kind

    def artifact_path(self, report_id: str, artifact_id: str) -> Path:
        self.require(report_id)
        if artifact_id == "markdown":
            if self.require(report_id).quality_status == "failed_validation":
                raise ValueError("formal Markdown is unavailable because the report did not pass validation")
            path = self._dir(report_id) / "report.md"
        elif artifact_id == "diagnostic":
            path = self._dir(report_id) / "diagnostic.md"
            if not path.exists() and self.require(report_id).quality_status == "failed_validation":
                path = self._dir(report_id) / "report.md"
        elif artifact_id == "diff":
            path = self._dir(report_id) / "revision_diff.md"
        elif artifact_id == "pdf":
            path = self._dir(report_id) / "report.pdf"
        else:
            raise KeyError(artifact_id)
        if not path.exists():
            raise FileNotFoundError(path)
        return path

    def ensure_pdf(
        self,
        report_id: str,
        renderer: Callable[[str, str], bytes],
        *,
        force: bool = False,
    ) -> tuple[Path, DeepReportRecord]:
        with self._lock:
            record = self.require(report_id)
            if record.status != "completed" or record.quality_status == "failed_validation":
                raise ValueError("PDF is unavailable because the report did not pass validation")
            path = self._dir(report_id) / "report.pdf"
            if force or not path.exists():
                content = self.read_markdown(report_id)
                title = f"{record.security_name}（{record.symbol}）穿透式深度研究"
                # The renderer owns the visual title. Remove the compiler H1
                # from the derivative input so the first page does not repeat
                # the same report title twice.
                pdf_content = re.sub(r"^#\s+[^\n]+\n?", "", content, count=1)
                pdf = renderer(title, pdf_content)
                tmp = path.with_suffix(".pdf.tmp")
                tmp.write_bytes(pdf)
                tmp.replace(path)
                for artifact in record.artifacts:
                    if artifact.get("artifact_id") == "pdf":
                        artifact["available"] = True
                        artifact["materialized"] = True
                        artifact["path"] = str(path)
                record.updated_at = utc_now()
                self._write_manifest(record)
            return path, record

    def _write_manifest(self, record: DeepReportRecord) -> None:
        _atomic_json(self._manifest_path(record.report_id), record.to_dict())

    @staticmethod
    def _write_claims(
        path: Path,
        content: str,
        *,
        reader_fact_ids: dict[str, str] | None = None,
        reader_evidence_ids: dict[str, str] | None = None,
    ) -> None:
        claims: list[ClaimItem] = []
        section_id: str | None = None
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if line.startswith("## "):
                section_id = line[3:].strip()
                continue
            if not line or line.startswith("#") or line.startswith("|"):
                continue
            fact_ids = _FACT_RE.findall(line)
            evidence_ids = _EVIDENCE_RE.findall(line)
            fact_ids.extend(
                (reader_fact_ids or {}).get(alias, "")
                for alias in _READER_FACT_RE.findall(line)
            )
            evidence_ids.extend(
                (reader_evidence_ids or {}).get(alias, "")
                for alias in _READER_EVIDENCE_RE.findall(line)
            )
            fact_ids = [value for value in fact_ids if value]
            evidence_ids = [value for value in evidence_ids if value]
            has_gap = "[data_gap]" in line or "证据说明" in line or "当前证据不足" in line
            if not fact_ids and not evidence_ids and not has_gap:
                continue
            claim_type = (
                "data_gap" if has_gap
                else "inference" if "[inference]" in line or "研究判断" in line
                else "calculation" if fact_ids
                else "fact"
            )
            claims.append(
                ClaimItem(
                    claim_id=f"claim_{hashlib.sha256(line.encode('utf-8')).hexdigest()[:20]}",
                    text=line,
                    claim_type=claim_type,  # type: ignore[arg-type]
                    evidence_ids=list(dict.fromkeys(evidence_ids)),
                    fact_ids=list(dict.fromkeys(fact_ids)),
                    section_id=section_id,
                )
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for claim in claims:
                handle.write(json.dumps(asdict(claim), ensure_ascii=False) + "\n")
        tmp.replace(path)
