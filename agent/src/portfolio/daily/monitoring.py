"""Deterministic MonitoringBundle assembly for holding daily reports.

The model proposes typed scenario content only. IDs, Claim links, degradation,
cross-report diffs, and final validation are deterministic and reuse the
portfolio monitoring contract.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from src.portfolio.monitoring.models import (
    DEFAULT_PRICE_VOLUME_POLICY,
    PlanValidationError,
    validate_monitoring_bundle,
    validate_monitoring_candidate,
)


CLAIM_SECTIONS = (
    "daily_level",
    "daily_trigger",
    "daily_confirmation",
    "daily_volume_confirmation",
    "daily_invalidation",
    "daily_action",
    "daily_calculation_basis",
    "daily_interpretation",
)


def structured_monitoring_enabled() -> bool:
    return os.getenv("PORTFOLIO_DAILY_STRUCTURED_MONITORING_ENABLED", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _sha(prefix: str, *parts: Any, length: int = 20) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:length]}"


def _aware(value: Any, *, fallback: datetime | None = None) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = fallback or datetime.now(timezone.utc)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _instrument_type(symbol: str) -> str:
    code = symbol.split(".", 1)[0]
    return "etf" if code.startswith(("15", "16", "50", "51", "52", "56", "58")) else "company_equity"


def _tick_size(symbol: str) -> float:
    return 0.001 if _instrument_type(symbol) == "etf" else 0.01


def _semantic_text(value: Any) -> str:
    text = re.sub(r"[-+]?\d+(?:\.\d+)?", "#", str(value or "").casefold())
    return re.sub(r"\s+", "", text)[:500]


def _family_id(symbol: str, candidate: dict[str, Any]) -> str:
    conditions = candidate.get("source_conditions") or []
    semantics = [
        {
            "role": item.get("role"),
            "source_text": _semantic_text(item.get("source_text")),
        }
        for item in conditions
        if isinstance(item, dict)
    ]
    return _sha(
        "scenario",
        symbol,
        candidate.get("intent"),
        candidate.get("trigger", {}).get("kind") if isinstance(candidate.get("trigger"), dict) else None,
        _semantic_text(candidate.get("label")),
        semantics,
    )


def _level_value(candidate: dict[str, Any]) -> float | None:
    level = candidate.get("original_level")
    if not isinstance(level, dict):
        return None
    try:
        if level.get("kind") == "zone":
            return (float(level.get("lower")) + float(level.get("upper"))) / 2
        return float(level.get("value"))
    except (TypeError, ValueError):
        return None


def _source_condition_refs(
    candidate: dict[str, Any],
    group_names: tuple[str, ...] = (
        "entry_conditions",
        "confirmation_conditions",
        "invalidation_conditions",
    ),
) -> set[str]:
    refs: set[str] = set()
    for group_name in group_names:
        group = candidate.get(group_name)
        if not isinstance(group, dict):
            continue
        for condition in group.get("conditions") or []:
            if isinstance(condition, dict) and condition.get("source_condition_id"):
                refs.add(str(condition["source_condition_id"]))
    return refs


def _normalize_condition_shapes(candidate: dict[str, Any]) -> list[str]:
    """Normalize a known nested condition shorthand into the existing v5 fields."""

    warnings: list[str] = []
    allowed_parameters = {
        "operator",
        "value",
        "level",
        "lower",
        "upper",
        "unit",
        "interval",
        "consecutive",
        "lookback_bars",
        "freshness_seconds",
        "metric",
        "direction",
    }
    for group_name in (
        "entry_conditions",
        "confirmation_conditions",
        "invalidation_conditions",
    ):
        group = candidate.get(group_name)
        if not isinstance(group, dict) or not isinstance(group.get("conditions"), list):
            continue
        for index, raw in enumerate(group["conditions"]):
            if not isinstance(raw, dict):
                continue
            parameters = raw.get("parameters")
            if parameters is not None:
                if not isinstance(parameters, dict) or set(parameters) - allowed_parameters:
                    continue
                normalized = {
                    key: value
                    for key, value in raw.items()
                    if key not in {"parameters", "label"}
                }
                normalized.update(parameters)
                if "level" in normalized and "value" not in normalized:
                    normalized["value"] = normalized.pop("level")
                group["conditions"][index] = normalized
                raw = normalized
                warnings.append("已将已知的嵌套条件格式确定性转换为现有监控白名单字段。")
            if not raw.get("condition_id"):
                raw["condition_id"] = _sha(
                    "condition",
                    candidate.get("candidate_id"),
                    group_name,
                    index,
                    raw.get("source_condition_id"),
                    raw.get("kind"),
                    length=16,
                )
            kind = str(raw.get("kind") or "")
            if kind == "price_reclaim" and not raw.get("operator"):
                raw["operator"] = "gte"
            elif kind in {"price_zone", "session_range"} and not raw.get("operator"):
                raw["operator"] = "between"
            elif kind == "bar_direction":
                direction = str(raw.get("direction") or "").lower()
                raw["direction"] = {
                    "up": "bullish",
                    "down": "bearish",
                }.get(direction, direction)
                raw.setdefault(
                    "operator",
                    "negative" if raw.get("direction") == "bearish" else "positive",
                )
    return list(dict.fromkeys(warnings))


def _normalize_engine_compatibility(candidate: dict[str, Any]) -> list[str]:
    """Map model shorthand to the existing engine without losing report semantics.

    The runtime price-rule engine only evaluates 1m/5m triggers.  A report-level
    daily trigger therefore becomes a minute price *reminder* while the original
    daily requirement is retained as an unsupported required source condition.
    The candidate is consequently never action-ready.
    """

    warnings: list[str] = []
    approach = candidate.get("approach_policy")
    if isinstance(approach, dict):
        if approach.get("source") == "system_default":
            approach["source"] = "atr20_default"
        if str(approach.get("check_interval") or "1m") != "1m":
            approach["check_interval"] = "1m"

    trigger = candidate.get("trigger")
    if not isinstance(trigger, dict):
        return warnings
    original_interval = str(trigger.get("interval") or "")
    if original_interval in {"30m", "1d"}:
        trigger_kind = str(trigger.get("kind") or "")
        threshold = trigger.get("threshold")
        if trigger_kind.startswith("price_zone"):
            level_text = f"{trigger.get('lower')}至{trigger.get('upper')}"
        else:
            level_text = str(threshold)
        direction = "上穿" if trigger_kind.endswith("above") else "下穿"
        if trigger_kind.startswith("price_zone"):
            direction = "进入区间" if trigger_kind.endswith("enter") else "离开区间"
        source_id = _sha(
            "source_condition",
            candidate.get("label"),
            trigger_kind,
            original_interval,
            threshold,
            trigger.get("lower"),
            trigger.get("upper"),
            length=16,
        )
        sources = candidate.setdefault("source_conditions", [])
        if isinstance(sources, list) and not any(
            isinstance(item, dict) and item.get("condition_id") == source_id
            for item in sources
        ):
            sources.append(
                {
                    "condition_id": source_id,
                    "source_text": f"{original_interval}闭合K线{direction}{level_text}",
                    "role": "required",
                    "coverage_status": "unsupported",
                    "reason": "当前价格触发引擎不直接执行日线或30分钟闭合条件；原条件保留，分钟规则仅作价格提醒。",
                    "evidence_refs": [],
                }
            )
        trigger["interval"] = "1m"
        trigger["confirmation_count"] = 1
        candidate["automation_status"] = "watch_only"
        warnings.append(
            "日线或30分钟触发条件已逐字义保留为必需条件；1分钟触发仅用于价格提醒，不构成条件确认。"
        )
    return warnings


def _degrade_unsafe_mappings(candidate: dict[str, Any]) -> list[str]:
    """Preserve unsupported source wording while removing unsafe executable mappings."""

    reasons: list[str] = []
    groups = {
        name: candidate.get(name)
        for name in ("entry_conditions", "confirmation_conditions", "invalidation_conditions")
    }
    invalid_source_ids: set[str] = set()
    for source in candidate.get("source_conditions") or []:
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("condition_id") or "")
        text = str(source.get("source_text") or "")
        source_id_lower = source_id.casefold()
        mapped_conditions = [
            condition
            for group in groups.values()
            if isinstance(group, dict)
            for condition in group.get("conditions") or []
            if isinstance(condition, dict) and str(condition.get("source_condition_id") or "") == source_id
        ]
        daily_wording = any(
            token in text for token in ("日线", "日K", "收盘", "连续两日", "连续2日")
        ) or any(token in source_id_lower for token in ("daily", "_day", "day_"))
        daily_volume = daily_wording and (
            any(token in text for token in ("成交量", "均量", "放量", "缩量"))
            or "volume" in source_id_lower
        )
        turnover_as_volume = "成交额" in text and any(
            str(item.get("kind") or "") in {"volume_ratio", "cumulative_volume"}
            for item in mapped_conditions
        )
        shortened_daily = daily_wording and any(
            str(item.get("interval") or "") in {"1m", "5m", "30m"}
            for item in mapped_conditions
        )
        if daily_volume or turnover_as_volume or shortened_daily:
            source["coverage_status"] = "unsupported" if daily_volume or turnover_as_volume else "ambiguous"
            source["reason"] = (
                "当前实时引擎不支持该日线量价条件，保留原文并仅作观察。"
                if daily_volume
                else "成交额条件不得映射为成交量条件。"
                if turnover_as_volume
                else "日线或收盘条件不得简化为盘中分钟条件。"
            )
            invalid_source_ids.add(source_id)
            reasons.append(source["reason"])
    if invalid_source_ids:
        for group in groups.values():
            if isinstance(group, dict) and isinstance(group.get("conditions"), list):
                group["conditions"] = [
                    item
                    for item in group["conditions"]
                    if not isinstance(item, dict)
                    or str(item.get("source_condition_id") or "") not in invalid_source_ids
                ]
        candidate["automation_status"] = "watch_only"

    for source in candidate.get("source_conditions") or []:
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("condition_id") or "")
        relevant_groups = (
            ("invalidation_conditions",)
            if source.get("role") == "invalidation"
            else ("entry_conditions", "confirmation_conditions")
        )
        referenced = _source_condition_refs(candidate, relevant_groups)
        if source.get("coverage_status") == "mapped" and source_id not in referenced:
            source["coverage_status"] = "awaiting_data"
            source["reason"] = "必要条件没有对应的可执行白名单条件。"
            if source.get("role") == "required":
                candidate["automation_status"] = "watch_only"
            reasons.append(source["reason"])
    return list(dict.fromkeys(reasons))


def _normalize_absolute_volume(candidate: dict[str, Any]) -> str | None:
    volume = candidate.get("volume_confirmation")
    if not isinstance(volume, dict) or volume.get("metric") != "absolute_cumulative_volume":
        return None
    unit = str(volume.get("unit") or "")
    if unit in {"shares", "lots"}:
        return None
    candidate["volume_confirmation"] = {
        "metric": "same_bucket_5m_volume_ratio",
        "comparator": "gte",
        "threshold": DEFAULT_PRICE_VOLUME_POLICY["expansion_ratio"],
        "min_samples": DEFAULT_PRICE_VOLUME_POLICY["min_samples"],
        "unit": "ratio",
        "mode": "classify_only",
    }
    candidate["automation_status"] = "watch_only"
    return "绝对成交量单位未确认，已禁用绝对量规则；价格提醒保留为仅观察。"


def _claim_texts(candidate: dict[str, Any]) -> dict[str, str]:
    level = candidate.get("original_level") or {}
    trigger = candidate.get("trigger") or {}
    confirmation = candidate.get("confirmation_conditions") or {}
    volume = candidate.get("volume_confirmation") or {}
    invalidation = candidate.get("invalidation") or {}
    action = candidate.get("action_template") or {}
    basis = candidate.get("calculation_basis") or {}
    interpretation = candidate.get("interpretation") or {}
    return {
        "daily_level": str(level.get("source_text") or json.dumps(level, ensure_ascii=False, sort_keys=True)),
        "daily_trigger": json.dumps(trigger, ensure_ascii=False, sort_keys=True),
        "daily_confirmation": json.dumps(confirmation, ensure_ascii=False, sort_keys=True),
        "daily_volume_confirmation": json.dumps(volume, ensure_ascii=False, sort_keys=True),
        "daily_invalidation": json.dumps(invalidation, ensure_ascii=False, sort_keys=True),
        "daily_action": json.dumps(action, ensure_ascii=False, sort_keys=True),
        "daily_calculation_basis": str(basis.get("summary") or basis.get("formula") or ""),
        "daily_interpretation": "；".join(str(interpretation.get(key) or "") for key in sorted(interpretation)),
    }


def _attach_claims(
    *,
    run_id: str,
    symbol: str,
    candidate: dict[str, Any],
) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    claim_ids: list[str] = []
    for section, text in _claim_texts(candidate).items():
        claim_id = _sha("claim", run_id, symbol, candidate["candidate_id"], section, text)
        claim_ids.append(claim_id)
        claims.append(
            {
                "claim_id": claim_id,
                "section_id": section,
                "claim_type": "inference" if section != "daily_level" else "fact",
                "text": text[:4000],
                "fact_ids": [],
                "evidence_ids": [],
            }
        )
    candidate["claim_ids"] = claim_ids
    candidate["evidence_refs"] = claim_ids
    for source in candidate.get("source_conditions") or []:
        if not isinstance(source, dict):
            continue
        refs = [str(item) for item in source.get("evidence_refs") or [] if str(item).strip()]
        source["evidence_refs"] = list(dict.fromkeys([*refs, *claim_ids[:3]]))[:8]
    return claims


def _apply_diff(
    candidate: dict[str, Any], previous_by_family: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    previous = previous_by_family.get(str(candidate["scenario_family_id"]))
    current_level = _level_value(candidate)
    if previous is None:
        candidate.update(
            change_type="new",
            previous_candidate_id=None,
            change_details={"current_level": current_level, "summary": "本期新增观察场景。"},
        )
        return {
            "scenario_family_id": candidate["scenario_family_id"],
            "candidate_id": candidate["candidate_id"],
            "previous_candidate_id": None,
            "change_type": "new",
            "change_details": candidate["change_details"],
        }
    previous_level = _level_value(previous)
    if previous.get("intent") != candidate.get("intent") or previous.get("trigger", {}).get("kind") != candidate.get("trigger", {}).get("kind"):
        change_type = "modified"
    elif previous_level is None or current_level is None:
        change_type = "modified"
    elif abs(current_level - previous_level) < 1e-12:
        change_type = "unchanged"
    elif current_level > previous_level:
        change_type = "raised"
    else:
        change_type = "lowered"
    details = {
        "previous_level": previous_level,
        "current_level": current_level,
        "delta": (
            round(current_level - previous_level, 8)
            if current_level is not None and previous_level is not None
            else None
        ),
        "summary": {
            "unchanged": "观察点位与上一份日报一致。",
            "raised": "观察点位较上一份日报上调。",
            "lowered": "观察点位较上一份日报下调。",
            "modified": "观察场景逻辑较上一份日报发生修改。",
        }[change_type],
    }
    candidate.update(
        change_type=change_type,
        previous_candidate_id=previous.get("candidate_id") or previous.get("scenario_id"),
        change_details={key: value for key, value in details.items() if value is not None},
    )
    return {
        "scenario_family_id": candidate["scenario_family_id"],
        "candidate_id": candidate["candidate_id"],
        "previous_candidate_id": candidate["previous_candidate_id"],
        "change_type": change_type,
        "change_details": candidate["change_details"],
    }


def build_monitoring_bundle(
    *,
    run_id: str,
    revision: int,
    symbol: str,
    raw_bundle: Any,
    generated_at: Any,
    data_as_of: Any,
    daily_actionable: bool,
    condition_actionable: bool,
    price_volume_context: dict[str, Any],
    previous_bundle: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return a validated bundle, generated Claims, and legacy conditions."""

    generated = _aware(generated_at)
    data_time = _aware(data_as_of, fallback=generated)
    valid_from = generated
    review_due = generated + timedelta(days=1)
    valid_until = generated + timedelta(days=7)
    previous_candidates = {
        str(item.get("scenario_family_id")): item
        for item in (previous_bundle or {}).get("candidates") or []
        if isinstance(item, dict) and item.get("scenario_family_id")
    }
    raw_candidates = (
        list(raw_bundle.get("candidates") or [])
        if isinstance(raw_bundle, dict) and isinstance(raw_bundle.get("candidates") or [], list)
        else []
    )
    errors: list[str] = []
    claims: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    single_source_blocked = (
        price_volume_context.get("data_mode") == "single_source"
        and not price_volume_context.get("single_source_authorized", False)
    )
    if daily_actionable and condition_actionable:
        for index, raw in enumerate(raw_candidates[:12]):
            if not isinstance(raw, dict):
                errors.append(f"candidates[{index}]: candidate must be an object")
                continue
            candidate = copy.deepcopy(raw)
            family_id = _family_id(symbol, candidate)
            candidate_id = _sha(
                "candidate",
                run_id,
                revision,
                symbol,
                family_id,
                index,
                candidate.get("original_level"),
            )
            candidate.update(
                candidate_id=candidate_id,
                scenario_id=candidate_id,
                scenario_family_id=family_id,
                client_rule_id=_sha("daily_rule", candidate_id),
                priority=str(candidate.get("priority") or "normal"),
            )
            candidate.setdefault("mapping_status", "mapped")
            candidate.setdefault("automation_status", "action_ready")
            condition_shape_warnings = _normalize_condition_shapes(candidate)
            compatibility_warnings = _normalize_engine_compatibility(candidate)
            absolute_warning = _normalize_absolute_volume(candidate)
            mapping_warnings = _degrade_unsafe_mappings(candidate)
            mapping_warnings.extend(condition_shape_warnings)
            mapping_warnings.extend(compatibility_warnings)
            if absolute_warning:
                mapping_warnings.append(absolute_warning)
            required_pending = any(
                isinstance(item, dict)
                and item.get("role") == "required"
                and item.get("coverage_status") != "mapped"
                for item in candidate.get("source_conditions") or []
            )
            if required_pending:
                candidate["mapping_status"] = "partial"
                candidate["automation_status"] = "watch_only"
            else:
                candidate["mapping_status"] = "mapped"
            basis = candidate.get("calculation_basis")
            if not isinstance(basis, dict) or not basis.get("references"):
                candidate["automation_status"] = "watch_only"
                mapping_warnings.append("点位计算依据缺少可核对引用，仅保留为观察。")
            if single_source_blocked:
                candidate["automation_status"] = "watch_only"
                mapping_warnings.append("当前仅有单一数据源且未获明确授权，不得提升为 action_ready。")
            candidate_claims = _attach_claims(
                run_id=run_id,
                symbol=symbol,
                candidate=candidate,
            )
            change = _apply_diff(candidate, previous_candidates)
            try:
                normalized = validate_monitoring_candidate(
                    candidate,
                    expected_symbol=symbol,
                    generated_at=generated.isoformat(),
                    data_as_of=data_time.isoformat(),
                    valid_until=valid_until.isoformat(),
                )
            except (PlanValidationError, TypeError, ValueError) as exc:
                errors.append(f"candidates[{index}]: {exc}")
                continue
            candidates.append(normalized)
            claims.extend(candidate_claims)
            changes.append(change)
            errors.extend(f"candidates[{index}]: {warning}" for warning in mapping_warnings)
    elif not daily_actionable:
        errors.append("日线趋势数据不可用，结构化监控候选已清空。")
    else:
        errors.append("条件单价格数据不可用，结构化监控候选已清空。")

    current_families = {str(item.get("scenario_family_id")) for item in candidates}
    for family_id, previous in previous_candidates.items():
        if family_id in current_families:
            continue
        changes.append(
            {
                "scenario_family_id": family_id,
                "candidate_id": None,
                "previous_candidate_id": previous.get("candidate_id") or previous.get("scenario_id"),
                "change_type": "withdrawn",
                "change_details": {
                    "previous_level": _level_value(previous),
                    "summary": "上一份日报中的观察场景已撤回。",
                },
            }
        )

    if not daily_actionable or not condition_actionable:
        status = "data_insufficient"
    elif candidates:
        status = "available"
    else:
        status = "not_recommended"
    level_snapshot_id = _sha(
        "monitoring_level_snapshot_v2",
        symbol,
        data_time.isoformat(),
        [
            (
                item.get("candidate_id"),
                item.get("original_level"),
                item.get("calculation_basis"),
            )
            for item in candidates
        ],
    )
    bundle = {
        "schema_version": 2,
        "symbol": symbol,
        "instrument_type": _instrument_type(symbol),
        "horizon": "daily",
        "generated_at": generated.isoformat(),
        "data_as_of": data_time.isoformat(),
        "valid_from": valid_from.isoformat(),
        "valid_until": valid_until.isoformat(),
        "review_due_at": review_due.isoformat(),
        "price_basis": {"adjustment": "raw", "currency": "CNY", "tick_size": _tick_size(symbol)},
        "monitoring_status": status,
        "price_volume_context": {
            "policy": dict(DEFAULT_PRICE_VOLUME_POLICY),
            "data_mode": str(price_volume_context.get("data_mode") or "verified"),
            "source_count": int(price_volume_context.get("source_count") or 0),
            "sources": list(price_volume_context.get("sources") or []),
            "single_source_authorized": bool(price_volume_context.get("single_source_authorized", False)),
            "warnings": list(dict.fromkeys([*price_volume_context.get("warnings", []), *errors])),
            "refresh_attempted": bool(price_volume_context.get("refresh_attempted", True)),
            "refresh_succeeded": bool(price_volume_context.get("refresh_succeeded", daily_actionable)),
        },
        "candidates": candidates,
        "scenario_changes": changes,
        "validation_errors": errors,
        "source": "structured_daily_report",
        "activation_policy": "manual_confirmation_required",
        "trade_execution": "forbidden",
        "level_snapshot_id": level_snapshot_id,
        "selection_mode": "report_candidate_validated",
        "price_conversion": {
            "analysis_basis": "raw",
            "runtime_basis": "raw",
            "events": [],
        },
    }
    validated = validate_monitoring_bundle(bundle, expected_symbol=symbol)
    legacy_conditions = [condition_order_from_candidate(item) for item in validated["candidates"]]
    return validated, claims, legacy_conditions


def condition_order_from_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    trigger = candidate.get("trigger") or {}
    action = candidate.get("action_template") or {}
    invalidation = candidate.get("invalidation") or {}
    source_conditions = candidate.get("source_conditions") or []
    confirmation = "；".join(
        str(item.get("source_text") or "")
        for item in source_conditions
        if isinstance(item, dict) and item.get("role") in {"required", "supportive"}
    )
    threshold = trigger.get("threshold")
    if threshold is None:
        threshold = f"{trigger.get('lower')}–{trigger.get('upper')}"
    return {
        "candidate_id": candidate.get("candidate_id"),
        "scenario_family_id": candidate.get("scenario_family_id"),
        "trigger": f"{trigger.get('kind')} {threshold}（{trigger.get('interval')}，{trigger.get('confirmation_count')} 根闭合K线）",
        "confirmation": confirmation or None,
        "invalidation": (
            f"{invalidation.get('kind')} {invalidation.get('level')}" if invalidation else None
        ),
        "response": str(action.get("action") or "observe"),
        "priority": str(candidate.get("priority") or "normal"),
        "automation_status": candidate.get("automation_status"),
    }
