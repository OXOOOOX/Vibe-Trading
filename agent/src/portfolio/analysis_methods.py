"""Versioned, deterministic market-analysis methods for daily and weekly reports.

The model may select and explain results from this module, but it must not
calculate or invent prices.  Every numeric level is derived from completed
daily bars and remains traceable to a method version and source dates.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from collections import defaultdict
from datetime import date
from typing import Any, Iterable


METHOD_REGISTRY_VERSION = "market-analysis-methods/1.2"
AGENT_ANALYSIS_VERSION = "market-analysis-agent/1.0"
DEFAULT_HORIZONS = (5, 20, 60, 120, 250)
WATCH_ONLY_MIN_BARS = 20
ACTION_READY_MIN_BARS = 60

# A method version is not eligible for autonomous activation merely because it
# exists in code.  Promotion requires a no-future-data walk-forward on both an
# equity and an ETF sample.  Any subsequent method change must bump the version
# and add a newly reviewed entry; otherwise the planner stays fail-closed.
METHOD_RELEASE_APPROVALS: dict[str, dict[str, Any]] = {
    "market-analysis-methods/1.1": {
        "eligible_for_automatic_release": True,
        "evaluated_at": "2026-07-22",
        "evaluation_policy": {
            "minimum_history": 120,
            "forward_bars": 5,
            "step": 5,
            "no_future_snapshot_inputs": True,
        },
        "samples": [
            {
                "symbol": "000651.SZ",
                "instrument_type": "company_equity",
                "evaluation_points": 122,
                "method_touch_rate": 0.618257,
                "baseline_touch_rate": 0.303279,
                "method_resolved_precision": 0.72,
                "baseline_resolved_precision": 0.709677,
                "method_first_invalidation_rate": 0.234899,
                "baseline_first_invalidation_rate": 0.243243,
            },
            {
                "symbol": "510300.SH",
                "instrument_type": "etf",
                "evaluation_points": 122,
                "method_touch_rate": 0.629167,
                "baseline_touch_rate": 0.29918,
                "method_resolved_precision": 0.764706,
                "baseline_resolved_precision": 0.590164,
                "method_first_invalidation_rate": 0.211921,
                "baseline_first_invalidation_rate": 0.342466,
            },
        ],
    },
    "market-analysis-methods/1.2": {
        "eligible_for_automatic_release": True,
        "evaluated_at": "2026-07-23",
        "evaluation_policy": {
            "minimum_history": 120,
            "forward_bars": 5,
            "step": 5,
            "no_future_snapshot_inputs": True,
        },
        "samples": [
            {
                "symbol": "000651.SZ",
                "instrument_type": "company_equity",
                "evaluation_points": 123,
                "method_touch_rate": 0.327869,
                "baseline_touch_rate": 0.292683,
                "method_resolved_precision": 0.923077,
                "baseline_resolved_precision": 0.730159,
                "method_first_invalidation_rate": 0.0625,
                "baseline_first_invalidation_rate": 0.236111,
            },
            {
                "symbol": "510300.SH",
                "instrument_type": "etf",
                "evaluation_points": 123,
                "method_touch_rate": 0.307377,
                "baseline_touch_rate": 0.292683,
                "method_resolved_precision": 0.885246,
                "baseline_resolved_precision": 0.639344,
                "method_first_invalidation_rate": 0.093333,
                "baseline_first_invalidation_rate": 0.305556,
            },
        ],
    },
}

METHOD_FAMILY_BY_ID = {
    "multi_horizon_structure": "structure",
    "confirmed_swing_points": "structure",
    "volume_profile": "cost",
    "anchored_vwap": "cost",
}


def method_release_status(version: str = METHOD_REGISTRY_VERSION) -> dict[str, Any]:
    approval = METHOD_RELEASE_APPROVALS.get(version)
    if approval is None:
        return {
            "registry_version": version,
            "eligible_for_automatic_release": False,
            "reason": "method_version_not_walk_forward_approved",
            "samples": [],
        }
    return {"registry_version": version, **json.loads(json.dumps(approval))}

METHOD_REGISTRY: tuple[dict[str, Any], ...] = (
    {
        "method_id": "market_regime",
        "version": "1.0",
        "label": "市场状态识别",
        "purpose": "区分趋势、震荡、突破和高波动状态。",
        "minimum_bars": 20,
    },
    {
        "method_id": "multi_horizon_structure",
        "version": "1.0",
        "label": "多周期价格结构",
        "purpose": "比较短、中、长期滚动边界，不把单一窗口当作最终结论。",
        "minimum_bars": 20,
    },
    {
        "method_id": "confirmed_swing_points",
        "version": "1.0",
        "label": "已确认摆动点",
        "purpose": "仅使用右侧交易日已经完成的高低点确认结构。",
        "minimum_bars": 9,
    },
    {
        "method_id": "volatility_normalization",
        "version": "1.0",
        "label": "波动归一化",
        "purpose": "用平均真实波幅区分有效穿越与正常噪声。",
        "minimum_bars": 14,
    },
    {
        "method_id": "reaction_evidence",
        "version": "1.0",
        "label": "触及反应证据",
        "purpose": "按触及次数、后续反应、量能、近期性和角色互换评价候选区间。",
        "minimum_bars": 20,
    },
    {
        "method_id": "volume_profile",
        "version": "1.0",
        "label": "成交密集区",
        "purpose": "使用已完成5分钟K线的成交额或标准化成交量识别价格密集区。",
        "minimum_bars": 0,
    },
    {
        "method_id": "anchored_vwap",
        "version": "1.0",
        "label": "锚定成交均价",
        "purpose": "从最近已确认摆动点开始计算成交量加权均价。",
        "minimum_bars": 20,
    },
)


class AgentAnalysisContractError(ValueError):
    """Raised when model synthesis escapes the method-selection contract."""


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _bar_date(raw: dict[str, Any]) -> str:
    value = raw.get("session_date") or raw.get("date") or raw.get("trade_date")
    if value:
        text = str(value).strip()
        if re.fullmatch(r"\d{8}", text):
            return f"{text[:4]}-{text[4:6]}-{text[6:]}"
        return text[:10]
    value = raw.get("bar_time") or raw.get("datetime") or raw.get("timestamp")
    return str(value or "")[:10]


def normalize_completed_daily_bars(
    raw_bars: Iterable[dict[str, Any]], *, through: str | None = None
) -> list[dict[str, Any]]:
    """Normalize and cut daily bars without admitting data after ``through``."""

    cutoff = str(through or "")[:10]
    normalized: dict[str, dict[str, Any]] = {}
    for raw in raw_bars:
        if not isinstance(raw, dict):
            continue
        day = _bar_date(raw)
        try:
            date.fromisoformat(day)
        except ValueError:
            continue
        if cutoff and day > cutoff:
            continue
        open_price = _number(raw.get("open"))
        high = _number(raw.get("high"))
        low = _number(raw.get("low"))
        close = _number(raw.get("close"))
        if None in {open_price, high, low, close}:
            continue
        assert open_price is not None and high is not None and low is not None and close is not None
        if min(open_price, high, low, close) <= 0 or high < max(open_price, low, close):
            continue
        if low > min(open_price, high, close):
            continue
        normalized[day] = {
            "date": day,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": max(0.0, _number(raw.get("volume")) or 0.0),
            "amount": max(
                0.0,
                _number(
                    raw.get("amount")
                    or raw.get("turnover")
                    or raw.get("turnover_amount")
                )
                or 0.0,
            ),
        }
    return [normalized[key] for key in sorted(normalized)]


def _verified_factor_events(
    factor_rows: Iterable[dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in factor_rows or []:
        if not isinstance(raw, dict):
            continue
        effective_date = str(raw.get("effective_date") or "")[:10]
        factor = _number(raw.get("factor"))
        if not effective_date or factor is None or factor <= 0:
            continue
        grouped[effective_date].append({**raw, "factor": factor})

    verified: dict[str, dict[str, Any]] = {}
    for effective_date, rows in grouped.items():
        sources = sorted({str(item.get("source") or "unknown") for item in rows})
        official_rows = [
            item
            for item in rows
            if str(item.get("confidence") or "").lower() in {"official", "exchange", "verified_official"}
            or any(token in str(item.get("source") or "").lower() for token in ("exchange", "official"))
        ]
        values = [float(item["factor"]) for item in rows]
        median_factor = statistics.median(values)
        spread_pct = (
            (max(values) - min(values)) / median_factor * 100
            if median_factor > 0 and len(values) > 1
            else 0.0
        )
        is_verified = bool(official_rows) or (len(sources) >= 2 and spread_pct <= 0.5)
        if is_verified:
            selected = official_rows or rows
            factor = statistics.median(float(item["factor"]) for item in selected)
            verified[effective_date] = {
                "effective_date": effective_date,
                "factor": factor,
                "sources": sources,
                "source_count": len(sources),
                "spread_pct": round(spread_pct, 6),
                "verification": "official" if official_rows else "independent_sources",
            }
    return verified


def analyze_price_continuity(
    raw_bars: Iterable[dict[str, Any]],
    *,
    through: str | None = None,
    factor_rows: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Detect and safely handle corporate-action-like raw price resets.

    Unverified discontinuities are never mixed with earlier prices.  Verified
    factors convert the full history into current-raw-equivalent price and
    share-volume units while preserving a trace of every transformation.
    """

    bars = normalize_completed_daily_bars(raw_bars, through=through)
    verified_factors = _verified_factor_events(factor_rows)
    events: list[dict[str, Any]] = []
    for index in range(1, len(bars)):
        previous = bars[index - 1]
        current = bars[index]
        if previous["close"] <= 0:
            continue
        absolute_return = abs(current["close"] / previous["close"] - 1)
        prior = bars[max(0, index - 61) : index]
        prior_returns = [
            abs(prior[item]["close"] / prior[item - 1]["close"] - 1)
            for item in range(1, len(prior))
            if prior[item - 1]["close"] > 0
        ]
        median_return = statistics.median(prior_returns) if prior_returns else 0.0
        mad = (
            statistics.median(abs(value - median_return) for value in prior_returns)
            if prior_returns
            else 0.0
        )
        atr_sample = prior[-15:]
        atr = _mean(_true_ranges(atr_sample)[-14:]) if len(atr_sample) >= 2 else 0.0
        atr_pct = float(atr or 0.0) / previous["close"]
        threshold = max(0.25, 8 * mad, 6 * atr_pct)
        if absolute_return <= threshold:
            continue
        effective_date = current["date"]
        factor = verified_factors.get(effective_date)
        events.append(
            {
                "effective_date": effective_date,
                "previous_close": round(previous["close"], 6),
                "current_close": round(current["close"], 6),
                "gap_return_pct": round((current["close"] / previous["close"] - 1) * 100, 6),
                "detection_threshold_pct": round(threshold * 100, 6),
                "factor_status": "verified" if factor else "unverified",
                "factor": round(float(factor["factor"]), 10) if factor else None,
                "factor_sources": list(factor.get("sources") or []) if factor else [],
                "factor_verification": factor.get("verification") if factor else None,
            }
        )

    unverified = [item for item in events if item["factor_status"] != "verified"]
    if unverified:
        cutoff = max(str(item["effective_date"]) for item in unverified)
        usable = [dict(item) for item in bars if item["date"] >= cutoff]
        sufficient_segment = len(usable) >= ACTION_READY_MIN_BARS
        return {
            "status": "segmented" if sufficient_segment else "blocked",
            "analysis_basis": "post_event_raw",
            "runtime_quote_basis": "raw",
            "events": events,
            "raw_bar_count": len(bars),
            "usable_bar_count": len(usable),
            "post_event_bar_count": len(usable),
            "last_unverified_event_date": cutoff,
            "blocked_reasons": (
                [
                    "price_series_discontinuity_unverified",
                    "adjustment_factor_unverified",
                    "insufficient_post_event_history",
                ]
                if not sufficient_segment
                else []
            ),
            "warnings": ["adjustment_factor_unverified"] if sufficient_segment else [],
            "bars": usable,
        }

    converted = [dict(item) for item in bars]
    if events:
        for event in sorted(events, key=lambda item: str(item["effective_date"])):
            factor = float(event["factor"])
            for item in converted:
                if item["date"] >= str(event["effective_date"]):
                    continue
                for field in ("open", "high", "low", "close"):
                    item[field] *= factor
                item["volume"] = item["volume"] / factor if factor > 0 else item["volume"]
        status = "adjusted_verified"
        basis = "current_raw_equivalent"
    else:
        status = "continuous"
        basis = "raw"
    return {
        "status": status,
        "analysis_basis": basis,
        "runtime_quote_basis": "raw",
        "events": events,
        "raw_bar_count": len(bars),
        "usable_bar_count": len(converted),
        "post_event_bar_count": len(converted),
        "last_unverified_event_date": None,
        "blocked_reasons": [],
        "bars": converted,
    }


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _nearest_rank_percentile(values: list[float], ratio: float) -> float | None:
    """Return a deterministic nearest-rank percentile for small audit samples."""

    clean = sorted(value for value in values if math.isfinite(value) and value >= 0)
    if not clean:
        return None
    index = max(0, min(len(clean) - 1, math.ceil(len(clean) * ratio) - 1))
    return clean[index]


