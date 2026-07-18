"""Deterministic, non-trading action and quantity recommendations."""

from __future__ import annotations

import math
import uuid
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from src.portfolio.mandate import load_mandate
from src.portfolio.state import load_state, normalize_symbol


_CN_TZ = ZoneInfo("Asia/Shanghai")


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _holding_value(holding: dict[str, Any], *, fallback_price: float | None = None) -> float | None:
    market_value = _number(holding.get("market_value"))
    if market_value is not None and market_value >= 0:
        return market_value
    quantity = _number(holding.get("quantity"))
    price = _number(holding.get("last_price")) or fallback_price
    if quantity is None or price is None:
        return None
    return max(0.0, quantity * price)


class RecommendationResolver:
    """Resolve a scenario into a bounded recommendation; never place an order."""

    def __init__(self, *, lot_size: int = 100) -> None:
        self.lot_size = max(1, int(lot_size))

    @staticmethod
    def _expires_at(now_utc: datetime, *, daily: bool) -> str:
        local = now_utc.astimezone(_CN_TZ)
        if daily:
            candidate = datetime.combine(local.date() + timedelta(days=1), time(15, 0), _CN_TZ)
            while candidate.weekday() >= 5:
                candidate += timedelta(days=1)
        else:
            candidate = min(
                local + timedelta(minutes=30),
                datetime.combine(local.date(), time(15, 0), _CN_TZ),
            )
        return candidate.astimezone(timezone.utc).isoformat()

    def resolve(
        self,
        *,
        symbol: str,
        scenario: dict[str, Any],
        current_price: Any,
        now_utc: datetime,
        profile_id: str | None = None,
        plan_version: int | None = None,
        episode_id: str | None = None,
        compound: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_symbol = normalize_symbol(symbol).upper()
        price = _number(current_price)
        action_template = scenario.get("action_template") or {"action": "observe"}
        action = str(action_template.get("action") or "observe")
        automation_status = str(scenario.get("automation_status") or "watch_only")
        evidence_pending = bool((compound or {}).get("evidence_pending")) or automation_status != "action_ready"
        if action not in {"observe", "add", "reduce", "exit"}:
            action = "observe"
            evidence_pending = True
        if evidence_pending and action != "observe":
            recommendation_status = "evidence_pending"
        else:
            recommendation_status = "ready"

        portfolio = load_state()
        holding = next(
            (
                item
                for item in portfolio.holdings
                if normalize_symbol(str(item.get("symbol") or item.get("code") or "")).upper()
                == normalized_symbol
            ),
            None,
        )
        quantity = max(0.0, _number((holding or {}).get("quantity")) or 0.0)
        current_value = _holding_value(holding or {}, fallback_price=price)
        sizing = action_template.get("sizing") or {"kind": "default_policy", "source": "system_default"}
        sizing_kind = str(sizing.get("kind") or "default_policy")
        sizing_value = _number(sizing.get("value"))
        source = str(sizing.get("source") or "system_default")
        default_used = sizing_kind == "default_policy"
        requested_units: float | None = None
        requested_amount: float | None = None
        notes: list[str] = []

        if action == "observe":
            requested_units = 0.0
            requested_amount = 0.0
        elif price is None or price <= 0 or holding is None:
            recommendation_status = "evidence_pending"
            notes.append("current holding or verified price is unavailable")
        elif sizing_kind in {"units", "target_position_units"} and sizing_value is not None:
            requested_units = (
                max(0.0, sizing_value - quantity)
                if sizing_kind == "target_position_units" and action == "add"
                else sizing_value
            )
            requested_amount = requested_units * price
        elif sizing_kind == "position_fraction" and sizing_value is not None:
            requested_units = quantity * sizing_value
            requested_amount = requested_units * price
        elif sizing_kind == "cash_amount" and sizing_value is not None:
            requested_amount = sizing_value
            requested_units = requested_amount / price
        else:
            source = "system_default"
            if action == "exit":
                requested_units = quantity
            elif action == "reduce":
                requested_units = quantity * 0.25
            elif action == "add":
                requested_amount = (current_value or 0.0) * 0.10
                requested_units = requested_amount / price
            requested_amount = requested_units * price if requested_units is not None else None
            notes.append("system default sizing applied")

        constrained_units = requested_units
        if constrained_units is not None and action in {"reduce", "exit"}:
            constrained_units = min(constrained_units, quantity)
        if constrained_units is not None and action == "add":
            mandate = load_mandate()
            cash = _number(portfolio.cash)
            cash_policy = mandate.get("cash_policy") or {}
            cash_floor = _number(cash_policy.get("min_amount")) if cash_policy.get("configured") else 0.0
            if cash is None:
                constrained_units = None
                recommendation_status = "evidence_pending"
                notes.append("available cash is not maintained")
            else:
                amount_cap = max(0.0, cash - (cash_floor or 0.0))
                assignments = mandate.get("assignments") or {}
                assignment = assignments.get(normalized_symbol) or {}
                sleeve_id = str(assignment.get("active_sleeve_id") or "unassigned")
                sleeve = next(
                    (item for item in mandate.get("sleeves") or [] if str(item.get("id")) == sleeve_id),
                    None,
                )
                if sleeve and sleeve.get("configured"):
                    sleeve_current = 0.0
                    sleeve_complete = True
                    for item in portfolio.holdings:
                        item_symbol = normalize_symbol(str(item.get("symbol") or item.get("code") or "")).upper()
                        if str((assignments.get(item_symbol) or {}).get("active_sleeve_id") or "unassigned") != sleeve_id:
                            continue
                        item_value = _holding_value(item, fallback_price=price if item_symbol == normalized_symbol else None)
                        if item_value is None:
                            sleeve_complete = False
                            break
                        sleeve_current += item_value
                    if not sleeve_complete:
                        constrained_units = None
                        recommendation_status = "evidence_pending"
                        notes.append("sleeve market values are incomplete")
                    else:
                        upper = _number(sleeve.get("max_amount")) or _number(sleeve.get("target_amount"))
                        if upper is not None:
                            amount_cap = min(amount_cap, max(0.0, upper - sleeve_current))
                        single_max = _number(sleeve.get("single_position_max_amount"))
                        if single_max is not None and current_value is not None:
                            amount_cap = min(amount_cap, max(0.0, single_max - current_value))
                if constrained_units is not None:
                    constrained_units = min(constrained_units, amount_cap / price)
        if constrained_units is not None:
            constrained_units = math.floor(max(0.0, constrained_units) / self.lot_size) * self.lot_size
        constrained_amount = constrained_units * price if constrained_units is not None and price is not None else None
        if action != "observe" and constrained_units == 0:
            recommendation_status = "evidence_pending"
            notes.append("constraints leave no executable board lot")
        intervals = {
            str(condition.get("interval") or "")
            for group_name in ("entry_conditions", "confirmation_conditions")
            for condition in (scenario.get(group_name) or {}).get("conditions", [])
        }
        daily = "1d" in intervals
        confidence = str(action_template.get("confidence_floor") or "medium")
        if recommendation_status != "ready":
            confidence = "low"
        return {
            "recommendation_id": uuid.uuid4().hex,
            "profile_id": profile_id,
            "plan_version": plan_version,
            "episode_id": episode_id,
            "symbol": normalized_symbol,
            "scenario_id": scenario.get("scenario_id"),
            "scenario_fingerprint": scenario.get("scenario_fingerprint"),
            "status": recommendation_status,
            "action": action,
            "original_sizing": sizing,
            "sizing_source": source,
            "system_default_used": default_used,
            "requested_quantity": requested_units,
            "constrained_quantity": constrained_units,
            "current_holding_quantity": quantity,
            "current_price": price,
            "requested_amount": requested_amount,
            "estimated_amount": constrained_amount,
            "currency": "CNY",
            "lot_size": self.lot_size,
            "confidence": confidence,
            "invalidation": scenario.get("invalidation_conditions") or scenario.get("invalidation"),
            "valid_until": self._expires_at(now_utc, daily=daily),
            "notes": notes,
            "created_at": now_utc.isoformat(),
            "feedback_status": "pending",
            "trade_execution": "forbidden",
        }
