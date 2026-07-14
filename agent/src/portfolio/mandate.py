"""Portfolio sleeve targets and classification governance.

The portfolio state module owns account facts.  This module deliberately keeps
strategy targets and user/agent classification decisions in a separate,
versioned document so a report can freeze both inputs independently.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from src.config.paths import get_runtime_root
from src.portfolio.state import normalize_symbol


_DEFENSIVE_KEYWORDS = (
    "银行",
    "红利",
    "股息",
    "黄金",
    "债",
    "货币",
    "现金",
    "公用",
    "高速",
    "价值",
)
_OFFENSIVE_KEYWORDS = (
    "科技",
    "科创",
    "创业",
    "半导体",
    "芯片",
    "软件",
    "人工智能",
    "ai",
    "成长",
    "军工",
    "证券",
    "创新药",
)
_CLASSIFICATION_DIMENSIONS = [
    "volatility",
    "drawdown",
    "earnings_stability",
    "cyclicality",
    "growth_and_catalyst",
    "cashflow_and_dividend",
    "portfolio_role",
]


class MandateValidationError(ValueError):
    """Raised when a mandate cannot satisfy its structural invariants."""


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def mandate_path() -> Path:
    override = os.getenv("VIBE_TRADING_PORTFOLIO_MANDATE_PATH")
    if override:
        return Path(override).expanduser()
    return get_runtime_root() / "portfolio" / "portfolio_mandate.json"


def default_mandate() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "version": 1,
        "suggestion_revision": 0,
        "base_currency": "CNY",
        "classification_policy": {
            "version": 1,
            "auto_assign_new": True,
            "reclassification_confirmations": 2,
            "min_confidence": 0.75,
            "apply_reclassification_next_run": True,
            "dimensions": list(_CLASSIFICATION_DIMENSIONS),
        },
        "cash_policy": {
            "configured": False,
            "target_amount": 0.0,
            "min_amount": 0.0,
            "max_amount": None,
        },
        "sleeves": [
            {
                "id": "offensive",
                "name": "进攻型",
                "parent_id": None,
                "configured": False,
                "target_amount": 0.0,
                "min_amount": 0.0,
                "max_amount": None,
                "rebalance_band_amount": 0.0,
                "single_position_max_amount": None,
                "sort_order": 10,
            },
            {
                "id": "defensive",
                "name": "防守型",
                "parent_id": None,
                "configured": False,
                "target_amount": 0.0,
                "min_amount": 0.0,
                "max_amount": None,
                "rebalance_band_amount": 0.0,
                "single_position_max_amount": None,
                "sort_order": 20,
            },
        ],
        "assignments": {},
        "classification_history": [],
        "updated_at": _now(),
    }


def _number(value: Any, *, field: str, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool):
        raise MandateValidationError(f"{field} must be a non-negative number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MandateValidationError(f"{field} must be a non-negative number") from exc
    if number < 0:
        raise MandateValidationError(f"{field} must be non-negative")
    return number


def _validate_band(raw: dict[str, Any], *, prefix: str) -> dict[str, Any]:
    target = _number(raw.get("target_amount", 0), field=f"{prefix}.target_amount")
    minimum = _number(raw.get("min_amount", 0), field=f"{prefix}.min_amount")
    maximum = _number(raw.get("max_amount"), field=f"{prefix}.max_amount", allow_none=True)
    assert target is not None and minimum is not None
    if minimum > target:
        raise MandateValidationError(f"{prefix}.min_amount cannot exceed target_amount")
    if maximum is not None and target > maximum:
        raise MandateValidationError(f"{prefix}.target_amount cannot exceed max_amount")
    return {
        "configured": bool(raw.get("configured", False)),
        "target_amount": target,
        "min_amount": minimum,
        "max_amount": maximum,
    }


def validate_mandate(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a complete mandate document."""

    if not isinstance(payload, dict):
        raise MandateValidationError("mandate must be an object")
    normalized = copy.deepcopy(payload)
    normalized["schema_version"] = 1
    normalized["version"] = max(1, int(normalized.get("version") or 1))
    normalized["suggestion_revision"] = max(0, int(normalized.get("suggestion_revision") or 0))
    normalized["base_currency"] = str(normalized.get("base_currency") or "CNY").strip().upper()

    policy = dict(normalized.get("classification_policy") or {})
    policy["version"] = max(1, int(policy.get("version") or 1))
    policy["auto_assign_new"] = bool(policy.get("auto_assign_new", True))
    policy["reclassification_confirmations"] = max(
        1, int(policy.get("reclassification_confirmations") or 2)
    )
    confidence = float(policy.get("min_confidence", 0.75))
    if not 0 <= confidence <= 1:
        raise MandateValidationError("classification_policy.min_confidence must be between 0 and 1")
    policy["min_confidence"] = confidence
    policy["apply_reclassification_next_run"] = bool(
        policy.get("apply_reclassification_next_run", True)
    )
    policy["dimensions"] = list(policy.get("dimensions") or _CLASSIFICATION_DIMENSIONS)
    normalized["classification_policy"] = policy

    cash = _validate_band(dict(normalized.get("cash_policy") or {}), prefix="cash_policy")
    normalized["cash_policy"] = cash

    sleeves: list[dict[str, Any]] = []
    sleeve_ids: set[str] = set()
    for index, raw_value in enumerate(normalized.get("sleeves") or []):
        raw = dict(raw_value or {})
        sleeve_id = str(raw.get("id") or "").strip()
        if not sleeve_id:
            raise MandateValidationError(f"sleeves[{index}].id is required")
        if sleeve_id in sleeve_ids:
            raise MandateValidationError(f"duplicate sleeve id: {sleeve_id}")
        sleeve_ids.add(sleeve_id)
        band = _validate_band(raw, prefix=f"sleeves[{index}]")
        sleeve = {
            **raw,
            "id": sleeve_id,
            "name": str(raw.get("name") or sleeve_id).strip(),
            "parent_id": str(raw.get("parent_id") or "").strip() or None,
            **band,
            "rebalance_band_amount": _number(
                raw.get("rebalance_band_amount", 0),
                field=f"sleeves[{index}].rebalance_band_amount",
            ),
            "single_position_max_amount": _number(
                raw.get("single_position_max_amount"),
                field=f"sleeves[{index}].single_position_max_amount",
                allow_none=True,
            ),
            "sort_order": int(raw.get("sort_order") or (index + 1) * 10),
        }
        sleeves.append(sleeve)
    if not sleeves:
        raise MandateValidationError("at least one sleeve is required")
    for sleeve in sleeves:
        parent_id = sleeve.get("parent_id")
        if parent_id and parent_id not in sleeve_ids:
            raise MandateValidationError(f"unknown parent sleeve: {parent_id}")
        if parent_id == sleeve["id"]:
            raise MandateValidationError(f"sleeve {parent_id} cannot parent itself")
    parent_by_id = {str(item["id"]): item.get("parent_id") for item in sleeves}
    for sleeve_id in parent_by_id:
        seen: set[str] = set()
        candidate: str | None = sleeve_id
        while candidate:
            if candidate in seen:
                raise MandateValidationError(
                    f"sleeve hierarchy contains a cycle at {candidate}"
                )
            seen.add(candidate)
            parent = parent_by_id.get(candidate)
            candidate = str(parent) if parent else None
    normalized["sleeves"] = sorted(sleeves, key=lambda item: (item["sort_order"], item["id"]))

    assignments: dict[str, dict[str, Any]] = {}
    parent_sleeve_ids = {
        str(item.get("parent_id")) for item in sleeves if item.get("parent_id")
    }
    for raw_symbol, raw_value in dict(normalized.get("assignments") or {}).items():
        symbol = normalize_symbol(str(raw_symbol)).upper()
        if not symbol:
            continue
        assignment = dict(raw_value or {})
        active = str(assignment.get("active_sleeve_id") or "").strip()
        if active not in sleeve_ids:
            raise MandateValidationError(f"assignment {symbol} references unknown sleeve {active}")
        if active in parent_sleeve_ids:
            raise MandateValidationError(
                f"assignment {symbol} must reference a leaf sleeve, not {active}"
            )
        suggested = str(assignment.get("suggested_sleeve_id") or active).strip()
        if suggested and suggested not in sleeve_ids:
            raise MandateValidationError(
                f"assignment {symbol} suggests unknown sleeve {suggested}"
            )
        if suggested in parent_sleeve_ids:
            raise MandateValidationError(
                f"assignment {symbol} must suggest a leaf sleeve, not {suggested}"
            )
        assignment.update(
            {
                "active_sleeve_id": active,
                "assigned_by": "user" if assignment.get("assigned_by") == "user" else "agent",
                "confidence": max(0.0, min(float(assignment.get("confidence", 0.5)), 1.0)),
                "rationale": str(assignment.get("rationale") or "").strip(),
                "user_locked": bool(assignment.get("user_locked", False)),
                "suggested_sleeve_id": suggested or active,
                "suggested_rationale": str(assignment.get("suggested_rationale") or "").strip(),
                "suggestion_run_count": max(0, int(assignment.get("suggestion_run_count") or 0)),
                "needs_user_review": bool(assignment.get("needs_user_review", False)),
                "classification_policy_version": max(
                    1, int(assignment.get("classification_policy_version") or policy["version"])
                ),
                "updated_at": str(assignment.get("updated_at") or _now()),
            }
        )
        assignments[symbol] = assignment
    normalized["assignments"] = assignments
    normalized["classification_history"] = list(normalized.get("classification_history") or [])[-500:]
    normalized["updated_at"] = str(normalized.get("updated_at") or _now())
    return normalized