def _true_ranges(bars: list[dict[str, Any]]) -> list[float]:
    values: list[float] = []
    for index, item in enumerate(bars):
        previous_close = bars[index - 1]["close"] if index else item["open"]
        values.append(
            max(
                item["high"] - item["low"],
                abs(item["high"] - previous_close),
                abs(item["low"] - previous_close),
            )
        )
    return values


def _slope_pct(values: list[float], count: int) -> float | None:
    subset = values[-count:]
    if len(subset) < count or count < 2:
        return None
    x_mean = (count - 1) / 2
    y_mean = sum(subset) / count
    denominator = sum((index - x_mean) ** 2 for index in range(count))
    if denominator <= 0 or y_mean == 0:
        return None
    slope = sum(
        (index - x_mean) * (value - y_mean)
        for index, value in enumerate(subset)
    ) / denominator
    return slope / y_mean * 100


def _stable_id(prefix: str, *parts: Any, length: int = 20) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:length]}"


def _method_states(bar_count: int) -> list[dict[str, Any]]:
    return [
        {
            **dict(method),
            "status": "available" if bar_count >= int(method["minimum_bars"]) else "unavailable",
            "reason": None
            if bar_count >= int(method["minimum_bars"])
            else f"需要至少 {method['minimum_bars']} 个已完成交易日。",
        }
        for method in METHOD_REGISTRY
    ]


