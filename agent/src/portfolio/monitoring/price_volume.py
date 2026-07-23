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


def price_volume_interpretation(
    regime: str | None,
    *,
    accelerated_decline: bool = False,
    confidence: str = "medium",
) -> dict[str, str]:
    """Translate one deterministic regime into user-facing semantics.

    This is deliberately not model-generated: the wording is a stable mapping
    from closed-bar facts, so the UI can explain the classification without
    turning an interpretation into a new monitoring condition.
    """

    mapping = {
        "bullish_expansion": (
            "bullish",
            "价格上行且成交量显著高于同时间基准，说明主动买盘增强。",
            "若后续迅速跌回突破位且量能衰减，可能是假突破。",
            "观察后续 1–2 根 5 分钟K线能否守住突破位，且放量状态能否延续。",
        ),
        "bullish_contraction": (
            "mixed",
            "价格上涨但成交缩量，抛压有限，追价资金却仍不充分。",
            "缩量上涨容易在前高或压力位附近失去动能。",
            "等待放量站稳压力位，或回踩时缩量且不破关键支撑。",
        ),
        "bearish_expansion": (
            "bearish",
            (
                "价格下行且成交放大，主动卖盘占优，并出现下跌加速。"
                if accelerated_decline
                else "价格下行且成交量显著高于同时间基准，主动卖盘占优。"
            ),
            "若临近报告支撑位仍持续放量收低，跌破风险上升；单根放量也可能包含恐慌换手。",
            "观察后续 1–2 根 5 分钟K线是否止跌、收回支撑位，或量能回落后出现反包。",
        ),
        "bearish_contraction": (
            "mixed",
            "价格下跌但成交缩量，卖压有所减弱，但尚不能视为反转。",
            "弱势缩量也可能只是买盘缺席，价格仍可能继续阴跌。",
            "等待价格止跌并出现放量回升，或至少连续守住报告支撑位。",
        ),
        "high_volume_absorption": (
            "mixed",
            "价格变化不大但明显放量并收在区间高位，盘中承接较强。",
            "高换手不等于突破，若下一根跌回区间中下部，承接判断失效。",
            "观察后续是否放量突破区间高点并站稳。",
        ),
        "high_volume_rejection": (
            "bearish",
            "价格变化不大但明显放量并收在区间低位，上方抛压较重。",
            "若靠近关键支撑，这可能演变为放量跌破；也可能是一次性换手。",
            "观察后续能否快速收回区间中位和报告支撑位，否则按弱势确认。",
        ),
        "high_volume_stall": (
            "mixed",
            "价格变化不大但成交明显放大，多空分歧加剧，方向尚未确认。",
            "放量滞涨后若收低，可能转为派发；向上突破前不宜只凭成交量追随。",
            "等待价格脱离当前区间，并结合收盘位置确认方向。",
        ),
        "low_volume_balance": (
            "neutral",
            "价格变化不大且成交缩量，市场暂时处于观望和平衡状态。",
            "低流动性下的小幅波动不具备趋势确认意义。",
            "等待成交恢复并突破报告中的支撑或压力区间。",
        ),
        "neutral": (
            "neutral",
            "当前价格与成交量组合没有形成清晰的方向性信号。",
            "中性状态不能单独支持加仓、减仓或突破判断。",
            "继续观察量能是否扩张，以及价格是否触及报告监测点位。",
        ),
    }
    bias, meaning, risk, next_confirmation = mapping.get(
        regime,
        (
            "neutral",
            "当前数据只能描述成交量状态，尚不足以判断多空方向。",
            "数据不足时不应把量价标签作为交易或监测规则。",
            "等待足够的同时间历史样本和连续已收盘K线。",
        ),
    )
    return {
        "bias": bias,
        "meaning": meaning,
        "risk": risk,
        "next_confirmation": next_confirmation,
        "confidence": confidence,
    }


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
        "interpretation": price_volume_interpretation(None, confidence="low"),
        "analysis_scope": "live",
        "data_as_of": None,
        "volume_quality": "unavailable",
        "volume_source_count": 0,
        "volume_sources": [],
        "volume_unit": None,
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
        "interpretation": price_volume_interpretation(None, confidence="low"),
        "analysis_scope": "live",
        "data_as_of": None,
        "volume_quality": "unavailable",
        "volume_source_count": 0,
        "volume_sources": [],
        "volume_unit": None,
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
    values = bar.get("volume_sources") or bar.get("sources") or []
    return tuple(sorted({str(value).strip() for value in values if str(value).strip()}))


