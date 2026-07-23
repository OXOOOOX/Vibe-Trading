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

from .contracts import (
    ClaimItem,
    ComponentDigestResolution,
    DeepReportRecord,
    ModuleResult,
    ReportSection,
    utc_now,
)
from .profile import get_report_profile
from .claim_support import build_claim_support_audit, source_tier
from .reader_terms import (
    SOURCE_STATUS_READER_LABELS,
    UNIT_READER_LABELS,
    VALUE_READER_LABELS,
    metric_reader_label,
    reader_machine_terms,
    semantics_reader_label,
)
from .monitoring_bundle import (
    build_structural_monitoring_bundle,
    validate_structural_monitoring_draft,
)
from .etf_report_readiness import (
    etf_report_presentation,
    evaluate_etf_report_readiness,
    project_etf_module_namespaces,
)
from src.research.enrichment import (
    ENRICHMENT_OUTCOMES,
    SECTION_TASKS,
    TERMINAL_ENRICHMENT_STATUSES,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SYMBOL_RE = re.compile(r"(?<![A-Z0-9])((?:\d{6}\.(?:SH|SZ|BJ))|(?:\d{5}\.HK)|(?:[A-Z][A-Z0-9.-]{0,14}\.US))(?![A-Z0-9])", re.I)
_TITLE_RE = re.compile(
    r"^#\s*(.+?)[（(]([^（）()]+)[）)](?:穿透式深度研究|ETF\s*(?:结构研究|穿透研究[（(]部分覆盖[）)]|穿透式深度研究|研究诊断草稿))\s*$",
    re.M,
)
_DATA_AS_OF_RE = re.compile(r"数据截至(?:时间)?\s*[：:]\s*([^\n]+)")
_QUALITY_RE = re.compile(r"质量状态\s*[：:]\s*(passed_with_gaps|failed_validation|passed)\b", re.I)
_FACT_RE = re.compile(r"\[Fact:([A-Za-z0-9_-]+)\]")
_EVIDENCE_RE = re.compile(r"\[Evidence:([A-Za-z0-9_-]+)\]")
_REPORT_RE = re.compile(r"\[Report:([A-Za-z0-9_-]+)\]")
_READER_FACT_RE = re.compile(r"〔数据(\d+)〕")
_READER_EVIDENCE_RE = re.compile(r"〔来源(\d+)〕")
_READER_CITATION_RE = re.compile(r"\[\^(\d+)\]")
_HIDDEN_FACT_RE = re.compile(r"<!--fact:([A-Za-z0-9_-]+)-->")
_HIDDEN_EVIDENCE_RE = re.compile(r"<!--evidence:([A-Za-z0-9_-]+)-->")
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
    r"(?P<unit>%|％|倍|[xX×]|元|千元|万元|亿元|亿|万|股|shares?|million|billion)?",
    re.I,
)
_COMPILER_METHOD_HEADING = "数据缺口与方法说明"


def _profile_sections(profile: str) -> list[tuple[str, str]]:
    return list(get_report_profile(profile)["required_sections"])


def _profile_section_headings(profile: str) -> dict[str, str]:
    return dict(_profile_sections(profile))


