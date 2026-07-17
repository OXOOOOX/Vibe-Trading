"""Nasdaq chart loader for no-auth US-equity intraday price bars.

Nasdaq's public quote page exposes the current session as one price sample per
minute.  The endpoint is independent from Yahoo's chart API and therefore can
act as the second price observation used by the market-cache quorum.  It does
not expose per-minute OHLCV, so 1-minute samples use the observed price for all
four price fields and leave volume unknown; 5-minute bars are aggregated from
those samples.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

from backtest.loaders._http import (
    resolve_min_interval,
    throttled_get_json,
    throttled_urllib_get_json,
)
from backtest.loaders.base import cached_loader_fetch, validate_date_range
from backtest.loaders.registry import register

logger = logging.getLogger(__name__)

_CHART_URL = "https://api.nasdaq.com/api/quote/{symbol}/chart"
_HISTORICAL_URL = "https://api.nasdaq.com/api/quote/{symbol}/historical"
_MIN_INTERVAL_ENV = "VIBE_TRADING_NASDAQ_MIN_INTERVAL"
_DEFAULT_MIN_INTERVAL = 1.0
_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
}


def _to_nasdaq_symbol(code: str) -> str | None:
    """Return Nasdaq's bare ticker for a project US-equity symbol."""
    upper = str(code or "").strip().upper()
    if not upper.endswith(".US"):
        return None
    symbol = upper[:-3]
    return symbol or None


