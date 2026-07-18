"""Validated contracts for portfolio monitor plans."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any


RULE_KINDS = {
    "price_cross_above",
    "price_cross_below",
    "price_zone_enter",
    "price_zone_exit",
    "intraday_pct_change_above",
    "intraday_pct_change_below",
    "volume_ratio_above",
}
TIERS = {"low", "normal", "active"}
DATA_MODES = {"verified", "single_source"}
INTERVALS = {"1m", "5m"}
COMPOUND_INTERVALS = {"1m", "5m", "30m", "1d"}
SEVERITIES = {"info", "warning", "critical"}
ALERT_CUES = {"none", "ymca_v1"}
TARGET_INTENTS = {
    "buy_point",
    "add_position",
    "stop_loss",
    "take_profit",
    "watch",
    "breakout",
}
PRICE_VOLUME_INTERVALS = {"5m"}
PRICE_VOLUME_BASELINE_METHODS = {"same_time_bucket_median"}
REPORT_TYPES = {"single_stock_research", "holding_analysis", "daily_portfolio", "monitor_research"}
REPORT_QUALITY_STATUSES = {"ready", "data_limited", "conflicted", "invalidated"}
APPROACH_DISTANCE_SOURCES = {"report", "atr20_default", "user"}
VOLUME_CONFIRMATION_METRICS = {
    "same_bucket_5m_volume_ratio",
    "same_clock_cumulative_volume_ratio",
    "absolute_cumulative_volume",
}
VOLUME_CONFIRMATION_COMPARATORS = {"gte", "lte"}
VOLUME_UNITS = {"shares", "lots", "CNY"}
SOURCE_CONDITION_ROLES = {"required", "supportive", "invalidation"}
SOURCE_CONDITION_STATUSES = {"mapped", "awaiting_data", "ambiguous", "unsupported"}
CONDITION_GROUP_OPERATORS = {"all", "any"}
CONDITION_KINDS = {
    "price_compare",
    "price_zone",
    "bar_direction",
    "price_reclaim",
    "session_range",
    "session_amplitude_bps",
    "volume_ratio",
    "cumulative_volume",
    "cumulative_turnover",
    "fund_flow",
    "sector_state",
}
CONDITION_COMPARATORS = {
    "gte", "lte", "gt", "lt", "between", "positive", "negative", "equals",
}
CONDITION_METRICS = {
    "volume_ratio": {"volume_ratio"},
    "cumulative_volume": {"cumulative_volume", "cumulative_volume_ratio"},
    "cumulative_turnover": {"cumulative_amount", "cumulative_amount_ratio"},
    "fund_flow": {"main", "large", "super_large"},
    "sector_state": {"change_pct"},
}
AUTOMATION_STATUSES = {"action_ready", "watch_only"}
RECOMMENDATION_ACTIONS = {"observe", "add", "reduce", "exit"}
SIZING_KINDS = {
    "units", "position_fraction", "cash_amount", "target_position_units", "default_policy",
}
DEFAULT_PRICE_VOLUME_POLICY: dict[str, Any] = {
    "enabled": True,
    "interval": "5m",
    "baseline_method": "same_time_bucket_median",
    "baseline_sessions": 10,
    "min_samples": 5,
    "contraction_ratio": 0.8,
    "expansion_ratio": 1.5,
    "flat_return_bps": 10,
    "acceleration_multiplier": 1.2,
}


class PlanValidationError(ValueError):
    """Raised when a plan escapes the monitoring rule whitelist."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _finite(value: Any, field: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise PlanValidationError(f"{field} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PlanValidationError(f"{field} must be a number") from exc
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise PlanValidationError(f"{field} is outside the allowed range")
    return number


