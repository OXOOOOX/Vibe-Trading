"""Extract and register official ETF tracking-quality disclosures.

This module accepts text extracted from an official annual/semiannual report.
Acquisition remains a separate concern, allowing every ETF provider or filing
job to reuse the same deterministic parser and Fact/Evidence contract.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from src.research.knowledge import ResearchKnowledgeStore


ETF_TRACKING_DISCLOSURE_EXTRACTOR_ID = "etf-official-tracking-disclosure"
ETF_TRACKING_DISCLOSURE_EXTRACTOR_VERSION = "1.0"

_PERIOD_LABELS = {
    "过去三个月": "3m",
    "过去六个月": "6m",
    "过去一年": "1y",
    "过去两年": "2y",
    "过去三年": "3y",
    "过去五年": "5y",
    "自基金合同生效起至今": "since_inception",
}
_PERCENT = r"(-?\d+(?:\.\d+)?)%"


def _stable_id(prefix: str, *parts: Any) -> str:
    payload = "|".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def _normalize_text(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    for label in _PERIOD_LABELS:
        text = re.sub(r"\s*".join(map(re.escape, label)), label, text)
    return text


def extract_official_tracking_disclosure(text: str) -> dict[str, Any]:
    """Parse official NAV/benchmark comparison rows without model inference."""

    normalized = _normalize_text(text)
    rows: list[dict[str, Any]] = []
    for label, period_key in _PERIOD_LABELS.items():
        match = re.search(
            rf"{re.escape(label)}\s+{_PERCENT}\s+{_PERCENT}\s+{_PERCENT}\s+{_PERCENT}\s+{_PERCENT}\s+{_PERCENT}",
            normalized,
        )
        if match is None:
            continue
        values = [round(float(value) / 100.0, 10) for value in match.groups()]
        rows.append({
            "period_key": period_key,
            "period_label": label,
            "nav_return": values[0],
            "nav_return_volatility": values[1],
            "benchmark_return": values[2],
            "benchmark_return_volatility": values[3],
            "tracking_difference": values[4],
            "tracking_volatility_difference": values[5],
        })

    daily_limit = re.search(
        rf"日均跟踪偏离度的绝对值不超过\s*{_PERCENT}",
        normalized,
    )
    annual_limit = re.search(rf"年跟踪误差不超过\s*{_PERCENT}", normalized)
    return {
        "schema_version": 1,
        "extractor_id": ETF_TRACKING_DISCLOSURE_EXTRACTOR_ID,
        "extractor_version": ETF_TRACKING_DISCLOSURE_EXTRACTOR_VERSION,
        "status": "complete" if rows else "missing",
        "comparison_rows": rows,
        "objective_limits": {
            "daily_tracking_deviation_absolute_limit": (
                round(float(daily_limit.group(1)) / 100.0, 10) if daily_limit else None
            ),
            "annual_tracking_error_limit": (
                round(float(annual_limit.group(1)) / 100.0, 10) if annual_limit else None
            ),
        },
    }


def register_official_tracking_disclosure(
    *,
    store: ResearchKnowledgeStore,
    symbol: str,
    source_url: str,
    source_text: str,
    title: str,
    publisher: str,
    published_at: str,
    report_period: str,
) -> dict[str, Any]:
    """Persist one official filing extraction as reusable Evidence and Facts."""

    normalized_symbol = str(symbol or "").strip().upper()
    extraction = extract_official_tracking_disclosure(source_text)
    if extraction["status"] != "complete":
        raise ValueError("official ETF tracking disclosure table was not found")
    document = store.store_document(
        url=source_url,
        content=source_text,
        title=title,
        publisher=publisher,
        source_class="regulatory_filing",
        published_at=published_at,
        cached_status="network",
        aliases=(normalized_symbol, "ETF", "tracking difference", "跟踪偏离"),
    )
    relevant = store.read_document(
        document.document_ref,
        query="基金净值表现 业绩比较基准",
        limit=12,
    )
    chunk_refs = [
        str(item.get("chunk_ref"))
        for item in relevant.get("chunks") or []
        if item.get("chunk_ref")
    ]
    evidence_id = _stable_id(
        "evidence",
        normalized_symbol,
        document.document_ref,
        ETF_TRACKING_DISCLOSURE_EXTRACTOR_VERSION,
    )
    evidence = {
        "evidence_id": evidence_id,
        "symbol": normalized_symbol,
        "domain": "etf_tracking_quality",
        "status": "verified",
        "published_at": published_at,
        "summary": "基金定期报告披露的基金净值与业绩比较基准对照表。",
        "metadata": {
            "document_ref": document.document_ref,
            "chunk_refs": chunk_refs,
            "source_strength": "A",
            "scope_key": report_period,
            "source_kind": "official_periodic_report",
        },
    }
    facts: list[dict[str, Any]] = []
    for row in extraction["comparison_rows"]:
        for metric in ("tracking_difference", "tracking_volatility_difference"):
            facts.append({
                "fact_id": _stable_id(
                    "fact",
                    normalized_symbol,
                    metric,
                    report_period,
                    row["period_key"],
                    row[metric],
                ),
                "symbol": normalized_symbol,
                "metric": metric,
                "value": row[metric],
                "unit": "ratio",
                "period": report_period,
                "scope_key": row["period_key"],
                "formula": (
                    "official_nav_return_minus_benchmark_return"
                    if metric == "tracking_difference"
                    else "official_nav_volatility_minus_benchmark_volatility"
                ),
                "input_fact_ids": [],
                "evidence_ids": [evidence_id],
                "validation_status": "pass",
                "metadata": {
                    "scope_key": row["period_key"],
                    "period_label": row["period_label"],
                    "official_disclosure": True,
                },
            })
    for metric, value in extraction["objective_limits"].items():
        if value is None:
            continue
        facts.append({
            "fact_id": _stable_id(
                "fact", normalized_symbol, metric, report_period, value
            ),
            "symbol": normalized_symbol,
            "metric": metric,
            "value": value,
            "unit": "ratio",
            "period": report_period,
            "scope_key": "contract_objective",
            "formula": None,
            "input_fact_ids": [],
            "evidence_ids": [evidence_id],
            "validation_status": "pass",
            "metadata": {
                "scope_key": "contract_objective",
                "official_disclosure": True,
            },
        })
    result = store.register_bundle({"evidence": [evidence], "facts": facts})
    store.record_structured_extraction(
        document_ref=document.document_ref,
        subject_key=normalized_symbol,
        extractor_id=ETF_TRACKING_DISCLOSURE_EXTRACTOR_ID,
        extractor_version=ETF_TRACKING_DISCLOSURE_EXTRACTOR_VERSION,
        extraction_method="deterministic_text_table",
        status="passed",
        result=extraction,
        validation={
            "comparison_row_count": len(extraction["comparison_rows"]),
            "official_source": True,
        },
        evidence_ids=[evidence_id],
        fact_ids=[item["fact_id"] for item in facts],
    )
    return {
        "document_ref": document.document_ref,
        "evidence_id": evidence_id,
        "fact_ids": [item["fact_id"] for item in facts],
        "extraction": extraction,
        "registration": result,
    }
