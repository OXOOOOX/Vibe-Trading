"""Deterministic Claim-to-Evidence support classification for formal reports."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any


STRONG_SOURCE_CLASSES = {
    "official",
    "regulatory",
    "exchange",
    "company_filing",
    "regulatory_filing",
    "company_disclosure",
    "audited_financial_statement",
    "official_statistics",
    "index_provider",
    "index_methodology",
    "fund_manager",
    "fund_product",
}
CONFLICT_STATUSES = {"conflicted", "contradicted", "rejected", "invalid"}
REUSABLE_SUPPORT_STATUSES = {"verified", "triangulated"}
SOURCE_TIER_ORDER = {
    "official_structured": 0,
    "official_text": 1,
    "independent_triangulation": 2,
    "single_provider": 3,
    "search_lead": 4,
}


def _source_tier(item: dict[str, Any]) -> str:
    metadata = dict(item.get("metadata") or {})
    source_class = str(metadata.get("source_class") or "").casefold()
    verification = str(
        metadata.get("verification_status") or item.get("verification_status") or ""
    ).casefold()
    official = source_class in STRONG_SOURCE_CLASSES or verification == "official_primary"
    structured = bool(
        metadata.get("structured_status") in {"complete", "passed", "validated"}
        or metadata.get("structured_extraction_id")
        or metadata.get("extraction_id")
    )
    read_status = str(
        metadata.get("source_read_status") or item.get("source_read_status") or ""
    ).casefold()
    if official and structured:
        return "official_structured"
    if official:
        return "official_text"
    if read_status in {"search_summary", "search_snippet", "search_result"}:
        return "search_lead"
    return "single_provider"


def source_tier(item: dict[str, Any]) -> str:
    return _source_tier(item)


def claim_support_map(audit: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Return the immutable per-report support rows keyed by Claim ID."""

    if not isinstance(audit, dict):
        return {}
    return {
        str(item.get("claim_id")): dict(item)
        for item in audit.get("claims") or []
        if isinstance(item, dict) and item.get("claim_id")
    }


def build_claim_support_audit(
    claims: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    facts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Classify support without asking a model to judge its own prose."""

    evidence_by_id = {
        str(item.get("evidence_id")): item
        for item in evidence
        if item.get("evidence_id")
    }
    fact_evidence_ids = {
        str(item.get("fact_id")): [
            str(value) for value in item.get("evidence_ids") or [] if str(value)
        ]
        for item in facts or []
        if item.get("fact_id")
    }
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    audited_at = datetime.now(timezone.utc).isoformat()
    for claim in claims:
        claim_id = str(claim.get("claim_id") or "")
        fact_ids = [str(value) for value in claim.get("fact_ids") or [] if str(value)]
        evidence_ids = list(dict.fromkeys([
            *(str(value) for value in claim.get("evidence_ids") or [] if str(value)),
            *(
                evidence_id
                for fact_id in fact_ids
                for evidence_id in fact_evidence_ids.get(fact_id, [])
            ),
        ]))
        source_rows = [evidence_by_id[value] for value in evidence_ids if value in evidence_by_id]
        groups: set[str] = set()
        strong = False
        conflicted = False
        source_tiers: list[str] = []
        for item in source_rows:
            metadata = dict(item.get("metadata") or {})
            group = str(
                metadata.get("independence_group")
                or metadata.get("publisher")
                or item.get("source")
                or item.get("source_locator")
                or item.get("evidence_id")
            ).strip()
            if group:
                groups.add(group.casefold())
            source_class = str(metadata.get("source_class") or "").casefold()
            tier = _source_tier(item)
            source_tiers.append(tier)
            strong = strong or tier in {"official_structured", "official_text"}
            conflicted = conflicted or str(item.get("status") or "").casefold() in CONFLICT_STATUSES

        if claim.get("claim_type") == "data_gap":
            status = "insufficient"
            support_reason = "data_gap"
        elif conflicted:
            status = "conflicted"
            support_reason = "conflicting_evidence"
        elif len(groups) >= 2:
            status = "triangulated"
            support_reason = "independent_sources"
            source_tiers.append("independent_triangulation")
        elif len(groups) == 1 and strong:
            status = "verified"
            support_reason = "authoritative_source"
        elif len(groups) == 1:
            status = "weak"
            support_reason = "single_non_authoritative_source"
        else:
            status = "insufficient"
            support_reason = "no_registered_support"
        counts[status] += 1
        rows.append({
            "claim_id": claim_id,
            "claim_type": str(claim.get("claim_type") or "opinion"),
            "support_audit_version": 2,
            "audited_at": audited_at,
            "support_status": status,
            "support_reason": support_reason,
            "reusable": status in REUSABLE_SUPPORT_STATUSES,
            "source_count": len(source_rows),
            "independent_source_count": len(groups),
            "source_tiers": sorted(
                set(source_tiers), key=lambda value: SOURCE_TIER_ORDER.get(value, 99)
            ),
            "highest_source_tier": min(
                source_tiers,
                key=lambda value: SOURCE_TIER_ORDER.get(value, 99),
                default=None,
            ),
            "evidence_ids": evidence_ids,
            "fact_ids": fact_ids,
            "section_id": claim.get("section_id"),
        })
    return {
        "schema_version": 2,
        "support_audit_version": 2,
        "audited_at": audited_at,
        "status": "complete",
        "counts": dict(counts),
        "claims": rows,
    }
