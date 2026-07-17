"""Small, throttled client for Tencent Finance's symbol suggestion endpoint."""

from __future__ import annotations

import json
import re

from backtest.loaders._http import resolve_min_interval, throttled_get

_SUGGEST_URL = "https://smartbox.gtimg.cn/s3/"
_HOST_KEY = "tencent"
_MIN_INTERVAL_ENV = "VIBE_TRADING_TENCENT_MIN_INTERVAL"
_DEFAULT_MIN_INTERVAL = 0.2
_HINT_ENVELOPE = re.compile(
    r'^\s*v_hint\s*=\s*("(?:\\.|[^"\\])*")\s*;?\s*$',
    re.DOTALL,
)


def search(query: str, *, timeout: float = 10.0) -> list[str]:
    """Return Tencent smartbox rows for a company name or ticker fragment."""
    response = throttled_get(
        _SUGGEST_URL,
        host_key=_HOST_KEY,
        min_interval=resolve_min_interval(_MIN_INTERVAL_ENV, _DEFAULT_MIN_INTERVAL),
        params={"q": query, "t": "all"},
        headers={"Referer": "https://gu.qq.com/"},
        timeout=timeout,
    )
    response.raise_for_status()
    match = _HINT_ENVELOPE.fullmatch(response.text)
    if match is None:
        raise ValueError("Tencent symbol search returned an invalid response")
    decoded = json.loads(match.group(1))
    return [row for row in decoded.split("^") if row]
