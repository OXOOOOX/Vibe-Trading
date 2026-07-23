"""Deterministic trading-week, previous-scenario, and delta calculations."""

from __future__ import annotations

import copy
import json
import math
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_CHANGE_FIELDS = (
    "original_level.value",
    "original_level.lower",
    "original_level.upper",
    "intent",
    "trigger.kind",
    "trigger.interval",
    "trigger.confirmation_count",
    "volume_confirmation.metric",
    "volume_confirmation.threshold",
    "invalidation.kind",
    "invalidation.level",
    "action_template.action",
    "action_template.sizing.kind",
    "action_template.sizing.value",
    "automation_status",
)


def _parse_day(value: Any) -> date:
    return date.fromisoformat(str(value)[:10])


def _bar_day(bar: dict[str, Any]) -> date:
    raw = str(bar.get("bar_time") or bar.get("date") or "").strip()
    if "T" not in raw:
        return _parse_day(raw)
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        return parsed.astimezone(_SHANGHAI).date()
    return parsed.date()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _path(value: dict[str, Any], dotted: str) -> Any:
    current: Any = value
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def week_trading_days(calendar: Any, week_anchor: date) -> list[date]:
    monday = week_anchor - timedelta(days=week_anchor.weekday())
    return [
        candidate
        for offset in range(7)
        if calendar.is_trading_day(candidate := monday + timedelta(days=offset))
    ]


def resolve_completed_trading_week(
    calendar: Any,
    *,
    requested_week_end: str | None = None,
    now: datetime | None = None,
) -> tuple[str, str, list[str]]:
    """Resolve the last completed exchange session in the requested ISO week."""

    local_now = (now or datetime.now(_SHANGHAI)).astimezone(_SHANGHAI)
    anchor = _parse_day(requested_week_end) if requested_week_end else local_now.date()
    sessions = week_trading_days(calendar, anchor)
    completed: list[date] = []
    for session in sessions:
        if session < local_now.date():
            completed.append(session)
        elif session == local_now.date() and local_now.time() >= time(15, 10):
            completed.append(session)
    if not completed:
        raise ValueError("requested week has no completed trading session")
    resolved_end = completed[-1]
    if requested_week_end and _parse_day(requested_week_end) != resolved_end:
        raise ValueError(
            f"week_end must be the last completed trading session: {resolved_end.isoformat()}"
        )
    active = [item for item in sessions if item <= resolved_end]
    return active[0].isoformat(), resolved_end.isoformat(), [item.isoformat() for item in active]


def next_week_review_due(calendar: Any, week_end: str) -> str:
    current = _parse_day(week_end)
    next_monday = current + timedelta(days=(7 - current.weekday()))
    sessions = week_trading_days(calendar, next_monday)
    if not sessions:
        # Fail closed when a future calendar is unavailable: the report remains
        # weekly rather than inheriting the monitor plan's 30-365 day window.
        due_day = current + timedelta(days=7)
    else:
        due_day = sessions[-1]
    return datetime.combine(due_day, time(15, 30), _SHANGHAI).isoformat()


def normalize_daily_bars(bars: list[dict[str, Any]], *, through: str) -> list[dict[str, Any]]:
    cutoff = _parse_day(through)
    normalized: dict[str, dict[str, Any]] = {}
    for raw in bars:
        if not isinstance(raw, dict):
            continue
        try:
            day = _bar_day(raw)
        except (TypeError, ValueError):
            continue
        if day > cutoff:
            continue
        open_price = _number(raw.get("open"), -1)
        high = _number(raw.get("high"), -1)
        low = _number(raw.get("low"), -1)
        close = _number(raw.get("close"), -1)
        if min(open_price, high, low, close) <= 0 or high < low:
            continue
        normalized[day.isoformat()] = {
            **copy.deepcopy(raw),
            "date": day.isoformat(),
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": max(0.0, _number(raw.get("volume"))),
            "amount": max(
                0.0,
                _number(raw.get("amount") or raw.get("turnover") or raw.get("turnover_amount")),
            ),
        }
    return [normalized[key] for key in sorted(normalized)]