def _integer(
    value: Any,
    field: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    number = _finite(value, field)
    integer = int(number)
    if number != integer:
        raise PlanValidationError(f"{field} must be an integer")
    if (minimum is not None and integer < minimum) or (
        maximum is not None and integer > maximum
    ):
        raise PlanValidationError(f"{field} is outside the allowed range")
    return integer


def _bounded_text(value: Any, field: str, *, maximum: int, required: bool = True) -> str:
    text = str(value or "").strip()
    if (required and not text) or len(text) > maximum:
        raise PlanValidationError(f"{field} is invalid")
    return text


def _aware_iso_datetime(value: Any, field: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise PlanValidationError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PlanValidationError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PlanValidationError(f"{field} must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def _validate_target_ladder(rules: list[dict[str, Any]]) -> None:
    """Keep farther L2+ targets outside the preceding price level."""

    for kind, ascending in (("price_cross_above", True), ("price_cross_below", False)):
        levels: dict[int, list[float]] = {}
        for rule in rules:
            if rule["kind"] != kind or not rule.get("enabled", True):
                continue
            level = int(rule.get("target_level") or 1)
            levels.setdefault(level, []).append(float(rule["parameters"]["threshold"]))
        ordered = sorted(levels)
        for lower_level, higher_level in zip(ordered, ordered[1:]):
            lower_values = levels[lower_level]
            higher_values = levels[higher_level]
            valid = (
                min(higher_values) > max(lower_values)
                if ascending
                else max(higher_values) < min(lower_values)
            )
            if not valid:
                direction = "higher" if ascending else "lower"
                raise PlanValidationError(
                    f"{kind} L{higher_level} must be strictly {direction} than L{lower_level}"
                )


def _normalize_calculation_basis(value: Any, *, rule_index: int) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PlanValidationError(f"market_rules[{rule_index}].calculation_basis must be an object")
    prefix = f"market_rules[{rule_index}].calculation_basis"
    references = value.get("references") or []
    if not isinstance(references, list) or len(references) > 8:
        raise PlanValidationError(f"{prefix}.references must contain at most 8 items")
    normalized_references: list[dict[str, Any]] = []
    for reference_index, raw_reference in enumerate(references):
        if not isinstance(raw_reference, dict):
            raise PlanValidationError(f"{prefix}.references[{reference_index}] must be an object")
        reference = {
            "label": _bounded_text(
                raw_reference.get("label"),
                f"{prefix}.references[{reference_index}].label",
                maximum=80,
            ),
        }
        if raw_reference.get("value") is not None:
            reference["value"] = _finite(
                raw_reference.get("value"),
                f"{prefix}.references[{reference_index}].value",
            )
        if raw_reference.get("date"):
            reference["date"] = _bounded_text(
                raw_reference.get("date"),
                f"{prefix}.references[{reference_index}].date",
                maximum=40,
            )
        normalized_references.append(reference)
    return {
        "method": _bounded_text(value.get("method"), f"{prefix}.method", maximum=80),
        "method_label": _bounded_text(
            value.get("method_label"),
            f"{prefix}.method_label",
            maximum=120,
        ),
        "formula": _bounded_text(value.get("formula"), f"{prefix}.formula", maximum=300),
        "summary": _bounded_text(value.get("summary"), f"{prefix}.summary", maximum=800),
        "recommended_value": _finite(
            value.get("recommended_value"),
            f"{prefix}.recommended_value",
            minimum=0.000001,
        ),
        "references": normalized_references,
    }


def _normalize_price_volume_policy(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlanValidationError("price_volume_policy must be an object for schema_version>=2")
    policy = {**DEFAULT_PRICE_VOLUME_POLICY, **copy.deepcopy(value)}
    if not isinstance(policy.get("enabled"), bool):
        raise PlanValidationError("price_volume_policy.enabled must be a boolean")
    interval = str(policy.get("interval") or "")
    method = str(policy.get("baseline_method") or "")
    if interval not in PRICE_VOLUME_INTERVALS:
        raise PlanValidationError("price_volume_policy.interval must be 5m")
    if method not in PRICE_VOLUME_BASELINE_METHODS:
        raise PlanValidationError("price_volume_policy.baseline_method is not allowed")
    raw_baseline_sessions = _finite(
        policy.get("baseline_sessions"), "price_volume_policy.baseline_sessions"
    )
    raw_min_samples = _finite(policy.get("min_samples"), "price_volume_policy.min_samples")
    baseline_sessions = int(raw_baseline_sessions)
    min_samples = int(raw_min_samples)
    if raw_baseline_sessions != baseline_sessions or raw_min_samples != min_samples:
        raise PlanValidationError("price_volume_policy sample counts must be integers")
    if not 5 <= baseline_sessions <= 30:
        raise PlanValidationError("price_volume_policy.baseline_sessions must be between 5 and 30")
    if not 5 <= min_samples <= baseline_sessions:
        raise PlanValidationError("price_volume_policy.min_samples must be between 5 and baseline_sessions")
    contraction = _finite(
        policy.get("contraction_ratio"),
        "price_volume_policy.contraction_ratio",
        minimum=0.1,
    )
    expansion = _finite(
        policy.get("expansion_ratio"),
        "price_volume_policy.expansion_ratio",
        minimum=1.0,
    )
    if contraction >= 1 or expansion <= 1 or expansion > 10:
        raise PlanValidationError("price_volume_policy volume ratios are invalid")
    flat_return_bps = _finite(
        policy.get("flat_return_bps"),
        "price_volume_policy.flat_return_bps",
        minimum=0,
    )
    acceleration = _finite(
        policy.get("acceleration_multiplier"),
        "price_volume_policy.acceleration_multiplier",
        minimum=1.0,
    )
    if flat_return_bps > 200 or acceleration > 5:
        raise PlanValidationError("price_volume_policy movement thresholds are invalid")
    return {
        "enabled": policy["enabled"],
        "interval": interval,
        "baseline_method": method,
        "baseline_sessions": baseline_sessions,
        "min_samples": min_samples,
        "contraction_ratio": contraction,
        "expansion_ratio": expansion,
        "flat_return_bps": flat_return_bps,
        "acceleration_multiplier": acceleration,
    }


def _reject_unknown_keys(value: dict[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise PlanValidationError(f"{field} contains unsupported fields: {', '.join(unknown)}")


def _normalize_analysis_ref(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlanValidationError("analysis_ref must be an object for schema_version=4")
    _reject_unknown_keys(
        value,
        {
            "snapshot_id",
            "report_ref",
            "report_type",
            "title",
            "revision",
            "body_sha256",
            "quality_status",
            "generated_at",
            "data_as_of",
            "research_snapshot_id",
        },
        "analysis_ref",
    )
    report_type = str(value.get("report_type") or "")
    quality_status = str(value.get("quality_status") or "")
    if report_type not in REPORT_TYPES:
        raise PlanValidationError("analysis_ref.report_type is not allowed")
    if quality_status not in REPORT_QUALITY_STATUSES:
        raise PlanValidationError("analysis_ref.quality_status is not allowed")
    body_sha256 = _bounded_text(
        value.get("body_sha256"), "analysis_ref.body_sha256", maximum=64
    ).lower()
    if len(body_sha256) != 64 or any(character not in "0123456789abcdef" for character in body_sha256):
        raise PlanValidationError("analysis_ref.body_sha256 must be a SHA-256 digest")
    generated_at = _aware_iso_datetime(
        value.get("generated_at"), "analysis_ref.generated_at"
    ).isoformat()
    data_as_of = _aware_iso_datetime(
        value.get("data_as_of"), "analysis_ref.data_as_of"
    ).isoformat()
    revision = _integer(value.get("revision", 1), "analysis_ref.revision", minimum=1)
    normalized = {
        "snapshot_id": _bounded_text(
            value.get("snapshot_id"), "analysis_ref.snapshot_id", maximum=80
        ),
        "report_ref": _bounded_text(
            value.get("report_ref"), "analysis_ref.report_ref", maximum=300
        ),
        "report_type": report_type,
        "title": _bounded_text(value.get("title"), "analysis_ref.title", maximum=240),
        "revision": revision,
        "body_sha256": body_sha256,
        "quality_status": quality_status,
        "generated_at": generated_at,
        "data_as_of": data_as_of,
    }
    if value.get("research_snapshot_id"):
        normalized["research_snapshot_id"] = _bounded_text(
            value.get("research_snapshot_id"),
            "analysis_ref.research_snapshot_id",
            maximum=80,
        )
    return normalized


def _scenario_fingerprint(
    *,
    intent: str,
    original_level: dict[str, Any],
    source_conditions: list[dict[str, Any]],
) -> str:
    payload = {
        "intent": intent,
        "original_level": original_level,
        "source_conditions": [
            {
                "source_text": item["source_text"],
                "role": item["role"],
            }
            for item in source_conditions
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_source_conditions(value: Any, *, prefix: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > 24:
        raise PlanValidationError(f"{prefix}.source_conditions must contain between 1 and 24 items")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        field = f"{prefix}.source_conditions[{index}]"
        if not isinstance(raw, dict):
            raise PlanValidationError(f"{field} must be an object")
        _reject_unknown_keys(
            raw,
            {"condition_id", "source_text", "role", "coverage_status", "reason", "evidence_refs"},
            field,
        )
        condition_id = _bounded_text(raw.get("condition_id"), f"{field}.condition_id", maximum=80)
        if condition_id in seen:
            raise PlanValidationError(f"{prefix}.source condition ids must be unique")
        seen.add(condition_id)
        role = str(raw.get("role") or "required")
        status = str(raw.get("coverage_status") or "mapped")
        if role not in SOURCE_CONDITION_ROLES:
            raise PlanValidationError(f"{field}.role is not allowed")
        if status not in SOURCE_CONDITION_STATUSES:
            raise PlanValidationError(f"{field}.coverage_status is not allowed")
        refs = raw.get("evidence_refs") or []
        if not isinstance(refs, list) or len(refs) > 8:
            raise PlanValidationError(f"{field}.evidence_refs must contain at most 8 items")
        normalized.append(
            {
                "condition_id": condition_id,
                "source_text": _bounded_text(raw.get("source_text"), f"{field}.source_text", maximum=800),
                "role": role,
                "coverage_status": status,
                "reason": _bounded_text(
                    raw.get("reason"), f"{field}.reason", maximum=500, required=False
                ),
                "evidence_refs": [
                    _bounded_text(item, f"{field}.evidence_refs", maximum=300)
                    for item in refs
                ],
            }
        )
    return normalized


def _normalize_condition_group(value: Any, *, field: str) -> dict[str, Any]:
    if value in (None, {}):
        return {"operator": "all", "conditions": []}
    if not isinstance(value, dict):
        raise PlanValidationError(f"{field} must be an object")
    _reject_unknown_keys(value, {"operator", "conditions"}, field)
    operator = str(value.get("operator") or "all")
    if operator not in CONDITION_GROUP_OPERATORS:
        raise PlanValidationError(f"{field}.operator is not allowed")
    conditions = value.get("conditions") or []
    if not isinstance(conditions, list) or len(conditions) > 24:
        raise PlanValidationError(f"{field}.conditions must contain at most 24 items")
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(conditions):
        prefix = f"{field}.conditions[{index}]"
        if not isinstance(raw, dict):
            raise PlanValidationError(f"{prefix} must be an object")
        _reject_unknown_keys(
            raw,
            {
                "condition_id", "source_condition_id", "kind", "operator", "value",
                "lower", "upper", "unit", "interval", "consecutive",
                "lookback_bars", "freshness_seconds", "metric", "direction",
            },
            prefix,
        )
        kind = str(raw.get("kind") or "")
        comparator = str(raw.get("operator") or "")
        interval = str(raw.get("interval") or "5m").lower()
        if kind not in CONDITION_KINDS:
            raise PlanValidationError(f"{prefix}.kind is not allowed")
        if comparator not in CONDITION_COMPARATORS:
            raise PlanValidationError(f"{prefix}.operator is not allowed")
        if interval not in COMPOUND_INTERVALS:
            raise PlanValidationError(f"{prefix}.interval is not allowed")
        condition: dict[str, Any] = {
            "condition_id": _bounded_text(raw.get("condition_id"), f"{prefix}.condition_id", maximum=80),
            "source_condition_id": _bounded_text(
                raw.get("source_condition_id"), f"{prefix}.source_condition_id", maximum=80
            ),
            "kind": kind,
            "operator": comparator,
            "interval": interval,
            "consecutive": _integer(
                raw.get("consecutive", 1), f"{prefix}.consecutive", minimum=1, maximum=5
            ),
            "lookback_bars": _integer(
                raw.get("lookback_bars", 1), f"{prefix}.lookback_bars", minimum=1, maximum=60
            ),
            "freshness_seconds": _integer(
                raw.get("freshness_seconds", 900),
                f"{prefix}.freshness_seconds",
                minimum=60,
                maximum=172800,
            ),
        }
        if kind in CONDITION_METRICS:
            metric = _bounded_text(raw.get("metric"), f"{prefix}.metric", maximum=80)
            if metric not in CONDITION_METRICS[kind]:
                raise PlanValidationError(f"{prefix}.metric is not allowed for {kind}")
            condition["metric"] = metric
        elif raw.get("metric"):
            raise PlanValidationError(f"{prefix}.metric is not allowed for {kind}")
        if raw.get("direction"):
            direction = str(raw.get("direction") or "")
            if direction not in {"bullish", "bearish", "above", "below"}:
                raise PlanValidationError(f"{prefix}.direction is not allowed")
            condition["direction"] = direction
        if comparator == "between" or kind in {"price_zone", "session_range"}:
            lower = _finite(raw.get("lower"), f"{prefix}.lower")
            upper = _finite(raw.get("upper"), f"{prefix}.upper")
            if upper <= lower:
                raise PlanValidationError(f"{prefix} range is invalid")
            condition.update(lower=lower, upper=upper)
        elif comparator not in {"positive", "negative"} and kind != "bar_direction":
            condition["value"] = _finite(raw.get("value"), f"{prefix}.value")
        if raw.get("unit"):
            condition["unit"] = _bounded_text(raw.get("unit"), f"{prefix}.unit", maximum=20)
        if kind == "cumulative_turnover" and condition.get("unit") not in {None, "CNY", "ratio"}:
            raise PlanValidationError(f"{prefix}.unit must describe turnover, not volume")
        if kind == "cumulative_volume" and condition.get("unit") == "CNY":
            raise PlanValidationError(f"{prefix}.unit cannot use turnover units for volume")
        normalized.append(condition)
    return {"operator": operator, "conditions": normalized}


def _normalize_action_template(value: Any, *, prefix: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlanValidationError(f"{prefix}.action_template must be an object")
    _reject_unknown_keys(value, {"action", "sizing", "confidence_floor"}, f"{prefix}.action_template")
    action = str(value.get("action") or "observe")
    if action not in RECOMMENDATION_ACTIONS:
        raise PlanValidationError(f"{prefix}.action_template.action is not allowed")
    confidence = str(value.get("confidence_floor") or "medium")
    if confidence not in {"low", "medium", "high"}:
        raise PlanValidationError(f"{prefix}.action_template.confidence_floor is not allowed")
    raw_sizing = value.get("sizing") or {"kind": "default_policy", "source": "system_default"}
    if not isinstance(raw_sizing, dict):
        raise PlanValidationError(f"{prefix}.action_template.sizing must be an object")
    _reject_unknown_keys(raw_sizing, {"kind", "value", "unit", "source"}, f"{prefix}.action_template.sizing")
    kind = str(raw_sizing.get("kind") or "default_policy")
    if kind not in SIZING_KINDS:
        raise PlanValidationError(f"{prefix}.action_template.sizing.kind is not allowed")
    sizing: dict[str, Any] = {
        "kind": kind,
        "source": _bounded_text(
            raw_sizing.get("source") or "system_default",
            f"{prefix}.action_template.sizing.source",
            maximum=120,
        ),
    }
    if kind != "default_policy":
        sizing["value"] = _finite(
            raw_sizing.get("value"), f"{prefix}.action_template.sizing.value", minimum=0
        )
        if kind == "position_fraction" and sizing["value"] > 1:
            raise PlanValidationError(
                f"{prefix}.action_template.sizing.value must be at most 1 for position_fraction"
            )
    if raw_sizing.get("unit"):
        sizing["unit"] = _bounded_text(
            raw_sizing.get("unit"), f"{prefix}.action_template.sizing.unit", maximum=20
        )
    return {"action": action, "sizing": sizing, "confidence_floor": confidence}


def _normalize_watch_scenarios(
    value: Any,
    *,
    rules: list[dict[str, Any]],
    schema_version: int,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > 12:
        raise PlanValidationError("watch_scenarios must contain between 1 and 12 scenarios")
    rules_by_id = {str(rule["client_rule_id"]): rule for rule in rules}
    seen_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        prefix = f"watch_scenarios[{index}]"
        if not isinstance(raw, dict):
            raise PlanValidationError(f"{prefix} must be an object")
        allowed_keys = {
            "scenario_id",
            "client_rule_id",
            "label",
            "intent",
            "evidence_refs",
            "original_level",
            "trigger",
            "approach_policy",
            "volume_confirmation",
            "resolution_policy",
            "invalidation",
            "rationale",
        }
        if schema_version >= 5:
            allowed_keys.update(
                {
                    "source_conditions",
                    "entry_conditions",
                    "confirmation_conditions",
                    "invalidation_conditions",
                    "sequence_policy",
                    "action_template",
                    "automation_status",
                    "scenario_fingerprint",
                }
            )
        _reject_unknown_keys(
            raw,
            allowed_keys,
            prefix,
        )
        scenario_id = _bounded_text(raw.get("scenario_id"), f"{prefix}.scenario_id", maximum=80)
        client_rule_id = _bounded_text(
            raw.get("client_rule_id"), f"{prefix}.client_rule_id", maximum=80
        )
        if scenario_id in seen_ids:
            raise PlanValidationError("watch scenario ids must be unique")
        seen_ids.add(scenario_id)
        rule = rules_by_id.get(client_rule_id)
        if rule is None or not str(rule.get("kind") or "").startswith("price_"):
            raise PlanValidationError(f"{prefix}.client_rule_id must reference a price rule")
        intent = str(raw.get("intent") or "")
        if intent not in TARGET_INTENTS or intent != str(rule.get("target_intent") or ""):
            raise PlanValidationError(f"{prefix}.intent must match its price rule")
        evidence_refs = raw.get("evidence_refs")
        if not isinstance(evidence_refs, list) or not evidence_refs or len(evidence_refs) > 8:
            raise PlanValidationError(f"{prefix}.evidence_refs must contain between 1 and 8 items")
        normalized_evidence_refs = [
            _bounded_text(item, f"{prefix}.evidence_refs", maximum=300)
            for item in evidence_refs
        ]

        original_level = raw.get("original_level")
        if not isinstance(original_level, dict):
            raise PlanValidationError(f"{prefix}.original_level must be an object")
        _reject_unknown_keys(
            original_level,
            {"kind", "value", "lower", "upper", "unit", "adjustment", "source_text"},
            f"{prefix}.original_level",
        )
        level_kind = str(original_level.get("kind") or "")
        if level_kind not in {"price", "zone"}:
            raise PlanValidationError(f"{prefix}.original_level.kind is not allowed")
        level = {
            "kind": level_kind,
            "unit": _bounded_text(
                original_level.get("unit") or "CNY",
                f"{prefix}.original_level.unit",
                maximum=20,
            ),
            "adjustment": str(original_level.get("adjustment") or "raw"),
        }
        if level["adjustment"] != "raw":
            raise PlanValidationError(f"{prefix}.original_level.adjustment must be raw")
        if level_kind == "price":
            level["value"] = _finite(
                original_level.get("value"), f"{prefix}.original_level.value", minimum=0.000001
            )
        else:
            lower = _finite(
                original_level.get("lower"), f"{prefix}.original_level.lower", minimum=0.000001
            )
            upper = _finite(
                original_level.get("upper"), f"{prefix}.original_level.upper", minimum=0.000001
            )
            if upper <= lower:
                raise PlanValidationError(f"{prefix}.original_level zone is invalid")
            level.update(lower=lower, upper=upper)
        if original_level.get("source_text"):
            level["source_text"] = _bounded_text(
                original_level.get("source_text"),
                f"{prefix}.original_level.source_text",
                maximum=500,
            )

        trigger = raw.get("trigger")
        if not isinstance(trigger, dict):
            raise PlanValidationError(f"{prefix}.trigger must be an object")
        _reject_unknown_keys(
            trigger,
            {"kind", "threshold", "lower", "upper", "interval", "confirmation_count"},
            f"{prefix}.trigger",
        )
        trigger_kind = str(trigger.get("kind") or "")
        if trigger_kind != str(rule.get("kind") or ""):
            raise PlanValidationError(f"{prefix}.trigger.kind must match its price rule")
        interval = str(trigger.get("interval") or "5m")
        if interval not in INTERVALS:
            raise PlanValidationError(f"{prefix}.trigger.interval is not allowed")
        confirmation_count = _integer(
            trigger.get("confirmation_count", 2),
            f"{prefix}.trigger.confirmation_count",
            minimum=1,
            maximum=3,
        )
        normalized_trigger: dict[str, Any] = {
            "kind": trigger_kind,
            "interval": interval,
            "confirmation_count": confirmation_count,
        }
        if trigger_kind.startswith("price_cross"):
            threshold = _finite(
                trigger.get("threshold"), f"{prefix}.trigger.threshold", minimum=0.000001
            )
            if threshold != float(rule["parameters"].get("threshold")):
                raise PlanValidationError(f"{prefix}.trigger threshold must match its price rule")
            normalized_trigger["threshold"] = threshold
        else:
            lower = _finite(trigger.get("lower"), f"{prefix}.trigger.lower", minimum=0.000001)
            upper = _finite(trigger.get("upper"), f"{prefix}.trigger.upper", minimum=0.000001)
            if lower != float(rule["parameters"].get("lower")) or upper != float(
                rule["parameters"].get("upper")
            ):
                raise PlanValidationError(f"{prefix}.trigger zone must match its price rule")
            normalized_trigger.update(lower=lower, upper=upper)
        if interval != str(rule["parameters"].get("interval")) or confirmation_count != int(
            rule["parameters"].get("confirmation_count") or 0
        ):
            raise PlanValidationError(f"{prefix}.trigger confirmation must match its price rule")

        approach = raw.get("approach_policy")
        if not isinstance(approach, dict):
            raise PlanValidationError(f"{prefix}.approach_policy must be an object")
        _reject_unknown_keys(
            approach,
            {"distance_bps", "source", "check_interval"},
            f"{prefix}.approach_policy",
        )
        distance_bps = _integer(
            approach.get("distance_bps"),
            f"{prefix}.approach_policy.distance_bps",
            minimum=10,
            maximum=500,
        )
        distance_source = str(approach.get("source") or "")
        if distance_source not in APPROACH_DISTANCE_SOURCES:
            raise PlanValidationError(f"{prefix}.approach_policy.source is not allowed")
        if str(approach.get("check_interval") or "1m") != "1m":
            raise PlanValidationError(f"{prefix}.approach_policy.check_interval must be 1m")

        volume = raw.get("volume_confirmation")
        if not isinstance(volume, dict):
            raise PlanValidationError(f"{prefix}.volume_confirmation must be an object")
        _reject_unknown_keys(
            volume,
            {"metric", "comparator", "threshold", "min_samples", "mode", "unit"},
            f"{prefix}.volume_confirmation",
        )
        metric = str(volume.get("metric") or "")
        comparator = str(volume.get("comparator") or "")
        if metric not in VOLUME_CONFIRMATION_METRICS:
            raise PlanValidationError(f"{prefix}.volume_confirmation.metric is not allowed")
        if comparator not in VOLUME_CONFIRMATION_COMPARATORS:
            raise PlanValidationError(f"{prefix}.volume_confirmation.comparator is not allowed")
        if str(volume.get("mode") or "") != "classify_only":
            raise PlanValidationError(f"{prefix}.volume_confirmation.mode must be classify_only")
        unit = str(volume.get("unit") or ("ratio" if metric != "absolute_cumulative_volume" else ""))
        if metric == "absolute_cumulative_volume" and unit not in VOLUME_UNITS:
            raise PlanValidationError(
                f"{prefix}.volume_confirmation.unit is required for absolute volume"
            )
        if metric != "absolute_cumulative_volume" and unit != "ratio":
            raise PlanValidationError(f"{prefix}.volume_confirmation.unit must be ratio")
        normalized_volume = {
            "metric": metric,
            "comparator": comparator,
            "threshold": _finite(
                volume.get("threshold"),
                f"{prefix}.volume_confirmation.threshold",
                minimum=0,
            ),
            "min_samples": _integer(
                volume.get("min_samples", 5),
                f"{prefix}.volume_confirmation.min_samples",
                minimum=1,
                maximum=30,
            ),
            "mode": "classify_only",
            "unit": unit,
        }

        resolution = raw.get("resolution_policy")
        if not isinstance(resolution, dict):
            raise PlanValidationError(f"{prefix}.resolution_policy must be an object")
        _reject_unknown_keys(
            resolution,
            {"rejection_hysteresis_bps", "max_observation_bars", "close_action"},
            f"{prefix}.resolution_policy",
        )
        if str(resolution.get("close_action") or "") != "unresolved":
            raise PlanValidationError(f"{prefix}.resolution_policy.close_action must be unresolved")
        normalized_resolution = {
            "rejection_hysteresis_bps": _integer(
                resolution.get("rejection_hysteresis_bps", 30),
                f"{prefix}.resolution_policy.rejection_hysteresis_bps",
                minimum=0,
                maximum=500,
            ),
            "max_observation_bars": _integer(
                resolution.get("max_observation_bars", 6),
                f"{prefix}.resolution_policy.max_observation_bars",
                minimum=1,
                maximum=24,
            ),
            "close_action": "unresolved",
        }
        normalized_scenario = {
            "scenario_id": scenario_id,
            "client_rule_id": client_rule_id,
            "label": _bounded_text(raw.get("label"), f"{prefix}.label", maximum=160),
            "intent": intent,
            "evidence_refs": normalized_evidence_refs,
            "original_level": level,
            "trigger": normalized_trigger,
            "approach_policy": {
                "distance_bps": distance_bps,
                "source": distance_source,
                "check_interval": "1m",
            },
            "volume_confirmation": normalized_volume,
            "resolution_policy": normalized_resolution,
            "rationale": _bounded_text(
                raw.get("rationale"), f"{prefix}.rationale", maximum=1200
            ),
        }
        if raw.get("invalidation") is not None:
            invalidation = raw.get("invalidation")
            if not isinstance(invalidation, dict):
                raise PlanValidationError(f"{prefix}.invalidation must be an object")
            _reject_unknown_keys(
                invalidation,
                {"kind", "level"},
                f"{prefix}.invalidation",
            )
            invalidation_kind = str(invalidation.get("kind") or "")
            if invalidation_kind not in {"price_cross_above", "price_cross_below"}:
                raise PlanValidationError(f"{prefix}.invalidation.kind is not allowed")
            normalized_scenario["invalidation"] = {
                "kind": invalidation_kind,
                "level": _finite(
                    invalidation.get("level"), f"{prefix}.invalidation.level", minimum=0.000001
                ),
            }
        if schema_version >= 5:
            source_conditions = _normalize_source_conditions(raw.get("source_conditions"), prefix=prefix)
            source_ids = {item["condition_id"] for item in source_conditions}
            condition_groups = {
                name: _normalize_condition_group(raw.get(name), field=f"{prefix}.{name}")
                for name in (
                    "entry_conditions",
                    "confirmation_conditions",
                    "invalidation_conditions",
                )
            }
            mapped_ids = {
                condition["source_condition_id"]
                for group in condition_groups.values()
                for condition in group["conditions"]
            }
            unknown_ids = mapped_ids - source_ids
            if unknown_ids:
                raise PlanValidationError(
                    f"{prefix} compound conditions reference unknown source conditions"
                )
            declared_mapped = {
                item["condition_id"]
                for item in source_conditions
                if item["coverage_status"] == "mapped"
            }
            if not declared_mapped.issubset(mapped_ids):
                raise PlanValidationError(
                    f"{prefix} mapped source conditions must have an executable condition"
                )
            raw_sequence = raw.get("sequence_policy") or {}
            if not isinstance(raw_sequence, dict):
                raise PlanValidationError(f"{prefix}.sequence_policy must be an object")
            _reject_unknown_keys(
                raw_sequence,
                {"enabled", "max_wait_bars", "reset_on_invalidation"},
                f"{prefix}.sequence_policy",
            )
            enabled = raw_sequence.get("enabled", bool(condition_groups["confirmation_conditions"]["conditions"]))
            reset_on_invalidation = raw_sequence.get("reset_on_invalidation", True)
            if not isinstance(enabled, bool) or not isinstance(reset_on_invalidation, bool):
                raise PlanValidationError(f"{prefix}.sequence_policy flags must be booleans")
            sequence_policy = {
                "enabled": enabled,
                "max_wait_bars": _integer(
                    raw_sequence.get("max_wait_bars", normalized_resolution["max_observation_bars"]),
                    f"{prefix}.sequence_policy.max_wait_bars",
                    minimum=1,
                    maximum=60,
                ),
                "reset_on_invalidation": reset_on_invalidation,
            }
            action_template = _normalize_action_template(raw.get("action_template"), prefix=prefix)
            required_pending = any(
                item["role"] == "required" and item["coverage_status"] != "mapped"
                for item in source_conditions
            )
            automation_status = str(
                raw.get("automation_status")
                or ("watch_only" if required_pending else "action_ready")
            )
            if automation_status not in AUTOMATION_STATUSES:
                raise PlanValidationError(f"{prefix}.automation_status is not allowed")
            if required_pending and automation_status != "watch_only":
                raise PlanValidationError(
                    f"{prefix} must be watch_only while a required source condition is unavailable"
                )
            fingerprint = _scenario_fingerprint(
                intent=intent,
                original_level=level,
                source_conditions=source_conditions,
            )
            supplied_fingerprint = str(raw.get("scenario_fingerprint") or "")
            if supplied_fingerprint and supplied_fingerprint != fingerprint:
                raise PlanValidationError(f"{prefix}.scenario_fingerprint does not match its source conditions")
            normalized_scenario.update(
                source_conditions=source_conditions,
                sequence_policy=sequence_policy,
                action_template=action_template,
                automation_status=automation_status,
                scenario_fingerprint=fingerprint,
                **condition_groups,
            )
        normalized.append(normalized_scenario)
    return normalized


def validate_plan(payload: dict[str, Any], *, expected_symbol: str | None = None) -> dict[str, Any]:
    """Return a normalized copy of a plan or fail closed."""

    if not isinstance(payload, dict):
        raise PlanValidationError("plan must be an object")
    plan = copy.deepcopy(payload)
    schema_version = _integer(plan.get("schema_version", 1), "schema_version")
    if schema_version not in {1, 2, 3, 4, 5}:
        raise PlanValidationError("schema_version must be 1, 2, 3, 4, or 5")
    symbol = str(plan.get("symbol") or "").strip().upper()
    if not symbol or (expected_symbol and symbol != expected_symbol.upper()):
        raise PlanValidationError("plan symbol does not match the monitor profile")
    quote_tier = str(plan.get("quote_tier") or "normal")
    near_tier = str(plan.get("near_trigger_tier") or "active")
    data_mode = str(plan.get("data_mode") or "verified")
    if quote_tier not in TIERS or near_tier not in TIERS:
        raise PlanValidationError("quote tier is not allowed")
    if data_mode not in DATA_MODES:
        raise PlanValidationError("data mode is not allowed")
    near_bps = _integer(
        plan.get("near_trigger_distance_bps", 100),
        "near_trigger_distance_bps",
    )
    if not 10 <= near_bps <= 500:
        raise PlanValidationError("near_trigger_distance_bps must be between 10 and 500")

    rules = plan.get("market_rules") or []
    if not isinstance(rules, list) or len(rules) > 12:
        raise PlanValidationError("market_rules must contain at most 12 rules")
    normalized_rules: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    cue_rule_ids_by_kind: dict[str, list[str]] = {
        "price_cross_above": [],
        "price_cross_below": [],
    }
    for index, raw in enumerate(rules):
        if not isinstance(raw, dict):
            raise PlanValidationError(f"market_rules[{index}] must be an object")
        rule = copy.deepcopy(raw)
        rule_id = str(rule.get("client_rule_id") or f"rule-{index + 1}").strip()
        kind = str(rule.get("kind") or "")
        if not rule_id or rule_id in seen_ids or kind not in RULE_KINDS:
            raise PlanValidationError(f"market_rules[{index}] has an invalid id or kind")
        seen_ids.add(rule_id)
        severity = str(rule.get("severity") or "warning")
        if severity not in SEVERITIES:
            raise PlanValidationError(f"market_rules[{index}].severity is not allowed")
        raw_enabled = rule.get("enabled", True)
        if not isinstance(raw_enabled, bool):
            raise PlanValidationError(f"market_rules[{index}].enabled must be a boolean")
        enabled = raw_enabled
        alert_cue = str(rule.get("alert_cue") or "none")
        if alert_cue not in ALERT_CUES:
            raise PlanValidationError(f"market_rules[{index}].alert_cue is not allowed")
        if alert_cue != "none" and schema_version < 3:
            raise PlanValidationError("alert_cue requires schema_version=3")
        if alert_cue == "ymca_v1":
            if kind not in cue_rule_ids_by_kind or not enabled:
                raise PlanValidationError(
                    "ymca_v1 is only allowed on an enabled price_cross_above or "
                    "price_cross_below rule"
                )
            cue_rule_ids_by_kind[kind].append(rule_id)
        raw_parameters = rule.get("parameters")
        if raw_parameters is None:
            raw_parameters = {}
        if not isinstance(raw_parameters, dict):
            raise PlanValidationError(f"market_rules[{index}].parameters must be an object")
        parameters = copy.deepcopy(raw_parameters)
        interval = str(parameters.get("interval") or "5m")
        if interval not in INTERVALS:
            raise PlanValidationError(f"market_rules[{index}].interval is not allowed")
        confirmation = _integer(
            parameters.get("confirmation_count", 2),
            "confirmation_count",
        )
        cooldown = _integer(
            parameters.get("cooldown_minutes", 60),
            "cooldown_minutes",
        )
        hysteresis = _integer(
            parameters.get("clear_hysteresis_bps", 30),
            "clear_hysteresis_bps",
        )
        if not 1 <= confirmation <= 3:
            raise PlanValidationError("confirmation_count must be between 1 and 3")
        if not 5 <= cooldown <= 1440:
            raise PlanValidationError("cooldown_minutes must be between 5 and 1440")
        if not 0 <= hysteresis <= 500:
            raise PlanValidationError("clear_hysteresis_bps must be between 0 and 500")
        parameters.update(
            interval=interval,
            adjustment=str(parameters.get("adjustment") or "raw"),
            confirmation_count=confirmation,
            cooldown_minutes=cooldown,
            clear_hysteresis_bps=hysteresis,
        )
        if parameters["adjustment"] != "raw":
            raise PlanValidationError("initial monitoring only accepts raw prices")
        target_intent: str | None = None
        target_level: int | None = None
        if kind.startswith("price_cross"):
            parameters["threshold"] = _finite(parameters.get("threshold"), "threshold", minimum=0.000001)
        elif kind.startswith("price_zone"):
            lower = _finite(parameters.get("lower"), "lower", minimum=0.000001)
            upper = _finite(parameters.get("upper"), "upper", minimum=0.000001)
            if upper <= lower:
                raise PlanValidationError("price zone upper must be greater than lower")
            parameters.update(lower=lower, upper=upper)
        elif kind.startswith("intraday_pct_change"):
            parameters["threshold_pct"] = _finite(parameters.get("threshold_pct"), "threshold_pct")
        else:
            ratio = _finite(parameters.get("ratio"), "ratio", minimum=0.01)
            clear_ratio = _finite(parameters.get("clear_ratio", ratio * 0.8), "clear_ratio", minimum=0)
            if clear_ratio >= ratio:
                raise PlanValidationError("volume clear_ratio must be below ratio")
            raw_baseline_sessions = _finite(
                parameters.get("baseline_sessions", 10), "baseline_sessions"
            )
            raw_min_samples = _finite(parameters.get("min_samples", 5), "min_samples")
            baseline_sessions = int(raw_baseline_sessions)
            min_samples = int(raw_min_samples)
            if raw_baseline_sessions != baseline_sessions or raw_min_samples != min_samples:
                raise PlanValidationError("volume baseline sample counts must be integers")
            if not 5 <= baseline_sessions <= 30 or not 5 <= min_samples <= baseline_sessions:
                raise PlanValidationError("volume baseline history parameters are invalid")
            parameters.update(
                ratio=ratio,
                clear_ratio=clear_ratio,
                baseline_method="same_time_bucket_median",
                baseline_sessions=baseline_sessions,
                min_samples=min_samples,
            )
        if kind.startswith("price_"):
            default_intent = "breakout" if kind == "price_cross_above" else "watch"
            target_intent = str(rule.get("target_intent") or default_intent)
            if target_intent not in TARGET_INTENTS:
                raise PlanValidationError(f"market_rules[{index}].target_intent is not allowed")
            if kind == "price_cross_above" and target_intent == "stop_loss":
                raise PlanValidationError(
                    f"market_rules[{index}].target_intent stop_loss conflicts with an upward trigger"
                )
            if kind == "price_cross_below" and target_intent == "take_profit":
                raise PlanValidationError(
                    f"market_rules[{index}].target_intent take_profit conflicts with a downward trigger"
                )
            raw_level = _finite(rule.get("target_level", 1), "target_level")
            target_level = int(raw_level)
            if raw_level != target_level or not 1 <= target_level <= 4:
                raise PlanValidationError("target_level must be an integer between 1 and 4")
        normalized_rule = {
            "client_rule_id": rule_id,
            "kind": kind,
            "severity": severity,
            "enabled": enabled,
            "alert_cue": alert_cue,
            "parameters": parameters,
            "valid_until": rule.get("valid_until"),
            "rationale": str(rule.get("rationale") or "").strip(),
        }
        calculation_basis = _normalize_calculation_basis(
            rule.get("calculation_basis"),
            rule_index=index,
        )
        if calculation_basis is not None:
            if not kind.startswith("price_"):
                raise PlanValidationError("calculation_basis is only allowed for price rules")
            normalized_rule["calculation_basis"] = calculation_basis
        if target_intent is not None and target_level is not None:
            normalized_rule.update(
                target_intent=target_intent,
                target_level=target_level,
            )
        normalized_rules.append(normalized_rule)

    for kind, cue_rule_ids in cue_rule_ids_by_kind.items():
        if len(cue_rule_ids) > 1:
            raise PlanValidationError(
                f"at most one {kind} market rule may use ymca_v1"
            )

    _validate_target_ladder(normalized_rules)

    tier_seconds = {"active": 60, "normal": 300, "low": 900}
    interval_seconds = {"1m": 60, "5m": 300}
    enabled_intervals = [
        str(rule["parameters"]["interval"])
        for rule in normalized_rules
        if rule["enabled"]
    ]
    if enabled_intervals and any(
        tier_seconds[quote_tier] > interval_seconds[interval]
        for interval in enabled_intervals
    ):
        raise PlanValidationError(
            "quote tier cannot be slower than an enabled rule interval"
        )

    news_topics = plan.get("news_topics") or []
    if not isinstance(news_topics, list) or len(news_topics) > 8:
        raise PlanValidationError("news_topics must contain at most 8 topics")
    for topic in news_topics:
        if not isinstance(topic, dict) or not str(topic.get("semantic_description") or "").strip():
            raise PlanValidationError("each news topic needs a semantic_description")

    hard_valid_until = str(plan.get("hard_valid_until") or "").strip()
    if not hard_valid_until:
        raise PlanValidationError("hard_valid_until is required")
    try:
        datetime.fromisoformat(hard_valid_until.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PlanValidationError("hard_valid_until must be ISO-8601") from exc

    fundamental_monitor = plan.get("fundamental_monitor") or {"enabled": False}
    if not isinstance(fundamental_monitor, dict):
        raise PlanValidationError("fundamental_monitor must be an object")
    fundamental_enabled = fundamental_monitor.get("enabled", False)
    if not isinstance(fundamental_enabled, bool):
        raise PlanValidationError("fundamental_monitor.enabled must be a boolean")

    plan.update(
        schema_version=schema_version,
        symbol=symbol,
        data_mode=data_mode,
        quote_tier=quote_tier,
        near_trigger_tier=near_tier,
        near_trigger_distance_bps=near_bps,
        market_rules=normalized_rules,
        news_topics=news_topics,
        fundamental_monitor=copy.deepcopy(fundamental_monitor),
        hard_valid_until=hard_valid_until,
    )
    if schema_version >= 2:
        plan["price_volume_policy"] = _normalize_price_volume_policy(
            plan.get("price_volume_policy")
        )
    else:
        # A v1 plan remains price-only even if an older client echoes unknown
        # fields.  Enabling the feature requires an explicit v2 draft.
        plan.pop("price_volume_policy", None)
    if schema_version >= 4:
        plan["analysis_ref"] = _normalize_analysis_ref(plan.get("analysis_ref"))
        plan["watch_scenarios"] = _normalize_watch_scenarios(
            plan.get("watch_scenarios"),
            rules=normalized_rules,
            schema_version=schema_version,
        )
    else:
        plan.pop("analysis_ref", None)
        plan.pop("watch_scenarios", None)
    if schema_version >= 5:
        automation_policy = plan.get("automation_policy")
        if not isinstance(automation_policy, dict):
            raise PlanValidationError("automation_policy must be an object for schema_version=5")
        _reject_unknown_keys(
            automation_policy,
            {"activation_mode", "activated_by", "evidence_fingerprint", "trade_execution", "trigger_type"},
            "automation_policy",
        )
        if str(automation_policy.get("activation_mode") or "") != "autonomous":
            raise PlanValidationError("automation_policy.activation_mode must be autonomous")
        if str(automation_policy.get("activated_by") or "") != "autopilot":
            raise PlanValidationError("automation_policy.activated_by must be autopilot")
        if str(automation_policy.get("trade_execution") or "") != "forbidden":
            raise PlanValidationError("automation_policy.trade_execution must be forbidden")
        plan["automation_policy"] = {
            "activation_mode": "autonomous",
            "activated_by": "autopilot",
            "evidence_fingerprint": _bounded_text(
                automation_policy.get("evidence_fingerprint"),
                "automation_policy.evidence_fingerprint",
                maximum=128,
                required=False,
            ),
            "trade_execution": "forbidden",
        }
        if automation_policy.get("trigger_type"):
            plan["automation_policy"]["trigger_type"] = _bounded_text(
                automation_policy.get("trigger_type"),
                "automation_policy.trigger_type",
                maximum=80,
            )
    else:
        plan.pop("automation_policy", None)
    return plan


def validate_plan_for_activation(
    payload: dict[str, Any],
    *,
    expected_symbol: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate a plan plus the time-bounded guarantees required to activate it."""

    plan = validate_plan(payload, expected_symbol=expected_symbol)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    minimum = current + timedelta(days=30)
    maximum = current + timedelta(days=365)
    hard_valid_until = _aware_iso_datetime(
        plan.get("hard_valid_until"),
        "hard_valid_until",
    )
    if hard_valid_until < minimum or hard_valid_until > maximum:
        raise PlanValidationError(
            "hard_valid_until must be between 30 and 365 days in the future"
        )

    for index, rule in enumerate(plan.get("market_rules") or []):
        rule_valid_until = _aware_iso_datetime(
            rule.get("valid_until"),
            f"market_rules[{index}].valid_until",
        )
        if rule_valid_until < minimum or rule_valid_until > maximum:
            raise PlanValidationError(
                f"market_rules[{index}].valid_until must be between 30 and 365 days in the future"
            )
        if rule_valid_until > hard_valid_until:
            raise PlanValidationError(
                f"market_rules[{index}].valid_until cannot exceed hard_valid_until"
            )
    return plan
