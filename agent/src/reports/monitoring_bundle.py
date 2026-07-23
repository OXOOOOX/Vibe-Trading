"""Compiler-owned structural monitoring contract for formal Deep Reports.

The bundle is a research hand-off.  It may describe stable levels worth
watching, but it never activates a monitor, changes a plan, or executes a
trade.  Empty candidates are a valid and common result.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


ALLOWED_INTENTS = {
    "structural_invalidation",
    "major_support",
    "major_resistance",
    "breakout_confirmation",
    "trend_recovery",
    "research_review",
}
ALLOWED_TREND_STAGES = {"下降", "震荡", "筑底", "上升", "unknown"}
ALLOWED_TREND_DIRECTIONS = {"向上", "向下", "横盘", "unknown"}
ALLOWED_TREND_STRENGTHS = {"强", "中", "弱", "unknown"}
ALLOWED_THESIS_STATES = {"intact", "weakening", "invalidated", "unknown"}
FORBIDDEN_ACTION_RE = re.compile(
    r"(?:买入|卖出|建仓|加仓|减仓|清仓|做多|做空|下单|自动交易|目标仓位)",
    re.I,
)
PRICE_METRICS = {
    "latest_price",
    "market_price",
    "close",
    "close_price",
    "raw_price",
    "etf_price",
    "current_price",
}


class StructuralMonitoringValidationError(ValueError):
    """A draft violates the non-execution or lineage contract."""


def _iso(value: Any, fallback: datetime) -> str:
    raw = str(value or "").strip()
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.isoformat()
        except ValueError:
            pass
    return fallback.isoformat()


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _list_of_text(value: Any, *, maximum: int = 12) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()][:maximum]


def _instrument_type(profile: str) -> str:
    if profile == "etf_deep_research":
        return "etf"
    if profile == "index_deep_research":
        return "index"
    return "company_equity"


def _tick_size(instrument_type: str) -> float:
    return 0.001 if instrument_type == "etf" else 0.01


def _price_verified(snapshot: dict[str, Any], facts: list[dict[str, Any]]) -> bool:
    if snapshot.get("price_verified") is True:
        return True
    for fact in facts:
        metric = str(fact.get("metric") or "").strip().casefold()
        if metric in PRICE_METRICS and _decimal(fact.get("value")) is not None:
            return True
    return False


def _stable_scenario_id(symbol: str, intent: str, semantic_key: str) -> str:
    digest = hashlib.sha256(
        f"{symbol.upper()}|structural|{intent}|{semantic_key}".encode("utf-8")
    ).hexdigest()[:16]
    return f"struct_{digest}"


def _claim_key(value: Any) -> str:
    text = re.sub(r"\[(?:Fact|Evidence):[A-Za-z0-9_-]+\]", "", str(value or ""))
    text = re.sub(r"\[\^\d+\]", "", text)
    text = re.sub(r"[*_`\s]", "", text)
    return text.strip("。；，,.!?！？：:")


def _normalize_level(raw: Any) -> dict[str, Any] | None:
    payload = dict(raw or {}) if isinstance(raw, dict) else {}
    kind = str(payload.get("kind") or payload.get("type") or "").strip().casefold()
    if kind == "point" or payload.get("price") is not None:
        price = _decimal(payload.get("price"))
        return {"kind": "point", "price": float(price)} if price is not None else None
    if kind in {"range", "zone"} or payload.get("low") is not None:
        low = _decimal(payload.get("low"))
        high = _decimal(payload.get("high"))
        if low is None or high is None or low > high:
            return None
        return {"kind": "range", "low": float(low), "high": float(high)}
    return None


def validate_structural_monitoring_draft(
    draft: dict[str, Any],
    *,
    available_facts: dict[str, dict[str, Any]],
    available_evidence_ids: set[str],
    price_tick_size: Decimal = Decimal("0.01"),
) -> dict[str, Any]:
    """Validate and normalize the model-authored subset before compilation."""

    if not isinstance(draft, dict):
        raise StructuralMonitoringValidationError("monitoring bundle draft must be an object")
    raw_candidates = draft.get("candidates") or []
    if not isinstance(raw_candidates, list):
        raise StructuralMonitoringValidationError("monitoring candidates must be a list")
    if len(raw_candidates) > 6:
        raise StructuralMonitoringValidationError("structural monitoring allows at most 6 candidates")

    candidates: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_candidates):
        if not isinstance(raw, dict):
            raise StructuralMonitoringValidationError(f"candidate {index + 1} must be an object")
        intent = str(raw.get("intent") or "").strip()
        if intent not in ALLOWED_INTENTS:
            raise StructuralMonitoringValidationError(
                f"candidate {index + 1} has unsupported intent: {intent or 'missing'}"
            )
        source_text = str(raw.get("source_text") or raw.get("original_report_text") or "").strip()
        action = str(raw.get("recommended_action") or "人工复核并重新研究").strip()
        if FORBIDDEN_ACTION_RE.search(action):
            raise StructuralMonitoringValidationError(
                f"candidate {index + 1} contains a trading action; only research/watch actions are allowed"
            )
        fact_ids = _list_of_text(raw.get("fact_ids"))
        evidence_ids = _list_of_text(raw.get("evidence_ids"))
        unknown_facts = sorted(set(fact_ids) - set(available_facts))
        unknown_evidence = sorted(set(evidence_ids) - available_evidence_ids)
        if unknown_facts:
            raise StructuralMonitoringValidationError(
                f"candidate {index + 1} references unknown facts: {', '.join(unknown_facts)}"
            )
        if unknown_evidence:
            raise StructuralMonitoringValidationError(
                f"candidate {index + 1} references unknown evidence: {', '.join(unknown_evidence)}"
            )
        level = _normalize_level(raw.get("level") or raw.get("price_level"))
        if not source_text:
            raise StructuralMonitoringValidationError(
                f"candidate {index + 1} must preserve its original report text"
            )
        if not evidence_ids:
            raise StructuralMonitoringValidationError(
                f"candidate {index + 1} must cite at least one Evidence item"
            )
        if level is not None:
            if not fact_ids:
                raise StructuralMonitoringValidationError(
                    f"candidate {index + 1} price level must cite a raw-price Fact"
                )
            level_values = (
                [Decimal(str(level["price"]))]
                if level["kind"] == "point"
                else [Decimal(str(level["low"])), Decimal(str(level["high"]))]
            )
            if any(value % price_tick_size != 0 for value in level_values):
                raise StructuralMonitoringValidationError(
                    f"candidate {index + 1} price level does not match raw-price tick size"
                )
            cited_price_facts = [
                fact
                for fact_id in fact_ids
                if (
                    (fact := available_facts.get(fact_id)) is not None
                    and str(fact.get("unit") or "").strip().casefold()
                    in {"cny", "rmb", "yuan", "元"}
                    and str(fact.get("validation_status") or "pass") == "pass"
                    and _decimal(fact.get("value")) is not None
                )
            ]
            cited_values = [_decimal(fact.get("value")) for fact in cited_price_facts]
            if any(
                not any(
                    fact_value is not None and abs(fact_value - level_value) <= Decimal("0.000001")
                    for fact_value in cited_values
                )
                for level_value in level_values
            ):
                raise StructuralMonitoringValidationError(
                    f"candidate {index + 1} price level does not replay from a cited raw CNY Fact"
                )
        machine_expressible = bool(raw.get("machine_expressible")) and level is not None
        readiness = "action_ready" if (
            machine_expressible
            and fact_ids
            and evidence_ids
            and _list_of_text(raw.get("price_trigger_conditions"), maximum=6)
            and _list_of_text(raw.get("invalidation_conditions"), maximum=6)
        ) else "watch_only"
        semantic_key = str(raw.get("semantic_key") or raw.get("label") or intent).strip()
        candidates.append({
            "semantic_key": semantic_key,
            "label": str(raw.get("label") or intent).strip(),
            "intent": intent,
            "level": level,
            "proximity_conditions": _list_of_text(raw.get("proximity_conditions"), maximum=6),
            "price_trigger_conditions": _list_of_text(raw.get("price_trigger_conditions"), maximum=6),
            "confirmation_conditions": _list_of_text(raw.get("confirmation_conditions"), maximum=6),
            "volume_conditions": _list_of_text(raw.get("volume_conditions"), maximum=6),
            "invalidation_conditions": _list_of_text(raw.get("invalidation_conditions"), maximum=6),
            "observation_window": str(raw.get("observation_window") or "").strip(),
            "recommended_action": action,
            "source_text": source_text,
            "section_id": str(raw.get("section_id") or "").strip(),
            "fact_ids": fact_ids,
            "evidence_ids": evidence_ids,
            "machine_expressible": machine_expressible,
            "actionability": readiness,
        })

    context = dict(draft.get("structural_context") or {})
    return {
        "structural_context": {
            "trend_stage": (
                context.get("trend_stage")
                if context.get("trend_stage") in ALLOWED_TREND_STAGES else "unknown"
            ),
            "trend_direction": (
                context.get("trend_direction")
                if context.get("trend_direction") in ALLOWED_TREND_DIRECTIONS else "unknown"
            ),
            "trend_strength": (
                context.get("trend_strength")
                if context.get("trend_strength") in ALLOWED_TREND_STRENGTHS else "unknown"
            ),
            "thesis_state": (
                context.get("thesis_state")
                if context.get("thesis_state") in ALLOWED_THESIS_STATES else "unknown"
            ),
            "thesis_invalidation_conditions": _list_of_text(
                context.get("thesis_invalidation_conditions"), maximum=12
            ),
            "review_triggers": _list_of_text(context.get("review_triggers"), maximum=12),
        },
        "candidates": candidates,
    }


def build_structural_monitoring_bundle(
    *,
    record: Any,
    draft: dict[str, Any] | None,
    snapshot: dict[str, Any],
    facts: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    claim_support: dict[str, Any],
    references: dict[str, Any],
    report_sha256: str,
) -> dict[str, Any]:
    """Compile a safe, immutable monitoring hand-off for one formal report."""

    now = datetime.now(timezone.utc)
    generated_at = _iso(getattr(record, "updated_at", None), now)
    data_as_of = _iso(getattr(record, "data_as_of", None), now)
    base = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    instrument_type = _instrument_type(str(getattr(record, "profile", "")))
    normalized = validate_structural_monitoring_draft(
        draft or {},
        available_facts={
            str(item.get("fact_id")): item for item in facts if item.get("fact_id")
        },
        available_evidence_ids={
            str(item.get("evidence_id")) for item in evidence if item.get("evidence_id")
        },
        price_tick_size=Decimal(str(_tick_size(instrument_type))),
    )
    citation_by_fact: dict[str, set[int]] = {}
    citation_by_evidence: dict[str, set[int]] = {}
    for item in references.get("citations") or []:
        number = int(item.get("citation_number"))
        for fact_id in item.get("fact_ids") or []:
            citation_by_fact.setdefault(str(fact_id), set()).add(number)
        for evidence_id in item.get("evidence_ids") or []:
            citation_by_evidence.setdefault(str(evidence_id), set()).add(number)

    claims_by_text = {
        _claim_key(item.get("text")): str(item.get("claim_id"))
        for item in claims
        if item.get("claim_id") and str(item.get("text") or "").strip()
    }
    support_by_claim_id = {
        str(item.get("claim_id")): str(item.get("support_status") or "insufficient")
        for item in claim_support.get("claims") or []
        if item.get("claim_id")
    }
    compiled_candidates: list[dict[str, Any]] = []
    for raw in normalized["candidates"]:
        fact_ids = list(raw.pop("fact_ids"))
        evidence_ids = list(raw.pop("evidence_ids"))
        source_text = str(raw.get("source_text") or "").strip()
        claim_id = claims_by_text.get(_claim_key(source_text))
        support_status = support_by_claim_id.get(claim_id or "", "insufficient")
        if claim_id is None or support_status not in {"verified", "triangulated"}:
            raw["actionability"] = "watch_only"
            raw["machine_expressible"] = False
        reference_numbers = sorted(set().union(
            *(citation_by_fact.get(value, set()) for value in fact_ids),
            *(citation_by_evidence.get(value, set()) for value in evidence_ids),
        ))
        semantic_key = str(raw.pop("semantic_key"))
        compiled_candidates.append({
            "scenario_id": _stable_scenario_id(record.symbol, raw["intent"], semantic_key),
            **raw,
            "lineage": {
                "status": (
                    "complete" if claim_id and support_status in {"verified", "triangulated"}
                    else "claim_not_resolved" if not claim_id
                    else "claim_support_insufficient"
                ),
                "claim_support_status": support_status,
                "claim_ids": [claim_id] if claim_id else [],
                "fact_ids": fact_ids,
                "evidence_ids": evidence_ids,
                "reference_numbers": reference_numbers,
            },
        })

    price_ready = _price_verified(snapshot, facts)
    if compiled_candidates:
        monitoring_status = "available"
    elif price_ready:
        monitoring_status = "not_recommended"
    else:
        monitoring_status = "data_insufficient"

    structural_context = dict(normalized["structural_context"])
    structural_context["structural_levels"] = [
        {
            "scenario_id": item["scenario_id"],
            "intent": item["intent"],
            "level": item["level"],
        }
        for item in compiled_candidates
        if item.get("level") is not None
    ]
    bundle = {
        "schema_version": 1,
        "report_id": record.report_id,
        "report_revision": record.revision,
        "symbol": record.symbol,
        "instrument_type": instrument_type,
        "report_profile": record.profile,
        "horizon": "structural",
        "generated_at": generated_at,
        "data_as_of": data_as_of,
        "valid_from": generated_at,
        "valid_until": (base + timedelta(days=180)).isoformat(),
        "review_due_at": (base + timedelta(days=90)).isoformat(),
        "price_basis": {
            "adjustment": "raw",
            "currency": "CNY",
            "tick_size": _tick_size(instrument_type),
        },
        "report_quality_status": record.quality_status,
        "monitoring_status": monitoring_status,
        "activation_policy": "manual_confirmation_required",
        "trade_execution": "forbidden",
        "structural_context": structural_context,
        "candidates": compiled_candidates,
        "integrity": {
            "report_sha256": report_sha256,
            "references_sha256": hashlib.sha256(
                json.dumps(references, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
        },
    }
    bundle["integrity"]["bundle_sha256"] = hashlib.sha256(
        json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return bundle
