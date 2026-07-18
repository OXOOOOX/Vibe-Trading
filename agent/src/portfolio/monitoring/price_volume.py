"""Deterministic price-volume analysis for portfolio monitoring.

The analyzer only consumes closed cache bars.  It deliberately keeps the
decision surface small and auditable: the returned payload is made entirely
from bar facts and stable thresholds in the active monitor plan.
"""

from __future__ import annotations

import math
import os
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


_CN_TZ = ZoneInfo("Asia/Shanghai")
_ACCEPTED_STATUSES = {"verified", "single_source"}


def maximum_bar_lag_seconds(interval: str) -> int:
    """Use the same freshness budget for quotes and volume evidence."""

    interval_minutes = {"1m": 1, "5m": 5}.get(interval, 5)
    default_max_lag = max(180, interval_minutes * 180)
    try:
        return max(
            60,
            int(
                os.getenv(
                    "VIBE_TRADING_MONITOR_MAX_BAR_LAG_SECONDS",
                    str(default_max_lag),
                )
            ),
        )
    except ValueError:
        return default_max_lag


def disabled_price_volume(reason: str = "price_volume_mode_off") -> dict[str, Any]:
    """Return the stable wire shape used when analysis is disabled."""

    return {
        "status": "disabled",
        "regime": None,
        "volume_state": None,
        "volume_ratio": None,
        "baseline_samples": 0,
        "three_bar_return_bps": None,
        "latest_return_bps": None,
        "close_location": None,
        "accelerated_decline": False,
        "reason_codes": [reason],
    }


def _insufficient(
    reasons: list[str],
    *,
    volume_ratio: float | None = None,
    baseline_samples: int = 0,
    three_bar_return_bps: float | None = None,
    latest_return_bps: float | None = None,
    close_location: float | None = None,
) -> dict[str, Any]:
    return {
        "status": "insufficient_data",
        "regime": None,
        "volume_state": None,
        "volume_ratio": volume_ratio,
        "baseline_samples": int(baseline_samples),
        "three_bar_return_bps": three_bar_return_bps,
        "latest_return_bps": latest_return_bps,
        "close_location": close_location,
        "accelerated_decline": False,
        "reason_codes": list(dict.fromkeys(reasons)),
    }


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(value: Any, *, positive: bool = False) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or (positive and number <= 0):
        return None
    return number


def _source_signature(bar: dict[str, Any]) -> tuple[str, ...]:
    return tuple(sorted({str(value).strip() for value in (bar.get("sources") or []) if str(value).strip()}))