def split_week_bars(
    bars: list[dict[str, Any]], *, week_start: str, week_end: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    start, end = _parse_day(week_start), _parse_day(week_end)
    current = [item for item in bars if start <= _bar_day(item) <= end]
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=previous_end.weekday())
    previous = [item for item in bars if previous_start <= _bar_day(item) <= previous_end]
    return current, previous


def weekly_market_statistics(
    bars: list[dict[str, Any]], *, week_start: str, week_end: str, tick_size: float
) -> dict[str, Any]:
    current, previous = split_week_bars(bars, week_start=week_start, week_end=week_end)
    if not current:
        raise ValueError("current trading week has no daily bars")
    trailing = bars[-120:]
    closes = [item["close"] for item in trailing]
    volumes = [item["volume"] for item in trailing]
    true_ranges: list[float] = []
    for index, item in enumerate(trailing):
        previous_close = trailing[index - 1]["close"] if index else item["open"]
        true_ranges.append(
            max(
                item["high"] - item["low"],
                abs(item["high"] - previous_close),
                abs(item["low"] - previous_close),
            )
        )
    atr14 = sum(true_ranges[-14:]) / max(1, len(true_ranges[-14:]))
    open_price, close = current[0]["open"], current[-1]["close"]
    high, low = max(item["high"] for item in current), min(item["low"] for item in current)
    volume = sum(item["volume"] for item in current)
    amount = sum(item["amount"] for item in current)
    previous_volume = sum(item["volume"] for item in previous)
    previous_amount = sum(item["amount"] for item in previous)
    lookback20 = trailing[-20:] if len(trailing) >= 20 else trailing
    support = min(item["low"] for item in lookback20)
    resistance = max(item["high"] for item in lookback20)
    zone_width = max(tick_size * 3, atr14 * 0.15)
    support_upper = min(support + zone_width, resistance - tick_size)
    resistance_lower = max(resistance - zone_width, support + tick_size)

    def average(values: list[float], count: int) -> float | None:
        subset = values[-count:]
        return sum(subset) / count if len(subset) == count else None

    return {
        "trading_day_count": len(current),
        "open": open_price,
        "close": close,
        "high": high,
        "low": low,
        "week_return_pct": round((close / open_price - 1) * 100, 4),
        "max_amplitude_pct": round((high / low - 1) * 100, 4),
        "volume": volume,
        "amount": amount,
        "previous_week_volume": previous_volume,
        "previous_week_amount": previous_amount,
        "volume_ratio_vs_previous_week": (
            round(volume / previous_volume, 6) if previous_volume > 0 else None
        ),
        "amount_ratio_vs_previous_week": (
            round(amount / previous_amount, 6) if previous_amount > 0 else None
        ),
        "volume_ma5": average(volumes, 5),
        "volume_ma10": average(volumes, 10),
        "volume_ma20": average(volumes, 20),
        "close_ma5": average(closes, 5),
        "close_ma10": average(closes, 10),
        "close_ma20": average(closes, 20),
        "atr14": round(atr14, 8),
        "support": round(support, 6),
        "support_upper": round(max(support + tick_size, support_upper), 6),
        "resistance_lower": round(min(resistance - tick_size, resistance_lower), 6),
        "resistance": round(resistance, 6),
        "range_position": (
            round((close - support) / (resistance - support), 6)
            if resistance > support
            else 0.5
        ),
        "current_week_bars": current,
        "previous_week_bars": previous,
    }