def _method_states_with_availability(
    bar_count: int,
    availability: dict[str, tuple[bool, str | None]],
) -> list[dict[str, Any]]:
    states: list[dict[str, Any]] = []
    for method in METHOD_REGISTRY:
        method_id = str(method["method_id"])
        if method_id in availability:
            available, reason = availability[method_id]
        else:
            available = bar_count >= int(method["minimum_bars"])
            reason = None if available else f"需要至少 {method['minimum_bars']} 个已完成交易日。"
        states.append(
            {
                **dict(method),
                "status": "available" if available else "unavailable",
                "reason": reason,
            }
        )
    return states


def _regime(bars: list[dict[str, Any]], atr14: float) -> dict[str, Any]:
    closes = [item["close"] for item in bars]
    volumes = [item["volume"] for item in bars]
    close = closes[-1]
    ma20 = _mean(closes[-20:]) if len(closes) >= 20 else None
    ma60 = _mean(closes[-60:]) if len(closes) >= 60 else None
    slope20 = _slope_pct(closes, 20)
    slope60 = _slope_pct(closes, 60)
    prior = bars[-21:-1] if len(bars) >= 21 else bars[:-1]
    prior_high = max((item["high"] for item in prior), default=None)
    prior_low = min((item["low"] for item in prior), default=None)
    baseline_volume = _mean(volumes[-21:-1]) if len(volumes) >= 21 else _mean(volumes[:-1])
    volume_ratio = (
        volumes[-1] / baseline_volume
        if baseline_volume is not None and baseline_volume > 0
        else None
    )
    atr_pct = atr14 / close * 100 if close else 0.0
    tr_pct = [value / item["close"] * 100 for value, item in zip(_true_ranges(bars[-60:]), bars[-60:], strict=False)]
    typical_tr_pct = _mean(tr_pct[:-1]) or atr_pct
    volatility_state = "高波动" if atr_pct >= typical_tr_pct * 1.4 else "低波动" if atr_pct <= typical_tr_pct * 0.7 else "正常波动"

    breakout = prior_high is not None and close > prior_high and (volume_ratio or 0) >= 1.1
    breakdown = prior_low is not None and close < prior_low and (volume_ratio or 0) >= 1.1
    if breakout:
        stage, direction = "向上突破", "向上"
    elif breakdown:
        stage, direction = "向下破位", "向下"
    elif ma20 is not None and ma60 is not None and slope20 is not None and slope60 is not None:
        if close > ma20 > ma60 and slope20 > 0 and slope60 >= 0:
            stage, direction = "上升趋势", "向上"
        elif close < ma20 < ma60 and slope20 < 0 and slope60 <= 0:
            stage, direction = "下降趋势", "向下"
        else:
            stage, direction = "区间震荡", "横盘"
    elif ma20 is not None and slope20 is not None:
        if close > ma20 and slope20 > 0:
            stage, direction = "短期上行", "向上"
        elif close < ma20 and slope20 < 0:
            stage, direction = "短期下行", "向下"
        else:
            stage, direction = "待确认", "待确认"
    else:
        stage, direction = "待确认", "待确认"

    normalized_slope = abs(slope20 or 0) / max(atr_pct, 0.01)
    strength = "强" if normalized_slope >= 0.18 else "中" if normalized_slope >= 0.06 else "弱"
    return {
        "stage": stage,
        "direction": direction,
        "strength": strength,
        "volatility_state": volatility_state,
        "close": round(close, 6),
        "ma20": round(ma20, 6) if ma20 is not None else None,
        "ma60": round(ma60, 6) if ma60 is not None else None,
        "slope20_pct_per_bar": round(slope20, 6) if slope20 is not None else None,
        "slope60_pct_per_bar": round(slope60, 6) if slope60 is not None else None,
        "atr14": round(atr14, 8),
        "atr14_pct": round(atr_pct, 6),
        "latest_volume_ratio_20": round(volume_ratio, 6) if volume_ratio is not None else None,
        "basis": "仅使用截止日及之前的已完成日线。",
    }


def _confirmed_pivots(
    bars: list[dict[str, Any]], *, span: int = 2
) -> list[dict[str, Any]]:
    pivots: list[dict[str, Any]] = []
    for index in range(span, len(bars) - span):
        window = bars[index - span : index + span + 1]
        item = bars[index]
        if item["low"] == min(row["low"] for row in window):
            pivots.append(
                {
                    "source_kind": "swing_low",
                    "side": "support",
                    "price": item["low"],
                    "source_date": item["date"],
                    "confirmed_at": bars[index + span]["date"],
                    "method_id": "confirmed_swing_points",
                }
            )
        if item["high"] == max(row["high"] for row in window):
            pivots.append(
                {
                    "source_kind": "swing_high",
                    "side": "resistance",
                    "price": item["high"],
                    "source_date": item["date"],
                    "confirmed_at": bars[index + span]["date"],
                    "method_id": "confirmed_swing_points",
                }
            )
    return pivots[-80:]