def _volume_unit(bar: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (unit, error); consensus bars derive units from included rows."""

    explicit = str(bar.get("volume_unit") or "").strip().lower()
    observations = bar.get("observations") or []
    has_volume_membership = any(
        isinstance(item, dict) and "included_in_volume_consensus" in item
        for item in observations
    )
    included = [
        item
        for item in observations
        if isinstance(item, dict)
        and (
            item.get("included_in_volume_consensus", False)
            if has_volume_membership
            else item.get("included_in_consensus", True)
        )
    ]
    raw_units = [str(item.get("volume_unit") or "").strip().lower() for item in included]
    canonical_explicit = _canonical_volume_unit(explicit) if explicit else None
    canonical_raw_units = [_canonical_volume_unit(unit) for unit in raw_units]
    if (
        explicit == "unknown"
        or (explicit and canonical_explicit is None)
        or any(unit in {"", "unknown"} for unit in raw_units)
        or any(unit is None for unit in canonical_raw_units)
    ):
        return None, "volume_unit_unknown"
    units = {unit for unit in canonical_raw_units if unit is not None}
    if canonical_explicit:
        units.add(canonical_explicit)
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
    volume_status = str(bar.get("volume_status") or "")
    if not volume_status:
        volume_status = "conflict" if "volume_conflict" in flags else status
    if volume_status == "conflict" or "volume_conflict" in flags:
        return False, "volume_conflict", (), None
    if volume_status not in accepted_statuses:
        return False, "volume_status_not_actionable", (), None
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


def _bar_volume_metadata(bar: dict[str, Any], *, scope: str) -> dict[str, Any]:
    signature = _source_signature(bar)
    unit, _error = _volume_unit(bar)
    return {
        "analysis_scope": scope,
        "data_as_of": bar.get("bar_time"),
        "volume_quality": str(bar.get("volume_status") or bar.get("status") or "unknown"),
        "volume_source_count": int(bar.get("volume_source_count") or len(signature)),
        "volume_sources": list(signature),
        "volume_unit": _canonical_volume_unit(unit),
    }


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
            tuple[str, str, str, str, str, int],
            tuple[float, int, float, int, tuple[str, ...]],
        ] = {}
        self._historical_cache: dict[
            tuple[Any, ...],
            tuple[tuple[Any, ...], dict[str, Any]],
        ] = {}

    def invalidate(self, symbols: list[str] | set[str] | tuple[str, ...]) -> None:
        normalized = {str(symbol).upper() for symbol in symbols}
        self._baseline_cache = {
            key: value
            for key, value in self._baseline_cache.items()
            if key[0] not in normalized
        }
        self._historical_cache = {
            key: value
            for key, value in self._historical_cache.items()
            if key[0] not in normalized
        }

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
            amount_ready = (
                str(candidate.get("status") or "") in accepted
                and _number(candidate.get("amount"), positive=True) is not None
            )
            if amount_ready or (good and unit is not None):
                current_item = (candidate_time, candidate)
                current_signature = signature
                current_unit = unit
                break
            if candidate_error:
                current_errors.append(candidate_error)
        if current_item is None:
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
            amount_by_session: dict[str, float] = {}
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
                amount = _number(bar.get("amount"), positive=True)
                if str(bar.get("status") or "") in accepted and amount is not None:
                    amount_by_session.setdefault(bar_session, amount)
                if not good:
                    if reason:
                        baseline_issues.append(reason)
                    continue
                if unit is None or bar_unit != unit:
                    baseline_issues.append("volume_unit_conflict")
                    continue
                if (
                    volume is None
                    or bar_session in by_session
                ):
                    continue
                by_session[bar_session] = volume
                if (
                    len(by_session) >= baseline_sessions
                    and len(amount_by_session) >= baseline_sessions
                ):
                    break
            values = list(by_session.values())
            amount_values = list(amount_by_session.values())
            baseline = (
                statistics.median(values) if values else 0.0,
                len(values),
                statistics.median(amount_values) if amount_values else 0.0,
                len(amount_values),
                tuple(dict.fromkeys(baseline_issues)),
            )
            if (
                (len(values) >= min_samples and baseline[0] > 0)
                or (len(amount_values) >= min_samples and baseline[2] > 0)
            ):
                self._baseline_cache[cache_key] = baseline
        median_volume, volume_sample_count, median_amount, amount_sample_count, baseline_issues = baseline
        current_volume = _number(current.get("volume"), positive=True)
        current_amount = _number(current.get("amount"), positive=True)
        volume_ratio = (
            round(current_volume / median_volume, 4)
            if current_volume is not None and median_volume > 0
            else None
        )
        amount_ratio = (
            round(current_amount / median_amount, 4)
            if current_amount is not None and median_amount > 0
            else None
        )
        amount_ready = amount_sample_count >= min_samples and amount_ratio is not None
        volume_ready = volume_sample_count >= min_samples and volume_ratio is not None
        confirmation_ratio = amount_ratio if amount_ready else volume_ratio if volume_ready else None
        confirmation_metric = (
            "same_bucket_5m_amount_ratio"
            if amount_ready
            else "same_bucket_5m_volume_ratio"
            if volume_ready
            else None
        )
        sample_count = amount_sample_count if amount_ready else volume_sample_count
        if (require_pattern and not volume_ready) or confirmation_ratio is None:
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
        activity_ratio = volume_ratio if require_pattern else confirmation_ratio
        assert activity_ratio is not None
        if activity_ratio < contraction:
            volume_state = "contracted"
        elif activity_ratio >= expansion:
            volume_state = "expanded"
        else:
            volume_state = "normal"
        if not require_pattern:
            interpretation = price_volume_interpretation(None, confidence="low")
            interpretation["meaning"] = (
                f"当前{('成交额' if confirmation_metric == 'same_bucket_5m_amount_ratio' else '成交量')}为同时间基准的 {activity_ratio:.2f} 倍，"
                "但此规则只要求计算量比，未启用方向形态判断。"
            )
            return (
                {
                    "status": "ready",
                    "regime": None,
                    "volume_state": volume_state,
                    "volume_ratio": volume_ratio,
                    "amount_ratio": amount_ratio,
                    "confirmation_ratio": confirmation_ratio,
                    "confirmation_metric": confirmation_metric,
                    "baseline_samples": sample_count,
                    "three_bar_return_bps": None,
                    "latest_return_bps": None,
                    "close_location": None,
                    "accelerated_decline": False,
                    "reason_codes": ["volume_ratio_only", f"volume_{volume_state}"],
                    "interpretation": interpretation,
                    **_bar_volume_metadata(current, scope="live"),
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
            if good and bar_unit == unit:
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
            regime = (
                "high_volume_absorption"
                if volume_state == "expanded" and close_location is not None and close_location >= 0.65
                else "high_volume_rejection"
                if volume_state == "expanded" and close_location is not None and close_location <= 0.35
                else "high_volume_stall"
                if volume_state == "expanded"
                else "low_volume_balance"
                if volume_state == "contracted"
                else "neutral"
            )

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
        if regime in {"high_volume_stall", "high_volume_rejection"}:
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
                "interpretation": price_volume_interpretation(
                    regime,
                    accelerated_decline=accelerated_decline,
                    confidence=(
                        "high"
                        if volume_state == "expanded" and regime != "high_volume_stall"
                        else "medium"
                    ),
                ),
                **_bar_volume_metadata(current, scope="live"),
            },
            current.get("bar_time"),
        )

    def analyze_historical(
        self,
        *,
        market_store: Any,
        symbol: str,
        now_utc: datetime,
        policy: dict[str, Any],
        allow_single_source: bool = False,
        interval: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the latest complete display-only price/volume conclusion.

        The live analyzer deliberately rejects stale bars.  Historical fallback
        evaluates a bounded set of prior closed endpoints with a synthetic
        evaluation clock, then labels the result as historical.  Callers must
        never feed this payload into monitoring-event evaluation.
        """

        selected_interval = str(interval or policy.get("interval") or "5m")
        interval_minutes = {"1m": 1, "5m": 5}.get(selected_interval)
        if interval_minutes is None:
            return None
        cache_key = (
            symbol.upper(),
            selected_interval,
            bool(allow_single_source),
            int(policy.get("baseline_sessions", 10)),
            int(policy.get("min_samples", 5)),
            float(policy.get("contraction_ratio", 0.8)),
            float(policy.get("expansion_ratio", 1.5)),
            float(policy.get("flat_return_bps", 10)),
            float(policy.get("acceleration_multiplier", 1.2)),
        )
        try:
            recent_rows = market_store.query_bars(
                symbol=symbol,
                interval=selected_interval,
                adjustment="raw",
                view="consensus",
                limit=64,
            )
        except Exception:
            return None
        if not recent_rows:
            return None
        latest = recent_rows[-1]
        data_version = (
            latest.get("bar_time"),
            latest.get("verified_at"),
            latest.get("batch_id"),
            latest.get("status"),
            latest.get("volume_status"),
            latest.get("volume"),
        )
        cached = self._historical_cache.get(cache_key)
        if cached is not None and cached[0] == data_version:
            return dict(cached[1])
        try:
            rows = market_store.query_bars(
                symbol=symbol,
                interval=selected_interval,
                adjustment="raw",
                view="consensus",
                limit=3000,
            )
        except Exception:
            return None
        closed = self._closed_bars(rows, now_utc=now_utc, interval=selected_interval)
        accepted = set(_ACCEPTED_STATUSES if allow_single_source else {"verified"})
        candidates: list[tuple[datetime, dict[str, Any]]] = []
        for bar_time, bar in reversed(closed):
            good, _reason, _signature, unit = _bar_quality(
                bar,
                accepted_statuses=accepted,
            )
            if good and unit is not None:
                candidates.append((bar_time, bar))
            if len(candidates) >= 24:
                break

        for bar_time, bar in candidates:
            synthetic_now = bar_time + timedelta(minutes=interval_minutes, seconds=1)
            payload, evidence_time = self.analyze(
                market_store=market_store,
                symbol=symbol,
                now_utc=synthetic_now,
                policy=policy,
                allow_single_source=allow_single_source,
                interval=selected_interval,
            )
            if payload.get("status") != "ready":
                continue
            payload.update(_bar_volume_metadata(bar, scope="historical"))
            payload["data_as_of"] = evidence_time or bar.get("bar_time")
            payload["reason_codes"] = list(
                dict.fromkeys([*(payload.get("reason_codes") or []), "historical_reference"])
            )
            self._historical_cache[cache_key] = (data_version, dict(payload))
            return payload
        return None
