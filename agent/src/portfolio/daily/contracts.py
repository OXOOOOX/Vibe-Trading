"""Strict worker-output contract for a single holding briefing."""

from __future__ import annotations

import json
import re
from typing import Any


ALLOWED_ACTIONS = {"observe", "add", "reduce", "exit"}
ALLOWED_CONFIDENCE = {"low", "medium", "high"}
ALLOWED_CONDITION_STATUSES = {"available", "not_recommended", "data_insufficient"}


class BriefContractError(ValueError):
    """Raised when a worker did not return the required daily brief shape."""


def _extract_json(text: str) -> dict[str, Any]:
    stripped = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.S | re.I)
    candidate = fenced.group(1) if fenced else stripped
    if not candidate.startswith("{"):
        start, end = candidate.find("{"), candidate.rfind("}")
        candidate = candidate[start : end + 1] if start >= 0 and end > start else candidate
    value = json.loads(candidate)
    if not isinstance(value, dict):
        raise BriefContractError("worker output must be a JSON object")
    return value


def parse_holding_brief(text: str, *, symbol: str) -> dict[str, Any]:
    """Parse and normalize a worker response without silently inventing actions."""

    try:
        raw = _extract_json(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise BriefContractError("worker output is not valid JSON") from exc

    action = str(raw.get("action") or "").strip().lower()
    if action not in ALLOWED_ACTIONS:
        raise BriefContractError(f"unsupported action: {action or '<empty>'}")
    confidence = str(raw.get("confidence") or "medium").strip().lower()
    if confidence not in ALLOWED_CONFIDENCE:
        raise BriefContractError(f"unsupported confidence: {confidence}")

    reasons = [str(item).strip() for item in (raw.get("reasons") or []) if str(item).strip()]
    risks = [str(item).strip() for item in (raw.get("risks") or []) if str(item).strip()]
    watch = [str(item).strip() for item in (raw.get("watch_points") or []) if str(item).strip()]
    if not reasons:
        raise BriefContractError("reasons cannot be empty")

    amount = raw.get("suggested_amount")
    if amount in (None, ""):
        normalized_amount = None
    else:
        try:
            normalized_amount = max(0.0, float(amount))
        except (TypeError, ValueError) as exc:
            raise BriefContractError("suggested_amount must be numeric") from exc

    conditions: list[dict[str, Any]] = []
    for item in raw.get("condition_orders") or []:
        if not isinstance(item, dict):
            continue
        trigger = str(item.get("trigger") or "").strip()
        response = str(item.get("response") or "").strip()
        if trigger and response:
            conditions.append(
                {
                    "trigger": trigger,
                    "response": response,
                    "priority": str(item.get("priority") or "normal").strip(),
                    "confirmation": str(item.get("confirmation") or "").strip() or None,
                    "invalidation": str(item.get("invalidation") or "").strip() or None,
                }
            )

    data_limited = bool(raw.get("data_limited", False))
    condition_status = str(raw.get("condition_order_status") or "").strip()
    if condition_status not in ALLOWED_CONDITION_STATUSES:
        condition_status = (
            "data_insufficient"
            if data_limited
            else "available"
            if conditions
            else "not_recommended"
        )
    if data_limited:
        action = "observe"
        normalized_amount = None
    if data_limited or condition_status == "data_insufficient":
        conditions = []
        condition_status = "data_insufficient"

    raw_trend = raw.get("trend") if isinstance(raw.get("trend"), dict) else {}
    trend = {
        "summary": str(raw_trend.get("summary") or raw.get("summary") or reasons[0]).strip(),
        "stage": str(raw_trend.get("stage") or "待确认").strip(),
        "direction": str(raw_trend.get("direction") or "待确认").strip(),
        "strength": str(raw_trend.get("strength") or "待确认").strip(),
    }
    data_scopes = raw.get("data_scopes") if isinstance(raw.get("data_scopes"), dict) else {}

    return {
        "schema_version": 2,
        "symbol": symbol,
        "summary": str(raw.get("summary") or reasons[0]).strip(),
        "action": action,
        "confidence": confidence,
        "suggested_amount": normalized_amount,
        "reasons": reasons[:6],
        "risks": risks[:6],
        "watch_points": watch[:6],
        "condition_orders": conditions[:4],
        "condition_order_status": condition_status,
        "condition_order_summary": str(raw.get("condition_order_summary") or "").strip(),
        "trend": trend,
        "data_scopes": data_scopes,
        "data_limited": data_limited,
    }


def fallback_brief(symbol: str, reason: str, *, data_limited: bool = True) -> dict[str, Any]:
    """Return a conservative contract-valid result after a worker failure."""

    return {
        "schema_version": 2,
        "symbol": symbol,
        "summary": "本次未形成可靠的主动调整结论，今日仅观察。",
        "action": "observe",
        "confidence": "low",
        "suggested_amount": None,
        "reasons": [reason],
        "risks": ["分析结果不完整，不应据此扩大风险暴露。"],
        "watch_points": ["等待数据和分析恢复后重新运行。"],
        "condition_orders": [],
        "condition_order_status": "data_insufficient" if data_limited else "not_recommended",
        "condition_order_summary": "相关数据尚未形成可靠的条件单判断。",
        "trend": {
            "summary": "本次未形成可靠的主动调整结论，今日仅观察。",
            "stage": "待确认",
            "direction": "待确认",
            "strength": "待确认",
        },
        "data_scopes": {},
        "data_limited": data_limited,
    }