def deterministic_weekly_view(stats: dict[str, Any]) -> dict[str, Any]:
    close = float(stats["close"])
    ma5 = stats.get("close_ma5")
    ma20 = stats.get("close_ma20")
    return_pct = float(stats["week_return_pct"])
    atr_pct = float(stats.get("atr14") or 0) / close * 100 if close else 0
    if ma5 is None or ma20 is None:
        direction, stage = "待确认", "待确认"
    elif close > ma5 > ma20:
        direction, stage = "向上", "上升"
    elif close < ma5 < ma20:
        direction, stage = "向下", "下降"
    else:
        direction, stage = "横盘", "震荡"
    strength = "强" if abs(return_pct) >= max(atr_pct * 1.5, 3) else "中" if abs(return_pct) >= 1 else "弱"
    volume_ratio = stats.get("volume_ratio_vs_previous_week")
    volume_state = (
        "数据不足"
        if volume_ratio is None
        else "放量"
        if volume_ratio >= 1.2
        else "缩量"
        if volume_ratio <= 0.8
        else "正常"
    )
    amplitude = float(stats.get("max_amplitude_pct") or 0)
    volatility_state = "扩大" if amplitude >= max(atr_pct * 2, 6) else "收敛" if amplitude <= max(atr_pct, 2) else "正常"
    location = float(stats.get("range_position") or 0.5)
    location_context = "接近支撑" if location <= 0.25 else "接近阻力" if location >= 0.75 else "区间中部"
    summary = (
        f"本周收盘 {close:.3f}，周涨跌 {return_pct:+.2f}%；"
        f"趋势{direction}、强度{strength}，量能{volume_state}，价格{location_context}。"
    )
    return {
        "trend_stage": stage,
        "trend_direction": direction,
        "trend_strength": strength,
        "week_return_pct": return_pct,
        "relative_strength": "数据不足",
        "volume_state": volume_state,
        "volatility_state": volatility_state,
        "location_context": location_context,
        "summary": summary,
    }


def _trigger_hit(candidate: dict[str, Any], bar: dict[str, Any]) -> bool:
    trigger = candidate.get("trigger") or {}
    kind = str(trigger.get("kind") or "")
    if kind == "price_cross_above":
        return bar["high"] >= _number(trigger.get("threshold"), math.inf)
    if kind == "price_cross_below":
        return bar["low"] <= _number(trigger.get("threshold"), -math.inf)
    lower, upper = _number(trigger.get("lower")), _number(trigger.get("upper"))
    if kind == "price_zone_enter":
        return bar["low"] <= upper and bar["high"] >= lower
    if kind == "price_zone_exit":
        return bar["high"] > upper or bar["low"] < lower
    return False


def _approached(candidate: dict[str, Any], bar: dict[str, Any]) -> bool:
    trigger = candidate.get("trigger") or {}
    distance = _number((candidate.get("approach_policy") or {}).get("distance_bps"), 100) / 10_000
    threshold = _number(trigger.get("threshold"))
    if threshold > 0:
        return bar["high"] >= threshold * (1 - distance) and bar["low"] <= threshold * (1 + distance)
    lower, upper = _number(trigger.get("lower")), _number(trigger.get("upper"))
    return bar["high"] >= lower * (1 - distance) and bar["low"] <= upper * (1 + distance)


def _invalidation_hit(candidate: dict[str, Any], bar: dict[str, Any]) -> bool:
    invalidation = candidate.get("invalidation") or {}
    kind, level = str(invalidation.get("kind") or ""), _number(invalidation.get("level"))
    return bool(level) and (
        (kind == "price_cross_above" and bar["high"] >= level)
        or (kind == "price_cross_below" and bar["low"] <= level)
    )


