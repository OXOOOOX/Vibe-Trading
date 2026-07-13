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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from src.market_data import get_loader
from src.market_verification import normalize_adjustment, source_adjustment_policy, verified_cache_dir
from src.portfolio.state import load_state, save_state

from .storage import MarketCacheStore, utc_now


FetchSource = Callable[..., dict[str, Any]]
TERMINAL_RUN_STATUSES = {"completed", "partial", "failed", "interrupted"}
PROFILE_ITEMS: tuple[tuple[str, str, int, int], ...] = (
    ("1m", "raw", 10, 5),
    ("5m", "raw", 35, 20),
    ("1D", "raw", 1100, 750),
    ("1D", "qfq", 1100, 750),
)


def _market_timezone(symbol: str) -> ZoneInfo:
    upper = symbol.upper()
    if upper.endswith((".SH", ".SZ", ".BJ", ".HK")):
        return ZoneInfo("Asia/Shanghai")
    return ZoneInfo("UTC")


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


def _normalize_bar_time(value: Any, symbol: str) -> tuple[str, str]:
    timestamp = pd.Timestamp(value)
    market_tz = _market_timezone(symbol)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(market_tz)
    local = timestamp.tz_convert(market_tz)
    utc = timestamp.tz_convert(timezone.utc)
    return utc.isoformat(), local.date().isoformat()


def _volume_policy(source: str, symbol: str) -> tuple[str, float]:
    is_a_share = symbol.upper().endswith((".SH", ".SZ", ".BJ"))
    if is_a_share and source in {"tencent", "eastmoney"}:
        return "lot", 100.0
    if is_a_share and source == "mootdx":
        return "share", 1.0
    return "unknown", 1.0


def _actual_adjustment(source: str, symbol: str, requested: str) -> tuple[str, str]:
    requested = normalize_adjustment(requested)
    if source in {"tencent", "eastmoney"} and requested in {"raw", "qfq"}:
        return requested, "explicit_request"
    if source == "mootdx" and requested == "raw":
        return "raw", "loader_contract"
    policy = source_adjustment_policy(source, symbol)
    return str(policy["adjustment"]), str(policy["confidence"])


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
    loader_cls = get_loader(requested_source)
    loader = loader_cls()
    actual_source = str(getattr(loader, "name", requested_source))
    kwargs: dict[str, Any] = {"interval": interval}
    if "fields" in inspect.signature(loader.fetch).parameters:
        kwargs["fields"] = ["amount"]
    if "adjustment" in inspect.signature(loader.fetch).parameters:
        kwargs["adjustment"] = adjustment
    if request_timeout_s is not None and "request_timeout_s" in inspect.signature(loader.fetch).parameters:
        kwargs["request_timeout_s"] = request_timeout_s
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
    actual_adjustment, confidence = _actual_adjustment(actual_source, symbol, adjustment)
    return {
        "requested_source": requested_source,
        "actual_source": actual_source,
        "adapter_name": f"{loader_cls.__module__}.{loader_cls.__name__}",
        "source_fingerprint": actual_source,
        "requested_adjustment": normalize_adjustment(adjustment),
        "actual_adjustment": actual_adjustment,
        "adjustment_confidence": confidence,
        "records": records,
    }


def source_candidates(symbol: str, interval: str, adjustment: str) -> list[str]:
    upper = symbol.upper()
    if upper.endswith((".SH", ".SZ", ".BJ")):
        if interval in {"1m", "5m"}:
            return ["eastmoney", "tencent", "mootdx"]
        if adjustment == "qfq":
            return ["tencent", "eastmoney", "akshare"]
        return ["eastmoney", "tencent", "mootdx"]
    if upper.endswith(".US"):
        return ["yahoo", "stooq"]
    if upper.endswith(".HK"):
        return ["eastmoney", "yahoo"]
    if upper.endswith("-USDT") or upper.endswith("/USDT"):
        return ["okx", "ccxt"]
    return ["auto"]


def _spread(values: list[float]) -> float | None:
    if len(values) < 2:
        return 0.0 if values else None
    median = statistics.median(values)
    return ((max(values) - min(values)) / median * 100.0) if median else None


