"""Eastmoney loader: free, no-auth OHLCV across A-share, HK and US equities.

Eastmoney's ``push2his`` quote endpoints are free and require no token, but the
service rate-limits aggressively by source IP. All HTTP goes through the shared
:mod:`backtest.loaders.eastmoney_client`, which routes every call through the
per-host throttle in :mod:`backtest.loaders._http`. This loader only maps our
symbol/interval/DataFrame conventions onto that client; it owns no HTTP itself.

Symbol routing is delegated to :func:`eastmoney_client.resolve_secid`:

* A-share — ``600519.SH`` / ``000001.SZ`` / ``430139.BJ``
* Hong Kong — ``00700.HK`` (numeric code zero-padded to five digits)
* US — ``AAPL.US`` (market prefix discovered via Eastmoney search, cached)
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import pandas as pd
import requests

from backtest.loaders import eastmoney_client
from backtest.loaders.base import cached_loader_fetch, retry_with_budget, validate_date_range
from backtest.loaders.registry import register

logger = logging.getLogger(__name__)

# Canonical fields; amount is retained when the endpoint provides it.
_OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]
_OPTIONAL_COLUMNS = ["amount"]


def _to_compact_date(value: str) -> str:
    """Convert a ``YYYY-MM-DD`` date into Eastmoney's ``YYYYMMDD`` form.

    Args:
        value: Date string in any pandas-parseable form.

    Returns:
        The date rendered as ``YYYYMMDD``.

    Raises:
        ValueError: ``value`` is not a parseable date.
    """
    try:
        return pd.Timestamp(value).strftime("%Y%m%d")
    except Exception as exc:  # noqa: BLE001 - surfaced as a clear ValueError
        raise ValueError(f"Invalid date for eastmoney: {value!r}") from exc


@register
class DataLoader:
    """Eastmoney OHLCV loader (free, throttled HTTP, no auth)."""

    name = "eastmoney"
    markets = {"a_share", "hk_equity", "us_equity"}
    requires_auth = False

    def is_available(self) -> bool:
        """Always available — uses unauthenticated throttled HTTP."""
        return True

    def fetch(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        *,
        interval: str = "1D",
        fields: Optional[List[str]] = None,
        adjustment: str = "qfq",
        request_timeout_s: float | None = None,
    ) -> Dict[str, pd.DataFrame]:
        """Fetch OHLCV for each symbol; a single failure never aborts the batch.

        Args:
            codes: Symbols such as ``"600519.SH"``, ``"00700.HK"``, ``"AAPL.US"``.
            start_date: Inclusive start date (``YYYY-MM-DD``).
            end_date: Inclusive end date (``YYYY-MM-DD``).
            interval: Bar interval label (e.g. ``"1D"``, ``"1H"``, ``"5m"``).
            fields: Accepted for protocol parity; the OHLCV columns are fixed.

        Returns:
            Mapping ``{symbol: DataFrame}`` for every symbol that yielded bars.
            Each DataFrame has a ``trade_date`` ``DatetimeIndex`` and float
            columns ``open/high/low/close/volume``. Symbols that resolve to no
            data are omitted.

        Raises:
            ValueError: ``start_date``/``end_date`` are malformed or inverted.
        """
        validate_date_range(start_date, end_date)
        if adjustment not in {"raw", "qfq"}:
            raise ValueError("Eastmoney loader adjustment must be 'raw' or 'qfq'")
        include_amount = bool(fields and "amount" in fields)

        result: Dict[str, pd.DataFrame] = {}
        for code in codes:
            try:
                df = cached_loader_fetch(
                    source=self.name,
                    symbol=code,
                    timeframe=interval,
                    start_date=start_date,
                    end_date=end_date,
                    fields=[f"adjustment:{adjustment}", *(fields or [])],
                    fetch=lambda code=code: self._fetch_one(
                        code, start_date, end_date, interval, adjustment, include_amount,
                        request_timeout_s,
                    ),
                )
                if df is not None and not df.empty:
                    result[code] = df
            except Exception as exc:  # noqa: BLE001 - one bad symbol must not abort
                logger.warning("eastmoney failed for %s: %s", code, exc)
        return result

    def _fetch_one(
        self, code: str, start_date: str, end_date: str, interval: str,
        adjustment: str = "qfq", include_amount: bool = False,
        request_timeout_s: float | None = None,
    ) -> Optional[pd.DataFrame]:
        """Resolve one symbol and build its OHLCV frame, or ``None`` on a miss.

        Args:
            code: A single Vibe-Trading symbol.
            start_date: Inclusive start date (``YYYY-MM-DD``).
            end_date: Inclusive end date (``YYYY-MM-DD``).
            interval: Bar interval label.

        Returns:
            An OHLCV DataFrame, or ``None`` when the symbol/interval is
            unsupported or Eastmoney returns no bars.
        """
        klt = eastmoney_client.KLT_BY_INTERVAL.get(interval)
        if klt is None:
            logger.warning("eastmoney unsupported interval %r for %s", interval, code)
            return None

        secid = eastmoney_client.resolve_secid(code)
        if not secid:
            return None

        timeout_s = max(1.0, min(float(request_timeout_s or 20.0), 20.0))
        rows = retry_with_budget(
            lambda: eastmoney_client.fetch_kline(
                secid,
                klt=klt,
                fqt=0 if adjustment == "raw" else 1,
                beg=_to_compact_date(start_date),
                end=_to_compact_date(end_date),
                timeout=timeout_s,
            ),
            transient=requests.RequestException,
            deadline=time.monotonic() + timeout_s,
            label=f"eastmoney {interval} fetch for {code}",
            max_retries=2,
            backoff=(1.0, 2.0),
        )
        return self._frame_from_rows(rows, include_amount=include_amount)

    @staticmethod
    def _frame_from_rows(rows: List[dict], *, include_amount: bool = False) -> Optional[pd.DataFrame]:
        """Assemble the canonical OHLCV DataFrame from client kline rows.

        Args:
            rows: Ascending ``{trade_date, open, high, low, close, volume, ...}``
                dicts from :func:`eastmoney_client.fetch_kline`.

        Returns:
            A DataFrame indexed by ``trade_date`` with float OHLCV columns, or
            ``None`` when ``rows`` is empty or fully unparseable.
        """
        if not rows:
            return None

        df = pd.DataFrame(rows)
        if df.empty or "trade_date" not in df.columns:
            return None

        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df = df.set_index("trade_date").sort_index()
        df.index.name = "trade_date"

        for column in _OHLCV_COLUMNS:
            if column not in df.columns:
                return None
            df[column] = pd.to_numeric(df[column], errors="coerce").astype(float)

        for column in _OPTIONAL_COLUMNS:
            if column in df.columns:
                df[column] = pd.to_numeric(df[column], errors="coerce").astype(float)

        selected = _OHLCV_COLUMNS + [
            column for column in _OPTIONAL_COLUMNS if include_amount and column in df.columns
        ]
        df = df[selected].dropna(subset=["open", "high", "low", "close"])
        if df.empty:
            return None
        return df