def _source_levels(
    bars: list[dict[str, Any]], horizons: tuple[int, ...]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sources: list[dict[str, Any]] = []
    states: list[dict[str, Any]] = []
    for horizon in horizons:
        if len(bars) < horizon:
            states.append(
                {
                    "trading_days": horizon,
                    "status": "unavailable",
                    "available_bars": len(bars),
                }
            )
            continue
        sample = bars[-horizon:]
        low = min(sample, key=lambda item: (item["low"], item["date"]))
        high = max(sample, key=lambda item: (item["high"], item["date"]))
        states.append(
            {
                "trading_days": horizon,
                "status": "available",
                "low": round(low["low"], 6),
                "low_date": low["date"],
                "high": round(high["high"], 6),
                "high_date": high["date"],
            }
        )
        sources.extend(
            [
                {
                    "source_kind": "rolling_low",
                    "side": "support",
                    "price": low["low"],
                    "source_date": low["date"],
                    "horizon": horizon,
                    "method_id": "multi_horizon_structure",
                },
                {
                    "source_kind": "rolling_high",
                    "side": "resistance",
                    "price": high["high"],
                    "source_date": high["date"],
                    "horizon": horizon,
                    "method_id": "multi_horizon_structure",
                },
            ]
        )
    sources.extend(_confirmed_pivots(bars))
    return sources, states


def _anchored_vwap_sources(
    bars: list[dict[str, Any]], *, close: float
) -> tuple[list[dict[str, Any]], tuple[bool, str | None]]:
    pivots = _confirmed_pivots(bars)
    selected: list[dict[str, Any]] = []
    for side in ("support", "resistance"):
        pivot = next((item for item in reversed(pivots) if item["side"] == side), None)
        if pivot is None:
            continue
        anchored = [item for item in bars if item["date"] >= str(pivot["source_date"])]
        weights = [float(item.get("volume") or 0.0) for item in anchored]
        denominator = sum(weights)
        if denominator <= 0:
            continue
        vwap = sum(
            ((item["high"] + item["low"] + item["close"]) / 3) * weight
            for item, weight in zip(anchored, weights, strict=False)
        ) / denominator
        selected.append(
            {
                "source_kind": f"anchored_vwap_from_{pivot['source_kind']}",
                "side": "support" if vwap <= close else "resistance",
                "price": vwap,
                "source_date": pivot["source_date"],
                "confirmed_at": pivot.get("confirmed_at"),
                "method_id": "anchored_vwap",
            }
        )
    if selected:
        return selected, (True, None)
    return [], (False, "成交量或已确认摆动点不足，无法计算锚定VWAP。")


def _volume_profile_sources(
    raw_bars: Iterable[dict[str, Any]] | None,
    *,
    close: float,
    tolerance: float,
) -> tuple[list[dict[str, Any]], tuple[bool, str | None]]:
    usable: list[dict[str, Any]] = []
    for raw in raw_bars or []:
        if not isinstance(raw, dict):
            continue
        high = _number(raw.get("high"))
        low = _number(raw.get("low"))
        closed = _number(raw.get("close"))
        if None in {high, low, closed}:
            continue
        amount = _number(raw.get("amount")) or 0.0
        volume = _number(raw.get("volume")) or 0.0
        weight = amount if amount > 0 else volume
        if weight <= 0:
            continue
        usable.append(
            {
                "time": str(raw.get("bar_time") or raw.get("session_date") or ""),
                "price": (float(high) + float(low) + float(closed)) / 3,
                "weight": weight,
            }
        )
    if len(usable) < 100:
        return [], (False, f"已完成且量能可用的5分钟K线不足100根（当前{len(usable)}根）。")
    bucket_size = max(tolerance, close * 0.002)
    buckets: dict[int, float] = defaultdict(float)
    for item in usable:
        buckets[round(float(item["price"]) / bucket_size)] += float(item["weight"])
    total_weight = sum(buckets.values())
    strongest = sorted(buckets.items(), key=lambda item: item[1], reverse=True)[:3]
    latest_time = max(str(item["time"]) for item in usable)
    sources = [
        {
            "source_kind": "volume_profile_node",
            "side": "support" if bucket * bucket_size <= close else "resistance",
            "price": bucket * bucket_size,
            "source_date": latest_time[:10],
            "method_id": "volume_profile",
            "weight_share": weight / total_weight if total_weight > 0 else 0.0,
        }
        for bucket, weight in strongest
    ]
    return sources, (True, None)


def _clusters(
    sources: list[dict[str, Any]], *, close: float, tolerance: float
) -> list[list[dict[str, Any]]]:
    by_side: dict[str, list[dict[str, Any]]] = {"support": [], "resistance": []}
    for source in sources:
        side = "support" if float(source["price"]) <= close else "resistance"
        by_side[side].append(source)
    groups: list[list[dict[str, Any]]] = []
    for side_sources in by_side.values():
        current: list[dict[str, Any]] = []
        center = 0.0
        for source in sorted(side_sources, key=lambda item: float(item["price"])):
            price = float(source["price"])
            if current and abs(price - center) > tolerance:
                groups.append(current)
                current = []
            current.append(source)
            center = sum(float(item["price"]) for item in current) / len(current)
        if current:
            groups.append(current)
    return groups


def _candidate(
    group: list[dict[str, Any]],
    *,
    bars: list[dict[str, Any]],
    close: float,
    atr14: float,
    tolerance: float,
    tick_size: float,
    as_of: str,
    adjustment: str,
) -> dict[str, Any]:
    price = sum(float(item["price"]) for item in group) / len(group)
    side = "support" if price <= close else "resistance"
    half_width = max(tick_size * 2, tolerance * 0.55)
    lower, upper = price - half_width, price + half_width
    recent = bars[-120:]
    touch_indexes = [
        index
        for index, item in enumerate(recent)
        if item["low"] <= upper and item["high"] >= lower
    ]
    separated: list[int] = []
    for index in touch_indexes:
        if not separated or index - separated[-1] >= 2:
            separated.append(index)
    reactions: list[float] = []
    adverse_excursions: list[float] = []
    touch_volumes: list[float] = []
    for index in separated:
        follow = recent[index + 1 : index + 6]
        if follow and atr14 > 0:
            if side == "support":
                move = max(item["high"] for item in follow) - price
                adverse = price - min(item["low"] for item in follow)
            else:
                move = price - min(item["low"] for item in follow)
                adverse = max(item["high"] for item in follow) - price
            reactions.append(max(0.0, move / atr14))
            adverse_excursions.append(max(0.0, adverse / atr14))
        touch_volumes.append(recent[index]["volume"])
    baseline_volume = _mean([item["volume"] for item in recent[-20:]]) or 0.0
    volume_ratio = (
        (_mean(touch_volumes) or 0.0) / baseline_volume if baseline_volume > 0 else None
    )
    source_sides = {str(item.get("side")) for item in group}
    role_reversal = len(source_sides) > 1
    methods = sorted({str(item["method_id"]) for item in group})
    horizons = sorted({int(item["horizon"]) for item in group if item.get("horizon")})
    latest_touch_age = len(recent) - 1 - max(separated, default=0)
    reaction_mean = _mean(reactions) or 0.0
    score = (
        min(len(methods) / 2, 1) * 20
        + min(len(horizons) / 3, 1) * 15
        + min(len(separated) / 4, 1) * 20
        + min(reaction_mean / 2, 1) * 20
        + max(0.0, 1 - latest_touch_age / 120) * 10
        + min(max((volume_ratio or 0) - 0.8, 0) / 0.7, 1) * 5
        + (10 if role_reversal else 0)
    )
    confidence = "high" if score >= 70 else "medium" if score >= 45 else "low"
    distance_atr = abs(close - price) / atr14 if atr14 > 0 else 0.0
    adverse_p80 = _nearest_rank_percentile(adverse_excursions, 0.80)
    anti_noise_calibrated = len(adverse_excursions) >= 3
    # A structural invalidation line must sit outside the instrument's normal
    # noise.  Sparse samples use a conservative one-ATR prior and remain
    # watch-only until enough completed touch episodes exist.
    noise_atr = max(0.75, adverse_p80 if anti_noise_calibrated and adverse_p80 is not None else 1.0)
    noise_band = atr14 * noise_atr
    invalidation = lower - noise_band if side == "support" else upper + noise_band
    method_families = {
        METHOD_FAMILY_BY_ID[method]
        for method in methods
        if method in METHOD_FAMILY_BY_ID
    }
    if len(separated) >= 2 and reaction_mean >= 0.5:
        method_families.add("reaction")
    if anti_noise_calibrated:
        method_families.add("volatility")
    longest_horizon = max(horizons, default=0)
    if longest_horizon >= 120:
        role = "S2" if side == "support" else "R2"
    elif any(horizon in {20, 60} for horizon in horizons):
        role = "S1" if side == "support" else "R1"
    else:
        role = "T0"
    structural_bonus = sum(5 for horizon in horizons if horizon in {20, 60})
    long_term_bonus = sum(7 for horizon in horizons if horizon in {120, 250})
    if role == "T0":
        rank_score = score - min(distance_atr, 6) * 5.0
    elif role in {"S1", "R1"}:
        rank_score = score + structural_bonus + min(reaction_mean, 2.0) * 3.0 - min(distance_atr, 12) * 1.0
    else:
        rank_score = score + long_term_bonus + min(len(method_families), 4) * 2.0
    rounded_price = round(price, 6)
    candidate_id = _stable_id(
        "level",
        METHOD_REGISTRY_VERSION,
        side,
        round(rounded_price / max(tick_size, 0.000001)),
        sorted((item["source_kind"], item["source_date"]) for item in group),
    )
    return {
        "candidate_id": candidate_id,
        "level_type": side,
        "kind": "zone",
        "lower": round(lower, 6),
        "upper": round(upper, 6),
        "representative_value": rounded_price,
        "unit": "CNY",
        "adjustment": adjustment,
        "price_actionability": "price_actionable" if adjustment == "raw" else "analysis_only",
        "score": round(score, 2),
        "confidence": confidence,
        "role": role,
        "method_families": sorted(method_families),
        "rank_score": round(rank_score, 2),
        "proximity_score": round(-distance_atr, 4),
        "distance_atr": round(distance_atr, 4),
        "method_ids": methods,
        "horizons": horizons,
        "evidence": {
            "source_count": len(group),
            "touch_count": len(separated),
            "mean_reaction_atr": round(reaction_mean, 4),
            "touch_volume_ratio_20": round(volume_ratio, 4) if volume_ratio is not None else None,
            "latest_touch_trading_days_ago": latest_touch_age,
            "role_reversal": role_reversal,
            "adverse_excursion_sample_count": len(adverse_excursions),
            "adverse_excursion_p80_atr": round(adverse_p80, 4) if adverse_p80 is not None else None,
            "anti_noise_calibrated": anti_noise_calibrated,
            "source_dates": sorted({str(item["source_date"]) for item in group}),
        },
        "noise_gate": {
            "noise_band": round(noise_band, 6),
            "noise_band_atr": round(noise_atr, 4),
            "atr_floor": 0.75,
            "calibrated": anti_noise_calibrated,
            "sample_count": len(adverse_excursions),
            "policy": "max_atr_floor_or_touch_adverse_excursion_p80",
        },
        "invalidation": {
            "kind": "daily_close_below" if side == "support" else "daily_close_above",
            "value": round(invalidation, 6),
            "adjustment": adjustment,
        },
        "calculation_basis": {
            "method": "multi_method_level_evidence",
            "method_version": METHOD_REGISTRY_VERSION,
            "summary": "由多周期边界、已确认摆动点、波动缓冲和触及反应共同评分。",
            "as_of": as_of,
            "references": [
                {
                    "kind": item["source_kind"],
                    "date": item["source_date"],
                    "value": round(float(item["price"]), 6),
                    **({"horizon": item["horizon"]} if item.get("horizon") else {}),
                }
                for item in group[:12]
            ],
        },
    }


def _build_market_analysis_snapshot_v1(
    raw_bars: Iterable[dict[str, Any]],
    *,
    through: str | None = None,
    symbol: str = "",
    instrument_type: str = "company_equity",
    adjustment: str = "raw",
    tick_size: float | None = None,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
) -> dict[str, Any]:
    """Build the sole numeric input that a report-analysis Agent may use."""

    bars = normalize_completed_daily_bars(raw_bars, through=through)
    as_of = bars[-1]["date"] if bars else str(through or "")[:10]
    tick = tick_size if tick_size is not None else (0.001 if instrument_type == "etf" else 0.01)
    methods = _method_states(len(bars))
    data_gaps: list[str] = []
    if len(bars) < 20:
        data_gaps.append("已完成日线不足，不能形成正式结构候选。")
        return {
            "schema_version": 1,
            "registry_version": METHOD_REGISTRY_VERSION,
            "symbol": symbol,
            "instrument_type": instrument_type,
            "as_of": as_of or None,
            "bar_count": len(bars),
            "cutoff_policy": "completed_daily_bars_only",
            "price_basis": {"adjustment": adjustment, "actionability": "analysis_only"},
            "methods": methods,
            "horizons": [],
            "regime": {"stage": "数据不足", "direction": "待确认", "strength": "待确认"},
            "level_candidates": [],
            "primary_levels": {},
            "level_ladder": {"support": [], "resistance": []},
            "instrument_context_requirements": [],
            "data_gaps": data_gaps,
        }

    trailing = bars[-250:]
    true_ranges = _true_ranges(trailing)
    atr14 = _mean(true_ranges[-14:]) or 0.0
    close = trailing[-1]["close"]
    tolerance = max(tick * 3, atr14 * 0.35, close * 0.0035)
    sources, horizon_states = _source_levels(trailing, horizons)
    candidates = [
        _candidate(
            group,
            bars=trailing,
            close=close,
            atr14=atr14,
            tolerance=tolerance,
            tick_size=tick,
            as_of=as_of,
            adjustment=adjustment,
        )
        for group in _clusters(sources, close=close, tolerance=tolerance)
    ]
    selected: list[dict[str, Any]] = []
    primary: dict[str, dict[str, Any]] = {}
    for side in ("support", "resistance"):
        ranked = sorted(
            (item for item in candidates if item["level_type"] == side),
            key=lambda item: (float(item["rank_score"]), float(item["score"])),
            reverse=True,
        )[:5]
        selected.extend(ranked)
        if ranked:
            primary[side] = dict(ranked[0])
        else:
            data_gaps.append(f"没有形成可复核的{'支撑' if side == 'support' else '阻力'}候选。")
    if adjustment != "raw":
        data_gaps.append("价格序列不是原始价格口径，候选区间只能用于结构分析，不能直接生成监控点位。")
    unavailable_horizons = [
        str(item["trading_days"])
        for item in horizon_states
        if item["status"] != "available"
    ]
    if unavailable_horizons:
        data_gaps.append("以下交易日窗口尚无足够历史：" + "、".join(unavailable_horizons) + "。")

    instrument_requirements = []
    if instrument_type == "etf":
        instrument_requirements = [
            {"scope": "tracking_index_relative_strength", "status": "external_context_required"},
            {"scope": "tracking_error", "status": "external_context_required"},
            {"scope": "fund_shares", "status": "external_context_required"},
            {"scope": "premium_discount", "status": "external_context_required"},
            {"scope": "component_contribution", "status": "external_context_required"},
        ]
    return {
        "schema_version": 1,
        "registry_version": METHOD_REGISTRY_VERSION,
        "symbol": symbol,
        "instrument_type": instrument_type,
        "as_of": as_of,
        "bar_count": len(bars),
        "cutoff_policy": "completed_daily_bars_only",
        "price_basis": {
            "adjustment": adjustment,
            "actionability": "price_actionable" if adjustment == "raw" else "analysis_only",
        },
        "methods": methods,
        "horizons": horizon_states,
        "regime": _regime(trailing, atr14),
        "level_candidates": sorted(
            selected,
            key=lambda item: (item["level_type"], -float(item["rank_score"])),
        ),
        "primary_levels": primary,
        "instrument_context_requirements": instrument_requirements,
        "data_gaps": list(dict.fromkeys(data_gaps)),
    }


def build_market_analysis_snapshot(
    raw_bars: Iterable[dict[str, Any]],
    *,
    through: str | None = None,
    symbol: str = "",
    instrument_type: str = "company_equity",
    adjustment: str = "raw",
    tick_size: float | None = None,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    factor_rows: Iterable[dict[str, Any]] | None = None,
    intraday_bars: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one continuity-safe, multi-method level snapshot.

    Numeric levels remain deterministic.  A report Agent may select candidate
    ids from this snapshot, but cannot calculate replacement prices.
    """

    raw_material = list(raw_bars)
    continuity = analyze_price_continuity(
        raw_material,
        through=through,
        factor_rows=factor_rows,
    )
    bars = list(continuity["bars"])
    snapshot = _build_market_analysis_snapshot_v1(
        bars,
        through=through,
        symbol=symbol,
        instrument_type=instrument_type,
        adjustment=adjustment,
        tick_size=tick_size,
        horizons=horizons,
    )
    snapshot["schema_version"] = 2
    snapshot["continuity"] = {
        key: value
        for key, value in continuity.items()
        if key != "bars"
    }
    data_gap_codes = [
        str(value)
        for value in continuity.get("blocked_reasons") or []
        if str(value)
    ]
    snapshot["data_gap_codes"] = list(dict.fromkeys(data_gap_codes))
    snapshot["level_snapshot_id"] = _stable_id(
        "level_snapshot",
        METHOD_REGISTRY_VERSION,
        symbol,
        snapshot.get("as_of"),
        [(item["date"], item["close"], item.get("volume")) for item in bars[-60:]],
        snapshot["continuity"],
    )
    if len(bars) < WATCH_ONLY_MIN_BARS:
        snapshot["level_candidates"] = []
        snapshot["primary_levels"] = {}
        snapshot["level_ladder"] = {"support": [], "resistance": []}
        snapshot["price_basis"] = {
            "adjustment": adjustment,
            "analysis_basis": continuity["analysis_basis"],
            "runtime_quote_basis": "raw",
            "actionability": "analysis_only",
            "conversion_verified": continuity["status"] != "blocked",
        }
        snapshot["methods"] = _method_states_with_availability(
            len(bars),
            {
                "volume_profile": (False, "结构历史不足，未计算5分钟成交密集区。"),
                "anchored_vwap": (False, "结构历史不足，未计算锚定VWAP。"),
            },
        )
        return snapshot

    trailing = bars[-250:]
    true_ranges = _true_ranges(trailing)
    atr14 = _mean(true_ranges[-14:]) or 0.0
    close = trailing[-1]["close"]
    tick = tick_size if tick_size is not None else (0.001 if instrument_type == "etf" else 0.01)
    tolerance = max(tick * 3, atr14 * 0.35, close * 0.0035)
    sources, horizon_states = _source_levels(trailing, horizons)
    anchored_sources, anchored_state = _anchored_vwap_sources(trailing, close=close)
    profile_sources, profile_state = _volume_profile_sources(
        intraday_bars,
        close=close,
        tolerance=tolerance,
    )
    sources.extend(anchored_sources)
    sources.extend(profile_sources)

    continuity_actionable = continuity["status"] in {"continuous", "adjusted_verified", "segmented"}
    price_actionability = (
        "price_actionable"
        if adjustment == "raw" and continuity_actionable and len(bars) >= ACTION_READY_MIN_BARS
        else "watch_only"
        if adjustment == "raw" and len(bars) >= WATCH_ONLY_MIN_BARS
        else "analysis_only"
    )
    candidates = [
        _candidate(
            group,
            bars=trailing,
            close=close,
            atr14=atr14,
            tolerance=tolerance,
            tick_size=tick,
            as_of=str(snapshot.get("as_of") or ""),
            adjustment=adjustment,
        )
        for group in _clusters(sources, close=close, tolerance=tolerance)
    ]
    for candidate in candidates:
        candidate["price_actionability"] = price_actionability
        independent_families = len(set(candidate.get("method_families") or []))
        role = str(candidate.get("role") or "T0")
        horizons_for_role = set(candidate.get("horizons") or [])
        role_horizon_ready = (
            bool(horizons_for_role.intersection({20, 60}))
            if role in {"S1", "R1"}
            else bool(horizons_for_role.intersection({60, 120, 250}))
            if role in {"S2", "R2"}
            else False
        )
        action_ready = bool(
            price_actionability == "price_actionable"
            and float(candidate.get("score") or 0.0) >= 70
            and candidate.get("confidence") == "high"
            and independent_families >= 2
            and role != "T0"
            and role_horizon_ready
            and bool((candidate.get("noise_gate") or {}).get("calibrated"))
        )
        candidate["automation_status"] = "action_ready" if action_ready else "watch_only"
        candidate["calculation_basis"].update(
            score=candidate.get("score"),
            confidence=candidate.get("confidence"),
            zone={"lower": candidate.get("lower"), "upper": candidate.get("upper")},
            horizons=list(candidate.get("horizons") or []),
            role=role,
            method_families=list(candidate.get("method_families") or []),
            evidence=dict(candidate.get("evidence") or {}),
            invalidation=dict(candidate.get("invalidation") or {}),
            noise_gate=dict(candidate.get("noise_gate") or {}),
            continuity_status=continuity["status"],
        )

    selected: list[dict[str, Any]] = []
    primary: dict[str, dict[str, Any]] = {}
    level_ladder: dict[str, list[dict[str, Any]]] = {"support": [], "resistance": []}
    for side in ("support", "resistance"):
        ranked = sorted(
            (
                item
                for item in candidates
                if item["level_type"] == side and float(item.get("score") or 0.0) >= 45
            ),
            key=lambda item: (float(item["rank_score"]), float(item["score"])),
            reverse=True,
        )[:5]
        selected.extend(ranked)
        role_order = {"T0": 0, "S1": 1, "R1": 1, "S2": 2, "R2": 2}
        ladder = sorted(
            ranked,
            key=lambda item: (
                role_order.get(str(item.get("role") or "T0"), 0),
                float(item.get("rank_score") or 0.0),
            ),
            reverse=True,
        )[:3]
        level_ladder[side] = [dict(item) for item in ladder]
        if ranked:
            structural = [item for item in ladder if str(item.get("role")) != "T0"]
            preferred_role = "S1" if side == "support" else "R1"
            preferred_structural = [
                item for item in structural if item.get("role") == preferred_role
            ]
            preferred_action_ready = [
                item
                for item in preferred_structural
                if item.get("automation_status") == "action_ready"
            ]
            action_ready_candidates = [
                item for item in structural if item.get("automation_status") == "action_ready"
            ]
            primary[side] = dict(
                (
                    preferred_action_ready
                    or preferred_structural
                    or action_ready_candidates
                    or structural
                    or ranked
                )[0]
            )

    snapshot.update(
        bar_count=len(bars),
        price_basis={
            "adjustment": adjustment,
            "analysis_basis": continuity["analysis_basis"],
            "runtime_quote_basis": "raw",
            "actionability": price_actionability,
            "conversion_verified": continuity_actionable,
        },
        methods=_method_states_with_availability(
            len(bars),
            {
                "volume_profile": profile_state,
                "anchored_vwap": anchored_state,
            },
        ),
        horizons=horizon_states,
        regime=_regime(trailing, atr14),
        level_candidates=sorted(
            selected,
            key=lambda item: (item["level_type"], -float(item["rank_score"])),
        ),
        primary_levels=primary,
        level_ladder=level_ladder,
    )
    if not primary:
        snapshot["data_gap_codes"].append("no_qualified_level")
    return snapshot


def market_analysis_snapshot_from_contexts(
    contexts: list[dict[str, Any]],
    *,
    symbol: str,
    through: str | None = None,
    instrument_type: str = "company_equity",
) -> dict[str, Any]:
    """Select the best frozen daily series for a Daily Run method snapshot."""

    normalized = symbol.upper()
    choices: list[tuple[int, int, str, list[dict[str, Any]]]] = []
    for context in contexts:
        market = context.get("market") if isinstance(context.get("market"), dict) else {}
        for series in market.get("series") or []:
            if not isinstance(series, dict):
                continue
            candidate_symbol = str(series.get("symbol") or normalized).upper()
            interval = str(series.get("interval") or "").upper()
            if candidate_symbol != normalized or interval not in {"1D", "D", "DAILY"}:
                continue
            bars = list(series.get("bars") or [])
            adjustment = str(series.get("adjustment") or "raw").lower()
            raw_rank = 1 if adjustment == "raw" else 0
            choices.append((raw_rank, len(bars), adjustment, bars))
    if not choices:
        return build_market_analysis_snapshot(
            [], through=through, symbol=normalized, instrument_type=instrument_type
        )
    raw_rank, _length, adjustment, bars = max(choices, key=lambda item: (item[0], item[1]))
    del raw_rank
    return build_market_analysis_snapshot(
        bars,
        through=through,
        symbol=normalized,
        instrument_type=instrument_type,
        adjustment=adjustment,
    )


def walk_forward_level_evaluation(
    raw_bars: Iterable[dict[str, Any]],
    *,
    symbol: str = "",
    instrument_type: str = "company_equity",
    adjustment: str = "raw",
    tick_size: float | None = None,
    minimum_history: int = 120,
    forward_bars: int = 5,
    step: int = 5,
) -> dict[str, Any]:
    """Compare v1 candidates with the old 20-day extreme baseline walk-forward.

    Every snapshot is built only from bars available at that evaluation point.
    Future bars are used solely for scoring the already-frozen candidate.
    """

    bars = normalize_completed_daily_bars(raw_bars)
    tick = tick_size if tick_size is not None else (0.001 if instrument_type == "etf" else 0.01)
    counters = {
        name: {
            "eligible_levels": 0,
            "touched": 0,
            "confirmed_reaction": 0,
            "invalidated_first": 0,
            "ambiguous": 0,
            "unresolved": 0,
        }
        for name in ("method_v1", "rolling_20_day_baseline")
    }
    evaluation_points = 0

    def score_level(
        bucket: dict[str, int],
        *,
        level_type: str,
        lower: float,
        upper: float,
        representative: float,
        invalidation: float,
        atr: float,
        future: list[dict[str, Any]],
    ) -> None:
        bucket["eligible_levels"] += 1
        touched = False
        for item in future:
            if not touched:
                touched = item["low"] <= upper and item["high"] >= lower
                if not touched:
                    continue
                bucket["touched"] += 1
            if level_type == "support":
                invalid = item["close"] < invalidation
                reacted = item["high"] >= representative + atr * 0.75
            else:
                invalid = item["close"] > invalidation
                reacted = item["low"] <= representative - atr * 0.75
            if invalid and reacted:
                bucket["ambiguous"] += 1
                return
            if invalid:
                bucket["invalidated_first"] += 1
                return
            if reacted:
                bucket["confirmed_reaction"] += 1
                return
        if touched:
            bucket["unresolved"] += 1

    for index in range(minimum_history - 1, len(bars) - forward_bars, max(1, step)):
        history = bars[: index + 1]
        future = bars[index + 1 : index + 1 + forward_bars]
        snapshot = build_market_analysis_snapshot(
            history,
            through=history[-1]["date"],
            symbol=symbol,
            instrument_type=instrument_type,
            adjustment=adjustment,
            tick_size=tick,
        )
        atr = float((snapshot.get("regime") or {}).get("atr14") or 0)
        if atr <= 0:
            continue
        evaluation_points += 1
        for level_type, level in (snapshot.get("primary_levels") or {}).items():
            if level_type not in {"support", "resistance"} or not isinstance(level, dict):
                continue
            score_level(
                counters["method_v1"],
                level_type=level_type,
                lower=float(level["lower"]),
                upper=float(level["upper"]),
                representative=float(level["representative_value"]),
                invalidation=float((level.get("invalidation") or {})["value"]),
                atr=atr,
                future=future,
            )

        lookback = history[-20:]
        baseline_support = min(item["low"] for item in lookback)
        baseline_resistance = max(item["high"] for item in lookback)
        width = max(tick * 3, atr * 0.15)
        for level_type, representative in (
            ("support", baseline_support),
            ("resistance", baseline_resistance),
        ):
            score_level(
                counters["rolling_20_day_baseline"],
                level_type=level_type,
                lower=representative - width * 0.5,
                upper=representative + width * 0.5,
                representative=representative,
                invalidation=(
                    representative - width * 0.5 - atr * 0.25
                    if level_type == "support"
                    else representative + width * 0.5 + atr * 0.25
                ),
                atr=atr,
                future=future,
            )

    methods: dict[str, Any] = {}
    for name, values in counters.items():
        touched = values["touched"]
        resolved = values["confirmed_reaction"] + values["invalidated_first"]
        methods[name] = {
            **values,
            "touch_rate": round(touched / values["eligible_levels"], 6)
            if values["eligible_levels"]
            else None,
            "confirmed_reaction_rate": round(values["confirmed_reaction"] / touched, 6)
            if touched
            else None,
            "first_invalidation_rate": round(values["invalidated_first"] / touched, 6)
            if touched
            else None,
            "resolved_precision": round(values["confirmed_reaction"] / resolved, 6)
            if resolved
            else None,
        }
    return {
        "schema_version": 1,
        "registry_version": METHOD_REGISTRY_VERSION,
        "symbol": symbol,
        "instrument_type": instrument_type,
        "adjustment": adjustment,
        "evaluation_policy": {
            "minimum_history": minimum_history,
            "forward_bars": forward_bars,
            "step": step,
            "reaction_threshold_atr": 0.75,
            "no_future_snapshot_inputs": True,
        },
        "evaluation_points": evaluation_points,
        "methods": methods,
    }


def evaluate_level_method_rollout_gate(evaluation: dict[str, Any]) -> dict[str, Any]:
    """Apply the non-regression gate required before a method is promoted."""

    methods = dict(evaluation.get("methods") or {})
    candidate = dict(methods.get("method_v1") or {})
    baseline = dict(methods.get("rolling_20_day_baseline") or {})

    def number(payload: dict[str, Any], key: str) -> float | None:
        value = payload.get(key)
        return float(value) if isinstance(value, (int, float)) else None

    touch = number(candidate, "touch_rate")
    baseline_touch = number(baseline, "touch_rate")
    precision = number(candidate, "resolved_precision")
    baseline_precision = number(baseline, "resolved_precision")
    invalidation = number(candidate, "first_invalidation_rate")
    baseline_invalidation = number(baseline, "first_invalidation_rate")
    checks = {
        "has_evaluation_points": int(evaluation.get("evaluation_points") or 0) > 0,
        "touch_rate_not_lower": (
            touch is not None and baseline_touch is not None and touch >= baseline_touch
        ),
        "resolved_precision_within_2pp": (
            precision is not None
            and baseline_precision is not None
            and precision >= baseline_precision - 0.02
        ),
        "first_invalidation_within_3pp": (
            invalidation is not None
            and baseline_invalidation is not None
            and invalidation <= baseline_invalidation + 0.03
        ),
    }
    return {
        "registry_version": evaluation.get("registry_version"),
        "eligible_for_automatic_release": all(checks.values()),
        "checks": checks,
        "thresholds": {
            "touch_rate_delta_min": 0.0,
            "resolved_precision_delta_min": -0.02,
            "first_invalidation_delta_max": 0.03,
        },
    }


def _extract_json(text: str) -> dict[str, Any]:
    stripped = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.S | re.I)
    candidate = fenced.group(1) if fenced else stripped
    if not candidate.startswith("{"):
        start, end = candidate.find("{"), candidate.rfind("}")
        candidate = candidate[start : end + 1] if start >= 0 and end > start else candidate
    value = json.loads(candidate)
    if not isinstance(value, dict):
        raise AgentAnalysisContractError("agent analysis must be a JSON object")
    return value


def _strings(value: Any, *, maximum: int = 8, required: bool = False) -> list[str]:
    if not isinstance(value, list):
        if required:
            raise AgentAnalysisContractError("agent analysis field must be a list")
        return []
    result = [str(item).strip()[:800] for item in value[:maximum] if str(item).strip()]
    if required and not result:
        raise AgentAnalysisContractError("agent analysis list cannot be empty")
    return result


def validate_agent_method_analysis(
    value: str | dict[str, Any],
    *,
    snapshot: dict[str, Any],
    allowed_data_gap_codes: set[str] | None = None,
) -> dict[str, Any]:
    """Allow method selection and prose synthesis, never model-created prices."""

    raw = _extract_json(value) if isinstance(value, str) else dict(value)
    allowed = {
        "regime_interpretation",
        "selected_methods",
        "selected_level_ids",
        "evidence_for",
        "counter_evidence",
        "cross_horizon_conclusion",
        "invalidation_conditions",
        "confidence",
        "data_gaps",
        "critic",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise AgentAnalysisContractError(
            "unsupported agent analysis fields: " + ", ".join(unknown)
        )
    available_methods = {
        str(item["method_id"])
        for item in snapshot.get("methods") or []
        if item.get("status") == "available"
    }
    selected_methods = _strings(raw.get("selected_methods"), maximum=8, required=True)
    if not set(selected_methods) <= available_methods:
        raise AgentAnalysisContractError("agent selected an unavailable analysis method")
    available_levels = {
        str(item["candidate_id"]) for item in snapshot.get("level_candidates") or []
    }
    selected_levels = _strings(raw.get("selected_level_ids"), maximum=8)
    if not set(selected_levels) <= available_levels:
        raise AgentAnalysisContractError("agent selected an unknown numeric level")
    confidence = str(raw.get("confidence") or "low").lower()
    if confidence not in {"low", "medium", "high"}:
        raise AgentAnalysisContractError("agent confidence is invalid")
    critic = raw.get("critic") if isinstance(raw.get("critic"), dict) else {}
    verdict = str(critic.get("verdict") or "insufficient").lower()
    if verdict not in {"pass", "revise", "insufficient"}:
        raise AgentAnalysisContractError("critic verdict is invalid")
    narratives = {
        "regime_interpretation": str(raw.get("regime_interpretation") or "").strip(),
        "cross_horizon_conclusion": str(raw.get("cross_horizon_conclusion") or "").strip(),
    }
    if not all(narratives.values()):
        raise AgentAnalysisContractError("agent analysis narrative is incomplete")
    prose = [
        *narratives.values(),
        *_strings(raw.get("evidence_for"), required=True),
        *_strings(raw.get("counter_evidence"), required=True),
        *_strings(raw.get("invalidation_conditions"), required=True),
        *_strings(critic.get("issues")),
    ]
    if any(re.search(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", item) for item in prose):
        raise AgentAnalysisContractError(
            "agent narrative introduced a numeric literal; use registered level IDs instead"
        )
    reported_gaps = _strings(raw.get("data_gaps"))
    if allowed_data_gap_codes is not None and not set(reported_gaps) <= set(
        allowed_data_gap_codes
    ):
        raise AgentAnalysisContractError(
            "agent reported a data gap that was not present in frozen inputs"
        )
    return {
        "schema_version": 1,
        "analysis_version": AGENT_ANALYSIS_VERSION,
        "status": "completed",
        "regime_interpretation": narratives["regime_interpretation"][:1200],
        "selected_methods": selected_methods,
        "selected_level_ids": selected_levels,
        "evidence_for": _strings(raw.get("evidence_for"), required=True),
        "counter_evidence": _strings(raw.get("counter_evidence"), required=True),
        "cross_horizon_conclusion": narratives["cross_horizon_conclusion"][:1600],
        "invalidation_conditions": _strings(
            raw.get("invalidation_conditions"), required=True
        ),
        "confidence": confidence,
        "data_gaps": reported_gaps,
        "critic": {
            "verdict": verdict,
            "issues": _strings(critic.get("issues")),
        },
        "trade_execution": "forbidden",
    }


def unavailable_agent_analysis(reason: str) -> dict[str, Any]:
    """Record why synthesis did not run without pretending it was Agent work."""

    return {
        "schema_version": 1,
        "analysis_version": AGENT_ANALYSIS_VERSION,
        "status": "not_run",
        "reason": str(reason or "Agent 分析未启用。")[:800],
        "selected_methods": [],
        "selected_level_ids": [],
        "evidence_for": [],
        "counter_evidence": [],
        "invalidation_conditions": [],
        "data_gaps": [],
        "critic": {"verdict": "insufficient", "issues": []},
        "trade_execution": "forbidden",
    }


def build_agent_method_prompt(
    *,
    symbol: str,
    horizon: str,
    snapshot: dict[str, Any],
    cross_horizon_context: dict[str, Any] | None = None,
    instrument_context: dict[str, Any] | None = None,
    allowed_data_gap_codes: list[str] | None = None,
) -> str:
    """Build the common Daily/Weekly method-selection prompt."""

    contract = {
        "regime_interpretation": "不含任何数字的市场状态解释",
        "selected_methods": ["只允许选择快照中 status=available 的 method_id"],
        "selected_level_ids": ["只允许选择快照中的 candidate_id，不得输出价格"],
        "evidence_for": ["支持证据，不含任何数字"],
        "counter_evidence": ["反对证据或尚未满足的确认条件，不含任何数字"],
        "cross_horizon_conclusion": "跨周期结论，不含任何数字",
        "invalidation_conditions": ["结论失效条件，不含任何数字"],
        "confidence": "low|medium|high",
        "data_gaps": ["只能引用允许的数据缺口代码"],
        "critic": {"verdict": "pass|revise|insufficient", "issues": ["审查问题"]},
    }
    return f"""你是只做研究解释的市场分析 Agent。先调用 load_skill，加载 market-analysis-method。

分析标的：{symbol}
分析周期：{horizon}

确定性方法快照：
{json.dumps(snapshot, ensure_ascii=False, default=str)}

跨周期结构化上下文：
{json.dumps(cross_horizon_context or {}, ensure_ascii=False, default=str)}

标的专用上下文：
{json.dumps(instrument_context or {}, ensure_ascii=False, default=str)}

约束：
- 只能选择快照已经计算出的 method_id 和 candidate_id。
- 不得计算、改写或输出任何数字、日期、价格、比例、点位或区间；数字由系统按 ID 渲染。
- 必须分别给出支持证据、反对证据和失效条件；不得只写单边结论。
- ETF 必须检查指数相对表现、跟踪、份额、折溢价和成分贡献范围；缺失时只降低对应范围。
- data_gaps 只能从以下冻结代码中选择，不得写入说明、安全声明或新缺口：{json.dumps(allowed_data_gap_codes or [], ensure_ascii=False)}
- 结论只供人工研究复核，不激活监控、不发送外部消息、不执行交易。
- 只输出一个 JSON 对象，不要 Markdown，不要解释。

字段契约：
{json.dumps(contract, ensure_ascii=False)}"""