def _chart_to_frame(
    chart: object,
    *,
    start_date: str,
    end_date: str,
    interval: str,
) -> pd.DataFrame | None:
    """Normalize Nasdaq minute price samples into project price bars."""
    if not isinstance(chart, list):
        return None

    rows: list[dict[str, Any]] = []
    for point in chart:
        if not isinstance(point, dict):
            continue
        try:
            # Nasdaq encodes the displayed ET wall clock as epoch milliseconds
            # (for example ``x`` decodes to 04:00 UTC while ``z.dateTime`` says
            # 04:00 AM ET).  Treat it as an ET clock label instead of a real
            # UTC instant, otherwise every bar is shifted four/five hours.
            timestamp = (
                pd.to_datetime(int(point["x"]), unit="ms", utc=True)
                .tz_localize(None)
                .tz_localize("America/New_York")
            )
            price = float(point["y"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        rows.append(
            {
                "trade_date": timestamp,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": None,
            }
        )
    if not rows:
        return None

    frame = pd.DataFrame(rows).set_index("trade_date").sort_index()
    local_dates = frame.index.tz_convert("America/New_York").date
    lower = pd.Timestamp(start_date).date()
    upper = pd.Timestamp(end_date).date()
    frame = frame[(local_dates >= lower) & (local_dates <= upper)]
    if frame.empty:
        return None

    if interval == "5m":
        prices = frame["close"].resample("5min")
        frame = pd.DataFrame(
            {
                "open": prices.first(),
                "high": prices.max(),
                "low": prices.min(),
                "close": prices.last(),
            }
        ).dropna(subset=["open", "high", "low", "close"])
        frame["volume"] = None
    frame.index.name = "trade_date"
    return frame


def _number(value: object) -> float:
    """Parse Nasdaq display numbers such as ``$317.31`` and ``43,257,800``."""
    cleaned = str(value or "").replace("$", "").replace(",", "").strip()
    return float(cleaned)


def _history_to_frame(
    rows: object, *, start_date: str, end_date: str
) -> pd.DataFrame | None:
    """Normalize Nasdaq historical-table rows into daily OHLCV bars."""
    if not isinstance(rows, list):
        return None
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            normalized.append(
                {
                    "trade_date": pd.to_datetime(str(row["date"]), format="%m/%d/%Y"),
                    "open": _number(row["open"]),
                    "high": _number(row["high"]),
                    "low": _number(row["low"]),
                    "close": _number(row["close"]),
                    "volume": _number(row["volume"]),
                }
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    if not normalized:
        return None
    frame = pd.DataFrame(normalized).set_index("trade_date").sort_index()
    lower = pd.Timestamp(start_date)
    upper = pd.Timestamp(end_date) + pd.Timedelta(days=1)
    frame = frame[(frame.index >= lower) & (frame.index < upper)]
    frame.index.name = "trade_date"
    return frame if not frame.empty else None


def _get_json(
    url: str, *, params: dict[str, Any], headers: dict[str, str], timeout: float
) -> Any:
    """Fetch Nasdaq JSON, retrying reset pooled connections once via urllib."""
    try:
        return throttled_get_json(
            url,
            host_key="nasdaq",
            min_interval=resolve_min_interval(_MIN_INTERVAL_ENV, _DEFAULT_MIN_INTERVAL),
            params=params,
            headers=headers,
            timeout=timeout,
        )
    except (requests.ConnectionError, requests.Timeout):
        return throttled_urllib_get_json(
            url,
            host_key="nasdaq",
            min_interval=resolve_min_interval(_MIN_INTERVAL_ENV, _DEFAULT_MIN_INTERVAL),
            params=params,
            headers=headers,
            timeout=timeout,
        )


@register
class DataLoader:
    """Fetch current-session US-equity price samples from Nasdaq."""

    name = "nasdaq"
    markets = {"us_equity"}
    requires_auth = False

    def __init__(self) -> None:
        self.transport_events: list[dict[str, object]] = []

    def is_available(self) -> bool:
        return True

    def fetch(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        *,
        interval: str = "1m",
        fields: Optional[List[str]] = None,
        request_timeout_s: float | None = None,
        strict: bool = False,
    ) -> Dict[str, pd.DataFrame]:
        del fields
        validate_date_range(start_date, end_date)
        if interval not in {"1m", "5m", "1D"}:
            raise ValueError("Nasdaq loader supports only 1m, 5m, and 1D intervals")

        result: Dict[str, pd.DataFrame] = {}
        for code in codes:
            try:
                frame = cached_loader_fetch(
                    source=self.name,
                    symbol=code,
                    timeframe=interval,
                    start_date=start_date,
                    end_date=end_date,
                    fields=None,
                    fetch=lambda code=code: self._fetch_one(
                        code, start_date, end_date, interval, request_timeout_s
                    ),
                )
                if frame is not None and not frame.empty:
                    result[code] = frame
            except Exception as exc:  # noqa: BLE001 - preserve batch-loader contract
                logger.warning("nasdaq failed for %s: %s", code, exc)
                if strict:
                    raise
        return result

    @staticmethod
    def _fetch_one(
        code: str,
        start_date: str,
        end_date: str,
        interval: str,
        request_timeout_s: float | None,
    ) -> pd.DataFrame | None:
        symbol = _to_nasdaq_symbol(code)
        if symbol is None:
            return None
        timeout = max(1.0, min(float(request_timeout_s or 15.0), 15.0))
        headers = {
            **_HEADERS,
            "Referer": f"https://www.nasdaq.com/market-activity/stocks/{symbol.lower()}",
        }
        if interval == "1D":
            payload = _get_json(
                _HISTORICAL_URL.format(symbol=symbol),
                params={
                    "assetclass": "stocks",
                    "fromdate": start_date,
                    "todate": end_date,
                    "limit": "5000",
                },
                headers={**headers, "Referer": f"{headers['Referer']}/historical"},
                timeout=timeout,
            )
            data = payload.get("data") if isinstance(payload, dict) else None
            table = data.get("tradesTable") if isinstance(data, dict) else None
            rows = table.get("rows") if isinstance(table, dict) else None
            return _history_to_frame(rows, start_date=start_date, end_date=end_date)

        payload = _get_json(
            _CHART_URL.format(symbol=symbol),
            params={"assetclass": "stocks"},
            headers=headers,
            timeout=timeout,
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        chart = data.get("chart") if isinstance(data, dict) else None
        return _chart_to_frame(
            chart,
            start_date=start_date,
            end_date=end_date,
            interval=interval,
        )
