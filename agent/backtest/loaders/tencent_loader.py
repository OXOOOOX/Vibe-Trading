"""Tencent Finance loader: free, no-auth A-share data via HTTP API.

Uses Tencent's ifzq.gtimg.cn API which is not blocked by eastmoney's CDN.
Covers: A-shares (SH/SZ).  No API token required.

API format:
  https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh601595,day,2026-06-01,2026-06-13,500,qfq
"""

from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional

import pandas as pd

from backtest.loaders.base import cached_loader_fetch, validate_date_range
from backtest.loaders.registry import register

logger = logging.getLogger(__name__)

_BASE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_MINUTE_URL = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"


def _is_a_share(code: str) -> bool:
    return code.upper().endswith((".SZ", ".SH"))


@register
class DataLoader:
    """Tencent Finance A-share OHLCV loader (free, HTTP, no auth)."""

    name = "tencent"
    markets = {"a_share"}
    requires_auth = False

    def is_available(self) -> bool:
        """Always available — uses plain HTTP."""
        return True

    def __init__(self) -> None:
        pass

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
        validate_date_range(start_date, end_date)
        if interval not in {"1D", "1m", "5m"}:
            raise ValueError("Tencent loader supports intervals 1D, 1m, and 5m")
        if adjustment not in {"raw", "qfq"}:
            raise ValueError("Tencent loader adjustment must be 'raw' or 'qfq'")
        if interval in {"1m", "5m"} and adjustment != "raw":
            raise ValueError("Tencent intraday data is available only with adjustment='raw'")

        result: Dict[str, pd.DataFrame] = {}
        for code in codes:
            try:
                df = cached_loader_fetch(
                    source=self.name,
                    symbol=code,
                    timeframe=interval,
                    start_date=start_date,
                    end_date=end_date,
                    fields=[f"adjustment:{adjustment}"],
                    fetch=lambda code=code: self._fetch_one(
                        code, start_date, end_date, adjustment, interval, request_timeout_s
                    ),
                )
                if df is not None and not df.empty:
                    result[code] = df
            except Exception as exc:
                logger.warning("tencent failed for %s: %s", code, exc)
        return result

    def _fetch_one(
        self, code: str, start_date: str, end_date: str, adjustment: str = "qfq", interval: str = "1D",
        request_timeout_s: float | None = None,
    ) -> Optional[pd.DataFrame]:
        if not _is_a_share(code):
            return None

        parts = code.upper().split(".")
        symbol = parts[0]
        suffix = parts[1] if len(parts) > 1 else ""

        if suffix == "SH":
            tencent_code = f"sh{symbol}"
        elif suffix == "SZ":
            tencent_code = f"sz{symbol}"
        else:
            return None

        if interval in {"1m", "5m"}:
            return self._fetch_intraday(tencent_code, start_date, end_date, interval, request_timeout_s)

        adjustment_token = "qfq" if adjustment == "qfq" else ""
        url = f"{_BASE_URL}?param={tencent_code},day,{start_date},{end_date},1000,{adjustment_token}"

        import urllib.request
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://web.ifzq.gtimg.cn/",
        })
        with urllib.request.urlopen(req, timeout=_request_timeout(request_timeout_s)) as resp:
            raw = resp.read().decode("utf-8")

        data = json.loads(raw)
        # Response: {"code":0,"data":{"sh601595":{"day":[["2026-06-01","21.32",...], ...]}}}
        stock_data = data.get("data", {})
        if not stock_data:
            return None

        # Get the first (only) key
        stock_key = next(iter(stock_data), None)
        if not stock_key:
            return None

        key_order = ("qfqday", "day") if adjustment == "qfq" else ("day",)
        klines = next((stock_data[stock_key].get(key) for key in key_order if stock_data[stock_key].get(key)), None)
        if not klines:
            return None

        # Each row: ["date", "open", "close", "high", "low", "volume"]
        rows = []
        for k in klines:
            if len(k) >= 6:
                rows.append({
                    "trade_date": k[0],
                    "open": float(k[1]),
                    "close": float(k[2]),
                    "high": float(k[3]),
                    "low": float(k[4]),
                    "volume": float(k[5]),
                })

        if not rows:
            return None

        df = pd.DataFrame(rows)
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df = df.set_index("trade_date").sort_index()
        df = df[["open", "high", "low", "close", "volume"]].dropna(
            subset=["open", "high", "low", "close"]
        )
        return df

    @staticmethod
    def _fetch_intraday(
        tencent_code: str, start_date: str, end_date: str, interval: str,
        request_timeout_s: float | None = None,
    ) -> Optional[pd.DataFrame]:
        import urllib.parse
        import urllib.request

        url = f"{_MINUTE_URL}?{urllib.parse.urlencode({'code': tencent_code})}"
        request = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://gu.qq.com/",
        })
        with urllib.request.urlopen(request, timeout=_request_timeout(request_timeout_s)) as response:
            payload = json.loads(response.read().decode("utf-8"))

        instrument = (payload.get("data") or {}).get(tencent_code) or {}
        minute_payload = instrument.get("data") or {}
        session_date = str(minute_payload.get("date") or "")
        if len(session_date) != 8:
            return None
        date_text = f"{session_date[:4]}-{session_date[4:6]}-{session_date[6:]}"
        if date_text < start_date or date_text > end_date:
            return None

        rows: list[dict[str, float | str]] = []
        previous_volume = 0.0
        previous_amount = 0.0
        for raw in minute_payload.get("data") or []:
            parts = str(raw).split()
            if len(parts) < 4:
                continue
            time_text = parts[0]
            if not ("0930" <= time_text <= "1130" or "1300" <= time_text <= "1500"):
                continue
            price = float(parts[1])
            cumulative_volume = float(parts[2])
            cumulative_amount = float(parts[3])
            volume = max(0.0, cumulative_volume - previous_volume)
            amount = max(0.0, cumulative_amount - previous_amount)
            previous_volume = cumulative_volume
            previous_amount = cumulative_amount
            rows.append({
                "trade_date": f"{date_text} {time_text[:2]}:{time_text[2:]}",
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": volume,
                "amount": amount,
            })
        if not rows:
            return None
        frame = pd.DataFrame(rows)
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
        frame = frame.set_index("trade_date").sort_index()
        if interval == "1m":
            return frame

        grouped = frame.groupby(frame.index.floor("5min"))
        output = grouped.agg({
            "open": "first", "high": "max", "low": "min", "close": "last",
            "volume": "sum", "amount": "sum",
        })
        output.index.name = "trade_date"
        return output


def _request_timeout(request_timeout_s: float | None) -> float:
    """Clamp a caller-provided live-refresh budget to a safe socket timeout."""
    return max(1.0, min(float(request_timeout_s or 15.0), 15.0))