def _volume_unit(bar: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (unit, error); consensus bars derive units from included rows."""

    explicit = str(bar.get("volume_unit") or "").strip().lower()
    observations = bar.get("observations") or []
    included = [
        item
        for item in observations
        if isinstance(item, dict) and item.get("included_in_consensus", True)
    ]
    raw_units = [str(item.get("volume_unit") or "").strip().lower() for item in included]
    if explicit == "unknown" or any(unit in {"", "unknown"} for unit in raw_units):
        return None, "volume_unit_unknown"
    units = set(raw_units)
    if explicit:
        units.add(explicit)
    if not units:
        return None, "volume_unit_unknown"
    if len(units) != 1:
        return None, "volume_unit_conflict"
    return next(iter(units)), None


def _canonical_volume_unit(value: str | None) -> str | None:
    unit = str(value or "").strip().lower()
    if unit in {"share", "shares", "股"}:
        return "shares"
    if unit in {"lot", "lots", "手"}:
        return "lots"
    if unit in {"cny", "rmb", "yuan", "元"}:
        return "CNY"
    return None


def _bar_quality(
    bar: dict[str, Any],
    *,
    accepted_statuses: set[str],
) -> tuple[bool, str | None, tuple[str, ...], str | None]:
    status = str(bar.get("status") or "")
    if status not in accepted_statuses:
        return False, "bar_status_not_actionable", (), None
    flags = {str(value) for value in (bar.get("quality_flags") or [])}
    if "volume_conflict" in flags:
        return False, "volume_conflict", (), None
    if _number(bar.get("volume"), positive=True) is None:
        return False, "volume_missing", (), None
    if _number(bar.get("close"), positive=True) is None:
        return False, "close_missing", (), None
    signature = _source_signature(bar)
    if not signature:
        return False, "source_signature_missing", (), None
    unit, unit_error = _volume_unit(bar)
    if unit_error:
        return False, unit_error, (), None
    return True, None, signature, unit


def target_distance_bps(rule: dict[str, Any], price: Any) -> float | None:
    """Distance to a point/zone target; zones use their nearest boundary."""

    current = _number(price, positive=True)
    if current is None:
        return None
    kind = str(rule.get("kind") or "")
    params = rule.get("parameters") or {}
    if kind in {"price_cross_above", "price_cross_below"}:
        target = _number(params.get("threshold"), positive=True)
        return None if target is None else round(abs(current - target) / target * 10000, 3)
    if kind in {"price_zone_enter", "price_zone_exit"}:
        lower = _number(params.get("lower"), positive=True)
        upper = _number(params.get("upper"), positive=True)
        if lower is None or upper is None:
            return None
        if lower <= current <= upper:
            return 0.0
        target = lower if current < lower else upper
        return round(abs(current - target) / target * 10000, 3)
    return None


def target_reached(rule: dict[str, Any], price: Any) -> bool:
    current = _number(price, positive=True)
    if current is None:
        return False
    kind = str(rule.get("kind") or "")
    params = rule.get("parameters") or {}
    if kind == "price_cross_above":
        target = _number(params.get("threshold"), positive=True)
        return target is not None and current >= target
    if kind == "price_cross_below":
        target = _number(params.get("threshold"), positive=True)
        return target is not None and current <= target
    if kind in {"price_zone_enter", "price_zone_exit"}:
        lower = _number(params.get("lower"), positive=True)
        upper = _number(params.get("upper"), positive=True)
        if lower is None or upper is None:
            return False
        inside = lower <= current <= upper
        return inside if kind == "price_zone_enter" else not inside
    return False


class PriceVolumeAnalyzer:
    """Analyze closed bars with a same-local-time historical volume baseline."""

    def __init__(self) -> None:
        self._baseline_cache: dict[
            tuple[str, str, str, str, tuple[str, ...], str, int],
            tuple[float, int, tuple[str, ...]],
        ] = {}

    @staticmethod
    def _closed_bars(
        rows: list[dict[str, Any]],
        *,
        now_utc: datetime,
        interval: str,
    ) -> list[tuple[datetime, dict[str, Any]]]:
        minutes = {"1m": 1, "5m": 5}.get(interval)
        if minutes is None:
            return []
        result: list[tuple[datetime, dict[str, Any]]] = []
        for row in rows:
            parsed = _parse_time(row.get("bar_time"))
            if parsed is not None and now_utc >= parsed + timedelta(minutes=minutes):
                result.append((parsed, row))
        return sorted(result, key=lambda item: item[0])

    def analyze_cumulative(
        self,
        *,
        market_store: Any,
        symbol: str,
        now_utc: datetime,
        policy: dict[str, Any],
        allow_single_source: bool = False,
        interval: str = "5m",
    ) -> dict[str, Any]:
        """Compare today's closed cumulative volume with the same clock historically."""

        baseline_sessions = int(policy.get("baseline_sessions", 10))
        min_samples = int(policy.get("min_samples", 5))
        try:
            rows = market_store.query_bars(
                symbol=symbol,
                interval=interval,
                adjustment="raw",
                view="consensus",
                limit=max(2000, baseline_sessions * 100),
            )
        except Exception:
            return {
                "status": "insufficient_data",
                "cumulative_volume": None,
                "cumulative_volume_ratio": None,
                "baseline_samples": 0,
                "volume_unit": None,
                "reason_codes": ["bar_cache_query_failed"],
            }
        closed = self._closed_bars(rows, now_utc=now_utc, interval=interval)
        accepted = set(_ACCEPTED_STATUSES if allow_single_source else {"verified"})
        current_item: tuple[datetime, dict[str, Any], tuple[str, ...], str] | None = None
        for bar_time, bar in reversed(closed):
            good, _reason, signature, raw_unit = _bar_quality(
                bar,
                accepted_statuses=accepted,
            )
            canonical_unit = _canonical_volume_unit(raw_unit)
            if good and signature and canonical_unit:
                current_item = (bar_time, bar, signature, canonical_unit)
                break
        if current_item is None:
            return {
                "status": "insufficient_data",
                "cumulative_volume": None,
                "cumulative_volume_ratio": None,
                "baseline_samples": 0,
                "volume_unit": None,
                "reason_codes": ["no_actionable_closed_bar"],
            }
        current_time, current_bar, signature, canonical_unit = current_item
        if (now_utc - current_time).total_seconds() > maximum_bar_lag_seconds(interval):
            return {
                "status": "insufficient_data",
                "cumulative_volume": None,
                "cumulative_volume_ratio": None,
                "baseline_samples": 0,
                "volume_unit": canonical_unit,
                "bar_time": current_bar.get("bar_time"),
                "reason_codes": ["stale_cumulative_volume_bar"],
            }

        current_local = current_time.astimezone(_CN_TZ)
        current_session = str(current_bar.get("session_date") or current_local.date().isoformat())
        by_session: dict[str, dict[str, float]] = {}
        invalid_sessions: set[str] = set()
        for bar_time, bar in closed:
            if bar_time > current_time:
                continue
            local = bar_time.astimezone(_CN_TZ)
            session = str(bar.get("session_date") or local.date().isoformat())
            if session > current_session or local.time() > current_local.time():
                continue
            good, _reason, bar_signature, raw_unit = _bar_quality(
                bar,
                accepted_statuses=accepted,
            )
            if (
                not good
                or bar_signature != signature
                or _canonical_volume_unit(raw_unit) != canonical_unit
            ):
                invalid_sessions.add(session)
                continue
            volume = _number(bar.get("volume"), positive=True)
            if volume is None:
                invalid_sessions.add(session)
                continue
            # Repeated cache rows cannot inflate the cumulative fact.
            bucket_key = bar_time.isoformat()
            by_session.setdefault(session, {})[bucket_key] = volume

        current_buckets = by_session.get(current_session, {})
        if current_session in invalid_sessions or not current_buckets:
            return {
                "status": "insufficient_data",
                "cumulative_volume": None,
                "cumulative_volume_ratio": None,
                "baseline_samples": 0,
                "volume_unit": canonical_unit,
                "bar_time": current_bar.get("bar_time"),
                "reason_codes": ["current_cumulative_volume_incomplete"],
            }
        bucket_count = len(current_buckets)
        current_total = float(sum(current_buckets.values()))
        historical: list[float] = []
        for session in sorted(by_session, reverse=True):
            if session >= current_session or session in invalid_sessions:
                continue
            buckets = by_session[session]
            if len(buckets) != bucket_count:
                continue
            historical.append(float(sum(buckets.values())))
            if len(historical) >= baseline_sessions:
                break
        median_volume = statistics.median(historical) if historical else 0.0
        ratio = current_total / median_volume if median_volume > 0 else None
        ready = len(historical) >= min_samples and ratio is not None
        return {
            "status": "ready" if ready else "insufficient_data",
            "cumulative_volume": current_total,
            "cumulative_volume_ratio": ratio,
            "baseline_samples": len(historical),
            "volume_unit": canonical_unit,
            "source_signature": list(signature),
            "bar_time": current_bar.get("bar_time"),
            "reason_codes": [] if ready else ["insufficient_same_clock_cumulative_baseline"],
        }

    def analyze_cumulative_amount(
        self,
        *,
        market_store: Any,
        symbol: str,
        now_utc: datetime,
        policy: dict[str, Any],
        allow_single_source: bool = False,
        interval: str = "5m",
    ) -> dict[str, Any]:
        """Compare closed-bar turnover using ``amount`` only, never volume.

        The cache contract defines amount as CNY turnover.  A session with a
        missing/invalid amount or changing source signature is excluded rather
        than silently estimating turnover from volume and price.
        """

        baseline_sessions = int(policy.get("baseline_sessions", 10))
        min_samples = int(policy.get("min_samples", 5))
        try:
            rows = market_store.query_bars(
                symbol=symbol,
                interval=interval,
                adjustment="raw",
                view="consensus",
                limit=max(2000, baseline_sessions * 100),
            )
        except Exception:
            return {
                "status": "insufficient_data",
                "cumulative_amount": None,
                "cumulative_amount_ratio": None,
                "baseline_samples": 0,
                "unit": "CNY",
                "reason_codes": ["bar_cache_query_failed"],
            }
        closed = self._closed_bars(rows, now_utc=now_utc, interval=interval)
        accepted = set(_ACCEPTED_STATUSES if allow_single_source else {"verified"})
        current_item: tuple[datetime, dict[str, Any], tuple[str, ...]] | None = None
        for bar_time, bar in reversed(closed):
            signature = _source_signature(bar)
            if (
                str(bar.get("status") or "") in accepted
                and signature
                and _number(bar.get("amount"), positive=True) is not None
            ):
                current_item = (bar_time, bar, signature)
                break
        if current_item is None:
            return {
                "status": "insufficient_data",
                "cumulative_amount": None,
                "cumulative_amount_ratio": None,
                "baseline_samples": 0,
                "unit": "CNY",
                "reason_codes": ["no_actionable_closed_amount_bar"],
            }
        current_time, current_bar, signature = current_item
        if (now_utc - current_time).total_seconds() > maximum_bar_lag_seconds(interval):
            return {
                "status": "insufficient_data",
                "cumulative_amount": None,
                "cumulative_amount_ratio": None,
                "baseline_samples": 0,
                "unit": "CNY",
                "bar_time": current_bar.get("bar_time"),
                "reason_codes": ["stale_cumulative_amount_bar"],
            }
        current_local = current_time.astimezone(_CN_TZ)
        current_session = str(current_bar.get("session_date") or current_local.date().isoformat())
        by_session: dict[str, dict[str, float]] = {}
        invalid_sessions: set[str] = set()
        for bar_time, bar in closed:
            if bar_time > current_time:
                continue
            local = bar_time.astimezone(_CN_TZ)
            session = str(bar.get("session_date") or local.date().isoformat())
            if session > current_session or local.time() > current_local.time():
                continue
            amount = _number(bar.get("amount"), positive=True)
            if (
                str(bar.get("status") or "") not in accepted
                or _source_signature(bar) != signature
                or amount is None
            ):
                invalid_sessions.add(session)
                continue
            by_session.setdefault(session, {})[bar_time.isoformat()] = amount
        current_buckets = by_session.get(current_session, {})
        if current_session in invalid_sessions or not current_buckets:
            return {
                "status": "insufficient_data",
                "cumulative_amount": None,
                "cumulative_amount_ratio": None,
                "baseline_samples": 0,
                "unit": "CNY",
                "bar_time": current_bar.get("bar_time"),
                "reason_codes": ["current_cumulative_amount_incomplete"],
            }
        bucket_count = len(current_buckets)
        current_total = float(sum(current_buckets.values()))
        historical: list[float] = []
        for session in sorted(by_session, reverse=True):
            if session >= current_session or session in invalid_sessions:
                continue
            buckets = by_session[session]
            if len(buckets) != bucket_count:
                continue
            historical.append(float(sum(buckets.values())))
            if len(historical) >= baseline_sessions:
                break
        median_amount = statistics.median(historical) if historical else 0.0
        ratio = current_total / median_amount if median_amount > 0 else None
        ready = len(historical) >= min_samples and ratio is not None
        return {
            "status": "ready" if ready else "insufficient_data",
            "cumulative_amount": current_total,
            "cumulative_amount_ratio": ratio,
            "baseline_samples": len(historical),
            "unit": "CNY",
            "source_signature": list(signature),
            "bar_time": current_bar.get("bar_time"),
            "reason_codes": [] if ready else ["insufficient_same_clock_cumulative_amount_baseline"],
        }

    def analyze(
        self,
        *,
        market_store: Any,
        symbol: str,
        now_utc: datetime,
        policy: dict[str, Any],
        allow_single_source: bool = False,
        interval: str | None = None,
        require_pattern: bool = True,
    ) -> tuple[dict[str, Any], str | None]:
        """Return (wire payload, evidence bar time) for the latest closed bar."""

        selected_interval = str(interval or policy.get("interval") or "5m")
        baseline_sessions = int(policy.get("baseline_sessions", 10))
        min_samples = int(policy.get("min_samples", 5))
        try:
            rows = market_store.query_bars(
                symbol=symbol,
                interval=selected_interval,
                adjustment="raw",
                view="consensus",
                # Repeated checks within the same closed-bar bucket only need
                # recent evidence. A cache miss below performs the bounded
                # historical read used to establish the seasonal baseline.
                limit=64,
            )
        except Exception:
            return _insufficient(["bar_cache_query_failed"]), None
        closed = self._closed_bars(rows, now_utc=now_utc, interval=selected_interval)
        if not closed:
            return _insufficient([f"no_closed_{selected_interval}_bar"]), None

        accepted = set(_ACCEPTED_STATUSES if allow_single_source else {"verified"})
        latest_closed_time, latest_closed_bar = closed[-1]
        latest_session_date = str(
            latest_closed_bar.get("session_date")
            or latest_closed_time.astimezone(_CN_TZ).date().isoformat()
        )
        current_item: tuple[datetime, dict[str, Any]] | None = None
        current_signature: tuple[str, ...] = ()
        current_unit: str | None = None
        current_errors: list[str] = []
        for candidate_time, candidate in reversed(closed):
            candidate_session = str(
                candidate.get("session_date")
                or candidate_time.astimezone(_CN_TZ).date().isoformat()
            )
            if candidate_session != latest_session_date:
                break
            good, candidate_error, signature, unit = _bar_quality(
                candidate,
                accepted_statuses=accepted,
            )
            if good and unit is not None:
                current_item = (candidate_time, candidate)
                current_signature = signature
                current_unit = unit
                break
            if candidate_error:
                current_errors.append(candidate_error)
        if current_item is None or current_unit is None:
            return _insufficient(
                [*current_errors, "no_actionable_closed_bar"]
            ), None
        current_time, current = current_item
        signature = current_signature
        unit = current_unit
        if (now_utc - current_time).total_seconds() > maximum_bar_lag_seconds(
            selected_interval
        ):
            return _insufficient(["stale_price_volume_bar"]), current.get("bar_time")

        local = current_time.astimezone(_CN_TZ)
        session_date = str(current.get("session_date") or local.date().isoformat())
        bucket = local.strftime("%H:%M")
        cache_key = (
            symbol.upper(),
            selected_interval,
            session_date,
            bucket,
            signature,
            unit,
            baseline_sessions,
        )
        baseline = self._baseline_cache.get(cache_key)
        if baseline is None:
            try:
                same_bucket_query = getattr(
                    market_store,
                    "query_same_time_bucket_bars",
                    None,
                )
                if callable(same_bucket_query):
                    history_rows = same_bucket_query(
                        symbol=symbol,
                        interval=selected_interval,
                        adjustment="raw",
                        view="consensus",
                        local_time_bucket=bucket,
                        before=current_time.isoformat(),
                        limit=2000,
                    )
                else:
                    history_rows = market_store.query_bars(
                        symbol=symbol,
                        interval=selected_interval,
                        adjustment="raw",
                        view="consensus",
                        limit=2000,
                    )
            except Exception:
                return _insufficient(["bar_cache_query_failed"]), current.get("bar_time")
            history_closed = self._closed_bars(
                history_rows,
                now_utc=now_utc,
                interval=selected_interval,
            )
            by_session: dict[str, float] = {}
            baseline_issues: list[str] = []
            for bar_time, bar in reversed(
                [item for item in history_closed if item[0] < current_time]
            ):
                bar_local = bar_time.astimezone(_CN_TZ)
                bar_session = str(bar.get("session_date") or bar_local.date().isoformat())
                if bar_session >= session_date or bar_local.strftime("%H:%M") != bucket:
                    continue
                good, reason, bar_signature, bar_unit = _bar_quality(
                    bar,
                    accepted_statuses=accepted,
                )
                volume = _number(bar.get("volume"), positive=True)
                if not good:
                    if reason:
                        baseline_issues.append(reason)
                    continue
                if bar_signature != signature:
                    baseline_issues.append("source_signature_mismatch")
                    continue
                if bar_unit != unit:
                    baseline_issues.append("volume_unit_conflict")
                    continue
                if (
                    volume is None
                    or bar_session in by_session
                ):
                    continue
                by_session[bar_session] = volume
                if len(by_session) >= baseline_sessions:
                    break
            values = list(by_session.values())
            baseline = (
                statistics.median(values) if values else 0.0,
                len(values),
                tuple(dict.fromkeys(baseline_issues)),
            )
            self._baseline_cache[cache_key] = baseline
        median_volume, sample_count, baseline_issues = baseline
        current_volume = _number(current.get("volume"), positive=True)
        volume_ratio = (
            round(current_volume / median_volume, 4)
            if current_volume is not None and median_volume > 0
            else None
        )
        if sample_count < min_samples or volume_ratio is None:
            return (
                _insufficient(
                    [*baseline_issues, "insufficient_same_time_baseline"],
                    volume_ratio=volume_ratio,
                    baseline_samples=sample_count,
                ),
                current.get("bar_time"),
            )

        contraction = float(policy.get("contraction_ratio", 0.8))
        expansion = float(policy.get("expansion_ratio", 1.5))
        if volume_ratio < contraction:
            volume_state = "contracted"
        elif volume_ratio >= expansion:
            volume_state = "expanded"
        else:
            volume_state = "normal"
        if not require_pattern:
            return (
                {
                    "status": "ready",
                    "regime": None,
                    "volume_state": volume_state,
                    "volume_ratio": volume_ratio,
                    "baseline_samples": sample_count,
                    "three_bar_return_bps": None,
                    "latest_return_bps": None,
                    "close_location": None,
                    "accelerated_decline": False,
                    "reason_codes": ["volume_ratio_only", f"volume_{volume_state}"],
                },
                current.get("bar_time"),
            )

        current_session: list[tuple[datetime, dict[str, Any]]] = []
        for bar_time, bar in closed:
            if bar_time > current_time:
                continue
            if str(bar.get("session_date") or "") != session_date:
                continue
            good, _reason, bar_signature, bar_unit = _bar_quality(
                bar,
                accepted_statuses=accepted,
            )
            if good and bar_signature == signature and bar_unit == unit:
                current_session.append((bar_time, bar))
        if len(current_session) < 4:
            return (
                _insufficient(
                    ["insufficient_recent_bars"],
                    volume_ratio=volume_ratio,
                    baseline_samples=sample_count,
                ),
                current.get("bar_time"),
            )

        recent_items = current_session[-4:]
        interval_minutes = {"1m": 1, "5m": 5}.get(selected_interval, 5)
        if any(
            not 0 < (right[0] - left[0]).total_seconds() <= interval_minutes * 60 * 1.5
            for left, right in zip(recent_items, recent_items[1:])
        ):
            return (
                _insufficient(
                    ["insufficient_recent_bars"],
                    volume_ratio=volume_ratio,
                    baseline_samples=sample_count,
                ),
                current.get("bar_time"),
            )
        recent = [item[1] for item in recent_items]
        closes = [_number(bar.get("close"), positive=True) for bar in recent]
        volumes = [_number(bar.get("volume"), positive=True) for bar in recent]
        if any(value is None for value in closes) or any(value is None for value in volumes):
            return (
                _insufficient(
                    ["recent_bar_values_invalid"],
                    volume_ratio=volume_ratio,
                    baseline_samples=sample_count,
                ),
                current.get("bar_time"),
            )
        numeric_closes = [float(value) for value in closes if value is not None]
        numeric_volumes = [float(value) for value in volumes if value is not None]
        returns = [
            (numeric_closes[index] / numeric_closes[index - 1] - 1) * 10000
            for index in range(1, len(numeric_closes))
        ]
        aggregate_three_bar_return = round(
            (numeric_closes[-1] / numeric_closes[0] - 1) * 10000,
            3,
        )
        latest_return = round(returns[-1], 3)
        low = _number(current.get("low"), positive=True)
        high = _number(current.get("high"), positive=True)
        current_close = numeric_closes[-1]
        close_location = None
        if low is not None and high is not None:
            close_location = 0.5 if high == low else max(0.0, min(1.0, (current_close - low) / (high - low)))
            close_location = round(close_location, 4)

        flat_bps = float(policy.get("flat_return_bps", 10))
        acceleration = float(policy.get("acceleration_multiplier", 1.2))

        if aggregate_three_bar_return > flat_bps:
            regime = (
                "bullish_expansion"
                if volume_state == "expanded"
                else "bullish_contraction"
                if volume_state == "contracted"
                else "neutral"
            )
        elif aggregate_three_bar_return < -flat_bps:
            regime = (
                "bearish_expansion"
                if volume_state == "expanded"
                else "bearish_contraction"
                if volume_state == "contracted"
                else "neutral"
            )
        else:
            regime = "high_volume_stall" if volume_state == "expanded" else "neutral"

        previous_drop = numeric_closes[-2] / numeric_closes[-3] - 1
        latest_drop = numeric_closes[-1] / numeric_closes[-2] - 1
        accelerated_decline = bool(
            numeric_closes[0] > numeric_closes[1] > numeric_closes[2] > numeric_closes[3]
            and previous_drop < 0
            and latest_drop < 0
            and abs(latest_drop) >= abs(previous_drop) * acceleration
            and volume_ratio >= expansion
            and close_location is not None
            and close_location <= 0.35
        )

        reasons = [regime, f"volume_{volume_state}"]
        if accelerated_decline:
            reasons.append("accelerated_decline")
        previous_bar = recent[-2]
        previous_low = _number(previous_bar.get("low"), positive=True)
        previous_high = _number(previous_bar.get("high"), positive=True)
        if latest_return >= -flat_bps:
            reasons.append("price_stabilized")
        if (
            previous_low is not None
            and low is not None
            and low >= previous_low
            and returns[-2] >= -flat_bps
            and numeric_closes[-1] > numeric_closes[-2]
            and volume_ratio < contraction
        ):
            reasons.append("add_shrinking_reversal")
        if (
            previous_high is not None
            and numeric_closes[-1] > previous_high
            and volume_ratio >= expansion
            and close_location is not None
            and close_location >= 0.65
        ):
            reasons.append("add_volume_breakout")
        if (
            numeric_closes[-3] < numeric_closes[-2] < numeric_closes[-1]
            and numeric_volumes[-3] > numeric_volumes[-2] > numeric_volumes[-1]
        ):
            reasons.append("take_profit_price_up_volume_down")
        if regime == "high_volume_stall":
            reasons.append("take_profit_high_volume_stall")
        if (
            high is not None
            and low is not None
            and high > low
            and (high - current_close) / (high - low) >= 0.5
        ):
            reasons.append("take_profit_upper_wick")
        if regime == "bullish_expansion" and close_location is not None and close_location >= 0.65:
            reasons.append("strong_bullish_momentum")

        return (
            {
                "status": "ready",
                "regime": regime,
                "volume_state": volume_state,
                "volume_ratio": volume_ratio,
                "baseline_samples": sample_count,
                "three_bar_return_bps": aggregate_three_bar_return,
                "latest_return_bps": latest_return,
                "close_location": close_location,
                "accelerated_decline": accelerated_decline,
                "reason_codes": list(dict.fromkeys(reasons)),
            },
            current.get("bar_time"),
        )