def _profile_section_ids(profile: str) -> set[str]:
    return set(_profile_section_headings(profile))

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
    "index_and_product": "指数与产品资料",
    "exposure_structure": "行业与组合集中度",
    "aggregate_fundamentals": "聚合基本面",
    "price_volume_structure": "量价结构",
    "flow_liquidity_tracking": "份额、流动性与跟踪质量",
    "holding_penetration": "关键成分穿透",
    "component_research": "成分公司研究",
    "scenarios_watchlist": "结构情景与跟踪框架",
    "identity": "ETF身份",
    "universe": "指数成分范围",
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


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _read_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _analysis_fact_id(*parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return f"fact_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def _upsert_jsonl_rows(path: Path, rows: Iterable[dict[str, Any]], *, key: str) -> None:
    current = {
        str(item.get(key)): item
        for item in _read_jsonl(path)
        if str(item.get(key) or "")
    }
    for row in rows:
        value = str(row.get(key) or "")
        if value:
            current[value] = dict(row)
    _atomic_jsonl(path, current.values())


def _safe_component(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", value.strip()).strip(" ._")
    return cleaned[:120] or fallback


def report_pdf_filename(record: DeepReportRecord) -> str:
    date_text = record.report_date or datetime.now(_SHANGHAI).date().isoformat()
    name = _safe_component(record.security_name or record.symbol, "上市公司")
    symbol = _safe_component(record.symbol, "UNKNOWN")
    label = (
        etf_report_presentation(
            {"status": "not_publishable"}
            if record.quality_status == "failed_validation"
            else record.etf_readiness
        )["filename_label"]
        if record.profile == "etf_deep_research"
        else "穿透式深度研究"
    )
    return f"{date_text}_{name}（{symbol}）_{label}.pdf"


def report_display_title(record: DeepReportRecord) -> str:
    fallback = "ETF" if record.profile == "etf_deep_research" else "上市公司"
    identity = record.security_name or record.symbol or fallback
    symbol = record.symbol or "UNKNOWN"
    if record.profile == "etf_deep_research":
        label = etf_report_presentation(
            {"status": "not_publishable"}
            if record.quality_status == "failed_validation"
            else record.etf_readiness
        )["title_label"]
    else:
        label = "穿透式深度研究"
    return f"{identity}（{symbol}）{label}"


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

    gap_ids: list[str] = []
    explicit_items: list[str] = []
    for key, value in module_values.items():
        payload = dict(value or {})
        if payload.get("missing_items"):
            gap_ids.append(key)
            explicit_items.extend(
                str(item) for item in payload.get("missing_items") or [] if str(item)
            )
            continue
        if str(payload.get("availability") or "") in {"partial", "missing"}:
            gap_ids.append(key)
            continue
        if str(payload.get("status")) in {
            "warning", "failed_validation", "insufficient_evidence", "not_requested",
        }:
            gap_ids.append(key)
    # These sections normally inherit a more specific gap from the valuation or
    # scenario modules. Repeating them makes a complete report look half empty.
    inherited = {"executive_summary", "counter_thesis", "conclusion_watchlist"}
    labels: list[str] = [
        metric_reader_label(item) for item in explicit_items
        if metric_reader_label(item)
    ]
    for module_id in gap_ids:
        if module_id in inherited:
            continue
        label = _MODULE_READER_LABELS.get(module_id, "部分研究结论")
        if label not in labels:
            labels.append(label)
    if not labels and gap_ids:
        labels.append("部分研究结论")
    return labels


def _claim_support_gate_issues(audit: dict[str, Any]) -> list[str]:
    """Reject non-gap conclusions that cannot be reused as verified knowledge."""

    issues: list[str] = []
    for item in audit.get("claims") or []:
        if (
            not isinstance(item, dict)
            or item.get("claim_type") not in {"inference", "opinion"}
        ):
            continue
        status = str(item.get("support_status") or "insufficient")
        if status in {"verified", "triangulated"}:
            continue
        issues.append(
            "claim_support_gate:"
            f"{item.get('claim_id') or 'unknown'}:{status}:"
            f"{item.get('support_reason') or 'unsupported'}"
        )
    return issues


def _reader_quality_label(value: str) -> str:
    return {
        "passed": "研究已完成，证据与校验均通过",
        "passed_with_gaps": "研究已完成；部分判断因公开证据不足而保留",
        "failed_validation": "仅生成诊断，尚不能发布正式研究结论",
    }.get(value, "研究结果正在校验")


def _available_field_value(raw: Any) -> str:
    if isinstance(raw, dict):
        if str(raw.get("status") or "available") not in {"available", "verified"}:
            return ""
        raw = raw.get("value")
    return str(raw or "").strip()


def _canonical_etf_name(
    analysis: dict[str, Any],
    snapshot: dict[str, Any],
) -> tuple[str, str, list[str]]:
    """Resolve ETF display identity from official name to weaker aliases."""

    subject_profile = dict(snapshot.get("subject_profile") or {})
    identity = dict(subject_profile.get("identity") or {})
    official_short = _available_field_value(identity.get("fund_short_name"))
    official_full = _available_field_value(identity.get("fund_full_name"))
    provider_name = _available_field_value(
        dict(snapshot.get("instrument_profile") or {}).get("identity", {}).get("name")
        if isinstance(dict(snapshot.get("instrument_profile") or {}).get("identity"), dict)
        else ""
    )
    requested_alias = str(
        analysis.get("security_name") or snapshot.get("security_name") or ""
    ).strip()
    candidates = [official_short, official_full, provider_name, requested_alias]
    canonical = next((item for item in candidates if item), "")
    source = (
        "official_fund_identity.fund_short_name" if official_short
        else "official_fund_identity.fund_full_name" if official_full
        else "instrument_profile.identity.name" if provider_name
        else "request_alias"
    )
    aliases = list(dict.fromkeys(item for item in candidates if item and item != canonical))
    return canonical, source, aliases


def _reader_fact_value(item: dict[str, Any]) -> str:
    metric = str(item.get("metric") or "").strip()
    raw_value = str(item.get("value") or "").strip()
    value = _decimal(item.get("value"))
    if value is None:
        if raw_value and metric == "ultra_high_freq_oscillator_global_suppliers":
            return f"{raw_value}家"
        return VALUE_READER_LABELS.get(raw_value.casefold(), raw_value) or "数值未登记"
    unit = str(item.get("unit") or "").strip()
    normalized = unit.casefold()
    if normalized in {"cny_thousand", "rmb_thousand", "yuan_thousand"}:
        value *= Decimal("1000")
        normalized = "cny"
    if normalized in {"ratio", "decimal"}:
        if metric in {"cfo_to_net_income"}:
            return f"{value:.2f}倍"
        return f"{value * Decimal('100'):.2f}%"
    if normalized in {"percent", "pct", "%"}:
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
    if normalized == "fund_units":
        return f"{value:,.0f}份"
    if normalized == "cny_per_fund_unit":
        return f"{value:.4f}元/份"
    if normalized == "cny_per_share":
        return f"{value:.4f}元/股"
    if normalized == "usd_billion":
        return f"{value.normalize()}十亿美元"
    if normalized in {"count", "个"}:
        return f"{value:.0f}个"
    if normalized in {"multiple", "times"}:
        return f"{value:.2f}倍"
    rendered = format(value.normalize(), "f")
    unit_label = UNIT_READER_LABELS.get(normalized)
    return f"{rendered}{unit_label}" if unit_label else rendered


def _reader_scope_label(value: Any) -> str:
    raw = str(value or "").strip()
    return {
        "consolidated": "合并报表口径",
        "parent_company": "母公司口径",
    }.get(raw.casefold(), "其他已登记口径" if raw else "已登记口径")


def _reader_fact_description(item: dict[str, Any]) -> str:
    metric = str(item.get("metric") or "").strip()
    label = metric_reader_label(metric) or "其他已登记指标"
    period = str(item.get("period") or "期间未明").strip()
    return f"{period} · {label}：{_reader_fact_value(item)}"


def _internal_report_reference_code(report_id: str) -> str:
    compact = re.sub(r"[^A-Za-z0-9]", "", report_id).upper()
    return f"VT-IR-{(compact[-10:] or 'UNKNOWN')}"


def _reference_plan(
    content: str,
    facts: dict[str, dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
    internal_report_resolver: Callable[[str], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Assign one reader citation number per source document in first-use order."""

    citations: list[dict[str, Any]] = []
    key_to_number: dict[str, int] = {}
    fact_citations: dict[str, list[int]] = {}
    evidence_citations: dict[str, int] = {}
    report_citations: dict[str, int] = {}

    def add_reference(key: str, payload: dict[str, Any]) -> int:
        if key in key_to_number:
            number = key_to_number[key]
            current = citations[number - 1]
            current["evidence_ids"] = sorted(set([
                *(current.get("evidence_ids") or []),
                *(payload.get("evidence_ids") or []),
            ]))
            current["fact_ids"] = sorted(set([
                *(current.get("fact_ids") or []),
                *(payload.get("fact_ids") or []),
            ]))
            return number
        number = len(citations) + 1
        key_to_number[key] = number
        citations.append({
            "reference_id": f"ref_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:20]}",
            "citation_number": number,
            **payload,
        })
        return number

    def evidence_number(evidence_id: str, *, fact_id: str | None = None) -> int | None:
        item = evidence.get(evidence_id)
        if item is None:
            return None
        metadata = dict(item.get("metadata") or {})
        locator = str(item.get("source_locator") or "").strip()
        document_ref = str(metadata.get("document_ref") or "").strip()
        key = f"document:{document_ref}" if document_ref else (
            f"locator:{locator}" if locator else f"evidence:{evidence_id}"
        )
        source_class = str(metadata.get("source_class") or "").strip()
        source_kind = source_class or (
            "web_page" if locator.startswith(("http://", "https://")) else "api_dataset"
        )
        evidence_title = str(metadata.get("title") or "").strip()
        if not evidence_title:
            evidence_title = str(item.get("summary") or "").strip().split("\n", 1)[0][:96]
        if not evidence_title:
            evidence_title = f"{str(item.get('domain') or '已登记')}数据资料"
        tier = source_tier(item)
        number = add_reference(key, {
            "source_kind": source_kind,
            "source_level": tier,
            "title": evidence_title,
            "publisher": str(item.get("source") or metadata.get("publisher") or ""),
            "author": str(metadata.get("author") or ""),
            "published_at": item.get("published_at"),
            "retrieved_at": item.get("retrieved_at"),
            "data_as_of": metadata.get("data_as_of") or item.get("published_at"),
            "public_url": locator if locator.startswith(("http://", "https://")) else None,
            "source_locator": locator,
            "internal_report_id": None,
            "internal_revision": None,
            "internal_index_code": (
                None if locator.startswith(("http://", "https://"))
                else f"EVID-{evidence_id[-12:].upper()}"
            ),
            "filename": metadata.get("filename"),
            "document_ref": document_ref or None,
            "locators": list(metadata.get("chunk_refs") or []),
            "evidence_ids": [evidence_id],
            "fact_ids": [fact_id] if fact_id else [],
            "content_hash": item.get("content_hash"),
        })
        evidence_citations[evidence_id] = number
        return number

    def fact_evidence_ids(fact_id: str, seen: set[str] | None = None) -> list[str]:
        if seen is None:
            seen = set()
        if fact_id in seen:
            return []
        seen.add(fact_id)
        item = facts.get(fact_id) or {}
        result = [str(value) for value in item.get("evidence_ids") or [] if str(value)]
        for input_fact_id in item.get("input_fact_ids") or []:
            result.extend(fact_evidence_ids(str(input_fact_id), seen))
        return list(dict.fromkeys(result))

    marker_re = re.compile(
        r"\[Fact:(?P<fact>[A-Za-z0-9_-]+)\]"
        r"|\[Evidence:(?P<evidence>[A-Za-z0-9_-]+)\]"
        r"|\[Report:(?P<report>[A-Za-z0-9_-]+)\]"
    )
    for match in marker_re.finditer(content):
        fact_id = match.group("fact")
        evidence_id = match.group("evidence")
        report_id = match.group("report")
        if fact_id and fact_id not in fact_citations:
            numbers = [
                number
                for source_id in fact_evidence_ids(fact_id)
                if (number := evidence_number(source_id, fact_id=fact_id)) is not None
            ]
            if not numbers:
                item = facts.get(fact_id) or {}
                key = f"fact:{fact_id}"
                numbers = [add_reference(key, {
                    "source_kind": "api_dataset",
                    "source_level": "single_provider",
                    "title": _reader_fact_description(item),
                    "publisher": "Vibe-Trading 结构化数据",
                    "author": "",
                    "published_at": None,
                    "retrieved_at": None,
                    "data_as_of": item.get("period"),
                    "public_url": None,
                    "source_locator": f"fact:{fact_id}",
                    "internal_report_id": None,
                    "internal_revision": None,
                    "internal_index_code": f"DATA-{fact_id[-10:].upper()}",
                    "filename": None,
                    "document_ref": None,
                    "locators": [],
                    "evidence_ids": [],
                    "fact_ids": [fact_id],
                    "content_hash": None,
                })]
            fact_citations[fact_id] = list(dict.fromkeys(numbers))
        elif evidence_id and evidence_id not in evidence_citations:
            evidence_number(evidence_id)
        elif report_id and report_id not in report_citations:
            resolved = (
                internal_report_resolver(report_id)
                if internal_report_resolver is not None
                else None
            ) or {}
            key = f"internal-report:{report_id}"
            report_citations[report_id] = add_reference(key, {
                "source_kind": "internal_report",
                "source_level": "internal_report",
                "title": str(resolved.get("title") or f"系统内研究报告 {report_id}"),
                "publisher": "Vibe-Trading",
                "author": "",
                "published_at": resolved.get("published_at"),
                "retrieved_at": None,
                "data_as_of": resolved.get("data_as_of"),
                "public_url": str(
                    resolved.get("public_url") or f"/reports/{report_id}"
                ),
                "source_locator": str(
                    resolved.get("source_locator") or f"deep-report:{report_id}"
                ),
                "internal_report_id": report_id,
                "internal_revision": resolved.get("revision"),
                "internal_index_code": str(
                    resolved.get("internal_index_code")
                    or _internal_report_reference_code(report_id)
                ),
                "filename": resolved.get("filename"),
                "document_ref": None,
                "locators": [],
                "evidence_ids": [],
                "fact_ids": [],
                "content_hash": None,
            })
    return {
        "schema_version": 2,
        "citations": citations,
        "fact_citation_map": fact_citations,
        "evidence_citation_map": evidence_citations,
        "internal_report_links": report_citations,
        "broken_links": [],
    }


def _readerize_report_text(
    content: str,
    references: dict[str, Any],
    facts: dict[str, dict[str, Any]],
) -> str:
    """Convert workspace notation into reader-facing Markdown language."""

    fact_map = dict(references.get("fact_citation_map") or {})
    evidence_map = dict(references.get("evidence_citation_map") or {})
    report_map = dict(references.get("internal_report_links") or {})

    def fact_marker(fact_id: str) -> str:
        return "".join(f"[^{number}]" for number in fact_map.get(fact_id, []))

    def evidence_marker(evidence_id: str) -> str:
        number = evidence_map.get(evidence_id)
        return f"[^{number}]" if number else ""

    def report_marker(report_id: str) -> str:
        number = report_map.get(report_id)
        return f"[^{number}]" if number else ""

    def canonicalize_ratio_facts(line: str) -> str:
        # Fact/Evidence/Report IDs are opaque machine tokens.  A ratio such as
        # zero must never match the ``0`` embedded in one of those IDs.
        marker_pattern = re.compile(
            r"(\[(?:Fact|Evidence|Report):[A-Za-z0-9_-]+\])"
        )
        parts = marker_pattern.split(line)
        for fact_id in _FACT_RE.findall(line):
            fact = facts.get(fact_id) or {}
            if str(fact.get("unit") or "").casefold() not in {
                "ratio", "decimal", "percent", "%",
            }:
                continue
            value = _decimal(fact.get("value"))
            if value is None:
                continue
            variants = list(dict.fromkeys([
                str(fact.get("value") or "").strip(),
                format(value.normalize(), "f"),
            ]))
            for variant in variants:
                if not variant:
                    continue
                replaced = False
                for index in range(0, len(parts), 2):
                    parts[index], count = re.subn(
                        rf"(?<![\d.]){re.escape(variant)}(?![\d.%％])",
                        _reader_fact_value(fact),
                        parts[index],
                        count=1,
                    )
                    if count:
                        replaced = True
                        break
                if replaced:
                    break
        return "".join(parts)

    def replace_money(match: re.Match[str]) -> str:
        fact_id = str(match.group("fact_id") or "")
        fact = facts.get(fact_id) or {}
        unit = str(fact.get("unit") or "").casefold()
        if unit not in {"cny", "rmb", "yuan", "元"}:
            return match.group(0)
        return f"{_reader_fact_value(fact)} {fact_marker(fact_id)}"

    def replace_shares(match: re.Match[str]) -> str:
        fact_id = str(match.group("fact_id") or "")
        fact = facts.get(fact_id) or {}
        unit = str(fact.get("unit") or "").casefold()
        if unit not in {"shares", "share", "股"}:
            return match.group(0)
        return f"{_reader_fact_value(fact)} {fact_marker(fact_id)}"

    rendered = "\n".join(canonicalize_ratio_facts(line) for line in content.splitlines())
    rendered = re.sub(
        r"[¥￥]\s*-?[\d,]+(?:\.\d+)?\s*\[Fact:(?P<fact_id>[A-Za-z0-9_-]+)\]",
        replace_money,
        rendered,
    )
    rendered = re.sub(
        r"-?[\d,]+(?:\.\d+)?\s*股\s*\[Fact:(?P<fact_id>[A-Za-z0-9_-]+)\]",
        replace_shares,
        rendered,
    )
    rendered = _FACT_RE.sub(lambda match: fact_marker(match.group(1)), rendered)
    rendered = _EVIDENCE_RE.sub(lambda match: evidence_marker(match.group(1)), rendered)
    rendered = _REPORT_RE.sub(lambda match: report_marker(match.group(1)), rendered)
    # Multiple Facts may intentionally resolve to the same source document.  A
    # sentence can therefore contain the same academic footnote more than once,
    # sometimes separated by spaces after the workspace markers are replaced.
    # Keep a single reader-facing marker for each adjacent run.
    rendered = re.sub(r"(?:\[\^(\d+)\])(?:\s*\[\^\1\])+", r"[^\1]", rendered)
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
        "本次不可变修订": "本次更新",
        "服务端编译的确定性成分表": "本报告的成分穿透表",
        "服务端确定性表格": "本报告的成分穿透表",
        "服务端按既定规则": "本报告按既定规则",
        "服务端编译资料": "已核验资料",
        "当前工作区": "当前资料",
        "带 数据 的": "具备可追溯来源的",
        "本 revision": "本版本",
        "未经 数据/资料 登记": "未经已核验数据或资料登记",
        "已验证 ETF 快照": "已验证的交易型开放式指数基金快照",
        "同指数 ETF 资金流代理": "同指数交易型开放式指数基金资金流代理",
        "同指数 ETF 组": "同指数交易型开放式指数基金组",
        "ETF 不适用": "交易型开放式指数基金不适用",
        "“ETF 营收”": "“基金组合营收”",
        "“ETF 利润”": "“基金组合利润”",
        "“ETF PE”": "“基金组合市盈率”",
        "AI 芯片": "人工智能芯片",
        "Fabless 模式": "无晶圆厂设计模式",
        "兼容 x86 的 CPU": "兼容 x86 指令集的中央处理器",
        "面向 AI 和大模型训练推理的 DCU": "面向人工智能和大模型训练推理的深度计算处理器",
        "ETF 聚合基本面结论": "基金组合层面的聚合基本面结论",
        "本版本 绑定": "本版本绑定",
        "涉及 人工智能": "涉及人工智能",
        "采用 无晶圆厂": "采用无晶圆厂",
        "外推为 基金组合": "外推为基金组合",
        "确定性表格": "成分穿透表",
        "P4B 状态": "研究资料状态",
        "P4B状态": "研究资料状态",
        "及 研究资料状态": "及研究资料状态",
        "这些字段优先于正文": "表中的权重、入选理由和覆盖情况以已核验数据为准",
        "隐式调用模型补齐": "用推测内容补齐",
    }
    for source, target in replacements.items():
        rendered = rendered.replace(source, target)
    rendered = re.sub(r"\brevision\b", "版本", rendered, flags=re.I)
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
    if normalized.endswith("_thousand") or "千元" in normalized:
        return Decimal("1000")
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
    if issue.startswith("derived_fact_lineage_incomplete:"):
        return "一项派生数据缺少完整公式或输入数据关系，当前无法复算。"
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
    diagnostic_title = report_display_title(record)
    subject_label = "ETF" if record.profile == "etf_deep_research" else "股票"
    return (
        f"# {diagnostic_title}\n\n"
        "> **当前状态：尚未形成可发布的正式报告**\n"
        f"> - {subject_label}：{record.symbol or '尚未明确'}\n"
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
_ETF_HARD_VALIDATION_MODULE_IDS = {"report_gate", "market_data", "identity", "universe"}


def _hard_validation_module_ids(profile: str) -> set[str]:
    return (
        _ETF_HARD_VALIDATION_MODULE_IDS
        if profile == "etf_deep_research"
        else _HARD_VALIDATION_MODULE_IDS
    )


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


def _without_redundant_section_heading(body: str, heading: str) -> str:
    """Drop a model-authored H3 that merely repeats the compiler-owned H2."""

    lines = body.splitlines()
    first = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first is None:
        return body.strip()
    match = re.fullmatch(r"###\s+(.+?)\s*", lines[first].strip())
    if not match or _normalized_heading(match.group(1)) != _normalized_heading(heading):
        return body.strip()
    del lines[first]
    while first < len(lines) and not lines[first].strip():
        del lines[first]
    return "\n".join(lines).strip()


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
    if unit == "千元":
        return value * _unit_multiplier(fact_unit) / Decimal("1000")
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


def _numeric_audit_text(line: str) -> str:
    """Return reader-visible text while excluding link destinations from audits.

    Percent-encoded query parameters such as ``%2C`` are transport syntax, not
    material numbers asserted by the report.  Keep the Markdown link label so
    visible numbers still receive the normal Fact/Evidence checks.
    """

    without_markdown_destinations = re.sub(
        r"(?<=\])\((?:https?|file)://[^)\s]+(?:\s+\"[^\"]*\")?\)",
        "",
        line,
        flags=re.I,
    )
    return re.sub(r"(?:https?|file)://\S+", "", without_markdown_destinations, flags=re.I)


def _line_material_numbers(line: str) -> list[re.Match[str]]:
    matches: list[re.Match[str]] = []
    for match in _NUMBER_CAPTURE_RE.finditer(_numeric_audit_text(line)):
        prefix = match.group("prefix") or ""
        unit = match.group("unit") or ""
        if not prefix and not unit:
            continue
        matches.append(match)
    return matches


_ETF_SELECTION_REASON_LABELS = {
    "large_weight": "权重较高",
    "weight_at_least_8pct": "核心高权重成分",
    "weight_at_least_5pct": "重要高权重成分",
    "structural_representative": "具有结构代表性",
    "material_price_contribution": "价格贡献显著",
    "material_earnings_contribution": "盈利贡献显著",
    "major_event": "存在重大事件",
    "evidence_conflict": "关键证据存在冲突",
    "research_stale": "既有研究已过期",
    "forced_minimum": "结构最低穿透要求",
}

_REFERENCE_READER_IDENTITIES = {
    "mootdx、tencent": ("mootdx、腾讯行情", "行情数据交叉核验"),
    "csi_official_close_weight": ("中证指数有限公司", "指数成分权重文件"),
    "eastmoney": ("东方财富数据", "结构化行情或财务快照"),
    "mootdx": ("通达信行情接口", "市场行情数据"),
    "tencent": ("腾讯行情", "市场行情数据"),
}

_SOURCE_LEVEL_READER_LABELS = {
    "official_structured": "官方结构化资料",
    "official_text": "官方原文",
    "independent_triangulation": "独立来源交叉验证",
    "single_provider": "单一数据提供方参考",
    "search_lead": "搜索线索（不可直接支持重大结论）",
    "internal_report": "内部正式报告索引",
}


def _reader_reference_identity(item: dict[str, Any]) -> tuple[str, str]:
    """Translate provider identifiers into useful reference-list language."""

    publisher = str(item.get("publisher") or "来源机构未登记").strip()
    title = str(item.get("title") or "资料标题未登记").strip()
    mapped = _REFERENCE_READER_IDENTITIES.get(title) or _REFERENCE_READER_IDENTITIES.get(
        publisher
    )
    if mapped:
        return mapped
    technical = re.compile(r"^[a-z][a-z0-9_.-]*$", re.I)
    return (
        "结构化数据提供方" if technical.fullmatch(publisher) else publisher,
        "已登记资料" if technical.fullmatch(title) else title,
    )

_COMPONENT_STATUS_LABELS = {
    "reusable": "研究可复用",
    "partial_reusable": "部分研究可复用",
    "stale": "研究已过期",
    "missing": "尚无可复用研究",
    "conflicted": "研究证据存在冲突",
    "unresolved": "尚未解析研究状态",
}


def _ratio_text(value: Any) -> str | None:
    parsed = _decimal(value)
    if parsed is None:
        return None
    return f"{parsed * Decimal('100'):.2f}%"


def _fact_supported_summary_text(
    text: str,
    fact_ids: list[str],
    facts_by_id: dict[str, dict[str, Any]],
) -> str:
    """Remove numeric clauses that cannot replay from the linked Fact set."""

    facts = [facts_by_id[fact_id] for fact_id in fact_ids if fact_id in facts_by_id]
    retained: list[str] = []
    for clause in re.split(r"(?<=[；。！？])", text):
        matches = _line_material_numbers(clause)
        if not matches:
            retained.append(clause)
            continue
        supported = all(
            any(
                _display_matches_fact(
                    match.group("value"),
                    match.group("unit") or (match.group("prefix") and "元") or "",
                    fact,
                )
                for fact in facts
            )
            for match in matches
        )
        if supported:
            rendered_clause = clause
            for match in reversed(matches):
                matched_fact = next(
                    (
                        fact
                        for fact in facts
                        if _display_matches_fact(
                            match.group("value"),
                            match.group("unit")
                            or (match.group("prefix") and "元")
                            or "",
                            fact,
                        )
                    ),
                    None,
                )
                if matched_fact is None:
                    continue
                rendered_clause = (
                    rendered_clause[: match.start()]
                    + _reader_fact_value(matched_fact)
                    + rendered_clause[match.end():]
                )
            retained.append(rendered_clause)
    return "".join(retained).strip()


def _etf_penetration_view(
    context: dict[str, Any],
    modules: dict[str, ModuleResult],
) -> dict[str, Any]:
    holding_module = modules.get("holding_penetration")
    research_module = modules.get("component_research")
    holding_details = dict(holding_module.details if holding_module else {})
    research_details = dict(research_module.details if research_module else {})
    selection = dict(context.get("etf_component_selection") or {})
    if not selection and holding_module and holding_module.selected_components:
        # Legacy reports may predate the dedicated P4A file while still having
        # a fully audited flat ModuleResult and immutable component Facts.
        selection = {
            "selection_id": holding_module.selection_id,
            "selected": [dict(item) for item in holding_module.selected_components],
            "selected_weight_coverage": holding_module.selected_weight_coverage,
            "explanation_coverage": holding_module.explanation_coverage,
            "quality": (
                "complete" if holding_module.availability == "complete" else "partial"
            ),
            "concentration": {
                "concentration_class": holding_details.get("concentration_class"),
            },
            "stop_reason": holding_details.get("stop_reason"),
            "warnings": list(holding_details.get("warnings") or []),
        }
    resolution = dict(context.get("component_digest_resolution") or {})
    if not resolution and research_module and research_module.selected_components:
        resolution = {
            "resolution_id": research_module.resolution_id,
            "bindings": [dict(item) for item in research_module.selected_components],
            "reusable_count": research_module.reusable_count,
            "partial_reusable_count": research_module.partial_reusable_count,
            "stale_count": research_module.stale_count,
            "missing_count": research_module.missing_count,
            "conflicted_count": research_module.conflicted_count,
            "warnings": list(research_details.get("warnings") or []),
        }
    deterministic_details = dict(
        dict(
            (holding_module.deterministic_result if holding_module else None)
            or holding_details.get("deterministic_analysis")
            or {}
        ).get("details") or {}
    )
    selection_fact_ids = dict(
        holding_details.get("fact_ids") or deterministic_details.get("fact_ids") or {}
    )
    coverage_fact_ids = dict(research_details.get("coverage_fact_ids") or {})
    bindings = {
        str(item.get("component_symbol") or "").upper(): dict(item)
        for item in (resolution.get("bindings") or [])
        if isinstance(item, dict) and str(item.get("component_symbol") or "")
    }
    digests = dict(context.get("component_research_digests") or {})
    claims_by_id = {
        str(item.get("claim_id") or ""): dict(item)
        for item in (context.get("component_research_claims") or [])
        if isinstance(item, dict) and str(item.get("claim_id") or "")
    }
    facts_by_id = {
        str(item.get("fact_id") or ""): dict(item)
        for item in (context.get("facts") or [])
        if isinstance(item, dict) and str(item.get("fact_id") or "")
    }

    def ratio_fact(metric: str) -> float | None:
        for fact in facts_by_id.values():
            if str(fact.get("metric") or "") != metric:
                continue
            try:
                return float(fact.get("value"))
            except (TypeError, ValueError):
                return None
        return None
    selected_items: list[dict[str, Any]] = []
    component_fact_ids = dict(selection_fact_ids.get("component_weights") or {})
    for rank, raw in enumerate(selection.get("selected") or [], start=1):
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        symbol = str(item.get("symbol") or "").upper()
        binding = bindings.get(symbol, {})
        digest_id = str(binding.get("digest_id") or "")
        digest = dict(digests.get(digest_id) or {}) if digest_id else {}
        summaries: list[dict[str, Any]] = []
        claim_ids_by_dimension = dict(digest.get("claim_ids_by_dimension") or {})
        for dimension, value in dict(digest.get("summaries_by_dimension") or {}).items():
            summary_text = str(value).strip()
            if not summary_text:
                continue
            linked_claims = [
                claims_by_id.get(str(claim_id), {})
                for claim_id in claim_ids_by_dimension.get(dimension) or []
            ]
            summary_fact_ids = list(dict.fromkeys(
                str(fact_id)
                for claim in linked_claims
                for fact_id in (claim.get("fact_ids") or [])
                if str(fact_id)
            ))
            # A reusable prose summary can mix replayable accounting Facts with
            # an unnormalized growth rate or guidance number. Retain only the
            # clauses whose material numbers replay from the linked Facts.
            summary_text = _fact_supported_summary_text(
                summary_text,
                summary_fact_ids,
                facts_by_id,
            )
            if not summary_text:
                continue
            summary_evidence_ids = list(dict.fromkeys(
                str(evidence_id)
                for claim in linked_claims
                for evidence_id in (claim.get("evidence_ids") or [])
                if str(evidence_id)
            ))
            summaries.append({
                "text": summary_text,
                "fact_ids": summary_fact_ids,
                "evidence_ids": summary_evidence_ids,
            })
        reasons = [
            _ETF_SELECTION_REASON_LABELS[str(reason)]
            for reason in (item.get("reasons") or binding.get("selection_reasons") or [])
            if str(reason) in _ETF_SELECTION_REASON_LABELS
        ]
        status = str(binding.get("digest_status") or "unresolved")
        selected_items.append({
            "rank": rank,
            "symbol": symbol,
            "name": str(item.get("name") or binding.get("component_name") or symbol),
            "weight": item.get("weight"),
            "weight_fact_id": component_fact_ids.get(symbol),
            "selection_reasons": reasons,
            "digest_status": status,
            "digest_status_label": _COMPONENT_STATUS_LABELS.get(status, status),
            "digest_id": digest_id or None,
            "source_report_ids": list(digest.get("source_report_ids") or []),
            "evidence_ids": list(digest.get("evidence_ids") or []),
            "summaries": summaries[:3],
            "warnings": list(binding.get("warnings") or []),
        })
    return {
        "available": bool(selection),
        "selection_id": selection.get("selection_id"),
        "resolution_id": resolution.get("resolution_id"),
        "selection_quality": selection.get("quality"),
        "concentration_class": dict(selection.get("concentration") or {}).get(
            "concentration_class"
        ),
        "selected_count": len(selected_items),
        "stop_reason": selection.get("stop_reason"),
        "warnings": list(dict.fromkeys([
            *(selection.get("warnings") or []),
            *(resolution.get("warnings") or []),
        ])),
        "coverage": {
            "observed_weight_coverage": {
                "value": dict(selection.get("concentration") or {}).get(
                    "observed_weight_coverage"
                ) or ratio_fact("etf_observed_weight_coverage"),
                "fact_id": selection_fact_ids.get("observed_weight_coverage"),
            },
            "selected_weight_coverage": {
                "value": selection.get("selected_weight_coverage"),
                "fact_id": selection_fact_ids.get("selected_weight_coverage"),
            },
            "explanation_coverage": {
                "value": selection.get("explanation_coverage"),
                "fact_id": selection_fact_ids.get("explanation_coverage"),
            },
            "research_coverage": {
                "value": (
                    research_details.get("research_coverage")
                    if research_details.get("research_coverage") is not None
                    else (research_module.research_coverage if research_module else None)
                ),
                "fact_id": coverage_fact_ids.get("research_coverage"),
            },
            "fully_supported_coverage": {
                "value": (
                    research_details.get("fully_supported_coverage")
                    if research_details.get("fully_supported_coverage") is not None
                    else (
                        research_module.fully_supported_coverage
                        if research_module else None
                    )
                ),
                "fact_id": coverage_fact_ids.get("fully_supported_coverage"),
            },
        },
        "status_counts": {
            key: int(resolution.get(f"{key}_count") or 0)
            for key in (
                "reusable", "partial_reusable", "stale", "missing", "conflicted"
            )
        },
        "components": selected_items,
    }


def _etf_penetration_markdown(view: dict[str, Any]) -> str:
    if not view.get("available"):
        return (
            "[data_gap] 当前尚未形成可验证的成分选择快照，因此不展示关键持仓穿透结论；"
            "这不会阻止产品、量价和流动性等其他章节发布。"
        )
    lines = ["### 确定性选择与研究覆盖", ""]
    coverage_labels = (
        ("observed_weight_coverage", "已知成分权重覆盖"),
        ("selected_weight_coverage", "入选成分权重覆盖"),
        ("explanation_coverage", "解释覆盖"),
        ("research_coverage", "可用研究覆盖"),
        ("fully_supported_coverage", "完全支持覆盖"),
    )
    coverage_parts: list[str] = []
    for key, label in coverage_labels:
        raw = dict(view.get("coverage") or {}).get(key) or {}
        rendered = _ratio_text(raw.get("value"))
        if rendered is None:
            coverage_parts.append(f"{label}：不适用")
            continue
        fact_id = str(raw.get("fact_id") or "")
        citation = f" [Fact:{fact_id}]" if fact_id else ""
        coverage_parts.append(f"{label}：{rendered}{citation}")
    lines.append("；".join(coverage_parts) + "。")

    components = list(view.get("components") or [])
    if not components:
        lines.extend([
            "",
            "本次确定性选择结果为零只成分。这表示当前结构下继续逐只穿透的边际解释增益不足，"
            "不是数据错误，也不代表任何单一成分不重要。",
        ])
        return "\n".join(lines)

    lines.extend([
        "",
        "| 成分 | 权重 | 入选原因 | 研究状态 | 可用摘要 |",
        "|---|---:|---|---|---|",
    ])
    has_gap = False
    for item in components:
        status = str(item.get("digest_status") or "unresolved")
        has_gap = has_gap or status not in {"reusable", "partial_reusable"}
        fact_id = str(item.get("weight_fact_id") or "")
        weight = _ratio_text(item.get("weight")) or "不适用"
        weight_cell = f"{weight} [Fact:{fact_id}]" if fact_id else weight
        summaries = list(item.get("summaries") or [])
        if summaries:
            summary_parts: list[str] = []
            for value in summaries:
                payload = dict(value) if isinstance(value, dict) else {"text": str(value)}
                text = str(payload.get("text") or "").replace("|", "｜").strip()
                fact_ids = [str(item) for item in payload.get("fact_ids") or [] if str(item)]
                evidence_ids = [
                    str(item) for item in payload.get("evidence_ids") or [] if str(item)
                ]
                markers = "".join(f"[Fact:{fact_id}]" for fact_id in fact_ids)
                markers += "".join(
                    f"[Evidence:{evidence_id}]" for evidence_id in evidence_ids[:3]
                )
                summary_parts.append(f"{text} {markers}".strip())
            summary = "；".join(summary_parts)
        elif status == "reusable":
            summary = "已有可复用研究记录；正文仅采用能够回溯原始资料的结论"
        elif status == "partial_reusable":
            summary = "仅部分研究维度可复用，未覆盖维度保留为空"
        else:
            summary = "不采用未经验证的成分结论"
        source_report_ids = [
            str(value) for value in item.get("source_report_ids") or [] if str(value)
        ]
        if source_report_ids and status in {"reusable", "partial_reusable"}:
            summary += " " + "".join(
                f"[Report:{report_id}]" for report_id in source_report_ids[:2]
            )
        lines.append(
            "| {name}（{symbol}） | {weight} | {reasons} | {status} | {summary} |".format(
                name=str(item.get("name") or "").replace("|", "｜"),
                symbol=item.get("symbol") or "",
                weight=weight_cell,
                reasons="、".join(item.get("selection_reasons") or []) or "确定性规则入选",
                status=item.get("digest_status_label") or status,
                summary=summary,
            )
        )
    if has_gap:
        lines.extend([
            "",
            "[data_gap] 部分入选成分的研究摘要缺失、过期或存在冲突。报告保留这些缺口，"
            "不会在本次报告生成过程中隐式调用模型补齐。",
        ])
    return "\n".join(lines)


def _etf_product_markdown(context: dict[str, Any]) -> str:
    """Render the report-bound ETF profile without asking the model for facts."""

    profile = dict((context.get("snapshot") or {}).get("subject_profile") or {})
    if not profile:
        return ""
    facts = {
        str(item.get("metric") or ""): dict(item)
        for item in context.get("facts") or []
        if isinstance(item, dict) and item.get("metric") and item.get("fact_id")
    }

    def rendered(section: str, key: str) -> str:
        raw = dict((profile.get(section) or {}).get(key) or {})
        if raw.get("status") != "available":
            note = str(raw.get("note") or "").strip()
            return f"暂缺（{note}）" if note else "暂缺"
        value = raw.get("value")
        unit = str(raw.get("unit") or "")
        if unit == "ratio" and _decimal(value) is not None:
            text = f"{_decimal(value) * Decimal('100'):.2f}%"
        elif unit == "CNY" and _decimal(value) is not None:
            text = f"¥{_decimal(value):,.2f}"
        elif unit == "CNY_per_fund_unit" and _decimal(value) is not None:
            text = f"¥{_decimal(value):,.4f}"
        elif unit == "fund_units" and _decimal(value) is not None:
            text = f"{_decimal(value):,.0f}"
        else:
            text = VALUE_READER_LABELS.get(str(value).casefold(), str(value))
        fact = facts.get(key) or {}
        marker = f" [Fact:{fact.get('fact_id')}]" if fact.get("fact_id") else ""
        return f"{text}{marker}"

    lines = [
        "### 产品身份与指数规则",
        "",
        "| 项目 | 已核验值 | 数据/规则时点 |",
        "|---|---|---|",
    ]
    rows = (
        ("identity", "fund_full_name", "基金全称"),
        ("identity", "manager", "基金管理人"),
        ("identity", "custodian", "基金托管人"),
        ("identity", "exchange", "上市交易所"),
        ("identity", "contract_effective_date", "合同生效日"),
        ("identity", "listing_date", "上市日"),
        ("identity", "tracked_index_code", "跟踪指数代码"),
        ("identity", "tracked_index_name", "跟踪指数名称"),
        ("index_methodology", "version", "指数规则版本"),
        ("index_methodology", "target_component_count", "目标成分数量"),
        ("index_methodology", "single_constituent_weight_cap", "单一成分权重上限"),
        ("index_methodology", "top_five_weight_cap", "前五大成分权重上限"),
        ("index_methodology", "review_frequency", "定期调样频率"),
    )
    for section, key, label in rows:
        raw = dict((profile.get(section) or {}).get(key) or {})
        lines.append(
            f"| {label} | {rendered(section, key)} | {str(raw.get('data_as_of') or '暂缺')} |"
        )

    lines.extend([
        "",
        "### 费率与关键时点数据",
        "",
        "| 项目 | 已核验值 | 口径 | 数据时点 |",
        "|---|---:|---|---|",
    ])
    metric_rows = (
        ("management_fee_rate", "管理费率"),
        ("custody_fee_rate", "托管费率"),
        ("unit_nav", "单位净值"),
        ("fund_units", "基金份额（份）"),
        ("published_net_assets", "定期报告基金资产净值"),
        ("exchange_market_value", "场内市值"),
        ("iopv", "基金份额参考净值"),
        ("premium_discount_rate", "折溢价率"),
    )
    for key, label in metric_rows:
        raw = dict((profile.get("product_metrics") or {}).get(key) or {})
        lines.append(
            f"| {label} | {rendered('product_metrics', key)} | "
            f"{semantics_reader_label(raw.get('semantics'))} | "
            f"{str(raw.get('data_as_of') or '暂缺')} |"
        )

    share = dict(profile.get("share_history") or {})
    peer = dict(profile.get("peer_group") or {})
    if share or peer:
        lines.extend([
            "",
            "### 份额变化与同指数 ETF 组",
            "",
        ])
        share_fact = facts.get("etf_fund_units_change_1d") or {}
        units_fact = facts.get("etf_fund_units") or {}
        peer_fact = facts.get("peer_group_estimated_net_flow_1d") or {}
        member_fact = facts.get("peer_group_member_count") or {}
        coverage_fact = facts.get("peer_group_unit_change_coverage") or {}
        current_units = share.get("current_units")
        delta_1d = share.get("delta_1d")
        if current_units is not None:
            marker = f" [Fact:{units_fact.get('fact_id')}]" if units_fact.get("fact_id") else ""
            lines.append(f"- 本基金最新交易所份额：{float(current_units):,.0f} 份{marker}。")
        if delta_1d is not None:
            marker = f" [Fact:{share_fact.get('fact_id')}]" if share_fact.get("fact_id") else ""
            lines.append(f"- 较前一可比交易日变化：{float(delta_1d):+,.0f} 份{marker}。")
        member_marker = f" [Fact:{member_fact.get('fact_id')}]" if member_fact.get("fact_id") else ""
        coverage_marker = f" [Fact:{coverage_fact.get('fact_id')}]" if coverage_fact.get("fact_id") else ""
        lines.append(
            f"- 同指数产品组：{int(peer.get('member_count') or 0)} 只{member_marker}；"
            f"份额变化可比覆盖 {float(peer.get('unit_change_coverage_ratio') or 0) * 100:.2f}%{coverage_marker}。"
        )
        if peer.get("estimated_net_flow_1d") is not None:
            marker = f" [Fact:{peer_fact.get('fact_id')}]" if peer_fact.get("fact_id") else ""
            lines.append(
                f"- 同指数产品组估算单日净流量：¥{float(peer['estimated_net_flow_1d']):+,.2f}{marker}。"
                "该值为份额变化×当前市场价格的代理估算，不等同于成交额或基金公司确认资金流。"
            )
        for warning in peer.get("warnings") or []:
            lines.append(f"- 口径提示：{warning}")

    sources = [
        item for item in profile.get("sources") or []
        if isinstance(item, dict) and str(item.get("url") or "").startswith(("http://", "https://"))
    ]
    if sources:
        lines.extend(["", "### 产品资料原文", ""])
        for source in sources[:12]:
            source_status = SOURCE_STATUS_READER_LABELS.get(
                str(source.get("verification_status") or "source_recorded"),
                "来源状态已登记",
            )
            lines.append(
                f"- [{str(source.get('title') or source.get('publisher') or '官方原文')}]"
                f"({source.get('url')})（{source_status}）"
            )
    return "\n".join(lines)


class DeepReportService:
    """Own the report state machine independently from the LLM attempt."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _sync_etf_readiness(self, record: DeepReportRecord) -> None:
        if record.profile != "etf_deep_research":
            return
        pipeline, sections = project_etf_module_namespaces(record.analysis_modules)
        record.pipeline_checks = {
            key: self._module_result(value) for key, value in pipeline.items()
        }
        record.report_sections = {
            key: self._module_result(value) for key, value in sections.items()
        }
        record.etf_readiness = evaluate_etf_report_readiness(
            quality_status=record.quality_status,
            analysis_modules=record.analysis_modules,
            research_coverage=record.research_coverage,
        )

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

    def _internal_report_reference(self, report_id: str) -> dict[str, Any] | None:
        try:
            referenced = self.get(report_id)
        except ValueError:
            referenced = None
        if referenced is None:
            try:
                from src.reports.catalog import _report_reference_code, get_report_library_service

                catalog_record = get_report_library_service().get_report(report_id)
            except Exception:
                catalog_record = None
            if not catalog_record:
                return None
            knowledge_link = dict(catalog_record.get("knowledge_link") or {})
            code = str(
                knowledge_link.get("internal_reference_code")
                or _report_reference_code(catalog_record)
            )
            artifact = next(
                (
                    item for item in catalog_record.get("artifacts") or []
                    if item.get("artifact_role") in {"report", "markdown"}
                ),
                {},
            )
            return {
                "title": (
                    str(catalog_record.get("security_name") or "系统内研究记录")
                    + f"（{catalog_record.get('symbol') or catalog_record.get('subject_key') or report_id}）"
                ),
                "published_at": catalog_record.get("generated_at"),
                "data_as_of": catalog_record.get("data_as_of"),
                "revision": catalog_record.get("source_revision"),
                "internal_index_code": code,
                "public_url": f"/report-library/references/{code}",
                "filename": artifact.get("filename"),
                "source_locator": f"report-library:{report_id}",
            }
        instrument = (
            "ETF" if referenced.profile == "etf_deep_research"
            else "IDX" if referenced.profile == "index_deep_research"
            else "EQ"
        )
        symbol = re.sub(r"[^A-Z0-9]", "", referenced.symbol.upper()) or "UNKNOWN"
        report_date = re.sub(r"\D", "", referenced.report_date)[:8] or "UNDATED"
        suffix = hashlib.sha256(report_id.encode("utf-8")).hexdigest()[:6].upper()
        code = f"VT-{instrument}-{symbol}-{report_date}-R{referenced.revision:02d}-{suffix}"
        return {
            "title": (
                f"{referenced.security_name or referenced.symbol}（{referenced.symbol}）"
                f"穿透式深度研究，第 {referenced.revision} 版"
            ),
            "published_at": referenced.updated_at,
            "data_as_of": referenced.data_as_of,
            "revision": referenced.revision,
            "internal_index_code": code,
            "public_url": f"/report-library/references/{code}",
            "filename": report_pdf_filename(referenced).removesuffix(".pdf") + ".md",
            "source_locator": f"deep-report:{report_id}",
        }

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
        domain_groups: dict[str, set[str]] = {}
        for item in evidence:
            domain = str(item.get("domain") or "other")
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
            metadata = dict(item.get("metadata") or {})
            group = str(metadata.get("independence_group") or item.get("source") or "")
            if group:
                domain_groups.setdefault(domain, set()).add(group)
        for domain in record.research_coverage.get("domains") or []:
            domain_id = str(domain.get("domain") or "")
            if domain_id in {"identity_market", "financial_statements"} and facts:
                domain["status"] = "covered"
            elif domain_id == "industry_tam_competition":
                groups = set().union(*(
                    domain_groups.get(key, set()) for key in ("industry", "tam", "competition")
                ))
                required = int(domain.get("minimum_independent_sources") or 1)
                domain["status"] = "covered" if len(groups) >= required else "weak_evidence"
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
        record.research_coverage["material_conflicts"] = store.unresolved_conflicts(
            record.symbol,
            fact_ids=[str(item.get("fact_id") or "") for item in facts],
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

    def _monitoring_draft_path(self, report_id: str) -> Path:
        return self._workspace_dir(report_id) / "monitoring_bundle_draft.json"

    def _enrichment_plan_path(self, report_id: str) -> Path:
        return self._workspace_dir(report_id) / "enrichment_plan.json"

    def initialize_enrichment_plan(
        self,
        report_id: str,
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        """Bind a consented acquisition plan to one immutable revision."""

        record = self.require(report_id)
        payload = json.loads(json.dumps(plan, ensure_ascii=False, default=str))
        tasks = payload.get("tasks")
        if not isinstance(tasks, list):
            raise ValueError("research enrichment plan tasks must be a list")
        task_ids: set[str] = set()
        for raw in tasks:
            if not isinstance(raw, dict):
                raise ValueError("research enrichment task must be an object")
            task_id = str(raw.get("task_id") or "")
            if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", task_id):
                raise ValueError(f"invalid research enrichment task_id: {task_id}")
            if task_id in task_ids:
                raise ValueError(f"duplicate research enrichment task_id: {task_id}")
            task_ids.add(task_id)
            raw["status"] = "planned"
            raw["reason_code"] = None
            raw["attempts"] = []
        payload.update({
            "report_id": report_id,
            "revision": record.revision,
            "status": "planned" if tasks else "not_applicable",
            "updated_at": utc_now(),
        })
        _atomic_json(self._enrichment_plan_path(report_id), payload)
        self._write_workspace_manifest(record)
        return payload

    def enrichment_plan(self, report_id: str) -> dict[str, Any] | None:
        path = self._enrichment_plan_path(report_id)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _attempt_reason(attempts: list[dict[str, Any]]) -> str:
        outcomes = [str(item.get("outcome") or "") for item in attempts]
        if "retrieval_failed" in outcomes:
            return "retrieval_failed_after_retry"
        if "evidence_rejected" in outcomes:
            return "evidence_rejected_by_validation"
        if "source_unavailable" in outcomes:
            return "provider_unavailable"
        if "evidence_accepted" in outcomes:
            return "partial_coverage"
        return "public_source_not_found"

    def _derive_enrichment_task_status(
        self,
        report_id: str,
        task: dict[str, Any],
    ) -> tuple[str, str | None]:
        attempts = [dict(item) for item in task.get("attempts") or [] if isinstance(item, dict)]
        minimum_attempts = max(1, int(task.get("minimum_attempts") or 1))
        accepted = [item for item in attempts if item.get("outcome") == "evidence_accepted"]
        context = self._analysis_context(report_id)
        facts_by_id = {
            str(item.get("fact_id")): item
            for item in context.get("facts") or []
            if item.get("fact_id")
        }
        evidence_by_id = {
            str(item.get("evidence_id")): item
            for item in context.get("evidence") or []
            if item.get("evidence_id")
        }
        groups: set[str] = set()
        accepted_fact_ids: set[str] = set()
        covered_years: set[int] = set()
        for attempt in accepted:
            accepted_fact_ids.update(str(value) for value in attempt.get("fact_ids") or [])
            covered_years.update(int(value) for value in attempt.get("covered_years") or [])
            attempt_evidence_ids = {str(value) for value in attempt.get("evidence_ids") or []}
            for fact_id in attempt.get("fact_ids") or []:
                fact = facts_by_id.get(str(fact_id)) or {}
                attempt_evidence_ids.update(
                    str(value) for value in fact.get("evidence_ids") or []
                )
            for evidence_id in attempt_evidence_ids:
                evidence = evidence_by_id.get(str(evidence_id)) or {}
                metadata = dict(evidence.get("metadata") or {})
                group = str(metadata.get("independence_group") or evidence.get("source") or "")
                if group:
                    groups.add(group)

        required_sources = max(1, int(task.get("minimum_independent_sources") or 1))
        required_metrics = {str(value) for value in task.get("required_fact_metrics") or []}
        accepted_facts = [facts_by_id[value] for value in accepted_fact_ids if value in facts_by_id]
        metric_names = {str(item.get("metric") or "") for item in accepted_facts}
        required_periods = max(0, int(task.get("required_metric_periods") or 0))
        metric_periods = {
            str(item.get("period") or "")
            for item in accepted_facts
            if str(item.get("metric") or "") in required_metrics and item.get("period")
        }
        target_years = {int(value) for value in task.get("target_years") or []}
        source_requirement_met = len(groups) >= required_sources or (
            str(task.get("task_id")) == "annual_filings" and bool(accepted)
        )
        metric_requirement_met = required_metrics.issubset(metric_names) and (
            not required_periods or len(metric_periods) >= required_periods
        )
        year_requirement_met = not target_years or target_years.issubset(covered_years)
        if accepted and source_requirement_met and metric_requirement_met and year_requirement_met:
            return "satisfied", None
        if len(attempts) >= minimum_attempts:
            return "exhausted", self._attempt_reason(attempts)
        return ("running" if attempts else "planned"), None

    def record_research_attempt(
        self,
        report_id: str,
        *,
        task_id: str,
        outcome: str,
        query: str = "",
        document_refs: list[str] | None = None,
        fact_ids: list[str] | None = None,
        evidence_ids: list[str] | None = None,
        independence_groups: list[str] | None = None,
        covered_years: list[int] | None = None,
        detail: str = "",
    ) -> dict[str, Any]:
        """Append a validated acquisition receipt and derive task completion."""

        plan = self.enrichment_plan(report_id)
        if plan is None:
            raise ValueError("research enrichment plan is not active")
        if outcome not in ENRICHMENT_OUTCOMES:
            raise ValueError(f"unsupported research outcome: {outcome}")
        tasks = [item for item in plan.get("tasks") or [] if isinstance(item, dict)]
        task = next((item for item in tasks if str(item.get("task_id")) == task_id), None)
        if task is None:
            raise ValueError(f"unknown research enrichment task: {task_id}")

        context = self._analysis_context(report_id)
        known_fact_ids = {
            str(item.get("fact_id")) for item in context.get("facts") or [] if item.get("fact_id")
        }
        known_evidence_ids = {
            str(item.get("evidence_id")) for item in context.get("evidence") or [] if item.get("evidence_id")
        }
        normalized_facts = [str(value) for value in fact_ids or [] if str(value)]
        normalized_evidence = [str(value) for value in evidence_ids or [] if str(value)]
        normalized_documents = [str(value) for value in document_refs or [] if str(value)]
        if outcome == "evidence_accepted":
            unknown_facts = sorted(set(normalized_facts) - known_fact_ids)
            unknown_evidence = sorted(set(normalized_evidence) - known_evidence_ids)
            if unknown_facts:
                raise ValueError(f"unknown enrichment Fact IDs: {', '.join(unknown_facts)}")
            if unknown_evidence:
                raise ValueError(f"unknown enrichment Evidence IDs: {', '.join(unknown_evidence)}")
            store = self._knowledge_store()
            if normalized_documents and store is not None:
                unknown_documents = [value for value in normalized_documents if not store.document(value)]
                if unknown_documents:
                    raise ValueError(
                        f"unknown enrichment document refs: {', '.join(unknown_documents)}"
                    )
            if not (normalized_facts or normalized_evidence or normalized_documents):
                raise ValueError("accepted enrichment attempt requires a Fact, Evidence, or document ref")
            if task_id != "annual_filings" and not (normalized_facts or normalized_evidence):
                raise ValueError(
                    "accepted non-filing enrichment attempt requires registered Fact or Evidence IDs"
                )

        attempt = {
            "attempt_id": f"attempt_{hashlib.sha256(f'{task_id}|{query}|{outcome}|{utc_now()}'.encode()).hexdigest()[:16]}",
            "outcome": outcome,
            "query": str(query or "")[:1000],
            "document_refs": normalized_documents,
            "fact_ids": normalized_facts,
            "evidence_ids": normalized_evidence,
            "independence_groups": [str(value) for value in independence_groups or [] if str(value)],
            "covered_years": sorted({int(value) for value in covered_years or []}, reverse=True),
            "detail": str(detail or "")[:2000],
            "recorded_at": utc_now(),
        }
        task.setdefault("attempts", []).append(attempt)
        task["status"], task["reason_code"] = self._derive_enrichment_task_status(report_id, task)
        statuses = {str(item.get("status") or "planned") for item in tasks}
        plan["status"] = (
            "completed" if statuses.issubset(TERMINAL_ENRICHMENT_STATUSES)
            else "running" if any(item.get("attempts") for item in tasks)
            else "planned"
        )
        plan["updated_at"] = utc_now()
        _atomic_json(self._enrichment_plan_path(report_id), plan)
        self._write_workspace_manifest(self.require(report_id))
        return {
            "plan_id": plan.get("plan_id"),
            "plan_status": plan.get("status"),
            "task": task,
        }

    def _section_path(self, report_id: str, section_id: str) -> Path:
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", section_id):
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
        for section_id in _profile_section_ids(record.profile):
            section = self._read_section(record.report_id, section_id)
            sections[section_id] = {
                "status": section.status if section else "missing",
                "content_hash": section.content_hash if section else None,
                "source_report_id": section.source_report_id if section else None,
                "issues": section.validation_issues if section else ["workspace_missing_section"],
            }
        enrichment = self.enrichment_plan(record.report_id)
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
                "monitoring_bundle_draft": (
                    "submitted" if self._monitoring_draft_path(record.report_id).exists()
                    else "empty_default"
                ),
                "research_enrichment": (
                    {
                        "plan_id": enrichment.get("plan_id"),
                        "status": enrichment.get("status"),
                        "tasks": {
                            str(item.get("task_id")): {
                                "status": item.get("status"),
                                "reason_code": item.get("reason_code"),
                                "attempt_count": len(item.get("attempts") or []),
                            }
                            for item in enrichment.get("tasks") or []
                            if isinstance(item, dict)
                        },
                    }
                    if enrichment
                    else None
                ),
                "sections": sections,
                "updated_at": utc_now(),
            },
        )

    def _legacy_sections(self, report_id: str, profile: str) -> dict[str, str]:
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
            profile_aliases = {
                _normalized_heading(title): section_id
                for section_id, title in _profile_sections(profile)
            }
            section_id = profile_aliases.get(heading) or _SECTION_ALIASES.get(heading)
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
        section_ids = _profile_section_ids(record.profile)
        parent_sections = {
            section_id: self._read_section(parent.report_id, section_id)
            for section_id in section_ids
        }
        if not any(parent_sections.values()):
            legacy = self._legacy_sections(parent.report_id, record.profile)
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
                for section_id in section_ids
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
        symbol: str = "",
        security_name: str = "",
        security_name_source: str = "",
    ) -> DeepReportRecord:
        profile_definition = get_report_profile(profile)
        section_ids = _profile_section_ids(profile)
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
                if parent.profile != profile:
                    raise ValueError("parent report profile does not match revision profile")
                revision = parent.revision + 1
            if revision_mode not in {"initial", "full_refresh", "section_revision", "repair"}:
                raise ValueError(f"unsupported revision mode: {revision_mode}")
            requested_sections = list(dict.fromkeys(revision_sections or []))
            invalid_sections = sorted(set(requested_sections) - section_ids)
            if invalid_sections:
                raise ValueError(f"unknown revision sections: {', '.join(invalid_sections)}")
            record = DeepReportRecord(
                session_id=session_id,
                attempt_id=attempt_id,
                profile=profile,
                instrument_type=(
                    "etf" if profile == "etf_deep_research"
                    else "index" if profile == "index_deep_research"
                    else "company_equity"
                ),
                request_content=request_content,
                report_date=datetime.now(_SHANGHAI).date().isoformat(),
                symbol=parent.symbol if parent is not None else str(symbol or "").upper(),
                security_name=(
                    parent.security_name if parent is not None else str(security_name or "")
                ),
                security_name_source=(
                    parent.security_name_source
                    if parent is not None else str(security_name_source or "")
                ),
                security_name_aliases=(
                    list(parent.security_name_aliases) if parent is not None else []
                ),
                identity_snapshot_id=(
                    parent.identity_snapshot_id if parent is not None else None
                ),
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
                research_coverage=(
                    json.loads(json.dumps(parent.research_coverage))
                    if parent is not None
                    and revision_mode in {"repair", "section_revision"}
                    else {}
                ),
                history_delta=(
                    {
                        "base_report_id": parent.report_id,
                        "added": [],
                        "updated": [],
                        "confirmed": [],
                        "superseded": [],
                        "contradicted": [],
                        "stale": [],
                        "still_unverified": [],
                    }
                    if parent is not None
                    and revision_mode in {"repair", "section_revision"}
                    else {}
                ),
                analysis_modules=(
                    {
                        key: ModuleResult(**asdict(value))
                        for key, value in parent.analysis_modules.items()
                    }
                    if parent is not None
                    else {
                        key: ModuleResult(status="pending")
                        for key, _ in profile_definition["required_sections"]
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
        legacy_narrative = details.pop("narrative_section", None)
        legacy_deterministic = details.pop("deterministic_analysis", None)
        details.update({
            key: value for key, value in payload.items()
            if key not in {
                "status", "coverage", "coverage_ratio", "reason", "details",
                "availability", "validation", "reason_code", "missing_items",
                "narrative_result", "deterministic_result",
                "module_id", "selection_id", "resolution_id",
                "universe_snapshot_id", "selected_count",
                "selected_weight_coverage", "explanation_coverage",
                "research_coverage", "fully_supported_coverage",
                "reusable_count", "partial_reusable_count", "stale_count",
                "missing_count", "conflicted_count", "selected_components",
            }
        })
        def projection(value: Any) -> dict[str, Any] | None:
            if not value:
                return None
            normalized = DeepReportService._module_result(value)
            merged_details = dict(normalized.details)
            for nested in (normalized.deterministic_result, normalized.narrative_result):
                if not isinstance(nested, dict):
                    continue
                for key, nested_value in dict(nested.get("details") or {}).items():
                    merged_details.setdefault(key, nested_value)
            return {
                "availability": normalized.availability,
                "validation": normalized.validation,
                "coverage": normalized.coverage,
                "reason_code": normalized.reason_code,
                "missing_items": list(normalized.missing_items),
                "details": merged_details,
            }
        result = ModuleResult(
            status=status,  # type: ignore[arg-type]
            coverage=float(coverage) if isinstance(coverage, (int, float)) else None,
            reason=str(payload.get("reason")) if payload.get("reason") else None,
            details=details,
            availability=payload.get("availability"),
            validation=payload.get("validation"),
            reason_code=(
                str(payload.get("reason_code")) if payload.get("reason_code") else None
            ),
            missing_items=[
                str(item) for item in payload.get("missing_items") or [] if str(item)
            ],
            narrative_result=projection(
                payload.get("narrative_result") or legacy_narrative
            ),
            deterministic_result=projection(
                payload.get("deterministic_result") or legacy_deterministic
            ),
            module_id=payload.get("module_id"),
            selection_id=payload.get("selection_id"),
            resolution_id=payload.get("resolution_id"),
            universe_snapshot_id=payload.get("universe_snapshot_id"),
            selected_count=payload.get("selected_count"),
            selected_weight_coverage=payload.get("selected_weight_coverage"),
            explanation_coverage=payload.get("explanation_coverage"),
            research_coverage=payload.get("research_coverage"),
            fully_supported_coverage=payload.get("fully_supported_coverage"),
            reusable_count=payload.get("reusable_count"),
            partial_reusable_count=payload.get("partial_reusable_count"),
            stale_count=payload.get("stale_count"),
            missing_count=payload.get("missing_count"),
            conflicted_count=payload.get("conflicted_count"),
            selected_components=[
                dict(item) for item in payload.get("selected_components") or []
                if isinstance(item, dict)
            ],
        )
        projections = [
            result.details,
            dict((result.deterministic_result or {}).get("details") or {}),
            dict((result.narrative_result or {}).get("details") or {}),
        ]
        for field_name in (
            "module_id", "selection_id", "resolution_id", "universe_snapshot_id",
            "selected_count", "selected_weight_coverage", "explanation_coverage",
            "research_coverage", "fully_supported_coverage", "reusable_count",
            "partial_reusable_count", "stale_count", "missing_count",
            "conflicted_count",
        ):
            if getattr(result, field_name) is not None:
                continue
            for source in projections:
                if source.get(field_name) is not None:
                    setattr(result, field_name, source[field_name])
                    break
        if not result.selected_components:
            for source in projections:
                raw_components = source.get("selected_components") or source.get("selected")
                if isinstance(raw_components, list):
                    result.selected_components = [
                        dict(item) for item in raw_components if isinstance(item, dict)
                    ]
                    break
        return result

    @staticmethod
    def _financial_fact_key(item: dict[str, Any]) -> tuple[str, str, str, str, str]:
        metadata = dict(item.get("metadata") or {})
        metric = str(item.get("metric") or "")
        unit = str(item.get("unit") or "").casefold()
        if metric in {"basic_eps", "diluted_eps"} and unit in {"cny", "rmb", "yuan"}:
            unit = "cny/share"
        scope = str(item.get("scope_key") or metadata.get("scope_key") or "")
        if not scope:
            scope = "consolidated"
        currency = str(
            item.get("currency") or metadata.get("currency") or ""
        ).upper()
        if not currency and unit in {
            "cny", "rmb", "yuan", "cny/share", "cny_per_share"
        }:
            currency = "CNY"
        return (
            metric,
            str(item.get("period") or ""),
            scope,
            unit,
            currency,
        )

    def _prefer_official_report_facts(
        self,
        symbol: str,
        facts: list[dict[str, Any]],
        evidence: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
        store = self._knowledge_store()
        if store is None:
            return facts, evidence, {}
        bundle = store.preferred_official_fact_bundle(symbol)
        official_facts = [
            dict(item) for item in bundle.get("facts") or [] if isinstance(item, dict)
        ]
        if not official_facts:
            return facts, evidence, {}
        official_by_key: dict[
            tuple[str, str, str, str, str], list[dict[str, Any]]
        ] = {}
        for item in official_facts:
            key = self._financial_fact_key(item)
            official_by_key.setdefault(key, []).append(item)
        replacements: dict[str, str] = {}
        for item in facts:
            if item.get("formula") or not item.get("fact_id"):
                continue
            candidates = official_by_key.get(self._financial_fact_key(item), [])
            exact = [
                candidate for candidate in candidates
                if str(candidate.get("value")) == str(item.get("value"))
                or (
                    _decimal(candidate.get("value")) is not None
                    and _decimal(candidate.get("value")) == _decimal(item.get("value"))
                )
            ]
            distinct_values = {
                str(_decimal(candidate.get("value")) or candidate.get("value"))
                for candidate in candidates
            }
            eligible = exact or (candidates if len(distinct_values) <= 1 else [])
            official = max(
                eligible,
                key=lambda candidate: len(candidate.get("evidence_ids") or []),
                default=None,
            )
            if official and official.get("fact_id") != item.get("fact_id"):
                replacements[str(item["fact_id"])] = str(official["fact_id"])
        if not replacements:
            return facts, evidence, {}
        official_ids = {str(item.get("fact_id")) for item in official_facts}
        merged_facts = [
            dict(item) for item in facts
            if str(item.get("fact_id") or "") not in replacements
            and str(item.get("fact_id") or "") not in official_ids
        ]
        merged_facts.extend(official_facts)
        official_by_id = {
            str(item.get("fact_id")): item for item in official_facts if item.get("fact_id")
        }
        for item in merged_facts:
            input_ids = [str(value) for value in item.get("input_fact_ids") or []]
            mapped_inputs = [replacements.get(value, value) for value in input_ids]
            if mapped_inputs != input_ids:
                item["input_fact_ids"] = mapped_inputs
                official_evidence = [
                    str(evidence_id)
                    for fact_id in mapped_inputs
                    for evidence_id in official_by_id.get(fact_id, {}).get("evidence_ids") or []
                    if str(evidence_id)
                ]
                item["evidence_ids"] = list(dict.fromkeys([
                    *official_evidence,
                    *(str(value) for value in item.get("evidence_ids") or [] if str(value)),
                ]))
                item.setdefault("metadata", {})["official_input_precedence_applied"] = True
        evidence_by_id = {
            str(item.get("evidence_id")): dict(item)
            for item in [*evidence, *(bundle.get("evidence") or [])]
            if isinstance(item, dict) and item.get("evidence_id")
        }
        return merged_facts, list(evidence_by_id.values()), replacements

    @staticmethod
    def _rewrite_fact_binding(
        body: str,
        old_fact_id: str,
        new_fact_id: str,
        new_fact: dict[str, Any],
    ) -> str:
        marker = re.escape(f"[Fact:{old_fact_id}]")
        pattern = re.compile(
            rf"(?P<prefix>[¥￥])?\s*(?P<value>-?[\d,]+(?:\.\d+)?)\s*"
            rf"(?P<unit>%|％|亿元|万元|千元|元|股|份|倍|家)?(?P<space>\s*){marker}"
        )

        def replace(match: re.Match[str]) -> str:
            unit = str(match.group("unit") or ("元" if match.group("prefix") else ""))
            expected = _fact_display_value(new_fact, unit)
            if expected is None:
                return match.group(0).replace(old_fact_id, new_fact_id)
            decimals = _display_decimals(match.group("value"))
            number = f"{expected:,.{decimals}f}" if "," in match.group("value") else f"{expected:.{decimals}f}"
            return (
                f"{match.group('prefix') or ''}{number}{match.group('unit') or ''}"
                f"{match.group('space')}[Fact:{new_fact_id}]"
            )

        rewritten = pattern.sub(replace, body)
        return rewritten.replace(f"[Fact:{old_fact_id}]", f"[Fact:{new_fact_id}]")

    def refresh_official_fact_precedence(self, report_id: str) -> dict[str, Any]:
        """Rebind a report workspace to already validated official Facts."""

        with self._lock:
            record = self.require(report_id)
            if record.profile != "equity_deep_research":
                return {"report_id": report_id, "replacement_count": 0}
            analysis_dir = self._dir(report_id) / "analysis"
            facts = _read_jsonl(analysis_dir / "facts.jsonl")
            evidence = _read_jsonl(analysis_dir / "evidence.jsonl")
            merged_facts, merged_evidence, replacements = self._prefer_official_report_facts(
                record.symbol, facts, evidence
            )
            if not replacements:
                return {"report_id": report_id, "replacement_count": 0}
            facts_by_id = {
                str(item.get("fact_id")): item for item in merged_facts if item.get("fact_id")
            }
            for section_id in _profile_section_ids(record.profile):
                section = self._read_section(report_id, section_id)
                if section is None:
                    continue
                body = section.body_markdown
                for old_fact_id, new_fact_id in replacements.items():
                    body = self._rewrite_fact_binding(
                        body,
                        old_fact_id,
                        new_fact_id,
                        facts_by_id.get(new_fact_id, {}),
                    )
                section.body_markdown = body
                section.fact_ids = sorted(set(_FACT_RE.findall(body)))
                section.content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
                self._write_section(report_id, section)
            _atomic_jsonl(analysis_dir / "facts.jsonl", merged_facts)
            _atomic_jsonl(analysis_dir / "evidence.jsonl", merged_evidence)
            index = _read_json(analysis_dir / "index.json")
            index["fact_count"] = len(merged_facts)
            index["evidence_count"] = len(merged_evidence)
            index.setdefault("source_statuses", {})["official_financial_precedence"] = {
                "status": "applied",
                "replacement_count": len(replacements),
            }
            _atomic_json(analysis_dir / "index.json", index)
            self._refresh_knowledge_state(record)
            record.updated_at = utc_now()
            self._write_workspace_manifest(record)
            self._write_manifest(record)
            return {
                "report_id": report_id,
                "replacement_count": len(replacements),
                "replacement_map": replacements,
            }

    def attach_analysis(self, report_id: str, analysis: dict[str, Any]) -> DeepReportRecord:
        """Persist the normalized financial snapshot and its complete ledgers."""

        with self._lock:
            record = self.require(report_id)
            if record.profile != "equity_deep_research":
                raise ValueError("financial analysis is only valid for equity_deep_research")
            if analysis.get("profile") not in {None, "equity_deep_research"}:
                raise ValueError("analysis profile does not match report profile")
            if analysis.get("status") not in {None, "ok"}:
                raise ValueError("cannot attach an unsuccessful financial analysis")

            analysis_dir = self._dir(report_id) / "analysis"
            snapshot = dict(analysis.get("snapshot") or {})
            facts = [dict(item) for item in (analysis.get("facts") or []) if isinstance(item, dict)]
            evidence = [dict(item) for item in (analysis.get("evidence") or []) if isinstance(item, dict)]
            facts, evidence, _official_replacements = self._prefer_official_report_facts(
                str(analysis.get("symbol") or snapshot.get("symbol") or record.symbol),
                facts,
                evidence,
            )
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

    def attach_etf_analysis(self, report_id: str, analysis: dict[str, Any]) -> DeepReportRecord:
        """Attach verified ETF Snapshot references without applying company gates."""

        with self._lock:
            record = self.require(report_id)
            if record.profile != "etf_deep_research":
                raise ValueError("ETF analysis is only valid for etf_deep_research")
            if analysis.get("profile") not in {None, "etf_deep_research"}:
                raise ValueError("analysis profile does not match report profile")
            snapshot = dict(analysis.get("snapshot") or {})
            snapshot_ids = dict(snapshot.get("snapshot_ids") or {})
            required_snapshot_types = {"identity", "universe", "market"}
            if not required_snapshot_types.issubset(snapshot_ids):
                missing = sorted(required_snapshot_types - set(snapshot_ids))
                raise ValueError(f"ETF analysis is missing snapshots: {', '.join(missing)}")
            if any(not str(snapshot_ids[key]).startswith("etfsnap_") for key in required_snapshot_types):
                raise ValueError("ETF analysis snapshot IDs are invalid")
            if snapshot.get("price_verified") is not True:
                raise ValueError("ETF market snapshot must contain a verified current price")
            coverage_ratio = float(snapshot.get("coverage_ratio") or 0.0)
            if coverage_ratio < 0.60:
                raise ValueError("ETF analysis coverage is below the publication floor")
            symbol = str(analysis.get("symbol") or snapshot.get("symbol") or "").upper()
            data_as_of = str(analysis.get("data_as_of") or snapshot.get("data_as_of") or "")
            if not symbol or not data_as_of:
                raise ValueError("ETF analysis requires symbol and data_as_of")

            analysis_dir = self._dir(report_id) / "analysis"
            shutil.rmtree(analysis_dir / "deterministic", ignore_errors=True)
            (analysis_dir / "report_audit.json").unlink(missing_ok=True)
            facts = [dict(item) for item in (analysis.get("facts") or []) if isinstance(item, dict)]
            evidence = [dict(item) for item in (analysis.get("evidence") or []) if isinstance(item, dict)]
            module_statuses = dict(analysis.get("module_statuses") or {})
            subject_profile = dict(snapshot.get("subject_profile") or {})
            identity_fields = dict(subject_profile.get("identity") or {})
            required_identity_fields = (
                "manager", "exchange", "tracked_index_code", "tracked_index_name",
            )
            identity_coverage = sum(
                (identity_fields.get(key) or {}).get("status") == "available"
                for key in required_identity_fields
            ) / len(required_identity_fields)
            module_statuses.setdefault("identity", {
                "status": (
                    "passed" if identity_coverage == 1.0
                    else "insufficient_evidence"
                ),
                "coverage": identity_coverage,
                "reason": (
                    None if identity_coverage == 1.0
                    else "required_identity_fields_missing"
                ),
            })
            module_statuses.setdefault("universe", {
                "status": "passed" if coverage_ratio >= 0.95 else "warning",
                "coverage": coverage_ratio,
            })
            module_statuses.setdefault("market_data", {"status": "passed", "coverage": 1.0})
            quality_status = str(analysis.get("quality_status") or (
                "passed" if coverage_ratio >= 0.95 else "passed_with_gaps"
            ))
            if quality_status not in {"passed", "passed_with_gaps", "failed_validation"}:
                raise ValueError("invalid ETF analysis quality status")
            canonical_name, name_source, name_aliases = _canonical_etf_name(
                analysis,
                snapshot,
            )

            _atomic_json(analysis_dir / "snapshot.json", snapshot)
            _atomic_jsonl(analysis_dir / "facts.jsonl", facts)
            _atomic_jsonl(analysis_dir / "evidence.jsonl", evidence)
            _atomic_json(analysis_dir / "reconciliations.json", analysis.get("reconciliations") or [])
            _atomic_json(analysis_dir / "alerts.json", analysis.get("alerts") or [])
            index = {
                "profile": "etf_deep_research",
                "symbol": symbol,
                "security_name": canonical_name or symbol,
                "security_name_source": name_source,
                "security_name_aliases": name_aliases,
                "data_as_of": data_as_of,
                "quality_status": quality_status,
                "snapshot_ids": snapshot_ids,
                "module_statuses": module_statuses,
                "source_statuses": analysis.get("source_statuses") or {},
                "research_status": analysis.get("research_status") or {},
                "fact_count": len(facts),
                "evidence_count": len(evidence),
                "attached_at": utc_now(),
            }
            _atomic_json(analysis_dir / "index.json", index)
            record.symbol = symbol
            record.security_name = str(index.get("security_name") or record.security_name)
            record.security_name_source = name_source
            record.security_name_aliases = name_aliases
            record.identity_snapshot_id = str(snapshot_ids.get("identity") or "") or None
            record.data_as_of = data_as_of
            record.quality_status = quality_status  # type: ignore[assignment]
            for key, raw in module_statuses.items():
                record.analysis_modules[str(key)] = self._module_result(raw)
            self._refresh_knowledge_state(record)
            record.updated_at = utc_now()
            self._write_manifest(record)
            return record

    def attach_etf_component_selection(
        self,
        report_id: str,
        selection: dict[str, Any],
    ) -> DeepReportRecord:
        """Attach a cached P4A selection without generating component prose."""

        with self._lock:
            record = self.require(report_id)
            if record.profile != "etf_deep_research":
                raise ValueError("ETF component selection requires etf_deep_research")
            analysis_dir = self._dir(report_id) / "analysis"
            index_path = analysis_dir / "index.json"
            if not index_path.exists():
                raise ValueError("attach the ETF analysis snapshot before P4A selection")
            selected = [
                dict(item) for item in (selection.get("selected") or [])
                if isinstance(item, dict)
            ]
            concentration = dict(selection.get("concentration") or {})
            selection_id = str(selection.get("selection_id") or "")
            quality = str(selection.get("quality") or "insufficient")
            if not selection_id.startswith("p4aselection_"):
                raise ValueError("invalid P4A selection ID")
            if quality not in {"complete", "partial", "insufficient"}:
                raise ValueError("invalid P4A selection quality")
            _atomic_json(analysis_dir / "holding_penetration_selection.json", selection)
            evidence_rows = _read_jsonl(analysis_dir / "evidence.jsonl")
            universe_evidence_ids = [
                str(item.get("evidence_id"))
                for item in evidence_rows
                if item.get("evidence_id")
                and any(
                    token in str(item.get("domain") or "").casefold()
                    for token in ("universe", "component", "holding", "index", "exposure")
                )
            ]
            if not universe_evidence_ids:
                universe_evidence_ids = [
                    str(item.get("evidence_id"))
                    for item in evidence_rows
                    if item.get("evidence_id")
                ][:8]
            period = str(record.data_as_of or _read_json(index_path).get("data_as_of") or "")
            fact_rows: list[dict[str, Any]] = []
            fact_ids: dict[str, Any] = {}

            def add_ratio_fact(metric: str, value: Any, *, metadata: dict[str, Any]) -> str:
                fact_id = _analysis_fact_id(
                    record.symbol,
                    selection_id,
                    metric,
                    metadata.get("component_symbol"),
                )
                scope_key = str(metadata.get("scope_key") or "")
                fact_rows.append({
                    "fact_id": fact_id,
                    "symbol": record.symbol,
                    "metric": metric,
                    "value": str(value if value is not None else ""),
                    "unit": "ratio",
                    "period": period,
                    "scope_key": scope_key,
                    "formula": "ETF P4A deterministic selection",
                    "input_fact_ids": [],
                    "evidence_ids": universe_evidence_ids,
                    "calculation_version": "etf-p4a-v1",
                    "validation_status": "pass" if quality == "complete" else "warning",
                    "metadata": {
                        "selection_id": selection_id,
                        **metadata,
                    },
                })
                return fact_id

            fact_ids["observed_weight_coverage"] = add_ratio_fact(
                "etf_observed_weight_coverage",
                concentration.get("observed_weight_coverage"),
                metadata={"source": "concentration"},
            )
            fact_ids["selected_weight_coverage"] = add_ratio_fact(
                "etf_selected_weight_coverage",
                selection.get("selected_weight_coverage"),
                metadata={"source": "selection"},
            )
            fact_ids["explanation_coverage"] = add_ratio_fact(
                "etf_explanation_coverage",
                selection.get("explanation_coverage"),
                metadata={"source": "selection"},
            )
            component_fact_ids: dict[str, str] = {}
            for item in selected:
                component_symbol = str(item.get("symbol") or "").upper()
                if not component_symbol:
                    continue
                component_fact_ids[component_symbol] = add_ratio_fact(
                    "etf_component_weight",
                    item.get("weight"),
                    metadata={
                        "source": "selection_component",
                        "scope_key": component_symbol,
                        "component_symbol": component_symbol,
                        "component_name": item.get("name"),
                    },
                )
            fact_ids["component_weights"] = component_fact_ids
            _upsert_jsonl_rows(analysis_dir / "facts.jsonl", fact_rows, key="fact_id")
            module_status = (
                "passed" if quality == "complete"
                else "warning" if quality == "partial"
                else "insufficient_evidence"
            )
            module = ModuleResult(
                status=module_status,  # type: ignore[arg-type]
                coverage=float(selection.get("selected_weight_coverage") or 0.0),
                reason=(
                    None if quality == "complete"
                    else "partial_component_universe" if quality == "partial"
                    else "component_universe_missing"
                ),
                module_id="holding_penetration",
                selection_id=selection_id,
                universe_snapshot_id=str(
                    dict(
                        dict(_read_json(analysis_dir / "snapshot.json") or {}).get(
                            "snapshot_ids"
                        ) or {}
                    ).get("universe") or ""
                ) or None,
                selected_count=len(selected),
                selected_weight_coverage=(
                    float(selection.get("selected_weight_coverage"))
                    if isinstance(selection.get("selected_weight_coverage"), (int, float))
                    else None
                ),
                explanation_coverage=(
                    float(selection.get("explanation_coverage"))
                    if isinstance(selection.get("explanation_coverage"), (int, float))
                    else None
                ),
                selected_components=selected,
                details={
                    "selection_id": selection_id,
                    "input_fingerprint": selection.get("input_fingerprint"),
                    "concentration_class": concentration.get("concentration_class"),
                    "selected_symbols": [str(item.get("symbol") or "") for item in selected],
                    "selected_count": len(selected),
                    "explanation_coverage": selection.get("explanation_coverage"),
                    "stop_reason": selection.get("stop_reason"),
                    "warnings": selection.get("warnings") or [],
                    "fact_ids": fact_ids,
                    "source_evidence_ids": universe_evidence_ids,
                    "model_calls": 0,
                },
            )
            record.analysis_modules["holding_penetration"] = module
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index.setdefault("module_statuses", {})["holding_penetration"] = asdict(module)
            index["fact_count"] = len(_read_jsonl(analysis_dir / "facts.jsonl"))
            _atomic_json(index_path, index)
            self._refresh_knowledge_state(record)
            record.updated_at = utc_now()
            self._write_manifest(record)
            return record

    def attach_component_digest_resolution(
        self,
        report_id: str,
        resolution: dict[str, Any],
        materialization: dict[str, Any] | None = None,
    ) -> DeepReportRecord:
        """Attach a P4B1 resolution as analysis state only.

        This method deliberately does not compile sections, create ``report.md``,
        render a PDF, or register a report-library Artifact.
        """

        with self._lock:
            record = self.require(report_id)
            if record.profile != "etf_deep_research":
                raise ValueError("component digest resolution requires etf_deep_research")
            analysis_dir = self._dir(report_id) / "analysis"
            index_path = analysis_dir / "index.json"
            if not index_path.exists():
                raise ValueError("attach the ETF analysis snapshot before component research")
            resolution_id = str(resolution.get("resolution_id") or "")
            selection_id = str(resolution.get("selection_id") or "")
            if not resolution_id.startswith("componentresolution_"):
                raise ValueError("invalid component digest resolution ID")
            if not selection_id.startswith("p4aselection_"):
                raise ValueError("invalid P4A selection ID in component resolution")
            if any(int(resolution.get(key) or 0) != 0 for key in (
                "model_calls", "input_tokens", "output_tokens",
            )):
                raise ValueError("P4B1 component resolution must have zero model and token usage")
            existing_selection = record.analysis_modules.get("holding_penetration")
            if (
                existing_selection is not None
                and (
                    existing_selection.selection_id
                    or existing_selection.details.get("selection_id")
                ) != selection_id
            ):
                raise ValueError("component resolution does not match attached P4A selection")

            bindings = [
                dict(item) for item in (resolution.get("bindings") or [])
                if isinstance(item, dict)
            ]
            digest_ids = sorted({
                str(item) for item in (resolution.get("digest_ids") or []) if str(item)
            })
            binding_ids = [
                str(item.get("binding_id") or "") for item in bindings
                if str(item.get("binding_id") or "")
            ]
            status_symbols = {
                status: [
                    str(item.get("component_symbol") or "")
                    for item in bindings if item.get("digest_status") == status
                ]
                for status in (
                    "reusable", "partial_reusable", "stale", "missing", "conflicted"
                )
            }
            _atomic_json(analysis_dir / "component_digest_resolution.json", resolution)
            if materialization is None and digest_ids:
                try:
                    from src.reports.component_research import (
                        get_component_research_service,
                    )

                    materialization = get_component_research_service().materialize_resolution(
                        ComponentDigestResolution.from_dict(resolution)
                    )
                except (KeyError, OSError, TypeError, ValueError):
                    materialization = None
            if materialization:
                if str(materialization.get("resolution_id") or "") != resolution_id:
                    raise ValueError("component materialization does not match resolution")
                materialized_digests = dict(materialization.get("digests") or {})
                unknown_digests = set(materialized_digests) - set(digest_ids)
                if unknown_digests:
                    raise ValueError("component materialization contains unknown digests")
                _atomic_json(
                    analysis_dir / "component_research_digests.json",
                    materialized_digests,
                )
                _atomic_json(
                    analysis_dir / "component_research_claims.json",
                    materialization.get("claims") or [],
                )
                _upsert_jsonl_rows(
                    analysis_dir / "facts.jsonl",
                    [
                        dict(item) for item in (materialization.get("facts") or [])
                        if isinstance(item, dict)
                    ],
                    key="fact_id",
                )
                _upsert_jsonl_rows(
                    analysis_dir / "evidence.jsonl",
                    [
                        dict(item) for item in (materialization.get("evidence") or [])
                        if isinstance(item, dict)
                    ],
                    key="evidence_id",
                )
            research_weight = sum(
                float(item.get("component_weight") or 0.0)
                for item in bindings
                if item.get("digest_status") in {"reusable", "partial_reusable"}
            )
            fully_supported_weight = sum(
                float(item.get("component_weight") or 0.0)
                for item in bindings
                if item.get("digest_status") == "reusable"
            )
            selected_weight = sum(
                max(0.0, float(item.get("component_weight") or 0.0))
                for item in bindings
            )
            research_coverage = (
                min(1.0, research_weight / selected_weight)
                if selected_weight > 0 else None
            )
            fully_supported_coverage = (
                min(1.0, fully_supported_weight / selected_weight)
                if selected_weight > 0 else None
            )
            selection_module = record.analysis_modules.get("holding_penetration")
            selection_fact_ids = dict(
                (selection_module.details if selection_module else {}).get("fact_ids") or {}
            )
            source_evidence_ids = list(
                (selection_module.details if selection_module else {}).get(
                    "source_evidence_ids"
                ) or []
            )
            fact_rows: list[dict[str, Any]] = []
            coverage_fact_ids: dict[str, str] = {}
            if research_coverage is not None:
                coverage_fact_ids["research_coverage"] = _analysis_fact_id(
                    record.symbol, resolution_id, "etf_component_research_coverage"
                )
                fact_rows.append({
                    "fact_id": coverage_fact_ids["research_coverage"],
                    "symbol": record.symbol,
                    "metric": "etf_component_research_coverage",
                    "value": str(research_coverage),
                    "unit": "ratio",
                    "period": str(record.data_as_of or ""),
                    "formula": "(reusable + partial_reusable selected weight) / selected weight",
                    "input_fact_ids": list(
                        dict(selection_fact_ids.get("component_weights") or {}).values()
                    ),
                    "evidence_ids": source_evidence_ids,
                    "calculation_version": "etf-p4b-report-bridge-v1",
                    "validation_status": "pass",
                    "metadata": {"resolution_id": resolution_id},
                })
                coverage_fact_ids["fully_supported_coverage"] = _analysis_fact_id(
                    record.symbol, resolution_id, "etf_component_fully_supported_coverage"
                )
                fact_rows.append({
                    "fact_id": coverage_fact_ids["fully_supported_coverage"],
                    "symbol": record.symbol,
                    "metric": "etf_component_fully_supported_coverage",
                    "value": str(fully_supported_coverage),
                    "unit": "ratio",
                    "period": str(record.data_as_of or ""),
                    "formula": "reusable selected weight / selected weight",
                    "input_fact_ids": list(
                        dict(selection_fact_ids.get("component_weights") or {}).values()
                    ),
                    "evidence_ids": source_evidence_ids,
                    "calculation_version": "etf-p4b-report-bridge-v1",
                    "validation_status": "pass",
                    "metadata": {"resolution_id": resolution_id},
                })
                _upsert_jsonl_rows(
                    analysis_dir / "facts.jsonl", fact_rows, key="fact_id"
                )
            module = ModuleResult(
                status=(
                    "warning" if (
                        status_symbols["partial_reusable"]
                        or status_symbols["stale"]
                        or status_symbols["missing"]
                        or status_symbols["conflicted"]
                    )
                    else "passed"
                ),
                coverage=float(resolution.get("reuse_ratio") or 0.0),
                reason=(
                    "component_research_conflicted" if status_symbols["conflicted"]
                    else "component_research_gaps" if (
                        status_symbols["partial_reusable"]
                        or status_symbols["stale"]
                        or status_symbols["missing"]
                    )
                    else None
                ),
                module_id="component_research",
                selection_id=selection_id,
                resolution_id=resolution_id,
                selected_count=int(resolution.get("selected_count") or 0),
                research_coverage=research_coverage,
                fully_supported_coverage=fully_supported_coverage,
                reusable_count=int(resolution.get("reusable_count") or 0),
                partial_reusable_count=int(
                    resolution.get("partial_reusable_count") or 0
                ),
                stale_count=int(resolution.get("stale_count") or 0),
                missing_count=int(resolution.get("missing_count") or 0),
                conflicted_count=int(resolution.get("conflicted_count") or 0),
                selected_components=bindings,
                details={
                    "resolution_id": resolution_id,
                    "selection_id": selection_id,
                    "selected_count": int(resolution.get("selected_count") or 0),
                    "reusable_count": int(resolution.get("reusable_count") or 0),
                    "partial_reusable_count": int(
                        resolution.get("partial_reusable_count") or 0
                    ),
                    "stale_count": int(resolution.get("stale_count") or 0),
                    "missing_count": int(resolution.get("missing_count") or 0),
                    "conflicted_count": int(resolution.get("conflicted_count") or 0),
                    "digest_ids": digest_ids,
                    "binding_ids": binding_ids,
                    "reuse_ratio": float(resolution.get("reuse_ratio") or 0.0),
                    "research_coverage": research_coverage,
                    "fully_supported_coverage": fully_supported_coverage,
                    "coverage_fact_ids": coverage_fact_ids,
                    "status_symbols": status_symbols,
                    "estimated_avoided_model_calls": int(
                        resolution.get("estimated_avoided_model_calls") or 0
                    ),
                    "model_calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                },
            )
            record.analysis_modules["component_research"] = module
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index.setdefault("module_statuses", {})["component_research"] = asdict(module)
            index["component_research"] = {
                "resolution_id": resolution_id,
                "selection_id": selection_id,
                "digest_ids": digest_ids,
                "binding_ids": binding_ids,
                "research_coverage": research_coverage,
                "fully_supported_coverage": fully_supported_coverage,
            }
            index["fact_count"] = len(_read_jsonl(analysis_dir / "facts.jsonl"))
            _atomic_json(index_path, index)
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
                snapshot_kind = (
                    "ETF analysis snapshot"
                    if record.profile == "etf_deep_research"
                    else "financial snapshot"
                )
                raise ValueError(f"attach the {snapshot_kind} before deterministic results")
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
                snapshot_kind = (
                    "ETF analysis snapshot"
                    if record.profile == "etf_deep_research"
                    else "financial snapshot"
                )
                raise ValueError(f"attach the {snapshot_kind} before external evidence")
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
        snapshot = _read_json(analysis_dir / "snapshot.json")
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
            "snapshot": snapshot,
            "facts": facts,
            "evidence": evidence,
            "etf_component_selection": _read_json(
                analysis_dir / "holding_penetration_selection.json"
            ),
            "component_digest_resolution": _read_json(
                analysis_dir / "component_digest_resolution.json"
            ),
            "component_research_digests": _read_json(
                analysis_dir / "component_research_digests.json"
            ),
            "component_research_claims": _read_json_list(
                analysis_dir / "component_research_claims.json"
            ),
            "fact_ids": {str(item.get("fact_id")) for item in facts if item.get("fact_id")},
            "evidence_ids": {str(item.get("evidence_id")) for item in evidence if item.get("evidence_id")},
            "audit": audit,
        }

    @staticmethod
    def _fact_ratio(
        facts: list[dict[str, Any]],
        metric: str,
    ) -> float | None:
        for item in facts:
            if str(item.get("metric") or "") != metric:
                continue
            try:
                return float(item.get("value"))
            except (TypeError, ValueError):
                return None
        return None

    def _hydrate_etf_module_contract(
        self,
        record: DeepReportRecord,
        context: dict[str, Any],
    ) -> None:
        """Restore the flat P4A/P4B contract from immutable analysis rows.

        Older revisions can contain a valid deterministic P4A result nested
        under an evidence-gap narrative module.  The narrative gap must not
        erase the independently verifiable selection.  Prefer the dedicated
        selection/resolution files when present and reconstruct the same
        contract from their persisted Facts/module details for legacy reports.
        """

        facts = [
            dict(item) for item in context.get("facts") or []
            if isinstance(item, dict)
        ]
        snapshot_ids = dict(
            dict(context.get("snapshot") or {}).get("snapshot_ids") or {}
        )
        selection = dict(context.get("etf_component_selection") or {})
        holding_existing = record.analysis_modules.get("holding_penetration")
        holding_details: dict[str, Any] = {}
        if holding_existing is not None:
            holding_details.update(holding_existing.details)
            holding_details.update(
                dict((holding_existing.deterministic_result or {}).get("details") or {})
            )
        selected = [
            dict(item) for item in selection.get("selected") or []
            if isinstance(item, dict)
        ]
        component_facts = [
            item for item in facts
            if str(item.get("metric") or "") == "etf_component_weight"
        ]
        if not selected:
            for item in component_facts:
                metadata = dict(item.get("metadata") or {})
                try:
                    weight = float(item.get("value"))
                except (TypeError, ValueError):
                    continue
                symbol = str(
                    item.get("scope_key")
                    or metadata.get("component_symbol")
                    or ""
                ).upper()
                if not symbol:
                    continue
                selected.append({
                    "symbol": symbol,
                    "name": str(metadata.get("component_name") or symbol),
                    "weight": weight,
                })
        selection_id = str(
            selection.get("selection_id")
            or (holding_existing.selection_id if holding_existing else "")
            or holding_details.get("selection_id")
            or next((
                dict(item.get("metadata") or {}).get("selection_id")
                for item in component_facts
                if dict(item.get("metadata") or {}).get("selection_id")
            ), "")
        )
        selected_weight_coverage = selection.get("selected_weight_coverage")
        if not isinstance(selected_weight_coverage, (int, float)):
            selected_weight_coverage = self._fact_ratio(
                facts, "etf_selected_weight_coverage"
            )
        explanation_coverage = selection.get("explanation_coverage")
        if not isinstance(explanation_coverage, (int, float)):
            explanation_coverage = self._fact_ratio(
                facts, "etf_explanation_coverage"
            )
        selection_quality = str(selection.get("quality") or "")
        selection_facts_passed = bool(component_facts) and all(
            str(item.get("validation_status") or "") in {"pass", "warning"}
            for item in component_facts
        )
        selected_weight_reconciles = bool(
            selected_weight_coverage is not None
            and abs(
                sum(float(item.get("weight") or 0.0) for item in selected)
                - float(selected_weight_coverage)
            ) <= 1e-8
        )
        deterministic_passed = bool(
            holding_existing
            and (
                str(
                    (holding_existing.deterministic_result or {}).get("validation")
                    or ""
                ) == "passed"
                or str(
                    (holding_existing.deterministic_result or {}).get("availability")
                    or ""
                ) == "complete"
            )
        )
        selection_complete = bool(
            selected
            and selected_weight_coverage is not None
            and selection_id
            and (
                selection_quality == "complete"
                or deterministic_passed
                or (
                    selection_facts_passed
                    and selected_weight_reconciles
                    and snapshot_ids.get("universe")
                )
            )
        )
        if selected:
            holding_status = "passed" if selection_complete else "warning"
            holding_missing = [] if selection_complete else ["成份选择完整性待核验"]
            record.analysis_modules["holding_penetration"] = ModuleResult(
                status=holding_status,  # type: ignore[arg-type]
                coverage=(
                    float(selected_weight_coverage)
                    if selected_weight_coverage is not None else None
                ),
                reason=None if selection_complete else "partial_component_universe",
                availability="complete" if selection_complete else "partial",
                validation="passed" if selection_complete else "warning",
                reason_code=(
                    None if selection_complete else "partial_component_universe"
                ),
                missing_items=holding_missing,
                deterministic_result={
                    "availability": "complete" if selection_complete else "partial",
                    "validation": "passed" if selection_complete else "warning",
                    "coverage": (
                        float(selected_weight_coverage)
                        if selected_weight_coverage is not None else None
                    ),
                    "reason_code": (
                        None if selection_complete else "partial_component_universe"
                    ),
                    "missing_items": holding_missing,
                    "details": holding_details,
                },
                module_id="holding_penetration",
                selection_id=selection_id or None,
                universe_snapshot_id=str(snapshot_ids.get("universe") or "") or None,
                selected_count=len(selected),
                selected_weight_coverage=(
                    float(selected_weight_coverage)
                    if selected_weight_coverage is not None else None
                ),
                explanation_coverage=(
                    float(explanation_coverage)
                    if isinstance(explanation_coverage, (int, float)) else None
                ),
                selected_components=selected,
                details={
                    **holding_details,
                    "selection_id": selection_id or None,
                    "selected_count": len(selected),
                    "selected_weight_coverage": selected_weight_coverage,
                    "explanation_coverage": explanation_coverage,
                    "selected_components": selected,
                },
            )

        resolution = dict(context.get("component_digest_resolution") or {})
        research_existing = record.analysis_modules.get("component_research")
        research_details = dict(research_existing.details if research_existing else {})
        resolution_id = str(
            resolution.get("resolution_id")
            or (research_existing.resolution_id if research_existing else "")
            or research_details.get("resolution_id")
            or ""
        )
        bindings = [
            dict(item) for item in resolution.get("bindings") or []
            if isinstance(item, dict)
        ]
        status_symbols = {
            str(key): {str(symbol).upper() for symbol in value or []}
            for key, value in dict(research_details.get("status_symbols") or {}).items()
        }
        if not bindings and selected:
            for item in selected:
                symbol = str(item.get("symbol") or "").upper()
                status = next(
                    (key for key, symbols in status_symbols.items() if symbol in symbols),
                    "missing",
                )
                bindings.append({
                    "component_symbol": symbol,
                    "component_name": item.get("name"),
                    "selected_weight": item.get("weight"),
                    "digest_status": status,
                })
        def count_field(name: str) -> int:
            raw = resolution.get(name)
            if raw is None and research_existing is not None:
                raw = getattr(research_existing, name, None)
            if raw is None:
                raw = research_details.get(name)
            return int(raw or 0)

        if research_existing is not None or resolution:
            reusable_count = count_field("reusable_count")
            partial_count = count_field("partial_reusable_count")
            stale_count = count_field("stale_count")
            missing_count = count_field("missing_count")
            conflicted_count = count_field("conflicted_count")
            selected_count = int(
                resolution.get("selected_count")
                or (research_existing.selected_count if research_existing else 0)
                or research_details.get("selected_count")
                or len(bindings)
            )
            research_coverage = resolution.get("research_coverage")
            if not isinstance(research_coverage, (int, float)):
                research_coverage = (
                    research_existing.research_coverage
                    if research_existing else None
                )
            if not isinstance(research_coverage, (int, float)):
                research_coverage = self._fact_ratio(
                    facts, "etf_component_research_coverage"
                )
            fully_supported = resolution.get("fully_supported_coverage")
            if not isinstance(fully_supported, (int, float)):
                fully_supported = (
                    research_existing.fully_supported_coverage
                    if research_existing else None
                )
            if not isinstance(fully_supported, (int, float)):
                fully_supported = self._fact_ratio(
                    facts, "etf_component_fully_supported_coverage"
                )
            has_gaps = any((partial_count, stale_count, missing_count, conflicted_count))
            missing_items = [
                (
                    f"{item.get('component_name') or item.get('component_symbol')}："
                    f"{ {'partial_reusable': '部分研究可复用', 'stale': '研究已过期', 'missing': '研究缺失', 'conflicted': '研究证据冲突'}.get(str(item.get('digest_status') or ''), '研究待补充') }"
                )
                for item in bindings
                if str(item.get("digest_status") or "") != "reusable"
            ]
            record.analysis_modules["component_research"] = ModuleResult(
                status="warning" if has_gaps else "passed",
                coverage=(
                    float(research_coverage)
                    if isinstance(research_coverage, (int, float)) else None
                ),
                reason="component_research_gaps" if has_gaps else None,
                availability="partial" if has_gaps else "complete",
                validation="warning" if has_gaps else "passed",
                reason_code="component_research_gaps" if has_gaps else None,
                missing_items=missing_items,
                module_id="component_research",
                selection_id=selection_id or None,
                resolution_id=resolution_id or None,
                universe_snapshot_id=str(snapshot_ids.get("universe") or "") or None,
                selected_count=selected_count,
                research_coverage=(
                    float(research_coverage)
                    if isinstance(research_coverage, (int, float)) else None
                ),
                fully_supported_coverage=(
                    float(fully_supported)
                    if isinstance(fully_supported, (int, float)) else None
                ),
                reusable_count=reusable_count,
                partial_reusable_count=partial_count,
                stale_count=stale_count,
                missing_count=missing_count,
                conflicted_count=conflicted_count,
                selected_components=bindings,
                details={
                    **research_details,
                    "selection_id": selection_id or None,
                    "resolution_id": resolution_id or None,
                    "selected_count": selected_count,
                    "research_coverage": research_coverage,
                    "fully_supported_coverage": fully_supported,
                    "selected_components": bindings,
                },
            )

        index = dict(context.get("index") or {})
        module_statuses = dict(index.get("module_statuses") or {})
        for module_id in ("holding_penetration", "component_research"):
            module = record.analysis_modules.get(module_id)
            if module is not None:
                module_statuses[module_id] = asdict(module)
        index["module_statuses"] = module_statuses
        index["updated_at"] = utc_now()
        context["index"] = index
        _atomic_json(self._dir(record.report_id) / "analysis" / "index.json", index)
        record.updated_at = utc_now()
        self._write_manifest(record)

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
        profile_headings = _profile_section_headings(record.profile)
        profile_section_ids = set(profile_headings)
        requested_sections = section_ids or list(profile_headings)
        invalid = sorted(set(requested_sections) - profile_section_ids)
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
            "research_enrichment": self.enrichment_plan(report_id),
            "subject_profile": (
                dict(context.get("snapshot") or {}).get("subject_profile")
                if record.profile == "etf_deep_research"
                else None
            ),
        }
        if record.profile == "etf_deep_research":
            payload["etf_penetration"] = _etf_penetration_view(
                context,
                record.analysis_modules,
            )
            payload["etf_readiness"] = dict(record.etf_readiness)
            payload["pipeline_checks"] = {
                key: asdict(value) for key, value in record.pipeline_checks.items()
            }
            payload["report_sections"] = {
                key: asdict(value) for key, value in record.report_sections.items()
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
        issues.extend(
            f"unregistered_reader_term:{term}"
            for term in reader_machine_terms(body_markdown)
        )
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

        record = self.require(report_id)
        enrichment = self.enrichment_plan(report_id)
        if enrichment:
            tasks_by_id = {
                str(item.get("task_id")): item
                for item in enrichment.get("tasks") or []
                if isinstance(item, dict)
            }
            for task_id in SECTION_TASKS.get(section_id, ()):
                task = tasks_by_id.get(task_id)
                if task and str(task.get("status") or "planned") not in TERMINAL_ENRICHMENT_STATUSES:
                    issues.append(f"enrichment_task_not_exhausted:{task_id}")
        module_statuses = dict(context["index"].get("module_statuses") or {})
        if record.profile == "equity_deep_research":
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
            record = self.require(report_id)
            if (
                record.profile == "etf_deep_research"
                and not self._analysis_context(report_id).get("available")
            ):
                raise ValueError(
                    "etf_analysis_snapshot_required_before_section_submission"
                )
            if section_id not in _profile_section_ids(record.profile):
                raise ValueError(f"unknown report section: {section_id}")
            issues, details = self._validate_section_body(report_id, section_id, body_markdown)
            rejected_path = (
                self._workspace_dir(report_id)
                / "rejected_sections"
                / f"{section_id}.json"
            )
            if issues:
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
            rejected_path.unlink(missing_ok=True)
            record.pipeline_state = "drafting_sections"
            record.updated_at = utc_now()
            self._write_workspace_manifest(record)
            self._write_manifest(record)
            return section

    def submit_monitoring_bundle(
        self,
        report_id: str,
        *,
        monitoring_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist only the model-authored structural context and candidates."""

        with self._lock:
            record = self.require(report_id)
            context = self._analysis_context(report_id)
            if record.profile == "etf_deep_research" and not context.get("available"):
                raise ValueError(
                    "etf_analysis_snapshot_required_before_monitoring_submission"
                )
            normalized = validate_structural_monitoring_draft(
                monitoring_bundle,
                available_facts={
                    str(item.get("fact_id")): item
                    for item in context.get("facts") or []
                    if item.get("fact_id")
                },
                available_evidence_ids=set(context.get("evidence_ids") or []),
                price_tick_size=Decimal(
                    "0.001" if record.profile == "etf_deep_research" else "0.01"
                ),
            )
            _atomic_json(self._monitoring_draft_path(report_id), normalized)
            record.updated_at = utc_now()
            self._write_workspace_manifest(record)
            self._write_manifest(record)
            return {
                "status": "accepted",
                "candidate_count": len(normalized.get("candidates") or []),
                "activation_policy": "manual_confirmation_required",
                "trade_execution": "forbidden",
            }

    def _workspace_content(self, record: DeepReportRecord) -> tuple[str, list[str]]:
        self._sync_etf_readiness(record)
        lines = [
            f"# {report_display_title(record)}",
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
                mapped = [metric_reader_label(value) for value in values if value]
                mapped = [value for value in mapped if value]
                return "、".join(list(dict.fromkeys(mapped))[:8]) or "无重大项目"

            lines.extend([
                f"- 新增或发生变化：{labels([*added_metrics, *changed_metrics])}。",
                f"- 由新一轮资料再次确认：{labels(confirmed_metrics)}。",
                f"- 尚待复核或本次未覆盖：{labels(stale_metrics)}。",
                "- 历史结论仅作为上次判断展示，本次事实仍以当前登记资料和数据为准。",
            ])
        workspace_issues: list[str] = []
        context = self._analysis_context(record.report_id)
        deterministic_modules = dict(context["index"].get("module_statuses") or {})
        etf_penetration = (
            _etf_penetration_markdown(
                _etf_penetration_view(context, record.analysis_modules)
            )
            if record.profile == "etf_deep_research"
            else ""
        )
        etf_product = (
            _etf_product_markdown(context)
            if record.profile == "etf_deep_research"
            else ""
        )
        for section_id, heading in _profile_sections(record.profile):
            section = self._read_section(record.report_id, section_id)
            section_body = (
                _without_redundant_section_heading(section.body_markdown, heading)
                if section is not None
                else ""
            )
            lines.extend(["", f"## {heading}", ""])
            if section_id == "index_and_product" and etf_product:
                lines.append(etf_product)
                if section is not None and section.status == "passed" and section_body:
                    lines.extend(["", "### 研究解释", "", section_body])
                continue
            if section_id == "holding_penetration" and etf_penetration:
                lines.append(etf_penetration)
                if section is not None and section.status == "passed":
                    lines.extend(["", "### 研究解释", "", section_body])
                # P4A/P4B output is compiler-owned. Missing optional prose must
                # not turn an otherwise valid ETF report into a diagnostic.
                continue
            if section is not None and section.status == "passed":
                lines.append(section_body)
                continue
            implied_gap = (
                record.profile == "equity_deep_research"
                and
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

        if record.profile == "etf_deep_research":
            method_lines = [
                "本报告分别核验 ETF 产品、跟踪指数、成分权重、市场量价、份额与持有人披露；重要数字均与已登记数据逐项对应。",
                "份额变化仅表示申购赎回结果；除非有官方持有人披露，不归因于国家队、证金或其他特定主体。",
                "成分穿透采用自适应选择和覆盖率停止规则，不代表其余成分不重要，也不构成交易指令。",
            ]
        else:
            method_lines = [
                "本报告分别核验公司披露、市场数据和外部研究资料；重要数字均与已登记数据逐项对应。",
                "财务异常仅表示需要进一步核查，不等同于财务造假判断。",
                "市值隐含长期利润反推采用净利润近似口径，不是完整现金流折现模型，也不是目标价模型。",
            ]
        enrichment = self.enrichment_plan(record.report_id)
        if enrichment:
            task_labels = {
                "annual_filings": "历史年报",
                "business_position": "产业与竞争资料",
                "consensus": "连续前瞻盈利预测",
                "terminal_inputs": "长期经营情景输入",
            }
            reason_labels = {
                "public_source_not_found": "已完成规定检索但没有找到可核验公开来源",
                "retrieval_failed_after_retry": "候选来源存在，但正文读取在重试后仍失败",
                "evidence_rejected_by_validation": "候选资料无法重放关键事实，未纳入正式证据",
                "provider_unavailable": "指定数据提供方当前不可用",
                "partial_coverage": "仅取得部分所需期间或字段",
            }
            method_lines.append("本次扩展资料搜集按独立任务记录结果：")
            for task in enrichment.get("tasks") or []:
                if not isinstance(task, dict):
                    continue
                label = task_labels.get(str(task.get("task_id")), str(task.get("intent") or "资料补齐"))
                status = str(task.get("status") or "planned")
                if status == "satisfied":
                    method_lines.append(f"- {label}：已取得并通过证据校验。")
                elif status == "exhausted":
                    reason = reason_labels.get(
                        str(task.get("reason_code") or ""),
                        "已完成检索，但资料仍不足以形成正式结论",
                    )
                    method_lines.append(f"- {label}：{reason}。")
                else:
                    method_lines.append(f"- {label}：采集任务未完成，因此没有发布相关结论。")
        lines.extend([
            "",
            f"## {_COMPILER_METHOD_HEADING}",
            "",
            *method_lines,
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
        citation_fact_ids: dict[str, list[str]] | None = None,
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
            cited_ids.extend(_HIDDEN_FACT_RE.findall(line))
            cited_ids.extend(
                (reader_fact_ids or {}).get(alias, "")
                for alias in _READER_FACT_RE.findall(line)
            )
            for citation_number in _READER_CITATION_RE.findall(line):
                cited_ids.extend((citation_fact_ids or {}).get(citation_number, []))
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
        if record.profile == "etf_deep_research":
            canonical_name, name_source, name_aliases = _canonical_etf_name(
                dict(context.get("index") or {}),
                dict(context.get("snapshot") or {}),
            )
            if canonical_name:
                record.security_name = canonical_name
                record.security_name_source = name_source
                record.security_name_aliases = name_aliases
                snapshot_ids = dict(
                    dict(context.get("snapshot") or {}).get("snapshot_ids") or {}
                )
                record.identity_snapshot_id = (
                    str(snapshot_ids.get("identity") or "") or None
                )
                index = dict(context.get("index") or {})
                index.update({
                    "security_name": canonical_name,
                    "security_name_source": name_source,
                    "security_name_aliases": name_aliases,
                    "snapshot_ids": snapshot_ids,
                })
                context["index"] = index
                _atomic_json(self._dir(report_id) / "analysis" / "index.json", index)
                self._write_manifest(record)
            if (
                int(record.schema_version or 0) < 3
                or "holding_penetration" not in record.analysis_modules
                or "component_research" not in record.analysis_modules
            ):
                # Legacy migration/recovery only. Schema-v3 reports persist the
                # canonical selection and resolution modules when those events
                # are attached, so normal compilation must not rewrite copies.
                self._hydrate_etf_module_contract(record, context)
        raw_content, workspace_issues = self._workspace_content(record)
        facts_by_id = {
            str(item.get("fact_id")): item
            for item in context["facts"]
            if item.get("fact_id")
        }
        evidence_by_id = {
            str(item.get("evidence_id")): item
            for item in context["evidence"]
            if item.get("evidence_id")
        }
        references = _reference_plan(
            raw_content,
            facts_by_id,
            evidence_by_id,
            self._internal_report_reference,
        )
        citation_fact_ids: dict[str, list[str]] = {}
        citation_evidence_ids: dict[str, list[str]] = {}
        for fact_id, citation_numbers in dict(
            references.get("fact_citation_map") or {}
        ).items():
            normalized_numbers = (
                citation_numbers
                if isinstance(citation_numbers, list)
                else [citation_numbers]
            )
            for citation_number in normalized_numbers:
                citation_fact_ids.setdefault(str(citation_number), []).append(str(fact_id))
        for evidence_id, citation_numbers in dict(
            references.get("evidence_citation_map") or {}
        ).items():
            normalized_numbers = (
                citation_numbers
                if isinstance(citation_numbers, list)
                else [citation_numbers]
            )
            for citation_number in normalized_numbers:
                citation_evidence_ids.setdefault(str(citation_number), []).append(
                    str(evidence_id)
                )
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
        claim_support: dict[str, Any] = {}

        # Quality metadata is part of the published bytes. Recompile after
        # validation until the badge and missing-module summary agree with the
        # result, and rerun the audit against those exact final bytes.
        for _ in range(3):
            compiled = self._compile_report(
                record,
                raw_content,
                context,
                references=references,
                quality_status=desired_quality,
                analysis_modules=desired_modules,
            )
            audit = self._service_numeric_audit(
                compiled,
                context["facts"],
                reader_fact_ids=reader_fact_ids,
                citation_fact_ids=citation_fact_ids,
            )
            validation = self.validate(
                compiled,
                profile=record.profile,
                analysis_required=True,
                analysis_available=bool(context["available"]),
                available_fact_ids=context["fact_ids"],
                available_evidence_ids=context["evidence_ids"],
                available_facts=list(context.get("facts") or []),
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
            report_dir = self._dir(report_id)
            self._write_claims(
                report_dir / "claims.jsonl",
                compiled,
                reader_fact_ids=reader_fact_ids,
                reader_evidence_ids=reader_evidence_ids,
                citation_fact_ids=citation_fact_ids,
                citation_evidence_ids=citation_evidence_ids,
            )
            claim_support = build_claim_support_audit(
                _read_jsonl(report_dir / "claims.jsonl"),
                list(context.get("evidence") or []),
                list(context.get("facts") or []),
            )
            _atomic_json(report_dir / "claim_support_audit.json", claim_support)
            support_issues = _claim_support_gate_issues(claim_support)
            if support_issues:
                validation["issues"] = list(dict.fromkeys([
                    *validation["issues"], *support_issues,
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
            "claim_support": claim_support,
            "workspace_issues": workspace_issues,
            "references": {
                **references,
                "content_sha256": hashlib.sha256(compiled.encode("utf-8")).hexdigest(),
            },
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
            deterministic_result = raw.deterministic_result
        else:
            payload = dict(raw or {})
            status = str(payload.get("status") or "")
            details = dict(payload.get("details") or {})
            deterministic_result = payload.get("deterministic_result")
        if status != "failed_validation":
            return False
        if module_id == "financial_quality":
            deterministic = dict(
                deterministic_result
                or details.get("deterministic_analysis")
                or {}
            )
            if deterministic:
                return str(
                    deterministic.get("validation")
                    or deterministic.get("status")
                    or ""
                ) in {"failed", "failed_validation"}
        return True

    @staticmethod
    def _recoverable_validation(
        issues: list[str],
        modules: dict[str, Any],
        profile: str = "equity_deep_research",
    ) -> bool:
        if any(
            DeepReportService._hard_module_failed(key, modules.get(key))
            for key in _hard_validation_module_ids(profile)
        ):
            return False
        recoverable_prefixes = (
            "workspace_section_not_ready", "missing_required_section", "unknown_fact_reference",
            "unknown_evidence_reference", "uncited_material_numbers", "numeric_",
            "claim_support_gate",
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
                record.profile,
            )
        )

    def repair_context(
        self,
        report_id: str,
        issues: list[str] | None = None,
    ) -> dict[str, Any]:
        """Map report-level validation failures back to repairable workspace sections."""

        record = self.require(report_id)
        validation_issues = list(issues if issues is not None else record.validation_issues)
        workspace = self.inspect_workspace(report_id)
        target_ids = {
            section_id
            for section_id, section in dict(workspace.get("sections") or {}).items()
            if str((section or {}).get("status") or "missing") != "passed"
        }
        headings = _profile_section_headings(record.profile)
        heading_to_id = {heading: section_id for section_id, heading in headings.items()}
        claims = {
            str(item.get("claim_id")): item
            for item in _read_jsonl(self._dir(report_id) / "claims.jsonl")
            if item.get("claim_id")
        }
        support_audit = _read_json(self._dir(report_id) / "claim_support_audit.json")
        audited_claims = {
            str(item.get("claim_id")): item
            for item in list(support_audit.get("claims") or [])
            if isinstance(item, dict) and item.get("claim_id")
        }
        claim_repairs: list[dict[str, Any]] = []
        for issue in validation_issues:
            if not str(issue).startswith("claim_support_gate:"):
                continue
            _, claim_id, support_status, support_reason = (str(issue).split(":", 3) + [""] * 4)[:4]
            claim = dict(claims.get(claim_id) or {})
            audited = dict(audited_claims.get(claim_id) or {})
            section_label = str(claim.get("section_id") or audited.get("section_id") or "")
            section_id = (
                section_label
                if section_label in headings
                else heading_to_id.get(section_label, "")
            )
            if section_id:
                target_ids.add(section_id)
            claim_repairs.append({
                "claim_id": claim_id,
                "claim_type": str(claim.get("claim_type") or ""),
                "text": str(claim.get("text") or "")[:800],
                "section_id": section_id,
                "section_label": section_label,
                "support_status": support_status,
                "support_reason": support_reason,
            })

        ordered_targets = [section_id for section_id in headings if section_id in target_ids]
        prompt_lines = [
            "[PARENT_REPORT_VALIDATION_REPAIR]",
            "父报告未通过的是全局发布审查；章节局部 status=passed 不代表该章节可以原样复用。",
            "需要重新提交的章节：" + ("、".join(ordered_targets) or "按下列校验问题定位"),
            "父报告校验问题：",
            *[f"- {issue}" for issue in validation_issues],
        ]
        for item in claim_repairs:
            prompt_lines.extend([
                (
                    f"- 问题 Claim {item['claim_id']}，章节 {item['section_id'] or item['section_label'] or '未知'}，"
                    f"支持状态 {item['support_status']}/{item['support_reason']}：{item['text']}"
                ),
                "  必须补充独立权威来源，或删除该推断并改写为明确的数据缺口；"
                "不得仅把 inference/opinion 改标为 fact/calculation 来绕过审查。",
            ])
        prompt_lines.append(
            "先 inspect 上述章节，再用 submit_section 重写并提交；不得把同一问题句原样带入新 revision。"
        )
        return {
            "section_ids": ordered_targets,
            "claim_repairs": claim_repairs,
            "validation_issues": validation_issues,
            "prompt_block": "\n".join(prompt_lines),
        }

    def repair_blockers(self, report_id: str) -> list[str]:
        """Return deterministic hard gates that a section-only repair cannot resolve."""

        record = self.require(report_id)
        blockers: list[str] = []
        for module_id in sorted(_hard_validation_module_ids(record.profile)):
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
        for section_id, heading in _profile_sections(record.profile):
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
            reference_payload = dict(result.get("references") or {})
            citation_map = dict(result.get("citation_map") or {})
            reader_fact_ids = dict(citation_map.get("reader_fact_ids") or {})
            reader_evidence_ids = dict(citation_map.get("reader_evidence_ids") or {})
            citation_fact_ids = {
                str(item.get("citation_number")): [str(value) for value in item.get("fact_ids") or []]
                for item in reference_payload.get("citations") or []
                if item.get("citation_number") is not None
            }
            citation_evidence_ids = {
                str(item.get("citation_number")): [str(value) for value in item.get("evidence_ids") or []]
                for item in reference_payload.get("citations") or []
                if item.get("citation_number") is not None
            }
            _ensure_failed_validation_issues(validation)
            record.validation_issues = list(validation.get("issues") or [])
            record.quality_status = validation.get("quality_status", "failed_validation")
            record.analysis_modules = {
                key: ModuleResult(**value)
                for key, value in dict(validation.get("analysis_modules") or {}).items()
            }
            self._sync_etf_readiness(record)
            record.status = "completed"
            record.updated_at = utc_now()
            report_dir = self._dir(report_id)
            _atomic_json(report_dir / "validation.json", validation)
            _atomic_json(report_dir / "numeric_audit.json", audit)

            if record.quality_status == "failed_validation":
                record.pipeline_state = "diagnostic"
                record.delivery_kind = "diagnostic"
                (report_dir / "monitoring_bundle.json").unlink(missing_ok=True)
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
                    "materialization_status": (
                        "materialized" if (report_dir / "report.pdf").exists()
                        else "generatable"
                    ),
                    "previewable": False,
                })
                record.artifacts = artifacts

            reference_payload.update({
                "fact_ids": sorted(
                    set(reference_payload.get("fact_citation_map") or {})
                    or set(reader_fact_ids.values())
                    or set(_FACT_RE.findall(content))
                ),
                "evidence_ids": sorted(
                    set(reference_payload.get("evidence_citation_map") or {})
                    or set(reader_evidence_ids.values())
                    or set(_EVIDENCE_RE.findall(content))
                ),
                "reader_fact_ids": reader_fact_ids,
                "reader_evidence_ids": reader_evidence_ids,
                "content_sha256": (
                    reference_payload.get("content_sha256")
                    or hashlib.sha256(content.encode("utf-8")).hexdigest()
                ),
                "audit_mode": audit.get("audit_mode"),
            })
            _atomic_json(report_dir / "references.json", reference_payload)
            self._write_claims(
                report_dir / "claims.jsonl",
                content,
                reader_fact_ids=reader_fact_ids,
                reader_evidence_ids=reader_evidence_ids,
                citation_fact_ids=citation_fact_ids,
                citation_evidence_ids=citation_evidence_ids,
            )
            analysis = self._analysis_context(report_id)
            claims = _read_jsonl(report_dir / "claims.jsonl")
            claim_support = build_claim_support_audit(
                claims,
                list(analysis.get("evidence") or []),
                list(analysis.get("facts") or []),
            )
            _atomic_json(report_dir / "claim_support_audit.json", claim_support)
            support_issues = _claim_support_gate_issues(claim_support)
            if support_issues and record.quality_status != "failed_validation":
                record.validation_issues.extend(support_issues)
                record.quality_status = "failed_validation"
                record.pipeline_state = "diagnostic"
                record.delivery_kind = "diagnostic"
                validation["issues"] = list(record.validation_issues)
                validation["quality_status"] = "failed_validation"
                self._sync_etf_readiness(record)
                _atomic_json(report_dir / "validation.json", validation)
                report_path = report_dir / "report.md"
                if report_path.exists():
                    report_path.replace(report_dir / "rejected_draft.md")
                diagnostic_path = report_dir / "diagnostic.md"
                diagnostic_path.write_text(
                    _diagnostic_markdown(record, record.validation_issues),
                    encoding="utf-8",
                )
                (report_dir / "report.pdf").unlink(missing_ok=True)
                (report_dir / "monitoring_bundle.json").unlink(missing_ok=True)
                record.artifacts = [{
                    "artifact_id": "diagnostic",
                    "artifact_type": "text/markdown",
                    "artifact_role": "diagnostic",
                    "filename": report_pdf_filename(record).removesuffix(".pdf") + "_诊断.md",
                    "path": str(diagnostic_path),
                    "available": True,
                    "previewable": True,
                }]
            if record.quality_status != "failed_validation":
                draft = _read_json(self._monitoring_draft_path(report_id))
                monitoring_bundle = build_structural_monitoring_bundle(
                    record=record,
                    draft=draft if isinstance(draft, dict) else {},
                    snapshot=dict(analysis.get("snapshot") or {}),
                    facts=list(analysis.get("facts") or []),
                    evidence=list(analysis.get("evidence") or []),
                    claims=claims,
                    claim_support=claim_support,
                    references=reference_payload,
                    report_sha256=str(reference_payload.get("content_sha256") or ""),
                )
                monitoring_path = report_dir / "monitoring_bundle.json"
                _atomic_json(monitoring_path, monitoring_bundle)
                record.artifacts.append({
                    "artifact_id": "monitoring_bundle",
                    "artifact_type": "application/json",
                    "artifact_role": "monitoring",
                    "filename": (
                        report_pdf_filename(record).removesuffix(".pdf")
                        + "_结构监控.json"
                    ),
                    "path": str(monitoring_path),
                    "available": True,
                    "previewable": True,
                })
            knowledge = self._knowledge_store()
            if knowledge is not None and record.symbol:
                prior = self._prior_report_for(record)
                coverage_id = str(record.research_coverage.get("coverage_snapshot_id") or "") or None
                record.history_delta = knowledge.link_report(
                    report_id=record.report_id,
                    revision=record.revision,
                    symbol=record.symbol,
                    quality_status=record.quality_status,
                    evidence=list(analysis.get("evidence") or []),
                    facts=list(analysis.get("facts") or []),
                    claims=claims,
                    claim_support=claim_support,
                    coverage_snapshot_id=coverage_id,
                    base_report_id=prior.report_id if prior else None,
                )
                live_conflicts = knowledge.unresolved_conflicts(
                    record.symbol,
                    fact_ids=[
                        str(item.get("fact_id") or "")
                        for item in analysis.get("facts") or []
                    ],
                )
                record.research_coverage["material_conflicts"] = live_conflicts
                record.history_delta["contradicted"] = live_conflicts
            self._sync_etf_readiness(record)
            from src.reports.catalog import register_deep_report_safely

            register_deep_report_safely(record)
            self._write_workspace_manifest(record)
            self._write_manifest(record)
            return record

    @staticmethod
    def _compile_report(
        record: DeepReportRecord,
        content: str,
        analysis_context: dict[str, Any],
        *,
        references: dict[str, Any] | None = None,
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
        facts = {
            str(item.get("fact_id")): item
            for item in analysis_context.get("facts") or []
            if item.get("fact_id")
        }
        reference_payload = dict(references or _reference_plan(
            content,
            facts,
            {
                str(item.get("evidence_id")): item
                for item in analysis_context.get("evidence") or []
                if item.get("evidence_id")
            },
        ))
        compiled = _readerize_report_text(compiled, reference_payload, facts)
        index_lines = ["", "---"]
        if fact_refs:
            index_lines.extend(["", "### 数据依据", ""])
            index_lines.extend([
                "| 指标 | 数值 | 期间/时点 | 口径 |",
                "|---|---:|---|---|",
            ])
            for fact_id in fact_refs:
                item = facts.get(fact_id) or {}
                metric = str(item.get("metric") or "已核实数据")
                label = metric_reader_label(metric)
                if label is None:
                    continue
                metadata = dict(item.get("metadata") or {})
                scope = _reader_scope_label(
                    metadata.get("scope")
                    or metadata.get("scope_key")
                    or item.get("scope_key")
                )
                index_lines.append(
                    f"| {label.replace('|', '｜')} | {_reader_fact_value(item)} | "
                    f"{_reader_datetime(item.get('period') or '期间未明').replace('|', '｜')} | "
                    f"{scope.replace('|', '｜')} |"
                )
        citations = list(reference_payload.get("citations") or [])
        if citations:
            index_lines.extend(["", "### 参考资料", ""])
            for item in citations:
                number = int(item.get("citation_number") or 0)
                publisher, title = _reader_reference_identity(item)
                url = str(item.get("public_url") or "").strip()
                title_label = f"[{title}]({url})" if url else title
                published = _reader_datetime(
                    item.get("published_at") or item.get("data_as_of") or "时间未明"
                )
                retrieved = item.get("retrieved_at")
                source_level = _SOURCE_LEVEL_READER_LABELS.get(
                    str(item.get("source_level") or ""),
                    "来源等级未登记",
                )
                suffix_parts = [
                    f"来源等级：{source_level}",
                    f"发布或数据时间：{published}",
                ]
                if retrieved:
                    suffix_parts.append(f"获取时间：{_reader_datetime(retrieved)}")
                index_lines.append(
                    f"[^{number}]: {publisher}，{title_label}；{'；'.join(suffix_parts)}。"
                )
        if fact_refs or citations:
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
                profile=record.profile,
                analysis_required=error is None,
                analysis_available=bool(analysis_context["available"]),
                available_fact_ids=analysis_context["fact_ids"],
                available_evidence_ids=analysis_context["evidence_ids"],
                available_facts=list(analysis_context.get("facts") or []),
                deterministic_modules=dict(analysis_context["index"].get("module_statuses") or {}),
                audit_result=analysis_context.get("audit"),
            )
            title = _TITLE_RE.search(content)
            if title:
                record.symbol = title.group(2).strip().upper()
                canonical_name = str(analysis_context["index"].get("security_name") or "")
                canonical_source = str(
                    analysis_context["index"].get("security_name_source") or ""
                )
                record.security_name = (
                    canonical_name
                    if canonical_name and canonical_source.startswith("official_")
                    else title.group(1).strip()
                )
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
            self._sync_etf_readiness(record)
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
            facts_by_id = {
                str(item.get("fact_id")): item
                for item in analysis_context.get("facts") or []
                if item.get("fact_id")
            }
            evidence_by_id = {
                str(item.get("evidence_id")): item
                for item in analysis_context.get("evidence") or []
                if item.get("evidence_id")
            }
            reference_payload = _reference_plan(
                published_content,
                facts_by_id,
                evidence_by_id,
                self._internal_report_reference,
            )
            compiled_content = self._compile_report(
                record,
                published_content,
                analysis_context,
                references=reference_payload,
            )
            markdown_path = report_dir / ("diagnostic.md" if validation_failed else "report.md")
            markdown_path.write_text(compiled_content, encoding="utf-8")
            if validation_failed:
                (report_dir / "report.md").unlink(missing_ok=True)
            validation_path = report_dir / "validation.json"
            _atomic_json(validation_path, validation)
            references = {
                **reference_payload,
                "fact_ids": sorted(set(_FACT_RE.findall(published_content))),
                "evidence_ids": sorted(set(_EVIDENCE_RE.findall(published_content))),
                "source_content_hash": hashlib.sha256(published_content.encode("utf-8")).hexdigest(),
                "content_sha256": hashlib.sha256(compiled_content.encode("utf-8")).hexdigest(),
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
                citation_fact_ids={
                    str(item.get("citation_number")): [str(value) for value in item.get("fact_ids") or []]
                    for item in reference_payload.get("citations") or []
                    if item.get("citation_number") is not None
                },
                citation_evidence_ids={
                    str(item.get("citation_number")): [str(value) for value in item.get("evidence_ids") or []]
                    for item in reference_payload.get("citations") or []
                    if item.get("citation_number") is not None
                },
            )
            claim_support = build_claim_support_audit(
                _read_jsonl(report_dir / "claims.jsonl"),
                list(analysis_context.get("evidence") or []),
                list(analysis_context.get("facts") or []),
            )
            _atomic_json(report_dir / "claim_support_audit.json", claim_support)
            support_issues = _claim_support_gate_issues(claim_support)
            if support_issues and not validation_failed:
                validation_failed = True
                record.quality_status = "failed_validation"
                record.pipeline_state = "diagnostic"
                record.validation_issues.extend(support_issues)
                validation["quality_status"] = "failed_validation"
                validation["issues"] = list(record.validation_issues)
                self._sync_etf_readiness(record)
                _atomic_json(validation_path, validation)
                (report_dir / "rejected_draft.md").write_text(
                    compiled_content,
                    encoding="utf-8",
                )
                compiled_content = self._compile_report(
                    record,
                    _diagnostic_markdown(record, record.validation_issues),
                    analysis_context,
                )
                markdown_path = report_dir / "diagnostic.md"
                markdown_path.write_text(compiled_content, encoding="utf-8")
                (report_dir / "report.md").unlink(missing_ok=True)
                (report_dir / "report.pdf").unlink(missing_ok=True)
                references["content_sha256"] = hashlib.sha256(
                    compiled_content.encode("utf-8")
                ).hexdigest()
                _atomic_json(report_dir / "references.json", references)

            if validation_failed:
                (report_dir / "monitoring_bundle.json").unlink(missing_ok=True)
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
                monitoring_bundle = build_structural_monitoring_bundle(
                    record=record,
                    draft=_read_json(self._monitoring_draft_path(report_id)),
                    snapshot=dict(analysis_context.get("snapshot") or {}),
                    facts=list(analysis_context.get("facts") or []),
                    evidence=list(analysis_context.get("evidence") or []),
                    claims=_read_jsonl(report_dir / "claims.jsonl"),
                    claim_support=claim_support,
                    references=references,
                    report_sha256=str(references.get("content_sha256") or ""),
                )
                monitoring_path = report_dir / "monitoring_bundle.json"
                _atomic_json(monitoring_path, monitoring_bundle)
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
                    "materialization_status": (
                        "materialized" if (report_dir / "report.pdf").exists()
                        else "generatable"
                    ),
                    "previewable": False,
                }, {
                    "artifact_id": "monitoring_bundle",
                    "artifact_type": "application/json",
                    "artifact_role": "monitoring",
                    "filename": report_pdf_filename(record).removesuffix(".pdf") + "_结构监控.json",
                    "path": str(monitoring_path),
                    "available": True,
                    "previewable": True,
                }]
            knowledge = self._knowledge_store()
            if knowledge is not None and record.symbol:
                claims = _read_jsonl(report_dir / "claims.jsonl")
                prior = self._prior_report_for(record)
                coverage_id = str(
                    record.research_coverage.get("coverage_snapshot_id") or ""
                ) or None
                record.history_delta = knowledge.link_report(
                    report_id=record.report_id,
                    revision=record.revision,
                    symbol=record.symbol,
                    quality_status=record.quality_status,
                    evidence=list(analysis_context.get("evidence") or []),
                    facts=list(analysis_context.get("facts") or []),
                    claims=claims,
                    claim_support=claim_support,
                    coverage_snapshot_id=coverage_id,
                    base_report_id=prior.report_id if prior else None,
                )
                live_conflicts = knowledge.unresolved_conflicts(
                    record.symbol,
                    fact_ids=[
                        str(item.get("fact_id") or "")
                        for item in analysis_context.get("facts") or []
                    ],
                )
                record.research_coverage["material_conflicts"] = live_conflicts
                record.history_delta["contradicted"] = live_conflicts
            self._write_manifest(record)
            from src.reports.catalog import register_deep_report_safely

            register_deep_report_safely(record)
            return record

    def mark_failed(self, report_id: str, error: str, *, cancelled: bool = False) -> DeepReportRecord:
        record = self.require(report_id)
        diagnostic = (
            f"# {report_display_title(record)}\n\n"
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
        profile: str = "equity_deep_research",
        analysis_required: bool = False,
        analysis_available: bool = False,
        available_fact_ids: set[str] | None = None,
        available_evidence_ids: set[str] | None = None,
        available_facts: list[dict[str, Any]] | None = None,
        deterministic_modules: dict[str, Any] | None = None,
        audit_result: dict[str, Any] | None = None,
        referenced_fact_ids: set[str] | None = None,
        referenced_evidence_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        issues: list[str] = []
        modules: dict[str, dict[str, Any]] = {}
        profile_definition = get_report_profile(profile)
        issues.extend(
            f"unregistered_reader_term:{term}"
            for term in reader_machine_terms(content)
        )
        for section_id, heading in profile_definition["required_sections"]:
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
            issues.append(
                "title_must_include_fund_and_symbol"
                if profile == "etf_deep_research"
                else "title_must_include_company_and_symbol"
            )
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
            issues.append(
                "etf_analysis_snapshot_missing"
                if profile == "etf_deep_research"
                else "financial_analysis_snapshot_missing"
            )
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
        if analysis_available and available_facts is not None:
            fact_ids = {
                str(item.get("fact_id") or "")
                for item in available_facts
                if isinstance(item, dict) and item.get("fact_id")
            }
            for item in available_facts:
                if not isinstance(item, dict):
                    continue
                metadata = dict(item.get("metadata") or {})
                formula = str(item.get("formula") or "").strip()
                inputs = [str(value) for value in item.get("input_fact_ids") or [] if str(value)]
                is_derived = (
                    metadata.get("source_kind") == "derived"
                    or bool(formula and inputs)
                )
                if not is_derived:
                    continue
                if not formula or not inputs or any(value not in fact_ids for value in inputs):
                    issues.append(
                        "derived_fact_lineage_incomplete:"
                        + str(item.get("fact_id") or item.get("metric") or "unknown")
                    )
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
        if profile == "equity_deep_research":
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
            if raw_line.startswith(("### 数据依据", "### 引用索引", "### 参考资料")):
                inside_reference_index = True
            if inside_reference_index or raw_line.startswith("> -"):
                continue
            if raw_line.startswith("## "):
                inside_body = True
            if not inside_body or not _MATERIAL_NUMBER_RE.search(
                _numeric_audit_text(raw_line)
            ):
                continue
            if (
                _FACT_RE.search(raw_line)
                or _EVIDENCE_RE.search(raw_line)
                or _READER_FACT_RE.search(raw_line)
                or _READER_EVIDENCE_RE.search(raw_line)
                or _READER_CITATION_RE.search(raw_line)
                or _HIDDEN_FACT_RE.search(raw_line)
                or _HIDDEN_EVIDENCE_RE.search(raw_line)
            ):
                continue
            uncited_material_lines.append(line_number)
        if uncited_material_lines:
            preview = ",".join(str(value) for value in uncited_material_lines[:12])
            issues.append(f"uncited_material_numbers:{preview}")
        if profile == "equity_deep_research" and "DCF" in content.upper() and not any(
            guard in content for guard in ("不是完整", "非完整", "不构成完整", "并非完整")
        ):
            issues.append("dcf_limitation_not_disclosed")
        if profile == "equity_deep_research" and "市值隐含预期" in content and not any(
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
            narrative_result = self._module_result(narrative)
            deterministic_is_usable = (
                deterministic.validation != "failed"
                and deterministic.availability in {"complete", "partial"}
                and deterministic.status not in {"pending", "running", "not_requested"}
            )
            chosen = (
                deterministic
                if deterministic_is_usable
                or rank.get(deterministic.status, 3) > rank.get(narrative_status, 3)
                else narrative_result
            )
            chosen.narrative_result = {
                "availability": narrative_result.availability,
                "validation": narrative_result.validation,
                "coverage": narrative_result.coverage,
                "reason_code": narrative_result.reason_code,
                "missing_items": list(narrative_result.missing_items),
                "details": dict(narrative_result.details),
            }
            chosen.deterministic_result = {
                "availability": deterministic.availability,
                "validation": deterministic.validation,
                "coverage": deterministic.coverage,
                "reason_code": deterministic.reason_code,
                "missing_items": list(deterministic.missing_items),
                "details": dict(deterministic.details),
            }
            modules[str(key)] = asdict(chosen)

        header_block = re.split(r"^##\s+", content, maxsplit=1, flags=re.M)[0]
        if any(
            value["status"] in {"insufficient_evidence", "warning", "not_requested"}
            for value in modules.values()
        ) and not any(label in header_block for label in ("缺失模块", "尚待补充")):
            issues.append("missing_modules_summary_missing")

        declared = _QUALITY_RE.search(content)
        snapshot_missing_issue = (
            "etf_analysis_snapshot_missing"
            if profile == "etf_deep_research"
            else "financial_analysis_snapshot_missing"
        )
        title_issue = (
            "title_must_include_fund_and_symbol"
            if profile == "etf_deep_research"
            else "title_must_include_company_and_symbol"
        )
        hard_module_ids = (
            ("report_gate", "market_data", "identity", "universe")
            if profile == "etf_deep_research"
            else ("report_gate", "financial_quality", "market_data", "symbol_identity")
        )
        hard_fail = any(
            issue.startswith("missing_required_section")
            or issue.startswith("unregistered_reader_term")
            or issue.startswith("unknown_fact_reference")
            or issue.startswith("unknown_evidence_reference")
            or issue.startswith("derived_fact_lineage_incomplete")
            or issue in {
                title_issue,
                "missing_fact_references",
                "template_placeholder_detected",
                "probability_weighted_target_detected",
                "target_price_or_reasonable_value_detected",
                snapshot_missing_issue,
                "numeric_audit_missing_incomplete_or_content_mismatch",
            }
            for issue in issues
        ) or any(issue.startswith("uncited_material_numbers") for issue in issues) or any(
            modules.get(key, {}).get("status") == "failed_validation"
            for key in hard_module_ids
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
            record = DeepReportRecord.from_dict(payload)
            self._sync_etf_readiness(record)
            return record
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None

    def subject_profile(self, report_id: str) -> dict[str, Any] | None:
        """Return the immutable ETF profile actually bound to this revision."""

        record = self.get(report_id)
        if record is None or record.profile != "etf_deep_research":
            return None
        profile = dict(self._analysis_context(report_id).get("snapshot") or {}).get(
            "subject_profile"
        )
        return dict(profile) if isinstance(profile, dict) and profile else None

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
        elif artifact_id == "monitoring_bundle":
            if self.require(report_id).quality_status == "failed_validation":
                raise ValueError(
                    "monitoring bundle is unavailable because the report did not pass validation"
                )
            path = self._dir(report_id) / "monitoring_bundle.json"
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
                try:
                    content = self.read_markdown(report_id)
                    title = f"{record.security_name}（{record.symbol}）穿透式深度研究"
                    # The renderer owns the visual title. Remove the compiler H1
                    # from the derivative input so the first page does not repeat
                    # the same report title twice.
                    pdf_content = re.sub(r"^#\s+[^\n]+\n?", "", content, count=1)
                    pdf = renderer(title, pdf_content)
                    if not pdf.startswith(b"%PDF-"):
                        raise ValueError("PDF renderer returned an invalid document")
                    tmp = path.with_suffix(".pdf.tmp")
                    tmp.write_bytes(pdf)
                    tmp.replace(path)
                except Exception as exc:
                    for artifact in record.artifacts:
                        if artifact.get("artifact_id") == "pdf":
                            artifact["materialized"] = False
                            artifact["materialization_status"] = "failed"
                            artifact["materialization_error"] = str(exc)[:500]
                    record.updated_at = utc_now()
                    self._write_manifest(record)
                    try:
                        from src.reports.catalog import register_deep_report_safely

                        register_deep_report_safely(record)
                    except Exception:
                        pass
                    raise
                for artifact in record.artifacts:
                    if artifact.get("artifact_id") == "pdf":
                        artifact["available"] = True
                        artifact["materialized"] = True
                        artifact["materialization_status"] = "materialized"
                        artifact.pop("materialization_error", None)
                        artifact["path"] = str(path)
                record.updated_at = utc_now()
                self._write_manifest(record)
            else:
                for artifact in record.artifacts:
                    if artifact.get("artifact_id") == "pdf":
                        artifact["materialized"] = True
                        artifact["materialization_status"] = "materialized"
                        artifact.pop("materialization_error", None)
                record.updated_at = utc_now()
                self._write_manifest(record)
            try:
                from src.reports.catalog import register_deep_report_safely

                register_deep_report_safely(record)
            except Exception:
                # The immutable report manifest remains authoritative if the
                # optional catalog projection is temporarily unavailable.
                pass
            return path, record

    def _write_manifest(self, record: DeepReportRecord) -> None:
        self._sync_etf_readiness(record)
        record.schema_version = max(3, int(record.schema_version or 0))
        _atomic_json(self._manifest_path(record.report_id), record.to_dict())

    @staticmethod
    def _write_claims(
        path: Path,
        content: str,
        *,
        reader_fact_ids: dict[str, str] | None = None,
        reader_evidence_ids: dict[str, str] | None = None,
        citation_fact_ids: dict[str, list[str]] | None = None,
        citation_evidence_ids: dict[str, list[str]] | None = None,
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
            fact_ids.extend(_HIDDEN_FACT_RE.findall(line))
            evidence_ids.extend(_HIDDEN_EVIDENCE_RE.findall(line))
            fact_ids.extend(
                (reader_fact_ids or {}).get(alias, "")
                for alias in _READER_FACT_RE.findall(line)
            )
            evidence_ids.extend(
                (reader_evidence_ids or {}).get(alias, "")
                for alias in _READER_EVIDENCE_RE.findall(line)
            )
            for citation_number in _READER_CITATION_RE.findall(line):
                fact_ids.extend((citation_fact_ids or {}).get(citation_number, []))
                evidence_ids.extend((citation_evidence_ids or {}).get(citation_number, []))
            fact_ids = [value for value in fact_ids if value]
            evidence_ids = [value for value in evidence_ids if value]
            has_gap = "[data_gap]" in line or "证据说明" in line or "当前证据不足" in line
            if not fact_ids and not evidence_ids and not has_gap:
                continue
            clean_line = _HIDDEN_FACT_RE.sub("", line)
            clean_line = _HIDDEN_EVIDENCE_RE.sub("", clean_line)
            clean_line = _READER_CITATION_RE.sub("", clean_line).strip()
            claim_type = (
                "data_gap" if has_gap
                else "inference" if "[inference]" in clean_line or "研究判断" in clean_line
                else "calculation" if fact_ids
                else "fact"
            )
            claims.append(
                ClaimItem(
                    claim_id=f"claim_{hashlib.sha256(clean_line.encode('utf-8')).hexdigest()[:20]}",
                    text=clean_line,
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