def load_mandate(path: Path | None = None) -> dict[str, Any]:
    path = path or mandate_path()
    if not path.exists():
        return default_mandate()
    return validate_mandate(json.loads(path.read_text(encoding="utf-8")))


def _write_mandate(mandate: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(mandate, ensure_ascii=False, indent=2, sort_keys=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    temporary.replace(path)
    return path


def save_mandate(
    payload: dict[str, Any], *, path: Path | None = None, bump_version: bool = True
) -> dict[str, Any]:
    path = path or mandate_path()
    normalized = validate_mandate(payload)
    previous = load_mandate(path) if path.exists() else default_mandate()
    if bump_version:
        normalized["version"] = max(int(previous.get("version") or 1) + 1, normalized["version"])
    normalized["updated_at"] = _now()
    _write_mandate(normalized, path)
    return normalized


def _classification_for_holding(
    holding: dict[str, Any], mandate: dict[str, Any]
) -> dict[str, Any]:
    name = str(holding.get("name") or holding.get("symbol") or holding.get("code") or "").strip()
    lowered = name.lower()
    defensive_hits = [keyword for keyword in _DEFENSIVE_KEYWORDS if keyword in lowered]
    offensive_hits = [keyword for keyword in _OFFENSIVE_KEYWORDS if keyword in lowered]
    if defensive_hits and not offensive_hits:
        sleeve_id, confidence, evidence = "defensive", 0.8, defensive_hits
        scores = {
            "volatility": 1.5,
            "drawdown": 1.5,
            "earnings_stability": 4.0,
            "cyclicality": 2.0,
            "growth_and_catalyst": 2.0,
            "cashflow_and_dividend": 4.0,
            "portfolio_role": 4.0,
        }
    elif offensive_hits and not defensive_hits:
        sleeve_id, confidence, evidence = "offensive", 0.8, offensive_hits
        scores = {
            "volatility": 4.0,
            "drawdown": 3.5,
            "earnings_stability": 2.0,
            "cyclicality": 3.5,
            "growth_and_catalyst": 4.5,
            "cashflow_and_dividend": 1.5,
            "portfolio_role": 3.5,
        }
    else:
        sleeve_id, confidence, evidence = "offensive", 0.55, []
        scores = {dimension: 2.5 for dimension in _CLASSIFICATION_DIMENSIONS}
    available = {item["id"] for item in mandate["sleeves"]}
    if sleeve_id not in available:
        sleeve_id = next(iter(available))
        confidence = min(confidence, 0.5)
    rationale = (
        f"名称和组合角色特征命中：{', '.join(evidence)}"
        if evidence
        else "现有结构化信息不足，先按进攻型建立低置信度初始分区，等待用户复核。"
    )
    return {
        "active_sleeve_id": sleeve_id,
        "assigned_by": "agent",
        "confidence": confidence,
        "rationale": rationale,
        "user_locked": False,
        "suggested_sleeve_id": sleeve_id,
        "suggested_rationale": "",
        "suggested_evidence": None,
        "suggestion_run_count": 0,
        "needs_user_review": confidence < float(
            mandate["classification_policy"].get("min_confidence", 0.75)
        ),
        "classification_evidence": {
            "policy_version": mandate["classification_policy"]["version"],
            "data_as_of": str(holding.get("updated_at") or _now()),
            "signals": list(evidence),
            "dimensions": [
                {
                    "name": dimension,
                    "score": float(scores[dimension]),
                    "evidence": rationale,
                }
                for dimension in _CLASSIFICATION_DIMENSIONS
            ],
        },
        "classification_policy_version": mandate["classification_policy"]["version"],
        "updated_at": _now(),
    }


def ensure_assignments(
    holdings: Iterable[dict[str, Any]], *, path: Path | None = None
) -> dict[str, Any]:
    """Assign every new holding before a Daily Run freezes the mandate."""

    path = path or mandate_path()
    mandate = load_mandate(path)
    if not mandate["classification_policy"].get("auto_assign_new", True):
        return mandate
    changed = False
    for holding in holdings:
        symbol = normalize_symbol(str(holding.get("symbol") or holding.get("code") or "")).upper()
        if not symbol or symbol in mandate["assignments"]:
            continue
        assignment = _classification_for_holding(holding, mandate)
        mandate["assignments"][symbol] = assignment
        mandate["classification_history"].append(
            {
                "symbol": symbol,
                "old_sleeve_id": None,
                "new_sleeve_id": assignment["active_sleeve_id"],
                "source": "agent_initial",
                "rationale": assignment["rationale"],
                "evidence": assignment.get("classification_evidence"),
                "at": assignment["updated_at"],
            }
        )
        changed = True
    return save_mandate(mandate, path=path) if changed else mandate


def update_assignment(
    symbol: str,
    sleeve_id: str,
    *,
    path: Path | None = None,
    user_locked: bool = True,
) -> dict[str, Any]:
    path = path or mandate_path()
    mandate = load_mandate(path)
    normalized_symbol = normalize_symbol(symbol).upper()
    sleeve_ids = {item["id"] for item in mandate["sleeves"]}
    if sleeve_id not in sleeve_ids:
        raise MandateValidationError(f"unknown sleeve: {sleeve_id}")
    previous = dict(mandate["assignments"].get(normalized_symbol) or {})
    now = _now()
    mandate["assignments"][normalized_symbol] = {
        **previous,
        "active_sleeve_id": sleeve_id,
        "assigned_by": "user" if user_locked else "agent",
        "confidence": 1.0 if user_locked else float(previous.get("confidence", 0.5)),
        "rationale": "用户最终分类" if user_locked else str(previous.get("rationale") or ""),
        "user_locked": user_locked,
        "suggested_sleeve_id": sleeve_id,
        "suggested_rationale": "",
        "suggestion_run_count": 0,
        "needs_user_review": False,
        "classification_policy_version": mandate["classification_policy"]["version"],
        "updated_at": now,
    }
    mandate["classification_history"].append(
        {
            "symbol": normalized_symbol,
            "old_sleeve_id": previous.get("active_sleeve_id"),
            "new_sleeve_id": sleeve_id,
            "source": "user" if user_locked else "agent",
            "at": now,
        }
    )
    return save_mandate(mandate, path=path)


def suggest_classifications(
    holdings: Iterable[dict[str, Any]], *, path: Path | None = None
) -> dict[str, Any]:
    """Update suggestions with hysteresis and never overwrite user locks."""

    path = path or mandate_path()
    mandate = ensure_assignments(holdings, path=path)
    policy = mandate["classification_policy"]
    effective_changed = False
    suggestion_changed = False
    for holding in holdings:
        symbol = normalize_symbol(str(holding.get("symbol") or holding.get("code") or "")).upper()
        assignment = mandate["assignments"].get(symbol)
        if not assignment:
            continue
        proposed = _classification_for_holding(holding, mandate)
        proposed_id = proposed["active_sleeve_id"]
        current_id = assignment["active_sleeve_id"]
        if proposed_id == current_id:
            if assignment.get("suggestion_run_count") or assignment.get("suggested_sleeve_id") != current_id:
                assignment["suggested_sleeve_id"] = current_id
                assignment["suggested_rationale"] = ""
                assignment["suggested_evidence"] = None
                assignment["suggestion_run_count"] = 0
                suggestion_changed = True
            continue
        prior_suggestion = assignment.get("suggested_sleeve_id")
        count = int(assignment.get("suggestion_run_count") or 0) + 1 if prior_suggestion == proposed_id else 1
        assignment["suggested_sleeve_id"] = proposed_id
        assignment["suggested_rationale"] = proposed["rationale"]
        assignment["suggested_evidence"] = proposed.get("classification_evidence")
        assignment["suggestion_run_count"] = count
        assignment["updated_at"] = _now()
        suggestion_changed = True
        if assignment.get("user_locked"):
            continue
        if proposed["confidence"] < float(policy["min_confidence"]):
            continue
        if count < int(policy["reclassification_confirmations"]):
            continue
        old = current_id
        assignment.update(
            {
                "active_sleeve_id": proposed_id,
                "assigned_by": "agent",
                "confidence": proposed["confidence"],
                "rationale": proposed["rationale"],
                "classification_evidence": proposed.get("classification_evidence"),
                "suggested_evidence": None,
                "suggestion_run_count": 0,
                "needs_user_review": False,
            }
        )
        mandate["classification_history"].append(
            {
                "symbol": symbol,
                "old_sleeve_id": old,
                "new_sleeve_id": proposed_id,
                "source": "agent_hysteresis",
                "rationale": proposed["rationale"],
                "evidence": proposed.get("classification_evidence"),
                "at": assignment["updated_at"],
            }
        )
        effective_changed = True
    if effective_changed:
        return save_mandate(mandate, path=path)
    if suggestion_changed:
        mandate["suggestion_revision"] = int(mandate.get("suggestion_revision") or 0) + 1
        mandate["updated_at"] = _now()
        normalized = validate_mandate(mandate)
        _write_mandate(normalized, path)
        return normalized
    return mandate
