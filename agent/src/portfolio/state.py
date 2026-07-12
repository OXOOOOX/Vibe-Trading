"""Structured user portfolio state.

PersistentMemory is intentionally prose-oriented. This module keeps the
account facts that should not be inferred from old chat text: exact symbols,
holdings, cash, and recent trades.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_path() -> Path:
    override = os.getenv("VIBE_TRADING_PORTFOLIO_STATE_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".vibe-trading" / "portfolio" / "portfolio_state.json"


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace(",", "").replace("%", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def normalize_symbol(code: str) -> str:
    """Normalize common China security codes to source-ready symbols."""
    text = str(code or "").strip().upper().replace(" ", "")
    if not text:
        return text
    if re.search(r"\.(SH|SZ|BJ|US|HK)$", text, re.I):
        return text
    if not re.fullmatch(r"\d{6}", text):
        return text

    # Shanghai-listed stocks, funds, and ETFs.
    if text.startswith(("5", "6", "688")):
        return f"{text}.SH"
    # Beijing exchange.
    if text.startswith(("4", "8")):
        return f"{text}.BJ"
    # Shenzhen-listed stocks, funds, and ETFs.
    if text.startswith(("0", "1", "2", "3")):
        return f"{text}.SZ"
    return text


KNOWN_SECURITY_CODES: dict[str, str] = {
    "招商银行": "600036",
    "招行": "600036",
    "格力电器": "000651",
    "格力": "000651",
    "兴业银行": "601166",
}

PLACEHOLDER_CODE_TEXTS = {"个股无ETF", "无ETF", "个股", "-", "--", "NA", "N/A", "NONE", "UNKNOWN"}

_HOLDING_LINE_RE = re.compile(
    r"^\s*(?P<name>.+?)\s+"
    r"(?P<code>\d{6}(?:\.(?:SH|SZ|BJ))?)\s+"
    r"(?P<quantity>[+-]?[\d,]+(?:\.\d+)?)\s+"
    r"(?P<cost_price>[+-]?[\d,]+(?:\.\d+)?)\s+"
    r"(?P<last_price>[+-]?[\d,]+(?:\.\d+)?)\s+"
    r"(?P<market_value>[+-]?[\d,]+(?:\.\d+)?)\s+"
    r"(?P<pnl>[+-]?[\d,]+(?:\.\d+)?)\s+"
    r"(?P<pnl_pct>[+-]?[\d,]+(?:\.\d+)?)%?\s*$",
    re.I,
)

_SIMPLE_HOLDING_LINE_RE = re.compile(
    r"^\s*(?P<name>.+?)\s+"
    r"(?P<code>\d{6}(?:\.(?:SH|SZ|BJ))?|[^\s]+)\s+"
    r"(?P<quantity>[+-]?[\d,]+(?:\.\d+)?)\s+"
    r"(?P<cost_price>[+-]?[\d,]+(?:\.\d+)?)\s*$",
    re.I,
)


def _infer_code_from_name(name: str) -> str | None:
    compact_name = str(name or "").replace(" ", "")
    for alias, code in KNOWN_SECURITY_CODES.items():
        if alias in compact_name:
            return code
    return None


def _holding_from_item(
    item: dict[str, str],
    *,
    source: str,
    include_market_fields: bool,
) -> dict[str, Any] | None:
    original_code = item["code"].strip().upper()
    inferred_code = None
    code = original_code
    if original_code in PLACEHOLDER_CODE_TEXTS:
        inferred_code = _infer_code_from_name(item["name"])
        if not inferred_code:
            return None
        code = inferred_code

    row: dict[str, Any] = {
        "name": item["name"].strip(),
        "code": code,
        "symbol": normalize_symbol(code),
        "quantity": _number(item["quantity"]),
        "cost_price": _number(item["cost_price"]),
        "source": source,
        "updated_at": _now(),
    }
    if inferred_code:
        row["symbol_inferred"] = True
        row["symbol_inference_source"] = "known_security_alias"
        row["original_code_text"] = original_code
    if include_market_fields:
        row.update(
            {
                "last_price": _number(item["last_price"]),
                "market_value": _number(item["market_value"]),
                "pnl": _number(item["pnl"]),
                "pnl_pct": _number(item["pnl_pct"]),
            }
        )
    else:
        row.update({"last_price": None, "market_value": None, "pnl": None, "pnl_pct": None})
    return row


def parse_holdings_text(raw_text: str) -> list[dict[str, Any]]:
    """Parse broker-style holdings rows from pasted text."""
    parsed: list[dict[str, Any]] = []
    for line in str(raw_text or "").splitlines():
        match = _HOLDING_LINE_RE.match(line)
        include_market_fields = True
        source = "user_pasted_text"
        if not match:
            match = _SIMPLE_HOLDING_LINE_RE.match(line)
            include_market_fields = False
            source = "user_pasted_table"
        if not match:
            continue
        row = _holding_from_item(match.groupdict(), source=source, include_market_fields=include_market_fields)
        if row:
            parsed.append(row)
    return parsed


@dataclass
class PortfolioState:
    holdings: list[dict[str, Any]] = field(default_factory=list)
    recent_trades: list[dict[str, Any]] = field(default_factory=list)
    cash: float | None = None
    cash_currency: str = "CNY"
    updated_at: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PortfolioState":
        return cls(
            holdings=list(payload.get("holdings") or []),
            recent_trades=list(payload.get("recent_trades") or []),
            cash=_number(payload.get("cash")),
            cash_currency=str(payload.get("cash_currency") or "CNY"),
            updated_at=payload.get("updated_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "holdings": self.holdings,
            "recent_trades": self.recent_trades,
            "cash": self.cash,
            "cash_currency": self.cash_currency,
            "updated_at": self.updated_at,
        }


def load_state(path: Path | None = None) -> PortfolioState:
    path = path or state_path()
    if not path.exists():
        return PortfolioState(updated_at=_now())
    return PortfolioState.from_dict(json.loads(path.read_text(encoding="utf-8")))


def save_state(state: PortfolioState, path: Path | None = None) -> Path:
    path = path or state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    state.updated_at = _now()
    payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(payload)
        tmp_name = handle.name
    Path(tmp_name).replace(path)
    return path


def update_holdings(
    *,
    raw_text: str | None = None,
    holdings: list[dict[str, Any]] | None = None,
    cash: float | None = None,
    cash_currency: str = "CNY",
    path: Path | None = None,
) -> PortfolioState:
    state = load_state(path)
    parsed = parse_holdings_text(raw_text or "") if raw_text else []
    supplied = []
    for item in holdings or []:
        normalized = dict(item)
        code = normalized.get("code") or normalized.get("symbol") or ""
        normalized["symbol"] = normalize_symbol(str(code))
        normalized.setdefault("code", str(code).split(".")[0])
        normalized.setdefault("updated_at", _now())
        normalized.setdefault("source", "tool_payload")
        supplied.append(normalized)
    new_holdings = parsed + supplied
    if new_holdings:
        state.holdings = new_holdings
    if cash is not None:
        state.cash = _number(cash)
        state.cash_currency = cash_currency or state.cash_currency
    save_state(state, path)
    return state


def record_trade(*, trade: dict[str, Any], path: Path | None = None) -> PortfolioState:
    state = load_state(path)
    entry = dict(trade)
    symbol = entry.get("symbol") or entry.get("code") or ""
    entry["symbol"] = normalize_symbol(str(symbol))
    entry.setdefault("recorded_at", _now())
    state.recent_trades.insert(0, entry)
    state.recent_trades = state.recent_trades[:200]
    save_state(state, path)
    return state


def clear_state(path: Path | None = None) -> Path:
    path = path or state_path()
    if path.exists():
        path.unlink()
    return path
