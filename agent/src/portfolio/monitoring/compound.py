"""Deterministic evaluator for schema-v5 compound monitoring conditions."""

from __future__ import annotations

import math
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


_CN_TZ = ZoneInfo("Asia/Shanghai")


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _compare(value: Any, operator: str, condition: dict[str, Any]) -> bool | None:
    number = _number(value)
    if operator in {"positive", "negative"}:
        if number is None:
            return None
        return number > 0 if operator == "positive" else number < 0
    if operator == "equals":
        expected = condition.get("value")
        if number is not None and _number(expected) is not None:
            return number == _number(expected)
        return str(value).lower() == str(expected).lower()
    if operator == "between":
        lower, upper = _number(condition.get("lower")), _number(condition.get("upper"))
        return None if number is None or lower is None or upper is None else lower <= number <= upper
    expected = _number(condition.get("value"))
    if number is None or expected is None:
        return None
    return {
        "gte": number >= expected,
        "lte": number <= expected,
        "gt": number > expected,
        "lt": number < expected,
    }.get(operator)


class CompoundConditionEvaluator:
    """Evaluate only whitelisted facts from verified closed bars/evidence."""

    @staticmethod
    def _closed_rows(
        rows: list[dict[str, Any]],
        *,
        interval: str,
        now_utc: datetime,
        accepted: set[str],
    ) -> list[dict[str, Any]]:
        minutes = {"1m": 1, "5m": 5}.get(interval)
        result: list[tuple[datetime, dict[str, Any]]] = []
        local_now = now_utc.astimezone(_CN_TZ)
        for row in rows:
            if str(row.get("status") or "") not in accepted:
                continue
            parsed = _parse_time(row.get("bar_time"))
            if parsed is None:
                continue
            if interval == "1D":
                session = str(row.get("session_date") or parsed.astimezone(_CN_TZ).date())
                if session == local_now.date().isoformat() and local_now.time() < time(15, 0):
                    continue
            elif minutes is not None and now_utc < parsed + timedelta(minutes=minutes):
                continue
            result.append((parsed, dict(row)))
        return [row for _at, row in sorted(result, key=lambda item: item[0])]

    @staticmethod
    def _synthesize_30m(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets: dict[tuple[str, int], list[tuple[datetime, dict[str, Any]]]] = {}
        for row in rows:
            parsed = _parse_time(row.get("bar_time"))
            if parsed is None:
                continue
            local = parsed.astimezone(_CN_TZ)
            minute = local.hour * 60 + local.minute
            if 570 <= minute < 690:
                segment_start = 570
            elif 780 <= minute < 900:
                segment_start = 780
            else:
                continue
            bucket = (minute - segment_start) // 30
            session = str(row.get("session_date") or local.date().isoformat())
            buckets.setdefault((session, segment_start + bucket * 30), []).append((parsed, row))
        synthesized: list[tuple[datetime, dict[str, Any]]] = []
        for (session, _bucket), values in buckets.items():
            ordered = sorted(values, key=lambda item: item[0])
            unique = {item[0].isoformat(): item for item in ordered}
            ordered = [unique[key] for key in sorted(unique)]
            if len(ordered) != 6:
                continue
            if any((right[0] - left[0]) != timedelta(minutes=5) for left, right in zip(ordered, ordered[1:])):
                continue
            bars = [item[1] for item in ordered]
            opens, closes = _number(bars[0].get("open")), _number(bars[-1].get("close"))
            highs = [_number(item.get("high")) for item in bars]
            lows = [_number(item.get("low")) for item in bars]
            if opens is None or closes is None or any(item is None for item in highs + lows):
                continue
            amount_values = [_number(item.get("amount")) for item in bars]
            volume_values = [_number(item.get("volume")) for item in bars]
            source_sets = [set(item.get("sources") or []) for item in bars]
            common_sources = sorted(set.intersection(*source_sets)) if source_sets else []
            if not common_sources:
                continue
            synthesized.append(
                (
                    ordered[0][0],
                    {
                        "bar_time": ordered[0][0].isoformat(),
                        "session_date": session,
                        "open": opens,
                        "high": max(float(item) for item in highs if item is not None),
                        "low": min(float(item) for item in lows if item is not None),
                        "close": closes,
                        "volume": sum(float(item) for item in volume_values if item is not None)
                        if all(item is not None for item in volume_values)
                        else None,
                        "amount": sum(float(item) for item in amount_values if item is not None)
                        if all(item is not None for item in amount_values)
                        else None,
                        "sources": common_sources,
                        "status": "verified",
                        "derived_from": "six_closed_5m_bars",
                    },
                )
            )
        return [row for _at, row in sorted(synthesized, key=lambda item: item[0])]

    def _bars(
        self,
        *,
        market_store: Any,
        symbol: str,
        now_utc: datetime,
        allow_single_source: bool,
    ) -> dict[str, list[dict[str, Any]]]:
        accepted = {"verified", "single_source"} if allow_single_source else {"verified"}
        values: dict[str, list[dict[str, Any]]] = {}
        for interval, limit in (("1m", 2000), ("5m", 3000), ("1D", 120)):
            try:
                rows = market_store.query_bars(
                    symbol=symbol,
                    interval=interval,
                    adjustment="raw",
                    view="consensus",
                    limit=limit,
                )
            except Exception:
                rows = []
            values[interval.lower()] = self._closed_rows(
                list(rows), interval=interval, now_utc=now_utc, accepted=accepted
            )
        values["30m"] = self._synthesize_30m(values["5m"])
        return values

    @staticmethod
    def _session_complete(now_utc: datetime) -> bool:
        return now_utc.astimezone(_CN_TZ).time() >= time(15, 0)

    def _condition(
        self,
        condition: dict[str, Any],
        *,
        bars: dict[str, list[dict[str, Any]]],
        auxiliary: dict[str, Any],
        now_utc: datetime,
    ) -> tuple[bool | None, dict[str, Any]]:
        kind = str(condition.get("kind") or "")
        interval = str(condition.get("interval") or "5m").lower()
        rows = bars.get(interval, [])
        consecutive = int(condition.get("consecutive") or 1)
        lookback = int(condition.get("lookback_bars") or 1)
        operator = str(condition.get("operator") or "")
        fact: dict[str, Any] = {
            "condition_id": condition.get("condition_id"),
            "kind": kind,
            "interval": interval,
            "operator": operator,
        }

        def evidence_is_fresh(value: Any) -> bool:
            parsed = _parse_time(value)
            if parsed is None:
                return False
            return (now_utc - parsed).total_seconds() <= int(
                condition.get("freshness_seconds") or 900
            )

        if kind == "rolling_volume_ratio" and interval == "1d":
            lookback = int(condition.get("lookback_bars") or 5)
            if len(rows) < lookback + 1:
                fact.update(
                    reason="closed_daily_volume_baseline_unavailable",
                    required_bars=lookback + 1,
                    available_bars=len(rows),
                )
                return None, fact
            latest = rows[-1]
            if not evidence_is_fresh(latest.get("bar_time")):
                fact.update(reason="stale_closed_bar", data_as_of=latest.get("bar_time"))
                return None, fact
            current = _number(latest.get("volume"))
            baseline_values = [
                _number(row.get("volume")) for row in rows[-(lookback + 1):-1]
            ]
            if (
                current is None
                or any(value is None for value in baseline_values)
                or not baseline_values
            ):
                fact["reason"] = "daily_volume_unavailable"
                return None, fact
            baseline = sum(float(value) for value in baseline_values if value is not None) / len(
                baseline_values
            )
            if baseline <= 0:
                fact["reason"] = "daily_volume_baseline_invalid"
                return None, fact
            ratio = current / baseline
            fact.update(
                value=ratio,
                current_volume=current,
                baseline_volume=baseline,
                baseline_sessions=[row.get("session_date") for row in rows[-(lookback + 1):-1]],
                session_date=latest.get("session_date"),
                data_as_of=latest.get("bar_time"),
                unit="ratio",
            )
            return _compare(ratio, operator, condition), fact

        if kind in {"cumulative_volume", "cumulative_turnover"} and interval == "1d":
            field = "volume" if kind == "cumulative_volume" else "amount"
            if len(rows) < consecutive:
                fact["reason"] = "closed_daily_bars_unavailable"
                return None, fact
            if not evidence_is_fresh(rows[-1].get("bar_time")):
                fact.update(reason="stale_closed_bar", data_as_of=rows[-1].get("bar_time"))
                return None, fact
            values = [_number(row.get(field)) for row in rows[-consecutive:]]
            fact.update(values=values, unit="CNY" if field == "amount" else condition.get("unit"))
            if any(value is None for value in values):
                return None, fact
            results = [_compare(value, operator, condition) for value in values]
            return all(value is True for value in results), fact

        if kind in {"volume_ratio", "cumulative_volume", "cumulative_turnover"}:
            source_key = {
                "volume_ratio": "price_volume",
                "cumulative_volume": "cumulative_volume",
                "cumulative_turnover": "cumulative_amount",
            }[kind]
            evidence = auxiliary.get(source_key) if isinstance(auxiliary.get(source_key), dict) else {}
            if evidence.get("status") != "ready":
                fact["reason"] = "evidence_unavailable"
                return None, fact
            evidence_time = evidence.get("bar_time") or evidence.get("data_as_of")
            if not evidence_is_fresh(evidence_time):
                fact.update(reason="stale_evidence", data_as_of=evidence_time)
                return None, fact
            metric = str(condition.get("metric") or "")
            default_metric = {
                "volume_ratio": "volume_ratio",
                "cumulative_volume": "cumulative_volume",
                "cumulative_turnover": "cumulative_amount",
            }[kind]
            value = evidence.get(metric or default_metric)
            fact.update(value=value, unit=condition.get("unit") or evidence.get("unit") or evidence.get("volume_unit"))
            return _compare(value, operator, condition), fact

        if kind in {"fund_flow", "sector_state"}:
            evidence = auxiliary.get(kind) if isinstance(auxiliary.get(kind), dict) else {}
            if evidence.get("status") not in {"ready", "verified"}:
                fact["reason"] = "evidence_unavailable"
                return None, fact
            if not evidence_is_fresh(evidence.get("data_as_of")):
                fact.update(reason="stale_evidence", data_as_of=evidence.get("data_as_of"))
                return None, fact
            metric = str(condition.get("metric") or "value")
            value = evidence.get(metric)
            fact.update(value=value, source=evidence.get("source"), data_as_of=evidence.get("data_as_of"))
            return _compare(value, operator, condition), fact

        if kind in {"session_range", "session_amplitude_bps"} and not self._session_complete(now_utc):
            fact["reason"] = "session_not_closed"
            return None, fact
        if len(rows) < max(1, consecutive):
            fact["reason"] = "closed_bars_unavailable"
            return None, fact
        latest_bar_time = rows[-1].get("bar_time")
        if not evidence_is_fresh(latest_bar_time):
            fact.update(reason="stale_closed_bar", data_as_of=latest_bar_time)
            return None, fact

        def bar_result(row: dict[str, Any]) -> bool | None:
            close, opened = _number(row.get("close")), _number(row.get("open"))
            if kind == "price_compare":
                return _compare(close, operator, condition)
            if kind == "price_zone":
                return _compare(close, "between", condition)
            if kind == "bar_direction":
                direction = str(condition.get("direction") or "")
                if close is None or opened is None:
                    return None
                value = close > opened if direction == "bullish" else close < opened
                return value if operator in {"equals", "positive"} else not value
            return None

        if kind in {"price_compare", "price_zone", "bar_direction"}:
            selected = rows[-consecutive:]
            results = [bar_result(row) for row in selected]
            fact["values"] = [row.get("close") for row in selected]
            fact["bar_times"] = [row.get("bar_time") for row in selected]
            if any(value is None for value in results):
                return None, fact
            return all(bool(value) for value in results), fact

        selected = rows[-lookback:]
        if kind == "price_reclaim":
            level = _number(condition.get("value"))
            if level is None:
                return None, fact
            direction = str(condition.get("direction") or "above")
            if direction == "above":
                crossed = any((_number(row.get("low")) or math.inf) < level for row in selected)
                result = crossed and (_number(selected[-1].get("close")) or -math.inf) >= level
            else:
                crossed = any((_number(row.get("high")) or -math.inf) > level for row in selected)
                result = crossed and (_number(selected[-1].get("close")) or math.inf) <= level
            fact.update(level=level, crossed=crossed, close=selected[-1].get("close"))
            return result, fact
        session = str(selected[-1].get("session_date") or "")
        session_rows = [row for row in rows if str(row.get("session_date") or "") == session]
        if kind == "session_range":
            if interval == "1d":
                selected_days = rows[-consecutive:]
                lower, upper = _number(condition.get("lower")), _number(condition.get("upper"))
                if len(selected_days) < consecutive or lower is None or upper is None:
                    return None, fact
                lows = [_number(row.get("low")) for row in selected_days]
                highs = [_number(row.get("high")) for row in selected_days]
                if any(value is None for value in lows + highs):
                    return None, fact
                fact.update(lows=lows, highs=highs, sessions=[row.get("session_date") for row in selected_days])
                return all(float(low) >= lower and float(high) <= upper for low, high in zip(lows, highs)), fact
            closes = [_number(row.get("close")) for row in session_rows]
            if not closes or any(value is None for value in closes):
                return None, fact
            lower, upper = _number(condition.get("lower")), _number(condition.get("upper"))
            if lower is None or upper is None:
                return None, fact
            fact.update(session=session, low_close=min(closes), high_close=max(closes))
            return min(closes) >= lower and max(closes) <= upper, fact
        if kind == "session_amplitude_bps":
            if interval == "1d":
                selected_days = rows[-consecutive:]
                if len(selected_days) < consecutive:
                    return None, fact
                amplitudes: list[float] = []
                for row in selected_days:
                    high, low, opened = _number(row.get("high")), _number(row.get("low")), _number(row.get("open"))
                    if high is None or low is None or opened in (None, 0):
                        return None, fact
                    amplitudes.append((high - low) / float(opened) * 10000)
                fact.update(values=amplitudes, unit="bps", sessions=[row.get("session_date") for row in selected_days])
                results = [_compare(value, operator, condition) for value in amplitudes]
                return all(value is True for value in results), fact
            highs = [_number(row.get("high")) for row in session_rows]
            lows = [_number(row.get("low")) for row in session_rows]
            opens = _number(session_rows[0].get("open")) if session_rows else None
            if not highs or not lows or opens in (None, 0) or any(value is None for value in highs + lows):
                return None, fact
            amplitude = (max(highs) - min(lows)) / float(opens) * 10000
            fact.update(value=amplitude, unit="bps", session=session)
            return _compare(amplitude, operator, condition), fact
        fact["reason"] = "unsupported_condition"
        return None, fact

    def _group(
        self,
        group: dict[str, Any],
        *,
        bars: dict[str, list[dict[str, Any]]],
        auxiliary: dict[str, Any],
        now_utc: datetime,
    ) -> tuple[bool | None, list[dict[str, Any]]]:
        conditions = group.get("conditions") or []
        if not conditions:
            return True, []
        results: list[bool | None] = []
        facts: list[dict[str, Any]] = []
        for condition in conditions:
            result, fact = self._condition(
                condition, bars=bars, auxiliary=auxiliary, now_utc=now_utc
            )
            results.append(result)
            fact["result"] = result
            facts.append(fact)
        if str(group.get("operator") or "all") == "any":
            outcome = True if any(value is True for value in results) else None if any(value is None for value in results) else False
        else:
            outcome = False if any(value is False for value in results) else None if any(value is None for value in results) else True
        return outcome, facts

    def evaluate(
        self,
        *,
        plan: dict[str, Any],
        symbol: str,
        market_store: Any,
        now_utc: datetime,
        auxiliary: dict[str, Any] | None = None,
        allow_single_source: bool = False,
    ) -> dict[str, dict[str, Any]]:
        bars = self._bars(
            market_store=market_store,
            symbol=symbol,
            now_utc=now_utc,
            allow_single_source=allow_single_source,
        )
        auxiliary = auxiliary or {}
        results: dict[str, dict[str, Any]] = {}
        for scenario in plan.get("watch_scenarios") or []:
            client_rule_id = str(scenario.get("client_rule_id") or "")
            source_pending = [
                item
                for item in scenario.get("source_conditions") or []
                if item.get("role") == "required" and item.get("coverage_status") != "mapped"
            ]
            entry, entry_facts = self._group(
                scenario.get("entry_conditions") or {}, bars=bars, auxiliary=auxiliary, now_utc=now_utc
            )
            confirmation, confirmation_facts = self._group(
                scenario.get("confirmation_conditions") or {}, bars=bars, auxiliary=auxiliary, now_utc=now_utc
            )
            invalidation, invalidation_facts = self._group(
                scenario.get("invalidation_conditions") or {}, bars=bars, auxiliary=auxiliary, now_utc=now_utc
            )
            pending = bool(source_pending) or entry is None or confirmation is None or invalidation is None
            results[client_rule_id] = {
                "scenario_id": scenario.get("scenario_id"),
                "scenario_fingerprint": scenario.get("scenario_fingerprint"),
                "automation_status": scenario.get("automation_status"),
                "entry_met": entry is True,
                "confirmation_met": confirmation is True,
                "invalidated": invalidation is True and bool((scenario.get("invalidation_conditions") or {}).get("conditions")),
                "evidence_pending": pending,
                "required_pending": [item.get("condition_id") for item in source_pending],
                "facts": {
                    "entry": entry_facts,
                    "confirmation": confirmation_facts,
                    "invalidation": invalidation_facts,
                },
                "evaluated_at": now_utc.isoformat(),
            }
        return results