def _research_verdict(
    source: dict[str, Any], *, bars: list[dict[str, Any]], all_bars: list[dict[str, Any]]
) -> tuple[str, str | None]:
    rule = source.get("research_condition")
    if not isinstance(rule, dict):
        return ("satisfied", None) if source.get("coverage_status") == "mapped" else ("insufficient", None)
    kind = str(rule.get("kind") or "")
    value = _number(rule.get("value") or rule.get("threshold"))
    consecutive = int(rule.get("consecutive") or 1)
    operator = str(rule.get("operator") or "gte")
    if kind in {"daily_close", "daily_close_above", "daily_close_below"}:
        hits: list[bool] = []
        for bar in bars:
            hit = bar["close"] >= value if operator in {"gte", "gt"} or kind.endswith("above") else bar["close"] <= value
            hits.append(hit)
            if len(hits) >= consecutive and all(hits[-consecutive:]):
                return "satisfied", bar["date"]
        return ("insufficient", None) if len(bars) < consecutive else ("not_satisfied", None)
    if kind == "daily_volume_ratio":
        lookback = int(rule.get("lookback") or 5)
        by_date = {item["date"]: index for index, item in enumerate(all_bars)}
        for bar in bars:
            index = by_date.get(bar["date"], -1)
            history = all_bars[max(0, index - lookback) : index]
            if len(history) < lookback:
                continue
            baseline = sum(item["volume"] for item in history) / lookback
            ratio = bar["volume"] / baseline if baseline > 0 else 0
            if (operator in {"gte", "gt"} and ratio >= value) or (operator in {"lte", "lt"} and ratio <= value):
                return "satisfied", bar["date"]
        return "insufficient" if len(all_bars) < lookback + 1 else "not_satisfied", None
    if kind == "weekly_volume_ratio":
        if not bars:
            return "insufficient", None
        week_start = _parse_day(bars[0]["date"])
        previous_end = week_start - timedelta(days=1)
        previous_start = previous_end - timedelta(days=previous_end.weekday())
        previous = [item for item in all_bars if previous_start <= _bar_day(item) <= previous_end]
        if not previous:
            return "insufficient", None
        ratio = sum(item["volume"] for item in bars) / max(1, sum(item["volume"] for item in previous))
        satisfied = ratio >= value if operator in {"gte", "gt"} else ratio <= value
        return ("satisfied", bars[-1]["date"]) if satisfied else ("not_satisfied", None)
    return "insufficient", None