class MarketRefreshService:
    def __init__(
        self,
        *,
        store: MarketCacheStore | None = None,
        fetcher: FetchSource = fetch_source_records,
        summary_dir: Path | None = None,
    ) -> None:
        self.store = store or MarketCacheStore()
        self.fetcher = fetcher
        self.summary_dir = summary_dir or verified_cache_dir()

    def prepare_startup(self) -> int:
        return self.store.mark_running_interrupted()

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
        }
        dedupe_payload = {"symbols": normalized, "profile": profile, **config}
        dedupe_key = _json_hash(dedupe_payload)
        active = self.store.find_active_run(dedupe_key)
        if active:
            run = self.store.get_run(active["run_id"])
            assert run is not None
            return run, True

        run_id = uuid.uuid4().hex
        rows = [(symbol, interval, adjustment) for symbol in normalized for interval, adjustment in item_specs]
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
                    if result["status"] == "conflict":
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
                if item.get("status") in {"verified", "single_source", "conflict"}
            ]
            for symbol in set(completed_symbols):
                self.store.refresh_latest_quote(symbol)
            if completed_symbols:
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
        requested_sources = sources or source_candidates(symbol, interval, adjustment)
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
        rows_written = 0
        primary_latest: float | None = None
        needs_third_source = False

        for index, requested_source in enumerate(requested_sources):
            if self._should_stop(deadline, should_cancel):
                message = "refresh deadline reached"
                self.store.update_item(
                    run_id, symbol, interval, adjustment,
                    status="interrupted", message=message, completed_at=utc_now(),
                )
                return {"status": "interrupted", "rows_written": rows_written, "actual_sources": actual_sources}
            if index >= 2 and not needs_third_source:
                break
            self.store.update_run(run_id, current_symbol=symbol, current_source=requested_source)
            checkpoint = index > 0 and not needs_third_source
            fetch_start = (date.fromisoformat(end_date) - timedelta(days=7)).isoformat() if checkpoint else start_date

            if not force and not checkpoint:
                actual_hint = requested_source
                try:
                    actual_hint = str(getattr(get_loader(requested_source), "name", requested_source))
                except Exception:  # The injected test fetcher may use synthetic source names.
                    pass
                tail = self.store.tail_start(
                    symbol, actual_hint, interval, adjustment, 5 if interval != "1D" else 3
                )
                if tail:
                    tail_date = pd.Timestamp(tail).tz_convert(_market_timezone(symbol)).date().isoformat()
                    if tail_date > fetch_start:
                        fetch_start = tail_date

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
                source_errors.append(f"{requested_source}: {exc}")
                continue
            if self._should_stop(deadline, should_cancel):
                self.store.update_item(
                    run_id, symbol, interval, adjustment,
                    status="interrupted", message="refresh deadline reached", completed_at=utc_now(),
                )
                return {"status": "interrupted", "rows_written": rows_written, "actual_sources": actual_sources}
            actual_source = str(probe["actual_source"])
            actual_adjustment = str(probe["actual_adjustment"])
            if actual_adjustment != adjustment:
                continue
            if actual_source in actual_sources:
                continue

            normalized = self._normalize_records(
                probe, symbol=symbol, interval=interval, batch_id=run_id,
                acquisition_mode="checkpoint" if checkpoint else "network",
            )
            if not normalized:
                continue
            latest_close = normalized[-1]["close"]
            if primary_latest is None:
                primary_latest = latest_close
            elif latest_close is not None:
                tick_size = float(self.store.instrument(symbol).get("tick_size") or 0.001)
                price_delta = abs(float(latest_close) - float(primary_latest))
                tolerance = max(2 * tick_size, abs(float(primary_latest)) * (0.002 if interval != "1D" else 0.005))
                needs_third_source = price_delta > tolerance
                if needs_third_source and checkpoint:
                    try:
                        full_fetch_kwargs = {
                            "requested_source": requested_source,
                            "symbol": symbol,
                            "start_date": start_date,
                            "end_date": end_date,
                            "interval": interval,
                            "adjustment": adjustment,
                        }
                        if deadline is not None:
                            full_fetch_kwargs["request_timeout_s"] = max(1.0, min(10.0, deadline - time.monotonic()))
                        full_probe = self.fetcher(**full_fetch_kwargs)
                        if self._should_stop(deadline, should_cancel):
                            self.store.update_item(
                                run_id, symbol, interval, adjustment,
                                status="interrupted", message="refresh deadline reached", completed_at=utc_now(),
                            )
                            return {"status": "interrupted", "rows_written": rows_written, "actual_sources": actual_sources}
                        normalized = self._normalize_records(
                            full_probe, symbol=symbol, interval=interval, batch_id=run_id,
                            acquisition_mode="conflict_backfill",
                        )
                    except Exception as exc:  # noqa: BLE001
                        source_errors.append(f"{requested_source} backfill: {exc}")

            rows_written += self.store.upsert_source_bars(normalized)
            actual_sources.append(actual_source)

        if interval == "5m" and adjustment == "raw":
            rows_written += self._derive_five_minute_bars(symbol, run_id)

        self.store.update_item(run_id, symbol, interval, adjustment, status="verifying")
        consensus = self._recompute_consensus(symbol, interval, adjustment, run_id)
        self.store.prune_sessions(symbol, interval, adjustment, keep_sessions)
        latest = consensus[-1] if consensus else None
        status = str(latest["status"]) if latest else "unresolved"
        message = "; ".join(source_errors) if source_errors else None
        if not latest:
            message = f"No compatible bars were returned{'; ' + message if message else ''}"
        self.store.update_item(
            run_id, symbol, interval, adjustment,
            status=status,
            actual_sources_json=json.dumps(actual_sources),
            rows_written=rows_written,
            message=message,
            completed_at=utc_now(),
        )
        return {"status": status, "rows_written": rows_written, "actual_sources": actual_sources}

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
            bar_time, session_date = _normalize_bar_time(timestamp, symbol)
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
        self, symbol: str, interval: str, adjustment: str, batch_id: str
    ) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self.store.source_bars(symbol, interval, adjustment):
            grouped[row["bar_time"]].append(row)
        tick_size = float(self.store.instrument(symbol).get("tick_size") or 0.001)
        rows: list[dict[str, Any]] = []
        for bar_time, candidates in grouped.items():
            independent: dict[str, dict[str, Any]] = {}
            for candidate in candidates:
                independent[candidate["source_fingerprint"]] = candidate
            all_observations = list(independent.values())
            closes = [float(row["close"]) for row in all_observations if row["close"] is not None]
            if not closes:
                continue
            price_spread = _spread(closes)
            tolerance_pct = max((2 * tick_size / statistics.median(closes) * 100), 0.2 if interval != "1D" else 0.5)
            observations = all_observations
            flags: list[str] = []
            if len(all_observations) >= 3 and price_spread is not None and price_spread > tolerance_pct:
                closest_pair = min(
                    (
                        (left, right)
                        for index, left in enumerate(all_observations)
                        for right in all_observations[index + 1:]
                    ),
                    key=lambda pair: abs(float(pair[0]["close"]) - float(pair[1]["close"])),
                )
                pair_spread = _spread([float(closest_pair[0]["close"]), float(closest_pair[1]["close"])])
                if pair_spread is not None and pair_spread <= tolerance_pct:
                    observations = list(closest_pair)
                    closes = [float(row["close"]) for row in observations]
                    price_spread = pair_spread
                    flags.append("outlier_excluded")
            status = "single_source" if len(closes) == 1 else (
                "verified" if price_spread is not None and price_spread <= tolerance_pct else "conflict"
            )
            volumes = [float(row["volume"]) for row in observations if row["volume"] is not None]
            amounts = [float(row["amount"]) for row in observations if row["amount"] is not None]
            volume_spread = _spread(volumes) if len(volumes) == len(observations) else None
            amount_spread = _spread(amounts) if len(amounts) == len(observations) else None
            if volume_spread is not None and volume_spread > 5:
                flags.append("volume_conflict")
            if amount_spread is not None and amount_spread > 5:
                flags.append("amount_conflict")

            def median_field(field: str) -> float | None:
                values = [float(row[field]) for row in observations if row[field] is not None]
                return statistics.median(values) if values else None

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
                    "included_in_consensus": row in observations,
                    "exclude_reason": None if row in observations else "price_outlier",
                }
                for row in all_observations
            ]
            volume = median_field("volume") if len(volumes) == len(observations) else None
            amount = median_field("amount") if len(amounts) == len(observations) else None
            rows.append(
                {
                    "symbol": symbol.upper(), "interval": interval, "bar_time": bar_time,
                    "session_date": observations[0]["session_date"], "adjustment": adjustment,
                    "open": median_field("open"), "high": median_field("high"),
                    "low": median_field("low"), "close": statistics.median(closes),
                    "volume": volume, "amount": amount,
                    "vwap": amount / volume if amount is not None and volume not in (None, 0) else None,
                    "status": status, "price_spread_pct": price_spread,
                    "volume_spread_pct": volume_spread, "amount_spread_pct": amount_spread,
                    "source_count": len(observations),
                    "sources_json": json.dumps([row["actual_source"] for row in observations]),
                    "observations_json": json.dumps(public_observations, ensure_ascii=False),
                    "quality_flags": json.dumps(flags), "verified_at": utc_now(), "batch_id": batch_id,
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
            if diagnostic and diagnostic.get("status") in {"conflict", "stale", "unresolved"} and (
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
