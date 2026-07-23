"""Persistent, deterministic structured extraction for official financial filings.

The immutable source content hash is the cache key.  A document is parsed once
per extractor version; successful, review-required, not-applicable, and failed
results are all persisted so later reports never repeat PDF text extraction or
OCR.  Current official PDF ingestion exposes a native text layer only, so this
extractor never performs OCR.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from .knowledge import ResearchKnowledgeStore, get_research_knowledge_store
from .source_ingestion import (
    CollectedSource,
    SourceIngestionService,
    market_for_symbol,
)


EXTRACTOR_ID = "official_financial_statement"
EXTRACTOR_VERSION = "v14"

_FINANCIAL_FILING_TERMS = (
    "年度报告",
    "半年度报告",
    "季度报告",
    "业绩快报",
    "业绩公告",
    "annual report",
    "interim report",
    "quarterly report",
    "results announcement",
    "10-k",
    "10-q",
    "20-f",
    "40-f",
)

_METRIC_ALIASES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "net_profit_parent",
        "income",
        (
            "扣除非经常性损益后归属于本行股东的净利润",
            "扣除非经常性损益后归属于上市公司股东的净利润",
            "归属于本行股东的净利润",
            "归属于母公司股东的净利润",
            "归属于上市公司股东的净利润",
            "profit attributable to owners of the company",
            "net income attributable to common shareholders",
        ),
    ),
    (
        "revenue",
        "income",
        (
            "营业总收入",
            "营业收入",
            "经营收入",
            "total operating income",
            "operating revenue",
            "total revenues",
            "net revenues",
            "revenue",
        ),
    ),
    (
        "operating_profit",
        "income",
        ("营业利润", "operating profit", "income from operations"),
    ),
    (
        "total_assets",
        "balance",
        ("资产总计", "资产合计", "资产总额", "总资产", "total assets"),
    ),
    (
        "total_liabilities",
        "balance",
        ("负债合计", "负债总额", "总负债", "total liabilities"),
    ),
    (
        "parent_equity",
        "balance",
        (
            "归属于本行股东权益",
            "归属于母公司股东权益",
            "归属于上市公司股东的净资产",
            "归属于上市公司股东的所有者权益",
            "equity attributable to owners of the company",
            "stockholders' equity",
        ),
    ),
    (
        "total_equity",
        "balance",
        (
            "股东权益合计",
            "所有者权益合计",
            "total equity",
        ),
    ),
    (
        "cfo",
        "cashflow",
        (
            "经营活动产生的现金流量净额",
            "net cash flows from operating activities",
            "net cash provided by operating activities",
        ),
    ),
    (
        "basic_eps",
        "indicators",
        ("基本每股收益", "basic earnings per share", "basic eps"),
    ),
    (
        "diluted_eps",
        "indicators",
        ("稀释每股收益", "diluted earnings per share", "diluted eps"),
    ),
    (
        "roe_reported",
        "indicators",
        (
            "加权平均净资产收益率",
            "平均净资产收益率",
            "return on equity",
        ),
    ),
    (
        "nonperforming_loan_ratio",
        "indicators",
        ("不良贷款率", "non-performing loan ratio", "nonperforming loan ratio"),
    ),
    (
        "provision_coverage_ratio",
        "indicators",
        ("拨备覆盖率", "provision coverage ratio"),
    ),
)

_RATIO_METRICS = {
    "roe_reported",
    "nonperforming_loan_ratio",
    "provision_coverage_ratio",
}
_PER_SHARE_METRICS = {"basic_eps", "diluted_eps"}

_FOOTNOTE_TERMS: dict[str, tuple[str, ...]] = {
    "basic_eps": ("基本每股收益", "basic earnings per share"),
    "diluted_eps": ("稀释每股收益", "diluted earnings per share"),
    "roe_reported": ("净资产收益率", "return on equity"),
    "nonperforming_loan_ratio": ("不良贷款率", "non-performing loan ratio"),
    "provision_coverage_ratio": ("拨备覆盖率", "provision coverage ratio"),
}

_UNIT_MARKERS: tuple[tuple[re.Pattern[str], Decimal, str], ...] = (
    (re.compile(r"人民币\s*百万元|rmb\s*(?:in\s+)?millions?", re.I), Decimal("1000000"), "CNY"),
    (re.compile(r"港币\s*百万元|hk\$\s*(?:in\s+)?millions?", re.I), Decimal("1000000"), "HKD"),
    (re.compile(r"美元\s*百万元|u\.?s\.?\s*dollars?\s*(?:in\s+)?millions?|\$\s*in\s*millions?", re.I), Decimal("1000000"), "USD"),
    (re.compile(r"人民币\s*亿元|单位\s*[:：]?\s*亿元", re.I), Decimal("100000000"), "CNY"),
    (re.compile(r"人民币\s*万元|单位\s*[:：]?\s*万元", re.I), Decimal("10000"), "CNY"),
    (re.compile(r"人民币\s*千元|单位\s*[:：]?\s*千元", re.I), Decimal("1000"), "CNY"),
    (re.compile(r"单位\s*[:：]\s*人民币元", re.I), Decimal("1"), "CNY"),
)

_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])\(?-?\d[\d,]*(?:\.\d+)?\)?%?")

_SEC_CONCEPTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "revenue": (
        "income",
        (
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
        ),
    ),
    "operating_profit": ("income", ("OperatingIncomeLoss",)),
    "net_profit_parent": (
        "income",
        ("NetIncomeLoss", "ProfitLoss"),
    ),
    "total_assets": ("balance", ("Assets",)),
    "total_liabilities": ("balance", ("Liabilities",)),
    "total_equity": (
        "balance",
        ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
    ),
    "cash": (
        "balance",
        ("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    ),
    "cfo": ("cashflow", ("NetCashProvidedByUsedInOperatingActivities",)),
    "capex": ("cashflow", ("PaymentsToAcquirePropertyPlantAndEquipment",)),
    "basic_eps": ("indicators", ("EarningsPerShareBasic",)),
    "diluted_eps": ("indicators", ("EarningsPerShareDiluted",)),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f") if normalized != normalized.to_integral() else str(normalized.quantize(Decimal("1")))


def _parse_number(raw: str) -> Decimal | None:
    value = str(raw or "").strip().replace(",", "")
    negative = value.startswith("(") and value.endswith(")")
    value = value.strip("()%")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return -parsed if negative else parsed


def _subject_code_present(subject_key: str, text: str) -> bool:
    code = str(subject_key or "").split(".", 1)[0].strip()
    if not code or code in {"PORTFOLIO", "MARKET"}:
        return False
    return re.search(rf"(?<!\d){re.escape(code)}(?!\d)", text, re.I) is not None


def _filing_type(title: str, text: str) -> str:
    title_descriptor = str(title or "").casefold()
    if "业绩快报" in title_descriptor or "results announcement" in title_descriptor:
        return "earnings_flash"
    if "半年度报告" in title_descriptor or "interim report" in title_descriptor:
        return "interim"
    if "第一季度" in title_descriptor or "q1" in title_descriptor:
        return "quarterly_q1"
    if "第三季度" in title_descriptor or "q3" in title_descriptor:
        return "quarterly_q3"
    if "季度报告" in title_descriptor or "10-q" in title_descriptor or "quarterly report" in title_descriptor:
        return "quarterly"
    if any(term in title_descriptor for term in ("年度报告", "annual report", "10-k", "20-f", "40-f")):
        return "annual"

    # The filing title is authoritative when it names a report type. Annual
    # reports commonly mention prior interim reports in their opening pages;
    # classifying the combined title/body text made those annual filings look
    # like half-year reports and assigned every metric to June 30.
    descriptor = str(text or "")[:5000].casefold()
    if "业绩快报" in descriptor or "results announcement" in descriptor:
        return "earnings_flash"
    if "半年度" in descriptor or "interim report" in descriptor:
        return "interim"
    if "第一季度" in descriptor or "q1" in descriptor:
        return "quarterly_q1"
    if "第三季度" in descriptor or "q3" in descriptor:
        return "quarterly_q3"
    if "季度" in descriptor or "10-q" in descriptor or "quarterly report" in descriptor:
        return "quarterly"
    if any(term in descriptor for term in ("年度报告", "annual report", "10-k", "20-f", "40-f")):
        return "annual"
    return "financial_filing"


def _reporting_period(title: str, text: str, filing_type: str) -> str:
    descriptor = f"{title}\n{text[:12000]}"
    years = [int(item) for item in re.findall(r"(?<!\d)(20\d{2})(?!\d)", descriptor)]
    year = years[0] if years else 0
    if year:
        suffix = {
            "annual": "12-31",
            "earnings_flash": "12-31",
            "interim": "06-30",
            "quarterly_q1": "03-31",
            "quarterly_q3": "09-30",
        }.get(filing_type)
        if suffix:
            return f"{year:04d}-{suffix}"
    chinese_date = re.search(r"(20\d{2})\s*年\s*(1[0-2]|[1-9])\s*月\s*(3[01]|[12]\d|[1-9])\s*日", descriptor)
    if chinese_date:
        return f"{int(chinese_date.group(1)):04d}-{int(chinese_date.group(2)):02d}-{int(chinese_date.group(3)):02d}"
    english_date = re.search(
        r"(?:ended|as\s+of)\s+(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2}),\s*(20\d{2})",
        descriptor,
        re.I,
    )
    if english_date:
        month = datetime.strptime(english_date.group(1)[:3], "%b").month
        return f"{int(english_date.group(3)):04d}-{month:02d}-{int(english_date.group(2)):02d}"
    return ""


def _unit_before(text: str, position: int, market: str) -> tuple[Decimal, str, str]:
    window = text[max(0, position - 1800):position]
    matches: list[tuple[int, Decimal, str, str]] = []
    for pattern, scale, currency in _UNIT_MARKERS:
        for match in pattern.finditer(window):
            matches.append((match.end(), scale, currency, match.group(0)))
    if matches:
        _, scale, currency, marker = max(matches, key=lambda item: item[0])
        return scale, currency, marker
    return Decimal("1"), {"CN": "CNY", "HK": "HKD", "US": "USD"}.get(market, ""), ""


def _candidate_lines(text: str) -> list[tuple[int, str]]:
    lines = [(offset, line.strip()) for offset, line in _line_offsets(text) if line.strip()]
    result: list[tuple[int, str]] = []

    def has_metric_value_pair(value: str) -> bool:
        folded_value = value.casefold()
        for _, _, aliases in _METRIC_ALIASES:
            for candidate_alias in aliases:
                alias_start = folded_value.find(candidate_alias.casefold())
                if alias_start < 0:
                    continue
                alias_end = alias_start + len(candidate_alias)
                if len(_NUMBER_RE.findall(value[alias_end:])) >= 2:
                    return True
        return False

    def has_metric_alias(value: str) -> bool:
        folded_value = value.casefold()
        return any(
            alias.casefold() in folded_value
            for _, _, aliases in _METRIC_ALIASES
            for alias in aliases
        )

    for index, (offset, line) in enumerate(lines):
        # PDF text layers sometimes serialize a wrapped table row as
        # ``label-prefix`` / ``values`` / ``label-suffix``. Reassemble that
        # row before normal forward merging so the value is bound to the
        # complete metric label instead of a later explanatory paragraph.
        if index + 2 < len(lines):
            values_line = lines[index + 1][1]
            suffix_line = lines[index + 2][1]
            if (
                not has_metric_alias(line)
                and _NUMBER_RE.match(values_line)
                and re.match(r"^[\u3400-\u9fff（(]", suffix_line)
            ):
                reordered = f"{line}{suffix_line} {values_line}"
                if has_metric_value_pair(reordered):
                    result.append((offset, reordered))
        merged = line
        for following in range(index + 1, min(index + 4, len(lines))):
            # A table label may wrap across lines and the first numeric token
            # may only be a footnote marker.  Keep merging until a value plus
            # at least one comparison/reference number is available.
            if has_metric_value_pair(merged):
                break
            following_line = lines[following][1]
            chinese_wrap = bool(
                re.search(r"[\u3400-\u9fff）)]$", merged)
                and re.match(r"^[\u3400-\u9fff（(]", following_line)
            )
            merged += ("" if chinese_wrap else " ") + following_line
        result.append((offset, merged))
    return result


def _line_offsets(text: str) -> Iterable[tuple[int, str]]:
    offset = 0
    for line in str(text or "").splitlines():
        yield offset, line
        offset += len(line) + 1


def _alias_is_financial_value(
    metric: str,
    folded_line: str,
    alias_start: int,
    alias_end: int,
) -> bool:
    """Reject aliases that occur inside a different ratio/indicator label."""

    before = folded_line[max(0, alias_start - 24):alias_start].strip()
    after = folded_line[alias_end:alias_end + 24].strip()
    if metric in {"total_assets", "total_liabilities"} and before.endswith(
        ("流动", "非流动", "current", "non-current", "noncurrent")
    ):
        return False
    if metric not in _RATIO_METRICS | _PER_SHARE_METRICS and after.startswith(
        (
            "比例",
            "占比",
            "增幅",
            "增减",
            "变动",
            "增加",
            "减少",
            "增长",
            "下降",
            "同比",
            "较",
            "ratio",
            "margin",
            "growth",
            "change",
        )
    ):
        return False
    if metric == "total_assets":
        if after.startswith(("收益率", "回报率", "周转率", "return", "turnover", "ratio")):
            return False
        if before.endswith(("平均", "average")) and after.startswith(("yield", "return")):
            return False
    if metric == "total_equity" and any(
        owner_scope in before
        for owner_scope in (
            "归属于母公司",
            "归属于本公司",
            "归属于上市公司",
            "归属于普通股股东",
            "attributable to owners",
            "attributable to shareholders",
        )
    ):
        # ``归属于母公司所有者权益合计`` is parent equity, not total
        # consolidated equity. Treating it as both metrics creates a false
        # cross-source mismatch whenever minority interests are present.
        return False
    return True


def _metric_footnote_ids(text: str) -> dict[str, set[int]]:
    result = {metric: set() for metric in _FOOTNOTE_TERMS}
    note_pattern = re.compile(
        r"(?ms)^\s*(\d{1,2})\s*[.．、]\s*(.*?)(?=^\s*\d{1,2}\s*[.．、]\s|\Z)"
    )
    for match in note_pattern.finditer(text):
        note_id = int(match.group(1))
        body = match.group(2).casefold()
        for metric, terms in _FOOTNOTE_TERMS.items():
            if any(term.casefold() in body for term in terms):
                result[metric].add(note_id)
    return result


def _numbers_after_alias(
    line: str,
    alias_end: int,
    *,
    known_footnotes: set[int],
) -> list[str]:
    """Return value tokens after removing an immediate statement footnote.

    Official filings commonly render labels as ``基本每股收益(1) 2.89``.
    Accounting negatives may also use parentheses, so only a leading integer
    footnote followed by another numeric token is removed.
    """

    segment = line[alias_end:]
    # Statement rows often insert a note locator between the label and value,
    # e.g. ``营业总收入 七、61 6,497,196,198.68``.  It is not a financial
    # value and must be removed before numeric replay.
    statement_reference = re.match(
        r"^\s*[一二三四五六七八九十]+[、，,.]\s*\d{1,3}(?:\s*[（(]\d{1,2}[）)])?",
        segment,
    )
    if statement_reference is not None:
        segment = segment[statement_reference.end():]
    # Labels often contain one or more non-numeric qualifiers before their
    # footnote, e.g. ``基本每股收益(人民币元)(1) 1.49``.  Remove only
    # non-numeric label qualifiers, then remove the immediate integer note.
    while True:
        qualifier = re.match(r"^\s*[（(]\s*([^（）()]*)\s*[）)]", segment)
        if qualifier is None or any(char.isdigit() for char in qualifier.group(1)):
            break
        segment = segment[qualifier.end():]
    footnote = re.match(r"^\s*[（(]\s*([1-9]\d?)\s*[）)]", segment)
    if footnote is not None:
        remaining = segment[footnote.end():]
        if _NUMBER_RE.search(remaining):
            segment = remaining
    tokens = _NUMBER_RE.findall(segment)
    if len(tokens) > 1:
        first = _parse_number(tokens[0])
        if (
            first is not None
            and first == first.to_integral()
            and int(abs(first)) in known_footnotes
        ):
            tokens = tokens[1:]
    return tokens


def _metric_candidates(text: str, market: str, period: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    footnote_ids = _metric_footnote_ids(text)
    for position, line in _candidate_lines(text):
        folded = line.casefold()
        # An annual report's "quarterly financial indicators" table contains
        # four single-quarter values under the same labels as the audited
        # statements.  Its first-quarter value must never be stored as the
        # full-year fact merely because that table appears earlier and has an
        # explicit unit marker.
        if period.endswith("-12-31"):
            context = text[max(0, position - 2400):position]
            quarterly_heading = max(
                context.rfind("分季度主要财务指标"),
                context.casefold().rfind("quarterly financial indicators"),
                context.casefold().rfind("quarterly financial information"),
            )
            later_statement = max(
                context.rfind("合并资产负债表"),
                context.rfind("合并利润表"),
                context.rfind("合并现金流量表"),
            )
            if quarterly_heading > later_statement:
                continue
        for metric, statement_type, aliases in _METRIC_ALIASES:
            alias = next((item for item in aliases if item.casefold() in folded), None)
            if alias is None:
                continue
            alias_start = folded.find(alias.casefold())
            alias_end = alias_start + len(alias)
            if not _alias_is_financial_value(metric, folded, alias_start, alias_end):
                continue
            number_tokens = _numbers_after_alias(
                line,
                alias_end,
                known_footnotes=footnote_ids.get(metric, set()),
            )
            if not number_tokens:
                continue
            if (
                metric not in _RATIO_METRICS | _PER_SHARE_METRICS
                and number_tokens[0].endswith("%")
            ):
                continue
            raw_value = _parse_number(number_tokens[0])
            if raw_value is None:
                continue
            scale, currency, marker = _unit_before(text, position, market)
            if metric in _RATIO_METRICS:
                value = raw_value
                unit = "percent"
                scale_applied = Decimal("1")
            elif metric in _PER_SHARE_METRICS:
                value = raw_value
                unit = f"{currency}/share" if currency else "per_share"
                scale_applied = Decimal("1")
            else:
                value = raw_value * scale
                unit = currency or "currency"
                scale_applied = scale
            score = 0
            score += 3 if marker or metric in _RATIO_METRICS | _PER_SHARE_METRICS else 0
            score += 2 if len(number_tokens) >= 2 else 0
            score += 2 if position < 120_000 else 0
            score += 2 if alias_start <= 16 and metric not in _RATIO_METRICS else 0
            score += 2 if alias in {"资产总计", "资产合计", "负债合计", "股东权益合计", "所有者权益合计"} else 0
            score += 1 if "资产负债表" in text[max(0, position - 1200):position] else 0
            score += 1 if any(term in text[max(0, position - 2500):position] for term in ("主要财务", "会计数据", "财务指标", "financial highlights")) else 0
            statement_window = text[max(0, position - 3000):position]
            consolidated_position = max(
                statement_window.rfind("合并资产负债表"),
                statement_window.rfind("合并利润表"),
                statement_window.rfind("合并现金流量表"),
            )
            parent_position = max(
                statement_window.rfind("母公司资产负债表"),
                statement_window.rfind("母公司利润表"),
                statement_window.rfind("母公司现金流量表"),
            )
            if consolidated_position > parent_position:
                score += 3
            elif parent_position > consolidated_position:
                score -= 2
            candidates.append(
                {
                    "metric": metric,
                    "statement_type": statement_type,
                    "value": _decimal_text(value),
                    "raw_value": _decimal_text(raw_value),
                    "unit": unit,
                    "currency": currency,
                    "period": period,
                    "unit_scale": _decimal_text(scale_applied),
                    "unit_marker": marker,
                    "source_line": line[:800],
                    "line_offset": position,
                    "confidence_score": score,
                }
            )
            break
    best: dict[str, dict[str, Any]] = {}
    for item in candidates:
        current = best.get(item["metric"])
        if current is None or (item["confidence_score"], -item["line_offset"]) > (
            current["confidence_score"],
            -current["line_offset"],
        ):
            best[item["metric"]] = item
    return sorted(best.values(), key=lambda item: (item["statement_type"], item["metric"]))


class OfficialFinancialExtractionService:
    """Create and replay validated structured financial snapshots."""

    def __init__(
        self,
        *,
        store: ResearchKnowledgeStore | None = None,
        ingestion: SourceIngestionService | None = None,
    ) -> None:
        self.store = store or get_research_knowledge_store()
        self.ingestion = ingestion or SourceIngestionService(self.store)

    def extract_document(
        self,
        document_ref: str,
        subject_key: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        normalized_subject = str(subject_key or "").strip().upper()
        if not force:
            cached = self.store.structured_extraction(
                document_ref=document_ref,
                subject_key=normalized_subject,
                extractor_id=EXTRACTOR_ID,
                extractor_version=EXTRACTOR_VERSION,
            )
            if cached is not None:
                return {
                    "status": cached["status"],
                    "document_ref": document_ref,
                    "subject_key": normalized_subject,
                    "cached": True,
                    "metrics_count": int(cached.get("metrics_count") or 0),
                    "ocr_performed": bool(cached.get("ocr_performed")),
                    "extraction_id": cached.get("extraction_id"),
                }
        document = self.store.document(document_ref)
        if document is None:
            raise KeyError(document_ref)
        with self.store.connect() as conn:
            official = conn.execute(
                """SELECT 1 FROM source_observations
                   WHERE document_ref=? AND subject_key=?
                     AND verification_status='official_primary' LIMIT 1""",
                (document_ref, normalized_subject),
            ).fetchone()
        if official is None:
            return self.store.record_structured_extraction(
                document_ref=document_ref,
                subject_key=normalized_subject,
                extractor_id=EXTRACTOR_ID,
                extractor_version=EXTRACTOR_VERSION,
                extraction_method="native_text",
                status="not_applicable",
                validation={"official_primary": False},
                error="structured extraction requires an official_primary observation",
            )
        try:
            text = Path(str(document["object_path"])).read_text(encoding="utf-8")
        except OSError as exc:
            return self.store.record_structured_extraction(
                document_ref=document_ref,
                subject_key=normalized_subject,
                extractor_id=EXTRACTOR_ID,
                extractor_version=EXTRACTOR_VERSION,
                extraction_method="native_text",
                status="failed",
                validation={"source_body_readable": False},
                error=str(exc),
            )
        title = str(document.get("title") or "")
        descriptor = f"{title}\n{text[:12000]}".casefold()
        if not _subject_code_present(normalized_subject, f"{title}\n{text[:200_000]}"):
            return self.store.record_structured_extraction(
                document_ref=document_ref,
                subject_key=normalized_subject,
                extractor_id=EXTRACTOR_ID,
                extractor_version=EXTRACTOR_VERSION,
                extraction_method="native_text",
                status="not_applicable",
                validation={"subject_code_present": False},
                error="official document does not identify this subject code",
            )
        if not any(term in descriptor for term in _FINANCIAL_FILING_TERMS):
            return self.store.record_structured_extraction(
                document_ref=document_ref,
                subject_key=normalized_subject,
                extractor_id=EXTRACTOR_ID,
                extractor_version=EXTRACTOR_VERSION,
                extraction_method="native_text",
                status="not_applicable",
                validation={"financial_filing": False},
                error="official document is not a supported financial filing",
            )
        filing_type = _filing_type(title, text)
        period = _reporting_period(title, text, filing_type)
        market = market_for_symbol(normalized_subject)
        metrics = _metric_candidates(text, market, period)
        return self._persist_metrics(
            document=document,
            subject_key=normalized_subject,
            market=market,
            filing_type=filing_type,
            reporting_period_end=period,
            metrics=metrics,
            extraction_method="native_text",
            ocr_performed=False,
        )

    def _persist_metrics(
        self,
        *,
        document: dict[str, Any],
        subject_key: str,
        market: str,
        filing_type: str,
        reporting_period_end: str,
        metrics: list[dict[str, Any]],
        extraction_method: str,
        ocr_performed: bool,
    ) -> dict[str, Any]:
        checks: dict[str, Any] = {
            "official_primary": True,
            "subject_code_present": True,
            "reporting_period_present": bool(reporting_period_end),
            "minimum_metric_count": len(metrics) >= 3,
            "numeric_values_replayable": all(item.get("source_line") or extraction_method == "native_xbrl" for item in metrics),
            "ocr_performed": bool(ocr_performed),
        }
        metric_by_name = {str(item["metric"]): item for item in metrics}
        balance_pair = {"total_assets", "total_liabilities"} <= metric_by_name.keys()
        if balance_pair:
            assets = Decimal(metric_by_name["total_assets"]["value"])
            liabilities = Decimal(metric_by_name["total_liabilities"]["value"])
            checks["balance_sheet_scale_consistent"] = bool(
                assets > 0
                and liabilities >= 0
                and liabilities <= assets * Decimal("3")
                and (not liabilities or assets <= liabilities * Decimal("100"))
            )
            equity_metric = (
                "total_equity"
                if "total_equity" in metric_by_name
                else "parent_equity" if "parent_equity" in metric_by_name else ""
            )
            if equity_metric:
                equity = Decimal(metric_by_name[equity_metric]["value"])
                residual = abs(assets - liabilities - equity)
                checks["balance_sheet_reconciles"] = bool(
                    assets and residual / abs(assets) <= Decimal("0.03")
                )
                checks["balance_sheet_equity_metric"] = equity_metric
                checks["balance_sheet_residual_ratio"] = _decimal_text(
                    residual / abs(assets) if assets else Decimal("0")
                )

        plausible = True
        for metric_name, item in metric_by_name.items():
            value = Decimal(str(item["value"]))
            if metric_name == "total_assets" and value <= 0:
                plausible = False
            elif metric_name == "total_liabilities" and value < 0:
                plausible = False
            elif metric_name == "nonperforming_loan_ratio" and not (Decimal("0") <= value <= Decimal("100")):
                plausible = False
            elif metric_name == "provision_coverage_ratio" and not (Decimal("0") <= value <= Decimal("10000")):
                plausible = False
            elif metric_name == "roe_reported" and not (Decimal("-1000") <= value <= Decimal("1000")):
                plausible = False
        checks["metric_values_plausible"] = plausible
        cross_checks = self._cross_validate_metrics(
            subject_key=subject_key,
            document_ref=str(document["document_ref"]),
            metrics=metrics,
        )
        checks["cross_source_checks"] = cross_checks
        if cross_checks["compared"]:
            checks["cross_source_consistent"] = not bool(cross_checks["mismatches"])
        failed_checks = [
            key
            for key in (
                "reporting_period_present",
                "minimum_metric_count",
                "numeric_values_replayable",
                "metric_values_plausible",
                "balance_sheet_scale_consistent",
                "balance_sheet_reconciles",
                "cross_source_consistent",
            )
            if key in checks and not checks[key]
        ]
        status = "validated" if not failed_checks else "needs_review"
        currency = next((str(item.get("currency") or "") for item in metrics if item.get("currency")), "")
        monetary_scales = [
            str(item.get("unit_scale") or "")
            for item in metrics
            if item.get("metric") not in (_PER_SHARE_METRICS | _RATIO_METRICS)
            and item.get("unit_scale")
        ]
        unit_scale = (
            max(set(monetary_scales), key=monetary_scales.count)
            if monetary_scales
            else ""
        )
        payload = {
            "subject_key": subject_key,
            "document_ref": document["document_ref"],
            "source_content_hash": document["content_hash"],
            "market": market,
            "filing_type": filing_type,
            "reporting_period_end": reporting_period_end,
            "currency": currency,
            "unit_scale": unit_scale,
            "extraction_method": extraction_method,
            "extractor_id": EXTRACTOR_ID,
            "extractor_version": EXTRACTOR_VERSION,
            "ocr_performed": bool(ocr_performed),
            "metrics": metrics,
            "validation": {
                "status": status,
                "checks": checks,
                "failed_checks": failed_checks,
            },
            "extracted_at": _utc_now(),
        }
        evidence_ids: list[str] = []
        fact_ids: list[str] = []
        evidence_id = ""
        evidence: dict[str, Any] | None = None
        facts: list[dict[str, Any]] = []
        if status == "validated":
            evidence_id = _stable_id(
                "ev",
                document["document_ref"],
                EXTRACTOR_ID,
                EXTRACTOR_VERSION,
                document["content_hash"],
            )
            chunk_refs = self._supporting_chunks(document["document_ref"], metrics)
            evidence = {
                "evidence_id": evidence_id,
                "symbol": subject_key,
                "domain": "financial_statement",
                "published_at": document.get("published_at"),
                "summary": (
                    f"Validated official structured financial snapshot: "
                    f"{len(metrics)} metrics for {reporting_period_end}."
                ),
                "status": "verified",
                "metadata": {
                    "document_ref": document["document_ref"],
                    "chunk_refs": chunk_refs,
                    "source_strength": "A",
                    "scope_key": "consolidated",
                    "extraction_method": extraction_method,
                    "extractor_version": EXTRACTOR_VERSION,
                    "ocr_performed": bool(ocr_performed),
                },
            }
            for item in metrics:
                fact_id = _stable_id(
                    "fact",
                    document["document_ref"],
                    subject_key,
                    item["metric"],
                    item["period"],
                    item["value"],
                    item["unit"],
                    EXTRACTOR_VERSION,
                )
                fact_ids.append(fact_id)
                facts.append(
                    {
                        "fact_id": fact_id,
                        "symbol": subject_key,
                        "metric": item["metric"],
                        "value": item["value"],
                        "unit": item["unit"],
                        "currency": item.get("currency") or "",
                        "period": item["period"],
                        "formula": None,
                        "input_fact_ids": [],
                        "evidence_ids": [evidence_id],
                        "validation_status": "pass",
                        "statement_type": item.get("statement_type"),
                        "metadata": {
                            "currency": item.get("currency") or "",
                            "scope_key": "consolidated",
                            "source_document_ref": document["document_ref"],
                            "extraction_method": extraction_method,
                        },
                    }
                )
        self._supersede_prior_extractor_versions(
            document_ref=str(document["document_ref"]),
            subject_key=subject_key,
            facts=facts,
        )
        if evidence is not None:
            self.store.register_bundle({"evidence": [evidence], "facts": facts})
            evidence_ids.append(evidence_id)
        stored = self.store.record_structured_extraction(
            document_ref=document["document_ref"],
            subject_key=subject_key,
            extractor_id=EXTRACTOR_ID,
            extractor_version=EXTRACTOR_VERSION,
            extraction_method=extraction_method,
            status=status,
            result=payload,
            validation=payload["validation"],
            ocr_performed=ocr_performed,
            error=";".join(failed_checks),
            evidence_ids=evidence_ids,
            fact_ids=fact_ids,
        )
        return {**stored, "cached": False, "validation": payload["validation"]}

    def _supersede_prior_extractor_versions(
        self,
        *,
        document_ref: str,
        subject_key: str,
        facts: list[dict[str, Any]],
    ) -> None:
        replacements = {
            (str(item.get("metric") or ""), str(item.get("period") or "")): str(item["fact_id"])
            for item in facts
            if item.get("fact_id")
        }
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT e.extraction_id,s.fact_ids_json
                   FROM structured_document_extractions e
                   LEFT JOIN financial_statement_snapshots s USING(extraction_id)
                   WHERE e.document_ref=? AND e.subject_key=?
                     AND e.extractor_id=? AND e.extractor_version<>?""",
                (document_ref, subject_key, EXTRACTOR_ID, EXTRACTOR_VERSION),
            ).fetchall()
            old_fact_ids: set[str] = set()
            for row in rows:
                try:
                    old_fact_ids.update(json.loads(row["fact_ids_json"] or "[]"))
                except (TypeError, json.JSONDecodeError):
                    continue
            for old_fact_id in old_fact_ids:
                old = conn.execute(
                    "SELECT metric,period FROM fact_records WHERE fact_id=?",
                    (old_fact_id,),
                ).fetchone()
                if old is None:
                    continue
                replacement = replacements.get((str(old["metric"]), str(old["period"])))
                conn.execute(
                    "UPDATE fact_records SET superseded_by=? WHERE fact_id=?",
                    (
                        replacement
                        or _stable_id(
                            "extractor_superseded",
                            document_ref,
                            subject_key,
                            EXTRACTOR_VERSION,
                        ),
                        old_fact_id,
                    ),
                )
            if rows:
                extraction_ids = [str(row["extraction_id"]) for row in rows]
                placeholders = ",".join("?" for _ in extraction_ids)
                conn.execute(
                    f"UPDATE structured_document_extractions SET status='superseded',updated_at=? WHERE extraction_id IN ({placeholders})",
                    (_utc_now(), *extraction_ids),
                )
                conn.execute(
                    f"UPDATE financial_statement_snapshots SET validation_status='superseded',updated_at=? WHERE extraction_id IN ({placeholders})",
                    (_utc_now(), *extraction_ids),
                )
            if old_fact_ids:
                for row in conn.execute(
                    "SELECT conflict_id,fact_ids_json FROM fact_conflicts WHERE resolution_status='needs_third_source'"
                ).fetchall():
                    try:
                        conflict_fact_ids = set(json.loads(row["fact_ids_json"] or "[]"))
                    except json.JSONDecodeError:
                        continue
                    if conflict_fact_ids & old_fact_ids:
                        conn.execute(
                            """UPDATE fact_conflicts SET resolution_status='superseded_source',
                               resolution_reason='older structured extractor version was superseded'
                               WHERE conflict_id=?""",
                            (row["conflict_id"],),
                        )

    def _cross_validate_metrics(
        self,
        *,
        subject_key: str,
        document_ref: str,
        metrics: list[dict[str, Any]],
    ) -> dict[str, Any]:
        comparisons: list[dict[str, Any]] = []
        mismatches: list[dict[str, Any]] = []
        with self.store.connect() as conn:
            for item in metrics:
                rows = conn.execute(
                    """SELECT f.fact_id,f.value,f.unit,f.currency,e.document_ref
                       FROM fact_records f
                       JOIN json_each(f.evidence_ids_json) j
                       JOIN evidence_records e ON e.evidence_id=j.value
                       WHERE f.symbol=? AND f.metric=? AND f.period=?
                         AND f.superseded_by IS NULL AND e.document_ref<>?
                       ORDER BY f.created_at DESC LIMIT 5""",
                    (
                        subject_key,
                        str(item.get("metric") or ""),
                        str(item.get("period") or ""),
                        document_ref,
                    ),
                ).fetchall()
                for row in rows:
                    if str(row["unit"] or "") != str(item.get("unit") or ""):
                        continue
                    try:
                        left = Decimal(str(item.get("value")))
                        right = Decimal(str(row["value"]))
                    except InvalidOperation:
                        continue
                    tolerance = max(abs(left), abs(right), Decimal("1")) * Decimal("0.02")
                    comparison = {
                        "metric": item.get("metric"),
                        "period": item.get("period"),
                        "official_value": str(item.get("value")),
                        "comparison_value": str(row["value"]),
                        "comparison_fact_id": str(row["fact_id"]),
                        "consistent": abs(left - right) <= tolerance,
                    }
                    comparisons.append(comparison)
                    if not comparison["consistent"]:
                        mismatches.append(comparison)
                    break
        return {
            "compared": len(comparisons),
            "consistent": len(comparisons) - len(mismatches),
            "mismatches": mismatches,
            "comparisons": comparisons,
        }

    def _supporting_chunks(
        self,
        document_ref: str,
        metrics: list[dict[str, Any]],
    ) -> list[str]:
        needles = [str(item.get("source_line") or "")[:40] for item in metrics if item.get("source_line")]
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT chunk_ref,search_text FROM source_chunks WHERE document_ref=?",
                (document_ref,),
            ).fetchall()
        matches = [
            str(row["chunk_ref"])
            for row in rows
            if any(needle and needle in str(row["search_text"] or "") for needle in needles)
        ]
        if matches:
            return list(dict.fromkeys(matches))[:12]
        return [str(row["chunk_ref"]) for row in rows[:1]]

    def extract_subject(
        self,
        subject_key: str,
        *,
        force: bool = False,
        repair_only: bool = False,
    ) -> dict[str, Any]:
        normalized = str(subject_key or "").strip().upper()
        with self.store.connect() as conn:
            if repair_only:
                rows = conn.execute(
                    """WITH ranked AS (
                           SELECT document_ref,status,
                                  ROW_NUMBER() OVER (
                                      PARTITION BY document_ref
                                      ORDER BY extracted_at DESC,updated_at DESC
                                  ) AS row_number
                           FROM structured_document_extractions
                           WHERE subject_key=? AND extractor_id=?
                       )
                       SELECT o.document_ref
                       FROM source_observations o
                       JOIN ranked r ON r.document_ref=o.document_ref
                                    AND r.row_number=1
                       WHERE o.subject_key=?
                         AND o.verification_status='official_primary'
                         AND r.status IN ('needs_review','failed')
                       GROUP BY o.document_ref
                       ORDER BY MAX(o.observed_at) DESC""",
                    (normalized, EXTRACTOR_ID, normalized),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT DISTINCT document_ref FROM source_observations
                       WHERE subject_key=? AND verification_status='official_primary'
                       ORDER BY observed_at DESC""",
                    (normalized,),
                ).fetchall()
        results = [
            self.extract_document(str(row["document_ref"]), normalized, force=force)
            for row in rows
        ]
        repair_failures = {"needs_review", "failed"}
        repaired = sum(item.get("status") not in repair_failures for item in results)
        remaining = sum(item.get("status") in repair_failures for item in results)
        return {
            "subject_key": normalized,
            "repair_only": repair_only,
            "repairable_before": len(results) if repair_only else 0,
            "repaired": repaired if repair_only else 0,
            "remaining": remaining if repair_only else 0,
            "documents": len(results),
            "validated": sum(item.get("status") == "validated" for item in results),
            "needs_review": sum(item.get("status") == "needs_review" for item in results),
            "not_applicable": sum(item.get("status") == "not_applicable" for item in results),
            "failed": sum(item.get("status") == "failed" for item in results),
            "cached": sum(bool(item.get("cached")) for item in results),
            "metrics": sum(int(item.get("metrics_count") or 0) for item in results),
            "results": results,
        }

    def ingest_sec_companyfacts(
        self,
        subject_key: str,
        *,
        payload: dict[str, Any] | None = None,
        cik: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        normalized = str(subject_key or "").strip().upper()
        if market_for_symbol(normalized) != "US":
            raise ValueError("SEC companyfacts supports .US symbols only")
        ticker = normalized.removesuffix(".US")
        if payload is None:
            from backtest.loaders.sec_edgar_client import cik_for, get_company_facts

            cik = cik or cik_for(ticker)
            if not cik:
                raise ValueError(f"SEC CIK not found for {ticker}")
            payload = get_company_facts(cik)
        if not isinstance(payload, dict):
            raise ValueError("SEC companyfacts payload must be an object")
        cik_value = str(cik or payload.get("cik") or "").zfill(10)
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_value}.json"
        archived = self.ingestion.ingest(
            CollectedSource(
                subject_key=normalized,
                market="US",
                source_kind="structured_financial",
                provider_id="sec_companyfacts",
                provider_record_id=f"CIK{cik_value}:companyfacts",
                publisher="U.S. Securities and Exchange Commission",
                title=f"{ticker} SEC XBRL companyfacts",
                source_locator=url,
                content=json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
                retrieved_at=_utc_now(),
                verification_status="official_primary",
                body_status="full_text",
                source_class="regulatory_filing",
                metadata={"cik": cik_value, "format": "SEC XBRL companyfacts"},
            ),
            origin_type="official_structured_refresh",
            origin_id=f"sec-companyfacts:{normalized}:{_utc_now()[:13]}",
        )
        document_ref = str(archived["document_ref"])
        if not force:
            cached = self.store.structured_extraction(
                document_ref=document_ref,
                subject_key=normalized,
                extractor_id=EXTRACTOR_ID,
                extractor_version=EXTRACTOR_VERSION,
            )
            if cached is not None:
                return {
                    "status": cached["status"],
                    "document_ref": document_ref,
                    "subject_key": normalized,
                    "cached": True,
                    "metrics_count": int(cached.get("metrics_count") or 0),
                    "ocr_performed": False,
                    "extraction_id": cached.get("extraction_id"),
                }
        metrics = self._sec_metrics(payload)
        document = self.store.document(document_ref)
        if document is None:
            raise KeyError(document_ref)
        period = max((str(item.get("period") or "") for item in metrics), default="")
        return self._persist_metrics(
            document=document,
            subject_key=normalized,
            market="US",
            filing_type="sec_companyfacts",
            reporting_period_end=period,
            metrics=metrics,
            extraction_method="native_xbrl",
            ocr_performed=False,
        )

    @staticmethod
    def _sec_metrics(payload: dict[str, Any]) -> list[dict[str, Any]]:
        gaap = ((payload.get("facts") or {}).get("us-gaap") or {})
        if not isinstance(gaap, dict):
            return []
        metrics: list[dict[str, Any]] = []
        for metric, (statement_type, concepts) in _SEC_CONCEPTS.items():
            concept_name = next((name for name in concepts if isinstance(gaap.get(name), dict)), None)
            if concept_name is None:
                continue
            concept = dict(gaap[concept_name])
            units = concept.get("units") or {}
            if not isinstance(units, dict):
                continue
            rows: list[tuple[str, dict[str, Any]]] = []
            for unit, raw_rows in units.items():
                if not isinstance(raw_rows, list):
                    continue
                rows.extend((str(unit), dict(row)) for row in raw_rows if isinstance(row, dict))
            eligible = [
                (unit, row)
                for unit, row in rows
                if str(row.get("form") or "").upper() in {"10-K", "10-Q", "20-F", "40-F"}
                and row.get("end")
                and row.get("val") is not None
            ]
            by_period: dict[str, tuple[str, dict[str, Any]]] = {}
            for unit, row in eligible:
                period = str(row.get("end") or "")[:10]
                current = by_period.get(period)
                rank = (
                    str(row.get("filed") or ""),
                    1 if str(row.get("fp") or "").upper() == "FY" else 0,
                    str(row.get("start") or ""),
                )
                if current is None:
                    by_period[period] = (unit, row)
                    continue
                old = current[1]
                old_rank = (
                    str(old.get("filed") or ""),
                    1 if str(old.get("fp") or "").upper() == "FY" else 0,
                    str(old.get("start") or ""),
                )
                if rank > old_rank:
                    by_period[period] = (unit, row)
            for period, (unit, row) in sorted(by_period.items(), reverse=True)[:8]:
                try:
                    value = Decimal(str(row["val"]))
                except InvalidOperation:
                    continue
                normalized_unit = unit
                currency = "USD" if unit.startswith("USD") else ""
                if unit.lower() in {"usd/shares", "usd/share"}:
                    normalized_unit = "USD/share"
                metrics.append(
                    {
                        "metric": metric,
                        "statement_type": statement_type,
                        "value": _decimal_text(value),
                        "raw_value": _decimal_text(value),
                        "unit": normalized_unit,
                        "currency": currency,
                        "period": period,
                        "unit_scale": "1",
                        "unit_marker": unit,
                        "source_line": (
                            f"us-gaap:{concept_name} {period} {row.get('form')} "
                            f"{row.get('accn')} {row.get('val')} {unit}"
                        ),
                        "line_offset": 0,
                        "confidence_score": 10,
                        "provider_record_id": row.get("accn"),
                    }
                )
        return metrics


_service: OfficialFinancialExtractionService | None = None


def get_official_financial_extraction_service() -> OfficialFinancialExtractionService:
    global _service
    if _service is None:
        _service = OfficialFinancialExtractionService()
    return _service


__all__ = [
    "EXTRACTOR_ID",
    "EXTRACTOR_VERSION",
    "OfficialFinancialExtractionService",
    "get_official_financial_extraction_service",
]