def validate_previous_week_scenarios(
    previous_bundle: dict[str, Any] | None,
    *,
    current_week_bars: list[dict[str, Any]],
    all_bars: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not previous_bundle:
        return []
    results: list[dict[str, Any]] = []
    observed_high = max((item["high"] for item in current_week_bars), default=None)
    observed_low = min((item["low"] for item in current_week_bars), default=None)
    for candidate in previous_bundle.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        approaches = [item for item in current_week_bars if _approached(candidate, item)]
        triggers = [item for item in current_week_bars if _trigger_hit(candidate, item)]
        invalidations = [item for item in current_week_bars if _invalidation_hit(candidate, item)]
        required = [
            item
            for item in candidate.get("source_conditions") or []
            if isinstance(item, dict) and item.get("role") == "required"
        ]
        condition_verdicts = [
            (item, _research_verdict(item, bars=current_week_bars, all_bars=all_bars))
            for item in required
        ]
        verdicts = [item[1] for item in condition_verdicts]
        trigger_day = triggers[0]["date"] if triggers else None
        invalidation_day = invalidations[0]["date"] if invalidations else None
        if not current_week_bars:
            outcome = "insufficient_data"
        elif trigger_day and invalidation_day and trigger_day == invalidation_day:
            outcome = "unresolved"
        elif invalidation_day and (not trigger_day or invalidation_day <= trigger_day):
            outcome = "invalidated"
        elif trigger_day and all(item[0] == "satisfied" for item in verdicts):
            outcome = "confirmed"
        elif trigger_day or approaches:
            outcome = "approached"
        elif any(item[0] == "insufficient" for item in verdicts):
            outcome = "insufficient_data"
        else:
            outcome = "not_triggered"
        volume_verdicts = [
            verdict[0]
            for condition, verdict in condition_verdicts
            if "volume" in json.dumps(condition, ensure_ascii=False).lower()
        ]
        results.append(
            {
                "scenario_family_id": candidate.get("scenario_family_id"),
                "previous_candidate_id": candidate.get("candidate_id") or candidate.get("scenario_id"),
                "outcome": outcome,
                "first_approach_at": approaches[0]["date"] if approaches else None,
                "first_trigger_at": trigger_day,
                "invalidation_at": invalidation_day,
                "observed_high": observed_high,
                "observed_low": observed_low,
                "volume_verdict": volume_verdicts[0] if volume_verdicts else "not_required",
                "evidence_refs": [item["date"] for item in current_week_bars],
                "summary": {
                    "confirmed": "本周真实日线满足触发及全部必需研究条件。",
                    "invalidated": "本周真实日线先触及失效条件。",
                    "approached": "本周接近或触发点位，但必需确认条件未全部满足。",
                    "not_triggered": "本周未触及该场景的触发或接近范围。",
                    "unresolved": "同一日线同时覆盖触发与失效范围，日线数据无法确定先后。",
                    "insufficient_data": "现有日线不足以判断该场景。",
                    "expired": "场景在观察前已到期。",
                }[outcome],
            }
        )
    return results


def compare_weekly_scenarios(
    current_candidates: list[dict[str, Any]],
    previous_bundle: dict[str, Any] | None,
    *,
    current_review_due_at: str,
) -> list[dict[str, Any]]:
    previous = {
        str(item.get("scenario_family_id")): item
        for item in (previous_bundle or {}).get("candidates") or []
        if isinstance(item, dict) and item.get("scenario_family_id")
    }
    changes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in current_candidates:
        family = str(candidate.get("scenario_family_id") or "")
        before = previous.get(family)
        seen.add(family)
        field_changes = [
            {"field": field, "before": _path(before or {}, field), "after": _path(candidate, field)}
            for field in _CHANGE_FIELDS
            if _path(before or {}, field) != _path(candidate, field)
        ]
        if before is None:
            change_type = "new"
        elif not field_changes:
            change_type = "unchanged"
        else:
            numeric = [item for item in field_changes if item["field"] in {"original_level.value", "original_level.lower", "original_level.upper"}]
            non_numeric = [item for item in field_changes if item not in numeric]
            if numeric and not non_numeric and all(_number(item["after"]) > _number(item["before"]) for item in numeric):
                change_type = "raised"
            elif numeric and not non_numeric and all(_number(item["after"]) < _number(item["before"]) for item in numeric):
                change_type = "lowered"
            else:
                change_type = "modified"
        previous_id = (before or {}).get("candidate_id") or (before or {}).get("scenario_id")
        candidate["change_type"] = change_type
        candidate["previous_candidate_id"] = previous_id
        candidate["change_details"] = {
            "previous_level": _path(before or {}, "original_level.value")
            or _path(before or {}, "original_level.lower"),
            "current_level": _path(candidate, "original_level.value")
            or _path(candidate, "original_level.lower"),
            "summary": "本周场景首次建立。" if before is None else "本周场景字段未变化。" if not field_changes else "本周场景按结构化字段更新。",
        }
        candidate["change_details"] = {
            key: value for key, value in candidate["change_details"].items() if value is not None
        }
        changes.append(
            {
                "scenario_family_id": family,
                "candidate_id": candidate.get("candidate_id"),
                "previous_candidate_id": previous_id,
                "change_type": change_type,
                "change_details": copy.deepcopy(candidate["change_details"]),
                "field_changes": field_changes,
                "reason_claim_ids": list(candidate.get("claim_ids") or [])[:2],
            }
        )
    previous_due = str((previous_bundle or {}).get("review_due_at") or "")
    expired = bool(previous_due and previous_due <= current_review_due_at)
    for family, candidate in previous.items():
        if family in seen:
            continue
        changes.append(
            {
                "scenario_family_id": family,
                "candidate_id": None,
                "previous_candidate_id": candidate.get("candidate_id") or candidate.get("scenario_id"),
                "change_type": "expired" if expired else "withdrawn",
                "change_details": {"summary": "上一周场景到期且本周未续用。" if expired else "本周撤销上一周场景。"},
                "field_changes": [],
                "reason_claim_ids": [],
            }
        )
    return changes
