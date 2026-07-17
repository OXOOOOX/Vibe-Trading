"""Incremental refresh orchestration, provenance capture, and verification."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import statistics
import tempfile
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from src.market_data import get_loader_strict
from src.market_verification import normalize_adjustment, source_adjustment_policy, verified_cache_dir
from src.portfolio.state import load_state, save_state
from src.usage import record_current_resource

from .storage import MarketCacheStore, utc_now


FetchSource = Callable[..., dict[str, Any]]
TERMINAL_RUN_STATUSES = {"completed", "partial", "failed", "interrupted"}
PROFILE_ITEMS: tuple[tuple[str, str, int, int], ...] = (
    ("1m", "raw", 10, 5),
    ("5m", "raw", 35, 20),
    ("1D", "raw", 1100, 750),
    ("1D", "qfq", 1100, 750),
)
SOURCE_SUCCESS_TTL_SECONDS = {"1m": 60, "5m": 300, "1D": 900}
SOURCE_BAR_MAX_AGE_SECONDS = {"1m": 180, "5m": 720}
MARKET_SETTLEMENT_GRACE = timedelta(minutes=2)
SOURCE_RETRY_BASE_SECONDS = {
    "transport_error": 60,
    "provider_error": 60,
    "rate_limited": 300,
    "no_coverage": 300,
    "basis_mismatch": 900,
    "duplicate_upstream": 900,
    "dependency_missing": 1800,
}


def _profile_items_for_symbol(symbol: str) -> list[tuple[str, str]]:
    """Return cache variants that have declared semantics for this market."""
    is_equity = symbol.upper().endswith((".SH", ".SZ", ".BJ", ".US", ".HK"))
    return [
        (interval, adjustment)
        for interval, adjustment, _, _ in PROFILE_ITEMS
        if is_equity or adjustment == "raw"
    ]


def _market_timezone(symbol: str) -> ZoneInfo:
    upper = symbol.upper()
    if upper.endswith((".SH", ".SZ", ".BJ", ".HK")):
        return ZoneInfo("Asia/Shanghai")
    if upper.endswith(".US"):
        return ZoneInfo("America/New_York")
    return ZoneInfo("UTC")


def _market_sessions(symbol: str) -> tuple[tuple[clock_time, clock_time], ...] | None:
    """Return regular local sessions; ``None`` means an always-open market."""
    upper = symbol.upper()
    if upper.endswith((".SH", ".SZ", ".BJ")):
        return (
            (clock_time(9, 30), clock_time(11, 30)),
            (clock_time(13, 0), clock_time(15, 0)),
        )
    if upper.endswith(".HK"):
        return (
            (clock_time(9, 30), clock_time(12, 0)),
            (clock_time(13, 0), clock_time(16, 0)),
        )
    if upper.endswith(".US"):
        return ((clock_time(9, 30), clock_time(16, 0)),)
    return None


def _as_utc(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _previous_weekday(value: date) -> date:
    candidate = value - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _closed_period_boundary(symbol: str, now: datetime) -> datetime | None:
    """Return the close after which one successful snapshot is valid until reopen."""
    sessions = _market_sessions(symbol)
    if not sessions:
        return None
    local_now = now.astimezone(_market_timezone(symbol))
    local_time = local_now.timetz().replace(tzinfo=None)
    timezone_info = local_now.tzinfo
    assert timezone_info is not None

    if local_now.weekday() >= 5 or local_time < sessions[0][0]:
        previous_day = _previous_weekday(local_now.date())
        return datetime.combine(
            previous_day,
            sessions[-1][1],
            tzinfo=timezone_info,
        ) + MARKET_SETTLEMENT_GRACE

    for index, (opens_at, closes_at) in enumerate(sessions):
        if opens_at <= local_time < closes_at:
            return None
        if local_time >= closes_at:
            next_open = sessions[index + 1][0] if index + 1 < len(sessions) else None
            if next_open is None or local_time < next_open:
                boundary = datetime.combine(
                    local_now.date(),
                    closes_at,
                    tzinfo=timezone_info,
                ) + MARKET_SETTLEMENT_GRACE
                return boundary if local_now >= boundary else None
    return None


def _source_success_is_fresh(
    state: dict[str, Any], *, symbol: str, interval: str, now: datetime
) -> bool:
    last_success = state.get("last_success_at")
    if not last_success:
        return False
    success_at = _as_utc(str(last_success))
    now_utc = _as_utc(now)
    if success_at > now_utc + timedelta(seconds=5):
        return False

    boundary = _closed_period_boundary(symbol, now_utc)
    if boundary is not None:
        return success_at >= boundary.astimezone(timezone.utc)

    ttl = timedelta(seconds=SOURCE_SUCCESS_TTL_SECONDS.get(interval, 60))
    if now_utc - success_at > ttl:
        return False

    sessions = _market_sessions(symbol)
    local_now = now_utc.astimezone(_market_timezone(symbol))
    local_time = local_now.timetz().replace(tzinfo=None)
    market_is_open = bool(
        sessions
        and local_now.weekday() < 5
        and any(opens_at <= local_time < closes_at for opens_at, closes_at in sessions)
    )
    if market_is_open and interval in SOURCE_BAR_MAX_AGE_SECONDS:
        latest_bar = state.get("latest_bar_time")
        if not latest_bar:
            return False
        bar_age = now_utc - _as_utc(str(latest_bar))
        if bar_age < timedelta(minutes=-1) or bar_age > timedelta(
            seconds=SOURCE_BAR_MAX_AGE_SECONDS[interval]
        ):
            return False
    return True


def _source_retry_is_due(state: dict[str, Any] | None, now: datetime) -> bool:
    if not state or state.get("last_status") == "success":
        return False
    retry_at = state.get("next_retry_at")
    return not retry_at or _as_utc(now) >= _as_utc(str(retry_at))


def _retry_base_seconds(category: str) -> int:
    return SOURCE_RETRY_BASE_SECONDS.get(category, 60)


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _json_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_bar_time(value: Any, symbol: str, interval: str, source: str) -> tuple[str, str]:
    timestamp = pd.Timestamp(value)
    market_tz = _market_timezone(symbol)
    if timestamp.tzinfo is None:
        # Yahoo's intraday loader removes the UTC tz marker while preserving
        # UTC clock values. Daily rows, and the other equity loaders, are
        # calendar-date values in the exchange timezone.
        timestamp = timestamp.tz_localize(
            timezone.utc if source == "yahoo" and interval != "1D" else market_tz
        )
    if interval == "1m":
        timestamp = timestamp.floor("min")
    elif interval == "5m":
        timestamp = timestamp.floor("5min")
    local = timestamp.tz_convert(market_tz)
    if interval == "1D":
        # Providers encode the same daily session either as local midnight or
        # the 15:00 market close. Canonicalize by trading date before quorum.
        local = local.normalize()
    utc = timestamp.tz_convert(timezone.utc)
    if interval == "1D":
        utc = local.tz_convert(timezone.utc)
    return utc.isoformat(), local.date().isoformat()


def _volume_policy(source: str, symbol: str) -> tuple[str, float]:
    is_a_share = symbol.upper().endswith((".SH", ".SZ", ".BJ"))
    if is_a_share and source in {"tencent", "eastmoney"}:
        return "lot", 100.0
    if is_a_share and source == "mootdx":
        return "share", 1.0
    return "unknown", 1.0


def _actual_adjustment(
    source: str,
    symbol: str,
    interval: str,
    requested: str,
) -> tuple[str, str]:
    """Classify the returned price basis without weakening the request.

    Provider names are irrelevant here: only an adapter path with an explicit
    contract may claim a canonical basis. Unknown/default series remain
    unclassified and are rejected by the exact-basis gate below.
    """

    source = source.lower()
    requested = normalize_adjustment(requested)
    if source in {"tencent", "eastmoney"} and requested in {"raw", "qfq"}:
        return requested, "explicit_request"
    if source == "yahoo":
        if requested == "qfq" and interval in {"1D", "1W", "1M"}:
            return "qfq", "adjusted_close_factor"
        if requested == "raw" and interval in {"1m", "5m", "1H"}:
            return "raw", "intraday_trade_price"
        if interval in {"1D", "1W", "1M"}:
            return "split_adjusted", "provider_contract"
    if source == "mootdx" and requested == "raw":
        return "raw", "loader_contract"
    if source == "sina" and interval == "1D" and requested == "raw":
        return "raw", "loader_contract"
    if source == "nasdaq" and interval in {"1m", "5m"} and requested == "raw":
        return "raw", "loader_contract"
    policy = source_adjustment_policy(source, symbol)
    return str(policy["adjustment"]), str(policy["confidence"])


def _source_fingerprint(source: str, symbol: str) -> str:
    """Identify the real upstream, not merely the Python adapter."""
    normalized = source.lower()
    if normalized == "eastmoney":
        return "eastmoney_push2his"
    if normalized == "tencent":
        return "tencent_ifzq"
    if normalized == "mootdx":
        return "tdx_tcp"
    if normalized == "baostock":
        return "baostock_tcp"
    if normalized in {"yahoo", "yfinance"}:
        return "yahoo_chart"
    if normalized == "nasdaq":
        return "nasdaq_official_chart"
    if normalized == "sina":
        return "sina_us_kline"
    if normalized == "akshare":
        upper = symbol.upper()
        is_etf = upper.endswith((".SH", ".SZ", ".BJ")) and upper[:2] in {
            "15", "16", "50", "51", "52", "56", "58",
        }
        return "sina_etf" if is_etf else "eastmoney_push2his"
    return normalized


def _error_category(error: BaseException | str) -> str:
    text = str(error).lower()
    if any(token in text for token in ("unavailable", "install", "no module named", "unknown data source")):
        return "dependency_missing"
    if any(token in text for token in ("429", "rate limit", "too many requests", "throttle")):
        return "rate_limited"
    if isinstance(error, (ConnectionError, TimeoutError, OSError)):
        return "transport_error"
    if any(token in text for token in (
        "connection", "remote end closed", "urlopen", "timed out", "timeout",
        "network", "socket", "ssl", "transport failed",
    )):
        return "transport_error"
    return "provider_error"


def fetch_source_records(
    *,
    requested_source: str,
    symbol: str,
    start_date: str,
    end_date: str,
    interval: str,
    adjustment: str,
    request_timeout_s: float | None = None,
) -> dict[str, Any]:
    """Fetch one source while preserving the actual fallback loader identity."""
    loader_cls = get_loader_strict(requested_source)
    loader = loader_cls()
    actual_source = str(getattr(loader, "name", requested_source))
    kwargs: dict[str, Any] = {"interval": interval}
    if "fields" in inspect.signature(loader.fetch).parameters:
        kwargs["fields"] = ["amount"]
    if "adjustment" in inspect.signature(loader.fetch).parameters:
        kwargs["adjustment"] = adjustment
    if request_timeout_s is not None and "request_timeout_s" in inspect.signature(loader.fetch).parameters:
        kwargs["request_timeout_s"] = request_timeout_s
    if "strict" in inspect.signature(loader.fetch).parameters:
        kwargs["strict"] = True
    data = loader.fetch([symbol], start_date, end_date, **kwargs)
    frame = data.get(symbol)
    if frame is None:
        for key, value in data.items():
            if str(key).upper() == symbol.upper():
                frame = value
                break
    records: list[dict[str, Any]] = []
    if frame is not None and not frame.empty:
        records = frame.reset_index().to_dict(orient="records")
    actual_adjustment, confidence = _actual_adjustment(
        actual_source,
        symbol,
        interval,
        adjustment,
    )
    return {
        "requested_source": requested_source,
        "actual_source": actual_source,
        "adapter_name": f"{loader_cls.__module__}.{loader_cls.__name__}",
        "source_fingerprint": str(
            getattr(loader, "source_fingerprint", None)
            or _source_fingerprint(actual_source, symbol)
        ),
        "requested_adjustment": normalize_adjustment(adjustment),
        "actual_adjustment": actual_adjustment,
        "adjustment_confidence": confidence,
        "records": records,
        "transport_events": list(getattr(loader, "transport_events", []) or []),
    }


def source_candidates(symbol: str, interval: str, adjustment: str) -> list[str]:
    upper = symbol.upper()
    adjustment = normalize_adjustment(adjustment)
    if upper.endswith((".SH", ".SZ", ".BJ")):
        if interval in {"1m", "5m"}:
            # Tencent + TDX are the fastest independent A-share pair in normal
            # operation.  Eastmoney remains the third-source tie breaker, but
            # no longer delays every symbol when its public endpoint is being
            # throttled or temporarily closes connections.
            return ["tencent", "mootdx", "eastmoney"]
        if adjustment == "qfq":
            # BaoStock is slower than Tencent but much less prone to public-HTTP
            # throttling than Eastmoney.  Put the reliable quorum pair first;
            # the remaining adapters are only needed for failure/conflict.
            return ["tencent", "baostock", "eastmoney", "akshare"]
        return ["tencent", "mootdx", "eastmoney"]
    if upper.endswith(".US"):
        if interval in {"1m", "5m"}:
            return ["yahoo", "nasdaq", "eastmoney"] if adjustment == "raw" else []
        if interval == "1D" and adjustment == "qfq":
            # Yahoo adjusted_close is converted into canonical qfq OHLC by the
            # adapter. No second independent upstream currently proves the same
            # total-return basis, so the result intentionally remains single-source.
            return ["yahoo"]
        if interval == "1D" and adjustment == "raw":
            # Sina preserves historical event-time prices across AAPL's 2020
            # split. Yahoo daily Close is split-adjusted and is therefore barred
            # from this raw quorum; Eastmoney is retained only when it has coverage.
            return ["sina", "eastmoney"]
        return []
    if upper.endswith(".HK"):
        if interval != "1D":
            return ["eastmoney"] if adjustment == "raw" else []
        if adjustment == "qfq":
            return ["eastmoney", "yahoo"]
        if adjustment == "raw":
            return ["eastmoney"]
        return []
    if upper.endswith("-USDT") or upper.endswith("/USDT"):
        return ["okx", "ccxt"]
    # ``auto`` is a routing instruction, not a real independent provider.  The
    # strict verification path must never persist it as source health or try to
    # instantiate it as a loader.  Unknown symbol families remain unresolved
    # until they are normalized to a market with concrete candidates.
    return []


def _spread(values: list[float]) -> float | None:
    if len(values) < 2:
        return 0.0 if values else None
    median = statistics.median(values)
    return ((max(values) - min(values)) / median * 100.0) if median else None


def _price_tolerance(*, price: float, interval: str, tick_size: float) -> float:
    return max(2 * tick_size, abs(price) * (0.002 if interval != "1D" else 0.005))


def _within_tolerance(left: float, right: float, *, interval: str, tick_size: float) -> bool:
    reference = statistics.median([left, right])
    return abs(left - right) <= _price_tolerance(
        price=float(reference), interval=interval, tick_size=tick_size
    )


def _session_is_settled(session_date: str, symbol: str) -> bool:
    """Return whether a session can safely reuse same-bar cached observations."""
    local_now = datetime.now(_market_timezone(symbol))
    try:
        session = date.fromisoformat(str(session_date))
    except ValueError:
        return False
    if session < local_now.date():
        return True
    if session > local_now.date():
        return False
    sessions = _market_sessions(symbol)
    if not sessions:
        return False
    settled_at = datetime.combine(
        local_now.date(), sessions[-1][1], tzinfo=local_now.tzinfo
    ) + timedelta(minutes=10)
    return local_now >= settled_at


class MarketRefreshService:
    def __init__(
        self,
        *,
        store: MarketCacheStore | None = None,
        fetcher: FetchSource = fetch_source_records,
        summary_dir: Path | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store or MarketCacheStore()
        self.fetcher = fetcher
        self.summary_dir = summary_dir or verified_cache_dir()
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))

    def _now_utc(self) -> datetime:
        return _as_utc(self.now_factory())

    def prepare_startup(self) -> int:
        interrupted = self.store.mark_running_interrupted()
        migrated_symbols = self.store.migrate_price_basis_contract_v2()
        for symbol in migrated_symbols:
            self._recompute_consensus(
                symbol,
                "1D",
                "raw",
                "price-basis-contract-v2",
            )
            self.store.refresh_latest_quote(symbol)
        return interrupted

    def create_refresh(
        self,
        *,
        symbols: list[str],
        profile: str = "portfolio_default",
        sources: list[str] | None = None,
        force: bool = False,
        start_date: str | None = None,
        end_date: str | None = None,
        items: list[tuple[str, str]] | None = None,
        read_only: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        normalized = sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
        if not normalized:
            raise ValueError("At least one symbol is required")
        item_specs = items or [(interval, adjustment) for interval, adjustment, _, _ in PROFILE_ITEMS]
        config = {
            "sources": sources or [],
            "force": bool(force),
            "start_date": start_date,
            "end_date": end_date,
            "items": item_specs,
            "read_only": bool(read_only),
        }
        dedupe_payload = {"symbols": normalized, "profile": profile, **config}
        dedupe_key = _json_hash(dedupe_payload)
        active = self.store.find_active_run(dedupe_key)
        if active:
            run = self.store.get_run(active["run_id"])
            assert run is not None
            return run, True

        run_id = uuid.uuid4().hex
        rows = [
            (symbol, interval, adjustment)
            for symbol in normalized
            for interval, adjustment in (item_specs if items is not None else _profile_items_for_symbol(symbol))
        ]
        self.store.create_run(
            run_id=run_id,
            dedupe_key=dedupe_key,
            profile=profile,
            symbols=normalized,
            config=config,
            items=rows,
        )
        run = self.store.get_run(run_id)
        assert run is not None
        return run, False

    def refresh_sync(
        self,
        *,
        deadline: float | None = None,
        should_cancel: Callable[[], bool] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Run a refresh synchronously, with optional cooperative cancellation.

        ``deadline`` limits this caller's refresh work without leaving a long
        sequence of source requests running after an Agent-facing data request
        has already returned.  In-flight socket calls still finish normally,
        but no later symbol or source is started once the budget expires.
        """
        run, deduplicated = self.create_refresh(**kwargs)
        if run["status"] in {"queued", "running"}:
            self.run_refresh(run["run_id"], deadline=deadline, should_cancel=should_cancel)
        result = self.store.get_run(run["run_id"])
        assert result is not None
        result["deduplicated"] = deduplicated
        result["quotes"] = self.store.list_quotes(result["symbols"])
        return result

    def run_refresh(
        self,
        run_id: str,
        *,
        deadline: float | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            raise KeyError(run_id)
        if run["status"] in TERMINAL_RUN_STATUSES:
            return run
        config = run["config"]
        self.store.update_run(run_id, status="running", started_at=utc_now(), error=None)
        completed = conflicts = failed = 0
        interrupted = False
        try:
            for item in run["items"]:
                if self._should_stop(deadline, should_cancel):
                    interrupted = True
                    break
                symbol = item["symbol"]
                interval = item["interval"]
                adjustment = item["adjustment"]
                self.store.update_run(run_id, current_symbol=symbol, current_source=None)
                try:
                    result = self._refresh_item(
                        run_id=run_id,
                        symbol=symbol,
                        interval=interval,
                        adjustment=adjustment,
                        sources=config.get("sources") or None,
                        force=bool(config.get("force")),
                        explicit_start=config.get("start_date"),
                        explicit_end=config.get("end_date"),
                        deadline=deadline,
                        should_cancel=should_cancel,
                    )
                    if result["status"] == "interrupted":
                        interrupted = True
                        break
                    if result["status"] == "unresolved_conflict":
                        conflicts += 1
                    elif result["status"] == "unresolved":
                        failed += 1
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    self.store.update_item(
                        run_id, symbol, interval, adjustment,
                        status="failed", message=str(exc), completed_at=utc_now(),
                    )
                completed += 1
                self.store.update_run(
                    run_id,
                    completed_items=completed,
                    conflict_items=conflicts,
                    failed_items=failed,
                )

            completed_symbols = [
                item["symbol"] for item in (self.store.get_run(run_id) or {}).get("items", [])
                if item.get("status") in {
                    "verified", "single_source", "source_lag", "provisional_mix",
                    "basis_mismatch", "unresolved_conflict",
                }
            ]
            for symbol in set(completed_symbols):
                self.store.refresh_latest_quote(symbol)
            if completed_symbols:
                if not bool(config.get("read_only")):
                    self._update_portfolio(list(set(completed_symbols)))
                self._write_compatibility_summaries(list(set(completed_symbols)))
            final_status = "interrupted" if interrupted else (
                "completed" if failed == 0 else ("partial" if completed > failed else "failed")
            )
            self.store.update_run(
                run_id,
                status=final_status,
                completed_at=utc_now(),
                current_symbol=None,
                current_source=None,
                error="refresh deadline reached" if interrupted else None,
            )
        except Exception as exc:  # noqa: BLE001
            self.store.update_run(run_id, status="failed", completed_at=utc_now(), error=str(exc))
            raise
        result = self.store.get_run(run_id)
        assert result is not None
        return result

    @staticmethod
    def _should_stop(deadline: float | None, should_cancel: Callable[[], bool] | None) -> bool:
        """Return whether a caller-specific refresh budget has expired."""
        return bool((should_cancel and should_cancel()) or (deadline is not None and time.monotonic() >= deadline))

    def _date_window(
        self, interval: str, adjustment: str, explicit_start: str | None, explicit_end: str | None
    ) -> tuple[str, str, int]:
        end = date.fromisoformat(explicit_end) if explicit_end else datetime.now(_market_timezone("000001.SH")).date()
        for candidate_interval, candidate_adjustment, calendar_days, sessions in PROFILE_ITEMS:
            if interval == candidate_interval and adjustment == candidate_adjustment:
                start = date.fromisoformat(explicit_start) if explicit_start else end - timedelta(days=calendar_days)
                return start.isoformat(), end.isoformat(), sessions
        start = date.fromisoformat(explicit_start) if explicit_start else end - timedelta(days=35)
        return start.isoformat(), end.isoformat(), 250

    def _refresh_item(
        self,
        *,
        run_id: str,
        symbol: str,
        interval: str,
        adjustment: str,
        sources: list[str] | None,
        force: bool,
        explicit_start: str | None,
        explicit_end: str | None,
        deadline: float | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        default_sources = source_candidates(symbol, interval, adjustment)
        if sources:
            requested_sources = [
                str(source).strip()
                for source in sources
                if str(source).strip() and str(source).strip().lower() != "auto"
            ]
            if any(str(source).strip().lower() == "auto" for source in sources):
                requested_sources.extend(default_sources)
            requested_sources = list(dict.fromkeys(requested_sources))
        else:
            requested_sources = default_sources
        start_date, end_date, keep_sessions = self._date_window(
            interval, adjustment, explicit_start, explicit_end
        )
        self.store.upsert_instrument(symbol)
        self.store.update_item(
            run_id, symbol, interval, adjustment,
            status="fetching", requested_sources_json=json.dumps(requested_sources), started_at=utc_now(),
        )
        actual_sources: list[str] = []
        source_errors: list[str] = []
        attempts: list[dict[str, Any]] = []
        successes: dict[str, dict[str, Any]] = {}
        reused_observation_keys: set[tuple[str, str]] = set()
        rows_written = 0
        tick_size = float(self.store.instrument(symbol).get("tick_size") or 0.001)
        adaptive_polling = not force and explicit_start is None and explicit_end is None

        def add_success(
            *,
            requested_source: str,
            actual_source: str,
            fingerprint: str,
            bars: list[dict[str, Any]],
            reused: bool = False,
        ) -> None:
            if not bars:
                return
            latest = bars[-1]
            successes[fingerprint] = {
                "requested_source": requested_source,
                "actual_source": actual_source,
                "fingerprint": fingerprint,
                "bar_time": latest["bar_time"],
                "close": float(latest["close"]),
                "bars": {
                    str(row["bar_time"]): float(row["close"])
                    for row in bars[-20:]
                    if row.get("close") is not None
                },
            }
            if actual_source not in actual_sources:
                actual_sources.append(actual_source)
            if reused:
                reused_observation_keys.update(
                    (fingerprint, str(row["bar_time"])) for row in bars[-20:]
                )

        def confirmation_state() -> tuple[bool, bool]:
            if not successes:
                return False, False
            grouped_successes: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for success in successes.values():
                for candidate_time, candidate_close in success["bars"].items():
                    grouped_successes[candidate_time].append(
                        {
                            **success,
                            "bar_time": candidate_time,
                            "close": candidate_close,
                        }
                    )
            newest_time = max(
                pd.Timestamp(success["bar_time"]) for success in successes.values()
            )
            maximum_confirmation_lag = (
                pd.Timedelta(minutes=10)
                if interval in {"1m", "5m"}
                else pd.Timedelta(days=3)
            )
            recent_groups = {
                candidate_time: group
                for candidate_time, group in grouped_successes.items()
                if newest_time - pd.Timestamp(candidate_time) <= maximum_confirmation_lag
            }
            has_quorum = any(
                _within_tolerance(
                    float(left["close"]),
                    float(right["close"]),
                    interval=interval,
                    tick_size=tick_size,
                )
                for group in recent_groups.values()
                for left_index, left in enumerate(group)
                for right in group[left_index + 1:]
            )
            return has_quorum, any(len(group) >= 3 for group in recent_groups.values())

        def should_stop_after(index: int) -> bool:
            has_quorum, has_three_sources = confirmation_state()
            if not (has_quorum or has_three_sources):
                return False
            now = self._now_utc()
            return not any(
                _source_retry_is_due(
                    self.store.source_poll_state(
                        symbol, remaining_source, interval, adjustment
                    ),
                    now,
                )
                for remaining_source in requested_sources[index + 1:]
            )

        def interrupted_result() -> dict[str, Any]:
            self.store.update_item(
                run_id, symbol, interval, adjustment,
                status="interrupted",
                actual_sources_json=json.dumps(actual_sources),
                attempts_json=json.dumps(attempts, ensure_ascii=False),
                rows_written=rows_written,
                message="refresh deadline reached",
                completed_at=utc_now(),
            )
            return {
                "status": "interrupted",
                "rows_written": rows_written,
                "actual_sources": actual_sources,
                "attempts": attempts,
            }

        for index, requested_source in enumerate(requested_sources):
            if self._should_stop(deadline, should_cancel):
                return interrupted_result()
            self.store.update_run(run_id, current_symbol=symbol, current_source=requested_source)
            actual_hint = requested_source
            try:
                actual_hint = str(
                    getattr(get_loader_strict(requested_source), "name", requested_source)
                )
            except Exception:  # The injected test fetcher may use synthetic source names.
                pass
            poll_state = self.store.source_poll_state(
                symbol, requested_source, interval, adjustment
            )
            cached_rows: list[dict[str, Any]] = []
            cache_is_fresh = bool(
                adaptive_polling
                and poll_state
                and _source_success_is_fresh(
                    poll_state,
                    symbol=symbol,
                    interval=interval,
                    now=self._now_utc(),
                )
            )
            if cache_is_fresh and poll_state is not None:
                cached_actual_source = str(poll_state.get("actual_source") or actual_hint)
                cached_rows = self.store.source_tail(
                    symbol,
                    cached_actual_source,
                    interval,
                    adjustment,
                    limit=20,
                )
                cache_is_fresh = bool(cached_rows)
                if cached_rows:
                    cached_fingerprint = str(
                        poll_state.get("source_fingerprint")
                        or cached_rows[-1].get("source_fingerprint")
                        or cached_actual_source
                    )
                    if cached_fingerprint not in successes:
                        add_success(
                            requested_source=requested_source,
                            actual_source=cached_actual_source,
                            fingerprint=cached_fingerprint,
                            bars=cached_rows,
                            reused=True,
                        )

            retry_due = _source_retry_is_due(poll_state, self._now_utc())
            if cache_is_fresh and not retry_due:
                record_current_resource(
                    provider=str(poll_state.get("actual_source") if poll_state else actual_hint),
                    category="market",
                    status="ok",
                    elapsed_ms=0,
                    cache_mode="cache_hit",
                    query={"code": symbol, "interval": interval, "source": requested_source},
                    network_request=False,
                    cache_access=True,
                    metadata={"reason": "adaptive_polling_fresh"},
                )
                attempts.append(
                    {
                        "requested_source": requested_source,
                        "actual_source": poll_state.get("actual_source") if poll_state else actual_hint,
                        "upstream_source": poll_state.get("source_fingerprint") if poll_state else None,
                        "actual_adjustment": adjustment,
                        "status": "cache_fresh",
                        "error_category": None,
                        "error": None,
                        "latency_ms": 0.0,
                        "rows": len(cached_rows),
                        "latest_bar_time": cached_rows[-1]["bar_time"],
                    }
                )
                if should_stop_after(index):
                    break
                continue
            if adaptive_polling and poll_state and poll_state.get("last_status") != "success" and not retry_due:
                record_current_resource(
                    provider=str(poll_state.get("actual_source") or actual_hint),
                    category="market",
                    status="error",
                    elapsed_ms=0,
                    cache_mode="cache_hit",
                    query={"code": symbol, "interval": interval, "source": requested_source},
                    network_request=False,
                    cache_access=True,
                    metadata={"reason": "retry_backoff"},
                )
                attempts.append(
                    {
                        "requested_source": requested_source,
                        "actual_source": poll_state.get("actual_source"),
                        "upstream_source": poll_state.get("source_fingerprint"),
                        "actual_adjustment": adjustment,
                        "status": "retry_backoff",
                        "error_category": poll_state.get("error_category"),
                        "error": poll_state.get("error"),
                        "next_retry_at": poll_state.get("next_retry_at"),
                        "latency_ms": 0.0,
                    }
                )
                if should_stop_after(index):
                    break
                continue

            tail = self.store.tail_start(
                symbol, actual_hint, interval, adjustment, 5 if interval != "1D" else 3
            )
            checkpoint = bool(index > 0 and tail and not force)
            fetch_start = (
                (date.fromisoformat(end_date) - timedelta(days=7)).isoformat()
                if checkpoint
                else start_date
            )
            if tail and not force and not checkpoint:
                tail_date = pd.Timestamp(tail).tz_convert(
                    _market_timezone(symbol)
                ).date().isoformat()
                if tail_date > fetch_start:
                    fetch_start = tail_date

            attempt_started = time.monotonic()
            try:
                fetch_kwargs = {
                    "requested_source": requested_source,
                    "symbol": symbol,
                    "start_date": fetch_start,
                    "end_date": end_date,
                    "interval": interval,
                    "adjustment": adjustment,
                }
                if deadline is not None:
                    fetch_kwargs["request_timeout_s"] = max(1.0, min(10.0, deadline - time.monotonic()))
                probe = self.fetcher(
                    **fetch_kwargs,
                )
            except Exception as exc:  # noqa: BLE001
                category = _error_category(exc)
                detail = f"{requested_source}: {type(exc).__name__}: {exc}"
                source_errors.append(detail)
                attempted_at = self._now_utc().isoformat()
                self.store.record_source_attempt(
                    symbol=symbol,
                    requested_source=requested_source,
                    interval=interval,
                    adjustment=adjustment,
                    status="failed",
                    attempted_at=attempted_at,
                    actual_source=(poll_state or {}).get("actual_source"),
                    source_fingerprint=(poll_state or {}).get("source_fingerprint"),
                    error_category=category,
                    error=str(exc),
                    retry_base_seconds=_retry_base_seconds(category),
                )
                attempts.append(
                    {
                        "requested_source": requested_source,
                        "actual_source": None,
                        "upstream_source": None,
                        "status": "failed",
                        "error_category": category,
                        "error": str(exc),
                        "latency_ms": round((time.monotonic() - attempt_started) * 1000, 1),
                    }
                )
                if should_stop_after(index):
                    break
                continue
            if self._should_stop(deadline, should_cancel):
                return interrupted_result()
            actual_source = str(probe["actual_source"])
            actual_adjustment = str(probe["actual_adjustment"])
            fingerprint = str(probe.get("source_fingerprint") or actual_source)
            attempt: dict[str, Any] = {
                "requested_source": requested_source,
                "actual_source": actual_source,
                "upstream_source": fingerprint,
                "actual_adjustment": actual_adjustment,
                "status": "success",
                "error_category": None,
                "error": None,
                "latency_ms": round((time.monotonic() - attempt_started) * 1000, 1),
                "transport_events": list(probe.get("transport_events") or []),
            }
            if actual_adjustment != adjustment:
                attempt.update(
                    status="basis_mismatch",
                    error_category="basis_mismatch",
                    error=f"requested {adjustment}, provider returned {actual_adjustment}",
                )
                attempts.append(attempt)
                self.store.record_source_attempt(
                    symbol=symbol,
                    requested_source=requested_source,
                    interval=interval,
                    adjustment=adjustment,
                    status="basis_mismatch",
                    attempted_at=self._now_utc().isoformat(),
                    actual_source=actual_source,
                    source_fingerprint=fingerprint,
                    error_category="basis_mismatch",
                    error=str(attempt["error"]),
                    retry_base_seconds=_retry_base_seconds("basis_mismatch"),
                )
                if should_stop_after(index):
                    break
                continue
            if (
                fingerprint in successes
                and successes[fingerprint].get("requested_source") != requested_source
            ):
                attempt.update(
                    status="duplicate_upstream",
                    error_category="duplicate_upstream",
                    error=f"same upstream as {successes[fingerprint]['actual_source']}",
                )
                attempts.append(attempt)
                self.store.record_source_attempt(
                    symbol=symbol,
                    requested_source=requested_source,
                    interval=interval,
                    adjustment=adjustment,
                    status="duplicate_upstream",
                    attempted_at=self._now_utc().isoformat(),
                    actual_source=actual_source,
                    source_fingerprint=fingerprint,
                    error_category="duplicate_upstream",
                    error=str(attempt["error"]),
                    retry_base_seconds=_retry_base_seconds("duplicate_upstream"),
                )
                if should_stop_after(index):
                    break
                continue

            normalized = self._normalize_records(
                probe, symbol=symbol, interval=interval, batch_id=run_id,
                acquisition_mode="checkpoint" if checkpoint else "network",
            )
            if not normalized:
                attempt.update(
                    status="no_coverage",
                    error_category="no_coverage",
                    error="provider returned no usable bars",
                )
                attempts.append(attempt)
                self.store.record_source_attempt(
                    symbol=symbol,
                    requested_source=requested_source,
                    interval=interval,
                    adjustment=adjustment,
                    status="no_coverage",
                    attempted_at=self._now_utc().isoformat(),
                    actual_source=actual_source,
                    source_fingerprint=fingerprint,
                    error_category="no_coverage",
                    error=str(attempt["error"]),
                    retry_base_seconds=_retry_base_seconds("no_coverage"),
                )
                if should_stop_after(index):
                    break
                continue
            rows_written += self.store.upsert_source_bars(normalized)
            if actual_source not in actual_sources:
                actual_sources.append(actual_source)
            latest = normalized[-1]
            attempt.update(
                rows=len(normalized),
                latest_bar_time=latest["bar_time"],
                latest_close=latest["close"],
            )
            attempts.append(attempt)
            self.store.record_source_attempt(
                symbol=symbol,
                requested_source=requested_source,
                interval=interval,
                adjustment=adjustment,
                status="success",
                attempted_at=self._now_utc().isoformat(),
                actual_source=actual_source,
                source_fingerprint=fingerprint,
                latest_bar_time=str(latest["bar_time"]),
            )
            add_success(
                requested_source=requested_source,
                actual_source=actual_source,
                fingerprint=fingerprint,
                bars=normalized,
            )
            if should_stop_after(index):
                break

        if interval == "5m" and adjustment == "raw" and rows_written:
            rows_written += self._derive_five_minute_bars(symbol, run_id)

        self.store.update_item(run_id, symbol, interval, adjustment, status="verifying")
        consensus = self._recompute_consensus(
            symbol,
            interval,
            adjustment,
            run_id,
            reused_observation_keys=reused_observation_keys,
        )
        self.store.prune_sessions(symbol, interval, adjustment, keep_sessions)
        latest = consensus[-1] if consensus else None
        if not successes:
            status = "basis_mismatch" if any(item["status"] == "basis_mismatch" for item in attempts) else "unresolved"
        else:
            status = str(latest["status"]) if latest else "unresolved"
        message = "; ".join(source_errors) if source_errors else None
        if not latest:
            message = f"No compatible bars were returned{'; ' + message if message else ''}"
        self.store.update_item(
            run_id, symbol, interval, adjustment,
            status=status,
            actual_sources_json=json.dumps(actual_sources),
            attempts_json=json.dumps(attempts, ensure_ascii=False),
            rows_written=rows_written,
            message=message,
            completed_at=utc_now(),
        )
        return {
            "status": status,
            "rows_written": rows_written,
            "actual_sources": actual_sources,
            "attempts": attempts,
        }

    def _normalize_records(
        self,
        outcome: dict[str, Any],
        *,
        symbol: str,
        interval: str,
        batch_id: str,
        acquisition_mode: str,
    ) -> list[dict[str, Any]]:
        source = str(outcome["actual_source"])
        unit, multiplier = _volume_policy(source, symbol)
        result: list[dict[str, Any]] = []
        for record in outcome.get("records") or []:
            timestamp = (
                record.get("trade_date") or record.get("datetime") or record.get("date")
                or record.get("timestamp")
            )
            close = _float(record.get("close"))
            if timestamp is None or close is None:
                continue
            bar_time, session_date = _normalize_bar_time(timestamp, symbol, interval, source)
            raw_volume = _float(record.get("volume"))
            volume = raw_volume * multiplier if raw_volume is not None and unit != "unknown" else None
            amount = _float(record.get("amount"))
            vwap = amount / volume if amount is not None and volume not in (None, 0) else None
            quality_flags: list[str] = []
            if unit == "unknown" and raw_volume is not None:
                quality_flags.append("volume_unit_unknown")
            payload = {
                "bar_time": bar_time,
                "open": _float(record.get("open")),
                "high": _float(record.get("high")),
                "low": _float(record.get("low")),
                "close": close,
                "raw_volume": raw_volume,
                "amount": amount,
            }
            result.append(
                {
                    "symbol": symbol.upper(),
                    "interval": interval,
                    "bar_time": bar_time,
                    "session_date": session_date,
                    "requested_source": outcome["requested_source"],
                    "actual_source": source,
                    "adapter_name": outcome["adapter_name"],
                    "source_fingerprint": outcome["source_fingerprint"],
                    "acquisition_mode": acquisition_mode,
                    "requested_adjustment": outcome["requested_adjustment"],
                    "actual_adjustment": outcome["actual_adjustment"],
                    "adjustment_confidence": outcome["adjustment_confidence"],
                    "open": payload["open"],
                    "high": payload["high"],
                    "low": payload["low"],
                    "close": close,
                    "volume": volume,
                    "raw_volume": raw_volume,
                    "volume_unit": "share" if volume is not None else unit,
                    "amount": amount,
                    "vwap": vwap,
                    "retrieved_at": utc_now(),
                    "batch_id": batch_id,
                    "payload_hash": _json_hash(payload),
                    "quality_flags": json.dumps(quality_flags),
                }
            )
        return sorted(result, key=lambda row: row["bar_time"])

    def _derive_five_minute_bars(self, symbol: str, batch_id: str) -> int:
        minute_rows = self.store.source_bars(symbol, "1m", "raw")
        if not minute_rows:
            return 0
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in minute_rows:
            bucket = pd.Timestamp(row["bar_time"]).floor("5min").isoformat()
            grouped[(str(row["actual_source"]), bucket)].append(row)
        derived: list[dict[str, Any]] = []
        for (_, bucket), rows in grouped.items():
            rows.sort(key=lambda item: item["bar_time"])
            first, last = rows[0], rows[-1]
            highs = [float(row["high"]) for row in rows if row["high"] is not None]
            lows = [float(row["low"]) for row in rows if row["low"] is not None]
            volumes = [float(row["volume"]) for row in rows if row["volume"] is not None]
            amounts = [float(row["amount"]) for row in rows if row["amount"] is not None]
            volume = sum(volumes) if len(volumes) == len(rows) else None
            amount = sum(amounts) if len(amounts) == len(rows) else None
            payload = {
                "bar_time": bucket,
                "open": first["open"], "high": max(highs) if highs else None,
                "low": min(lows) if lows else None, "close": last["close"],
                "volume": volume, "amount": amount,
            }
            derived.append(
                {
                    "symbol": symbol.upper(), "interval": "5m", "bar_time": bucket,
                    "session_date": first["session_date"],
                    "requested_source": first["requested_source"],
                    "actual_source": first["actual_source"],
                    "adapter_name": first["adapter_name"],
                    "source_fingerprint": first["source_fingerprint"],
                    "acquisition_mode": "derived_1m",
                    "requested_adjustment": "raw", "actual_adjustment": "raw",
                    "adjustment_confidence": first["adjustment_confidence"],
                    "open": payload["open"], "high": payload["high"], "low": payload["low"],
                    "close": payload["close"], "volume": volume,
                    "raw_volume": None, "volume_unit": "share" if volume is not None else "unknown",
                    "amount": amount,
                    "vwap": amount / volume if amount is not None and volume not in (None, 0) else None,
                    "retrieved_at": utc_now(), "batch_id": batch_id,
                    "payload_hash": _json_hash(payload), "quality_flags": "[]",
                }
            )
        return self.store.upsert_source_bars(derived)

    def _recompute_consensus(
        self,
        symbol: str,
        interval: str,
        adjustment: str,
        batch_id: str,
        *,
        reused_observation_keys: set[tuple[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        reused_observation_keys = reused_observation_keys or set()

        def is_current(row: dict[str, Any]) -> bool:
            return bool(
                row.get("batch_id") == batch_id
                or (
                    str(row.get("source_fingerprint")),
                    str(row.get("bar_time")),
                ) in reused_observation_keys
            )

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        source_rows = self.store.source_bars(symbol, interval, adjustment)
        for row in source_rows:
            grouped[row["bar_time"]].append(row)
        if not grouped:
            return []
        tick_size = float(self.store.instrument(symbol).get("tick_size") or 0.001)
        latest_bar_time = max(grouped)
        current_latest_by_source: dict[str, str] = {}
        for row in source_rows:
            if not is_current(row):
                continue
            fingerprint = str(row["source_fingerprint"])
            current_latest_by_source[fingerprint] = max(
                str(row["bar_time"]), current_latest_by_source.get(fingerprint, "")
            )
        current_max_time = max(current_latest_by_source.values(), default=None)
        lagging_sources = {
            fingerprint
            for fingerprint, current_latest in current_latest_by_source.items()
            if current_max_time is not None and current_latest < current_max_time
        }
        rows: list[dict[str, Any]] = []
        for bar_time, candidates in grouped.items():
            independent: dict[str, dict[str, Any]] = {}
            for candidate in candidates:
                fingerprint = str(candidate["source_fingerprint"])
                previous = independent.get(fingerprint)
                if previous is None or str(candidate["retrieved_at"]) >= str(previous["retrieved_at"]):
                    independent[fingerprint] = candidate
            all_observations = list(independent.values())
            current_observations = [row for row in all_observations if is_current(row)]
            network_observations = [
                row for row in current_observations if row.get("batch_id") == batch_id
            ]
            is_latest = bar_time == latest_bar_time
            settled = _session_is_settled(str(all_observations[0]["session_date"]), symbol)
            current_only = bool(is_latest and current_observations and not settled)
            observations = current_observations if current_only else all_observations
            closes = [float(row["close"]) for row in observations if row["close"] is not None]
            if not closes:
                continue
            price_spread = _spread(closes)
            tolerance_pct = max((2 * tick_size / statistics.median(closes) * 100), 0.2 if interval != "1D" else 0.5)
            flags: list[str] = []
            if current_only and len(all_observations) > len(current_observations):
                flags.append("stale_observation_excluded")
            elif (
                is_latest
                and settled
                and current_observations
                and len(all_observations) > len(current_observations)
            ):
                flags.append("cached_confirmation_reused")
            if current_observations and not network_observations:
                flags.append("fresh_source_cache_reused")

            included = list(observations)
            if len(observations) == 1:
                if is_latest and lagging_sources:
                    status = "source_lag"
                    flags.append("source_lag")
                elif is_latest and len(all_observations) > len(observations):
                    status = "provisional_mix"
                    flags.append("provisional_mix")
                else:
                    status = "single_source"
            elif price_spread is not None and price_spread <= tolerance_pct:
                status = "verified"
            else:
                agreeing_pairs = [
                    (left, right)
                    for left_index, left in enumerate(observations)
                    for right in observations[left_index + 1:]
                    if _within_tolerance(
                        float(left["close"]), float(right["close"]),
                        interval=interval, tick_size=tick_size,
                    )
                ]
                if len(observations) >= 3 and agreeing_pairs:
                    pair = min(
                        agreeing_pairs,
                        key=lambda value: abs(float(value[0]["close"]) - float(value[1]["close"])),
                    )
                    included = [pair[0], pair[1]]
                    closes = [float(row["close"]) for row in included]
                    price_spread = _spread(closes)
                    status = "verified"
                    flags.append("outlier_excluded")
                else:
                    included = []
                    status = "unresolved_conflict"
                    flags.append("no_source_majority")

            local_now = datetime.now(_market_timezone(symbol))
            if (
                interval == "1D"
                and observations[0]["session_date"] == local_now.date().isoformat()
                and local_now.time() < datetime.strptime("15:10", "%H:%M").time()
            ):
                flags.append("forming_bar")

            volumes = [float(row["volume"]) for row in included if row["volume"] is not None]
            amounts = [float(row["amount"]) for row in included if row["amount"] is not None]
            volume_spread = _spread(volumes) if included and len(volumes) == len(included) else None
            amount_spread = _spread(amounts) if included and len(amounts) == len(included) else None
            if volume_spread is not None and volume_spread > 5:
                flags.append("volume_conflict")
            if amount_spread is not None and amount_spread > 5:
                flags.append("amount_conflict")

            representative = max(included, key=lambda row: str(row["retrieved_at"])) if included else None

            public_observations = [
                {
                    "symbol": row["symbol"],
                    "requested_source": row["requested_source"],
                    "actual_source": row["actual_source"],
                    "source": row["actual_source"],
                    "source_fingerprint": row["source_fingerprint"],
                    "date": row["bar_time"],
                    "open": row["open"], "high": row["high"], "low": row["low"], "close": row["close"],
                    "volume": row["volume"], "raw_volume": row["raw_volume"],
                    "volume_unit": row["volume_unit"], "amount": row["amount"], "vwap": row["vwap"],
                    "requested_adjustment": row["requested_adjustment"],
                    "actual_adjustment": row["actual_adjustment"],
                    "adjustment": row["actual_adjustment"],
                    "adjustment_confidence": row["adjustment_confidence"],
                    "acquisition_mode": row["acquisition_mode"],
                    "retrieved_at": row["retrieved_at"],
                    "batch_id": row["batch_id"],
                    "included_in_consensus": row in included,
                    "exclude_reason": (
                        None if row in included
                        else "stale_observation" if row not in observations
                        else "price_outlier" if "outlier_excluded" in flags
                        else "no_source_majority"
                    ),
                }
                for row in all_observations
            ]
            volume = float(representative["volume"]) if representative and representative["volume"] is not None else None
            amount = float(representative["amount"]) if representative and representative["amount"] is not None else None
            rows.append(
                {
                    "symbol": symbol.upper(), "interval": interval, "bar_time": bar_time,
                    "session_date": observations[0]["session_date"], "adjustment": adjustment,
                    "open": representative["open"] if representative else None,
                    "high": representative["high"] if representative else None,
                    "low": representative["low"] if representative else None,
                    "close": representative["close"] if representative else None,
                    "volume": volume, "amount": amount,
                    "vwap": amount / volume if amount is not None and volume not in (None, 0) else None,
                    "status": status, "price_spread_pct": price_spread,
                    "volume_spread_pct": volume_spread, "amount_spread_pct": amount_spread,
                    "source_count": len(included),
                    "sources_json": json.dumps([row["actual_source"] for row in included]),
                    "observations_json": json.dumps(public_observations, ensure_ascii=False),
                    "quality_flags": json.dumps(flags),
                    "verified_at": utc_now() if network_observations else max(
                        str(row["retrieved_at"]) for row in all_observations
                    ),
                    "batch_id": batch_id if network_observations else max(
                        all_observations, key=lambda row: str(row["retrieved_at"])
                    )["batch_id"],
                }
            )
        self.store.replace_consensus(rows)
        return rows

    def _update_portfolio(self, symbols: list[str]) -> None:
        state = load_state()
        quotes = {row["symbol"]: row for row in self.store.list_quotes(symbols)}
        summaries = self.store.cache_summaries(limit=1000)
        latest_raw: dict[str, dict[str, Any]] = {}
        for row in summaries:
            if row.get("actual_adjustment") != "raw":
                continue
            current = latest_raw.get(row["symbol"])
            if current is None or str(row.get("bar_time")) > str(current.get("bar_time")):
                latest_raw[row["symbol"]] = row
        changed = False
        for holding in state.holdings:
            symbol = str(holding.get("symbol") or holding.get("code") or "").upper()
            if symbol not in symbols:
                continue
            quote = quotes.get(symbol)
            diagnostic = latest_raw.get(symbol)
            if diagnostic and diagnostic.get("status") in {
                "unresolved_conflict", "source_lag", "provisional_mix", "basis_mismatch",
                "stale", "unresolved",
            } and (
                not quote or str(diagnostic.get("bar_time")) >= str(quote.get("bar_time"))
            ):
                holding["market_status"] = diagnostic.get("status")
                holding["market_verified_at"] = diagnostic.get("verified_at")
                holding["market_spread_pct"] = diagnostic.get("spread_pct")
                changed = True
                continue
            if not quote:
                if diagnostic:
                    holding["market_status"] = diagnostic.get("status")
                    holding["market_verified_at"] = diagnostic.get("verified_at")
                    changed = True
                continue
            close = _float(quote.get("last_price"))
            if close is None or quote.get("status") not in {"verified", "single_source"}:
                continue
            quantity = _float(holding.get("quantity"))
            cost_price = _float(holding.get("cost_price"))
            holding["last_price"] = close
            holding["market_status"] = quote["status"]
            holding["market_spread_pct"] = quote.get("price_spread_pct")
            holding["market_verified_at"] = quote["verified_at"]
            holding["market_adjustment"] = quote["adjustment"]
            holding["market_interval"] = quote["interval"]
            holding["market_sources"] = quote["sources"]
            if quantity is not None:
                holding["market_value"] = close * quantity
                if cost_price is not None:
                    holding["pnl"] = (close - cost_price) * quantity
                    holding["pnl_pct"] = ((close - cost_price) / cost_price * 100) if cost_price else None
            changed = True
        if changed:
            save_state(state)

    def _write_compatibility_summaries(self, symbols: list[str]) -> None:
        self.summary_dir.mkdir(parents=True, exist_ok=True)
        for item in self.store.cache_summaries(limit=1000):
            if item["symbol"] not in symbols:
                continue
            payload = dict(item)
            payload["requested_adjustment"] = item["actual_adjustment"]
            payload["consensus_close"] = item["consensus_close"]
            path = self.summary_dir / (
                f"{self._safe_name(item['symbol'])}__{item['interval']}__adj-{item['actual_adjustment']}.json"
            )
            self._atomic_json(path, payload)
            if item["interval"] == "1D":
                legacy = self.summary_dir / f"{self._safe_name(item['symbol'])}__adj-{item['actual_adjustment']}.json"
                self._atomic_json(legacy, payload)

    @staticmethod
    def _safe_name(symbol: str) -> str:
        return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in symbol)

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            handle.write(text)
            tmp_name = handle.name
        Path(tmp_name).replace(path)


_service: MarketRefreshService | None = None
_service_path: str | None = None


def get_market_refresh_service() -> MarketRefreshService:
    global _service, _service_path
    current_path = str(os.getenv("VIBE_TRADING_MARKET_CACHE_DB") or "")
    if _service is None or _service_path != current_path:
        _service = MarketRefreshService()
        _service_path = current_path
    return _service
