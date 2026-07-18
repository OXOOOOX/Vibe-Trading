"""Pure deterministic market-rule evaluation."""

from __future__ import annotations

from typing import Any


def condition_for(rule: dict[str, Any], observation: dict[str, Any]) -> bool | None:
    """Evaluate one whitelisted rule. None means required evidence is absent."""

    kind = str(rule["kind"])
    params = rule["parameters"]
    price = observation.get("last_price")
    previous = observation.get("previous_close")
    if price is None:
        return None
    price = float(price)
    if kind == "price_cross_above":
        return price > float(params["threshold"])
    if kind == "price_cross_below":
        return price < float(params["threshold"])
    if kind == "price_zone_enter":
        return float(params["lower"]) <= price <= float(params["upper"])
    if kind == "price_zone_exit":
        return not float(params["lower"]) <= price <= float(params["upper"])
    if kind.startswith("intraday_pct_change"):
        if previous is None or float(previous) == 0:
            return None
        change = 100.0 * (price - float(previous)) / float(previous)
        threshold = float(params["threshold_pct"])
        return change > threshold if kind.endswith("above") else change < threshold
    if kind == "volume_ratio_above":
        ratios = observation.get("volume_ratios") or {}
        ratio = ratios.get(rule.get("client_rule_id"), observation.get("volume_ratio"))
        return None if ratio is None else float(ratio) >= float(params["ratio"])
    return None


def clear_for(rule: dict[str, Any], observation: dict[str, Any]) -> bool:
    """Return whether an existing episode has clearly rearmed."""

    kind = str(rule["kind"])
    params = rule["parameters"]
    price = observation.get("last_price")
    if price is None:
        return False
    price = float(price)
    h = float(params.get("clear_hysteresis_bps", 0)) / 10000.0
    if kind == "price_cross_above":
        return price < float(params["threshold"]) * (1.0 - h)
    if kind == "price_cross_below":
        return price > float(params["threshold"]) * (1.0 + h)
    if kind == "price_zone_enter":
        return price < float(params["lower"]) * (1.0 - h) or price > float(params["upper"]) * (1.0 + h)
    if kind == "price_zone_exit":
        return float(params["lower"]) * (1.0 + h) <= price <= float(params["upper"]) * (1.0 - h)
    if kind.startswith("intraday_pct_change"):
        previous = observation.get("previous_close")
        if previous is None or float(previous) == 0:
            return False
        change = 100.0 * (price - float(previous)) / float(previous)
        threshold = float(params["threshold_pct"])
        margin = abs(threshold) * h
        return change < threshold - margin if kind.endswith("above") else change > threshold + margin
    if kind == "volume_ratio_above":
        ratios = observation.get("volume_ratios") or {}
        ratio = ratios.get(rule.get("client_rule_id"), observation.get("volume_ratio"))
        return ratio is not None and float(ratio) < float(params["clear_ratio"])
    return False
