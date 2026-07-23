"""Structured user portfolio state.

PersistentMemory is intentionally prose-oriented. This module keeps the
account facts that should not be inferred from old chat text: exact symbols,
holdings, cash, and recent trades.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.portfolio.ledger import get_ledger


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


_BARE_US_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{0,9}(?:\.[A-Z])?$")


def normalize_symbol(code: str) -> str:
    """Normalize common portfolio codes to source-ready symbols."""
    text = str(code or "").strip().upper().replace(" ", "")
    if not text:
        return text
    if re.search(r"\.(SH|SZ|BJ|US|HK)$", text, re.I):
        return text
    if _BARE_US_TICKER_RE.fullmatch(text):
        return f"{text}.US"
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
    ledger_events: list[dict[str, Any]] = field(default_factory=list)
    cash: float | None = None
    cash_currency: str = "CNY"
    updated_at: str | None = None
    schema_version: int = 2
    revision: int = 0
    provenance: dict[str, Any] = field(default_factory=dict)
    performance: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PortfolioState":
        holdings = [dict(item) for item in (payload.get("holdings") or [])]
        for holding in holdings:
            symbol = normalize_symbol(str(holding.get("symbol") or holding.get("code") or ""))
            if symbol:
                holding["symbol"] = symbol
        recent_trades = [dict(item) for item in (payload.get("recent_trades") or [])]
        for trade in recent_trades:
            symbol = normalize_symbol(str(trade.get("symbol") or trade.get("code") or ""))
            if symbol:
                trade["symbol"] = symbol
        for index, trade in enumerate(recent_trades):
            trade.setdefault("trade_id", _legacy_trade_id(trade, index))
        return cls(
            holdings=holdings,
            recent_trades=recent_trades,
            ledger_events=[dict(item) for item in (payload.get("ledger_events") or [])],
            cash=_number(payload.get("cash")),
            cash_currency=str(payload.get("cash_currency") or "CNY"),
            updated_at=payload.get("updated_at"),
            schema_version=int(payload.get("schema_version") or 2),
            revision=int(payload.get("revision") or 0),
            provenance=dict(payload.get("provenance") or {}),
            performance=dict(payload.get("performance") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "holdings": self.holdings,
            "recent_trades": self.recent_trades,
            "ledger_events": self.ledger_events,
            "cash": self.cash,
            "cash_currency": self.cash_currency,
            "updated_at": self.updated_at,
            "schema_version": self.schema_version,
            "revision": self.revision,
            "provenance": self.provenance,
            "performance": self.performance,
        }


def _legacy_trade_id(trade: dict[str, Any], index: int) -> str:
    """Return a stable id for trade rows created before ids were introduced."""
    payload = {key: value for key, value in trade.items() if key != "trade_id"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return f"legacy-{index}-{hashlib.sha256(encoded).hexdigest()[:16]}"


def load_state(path: Path | None = None) -> PortfolioState:
    projection = path or state_path()
    return PortfolioState.from_dict(get_ledger(projection).snapshot())


def save_state(
    state: PortfolioState,
    path: Path | None = None,
    *,
    event_type: str = "manual_adjustment",
    event_payload: dict[str, Any] | None = None,
    source: str = "direct_api",
    expected_revision: int | None = None,
    idempotency_key: str | None = None,
) -> Path:
    """Commit a snapshot through the v2 ledger and refresh the JSON projection."""

    projection = path or state_path()
    committed = get_ledger(projection).replace_snapshot(
        state.to_dict(),
        event_type=event_type,
        event_payload=dict(event_payload or {}),
        source=source,
        expected_revision=state.revision if expected_revision is None else expected_revision,
        idempotency_key=idempotency_key,
    )
    refreshed = PortfolioState.from_dict(committed)
    state.__dict__.update(refreshed.__dict__)
    return projection


def update_holdings(
    *,
    raw_text: str | None = None,
    holdings: list[dict[str, Any]] | None = None,
    cash: float | None = None,
    cash_currency: str = "CNY",
    path: Path | None = None,
    attempt_id: str | None = None,
    expected_revision: int | None = None,
    idempotency_key: str | None = None,
) -> PortfolioState:
    projection = path or state_path()
    ledger = get_ledger(projection)
    pending = ledger.pending_snapshot(attempt_id) if attempt_id else None
    state = PortfolioState.from_dict(pending) if pending else load_state(projection)
    parsed = parse_holdings_text(raw_text or "") if raw_text else []
    supplied = []
    for item in holdings or []:
        normalized = dict(item)
        code = normalized.get("code") or normalized.get("symbol") or ""
        normalized["symbol"] = normalize_symbol(str(code))
        normalized.setdefault("code", str(code).split(".")[0])
        normalized.setdefault("updated_at", _now())
        normalized.setdefault("source", "broker_snapshot" if raw_text else "tool_payload")
        normalized.setdefault("cost_basis_kind", "broker_adjusted" if raw_text else "unknown")
        normalized.setdefault("lot_completeness", "incomplete")
        normalized.setdefault(
            "provenance",
            {"source": normalized["source"], "as_of": normalized["updated_at"]},
        )
        supplied.append(normalized)
    for row in parsed:
        row.setdefault("cost_basis_kind", "broker_adjusted")
        row.setdefault("lot_completeness", "incomplete")
        row.setdefault("provenance", {"source": row.get("source"), "as_of": row.get("updated_at")})
    new_holdings = parsed + supplied
    if new_holdings:
        state.holdings = new_holdings
    if cash is not None:
        state.cash = _number(cash)
        state.cash_currency = cash_currency or state.cash_currency
    event_payload = {
        "event_id": uuid.uuid4().hex,
        "holding_count": len(state.holdings),
        "cash": state.cash,
        "cash_currency": state.cash_currency,
        "source_label": "broker_pasted_text" if raw_text else "structured_payload",
        "recorded_at": _now(),
    }
    if attempt_id:
        staged = ledger.stage(
            attempt_id=attempt_id,
            action="update_holdings",
            payload=event_payload,
            preview=state.to_dict(),
            expected_revision=state.revision if expected_revision is None else expected_revision,
            idempotency_key=idempotency_key or f"{attempt_id}:update_holdings:{uuid.uuid4().hex}",
        )
        preview_state = PortfolioState.from_dict(staged["preview"])
        preview_state.provenance["pending_mutation_id"] = staged["mutation_id"]
        return preview_state
    save_state(
        state,
        projection,
        event_type="broker_snapshot",
        event_payload=event_payload,
        source="broker_snapshot" if raw_text else "direct_api",
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
    )
    return state


def update_cash(
    *,
    cash: float,
    cash_currency: str = "CNY",
    path: Path | None = None,
) -> PortfolioState:
    """Update manually confirmed account cash without touching holdings."""

    amount = _number(cash)
    if amount is None or amount < 0:
        raise ValueError("Cash must be a non-negative number.")
    currency = str(cash_currency or "CNY").strip().upper()
    if not currency:
        raise ValueError("Cash currency is required.")

    state = load_state(path)
    state.cash = amount
    state.cash_currency = currency
    save_state(
        state,
        path,
        event_type="manual_adjustment",
        event_payload={
            "field": "cash",
            "cash": amount,
            "cash_currency": currency,
            "recorded_at": _now(),
        },
        source="user_confirmed_cash",
    )
    return state


def _recalculate_holding_totals(holding: dict[str, Any]) -> None:
    """Keep derived holding values consistent after a quantity/cost change."""
    quantity = _number(holding.get("quantity"))
    cost_price = _number(holding.get("cost_price"))
    last_price = _number(holding.get("last_price"))
    if quantity is None or last_price is None:
        holding["market_value"] = None
        holding["pnl"] = None
        holding["pnl_pct"] = None
        return

    holding["market_value"] = quantity * last_price
    if cost_price is None:
        holding["pnl"] = None
        holding["pnl_pct"] = None
        return

    holding["pnl"] = quantity * (last_price - cost_price)
    holding["pnl_pct"] = ((last_price - cost_price) / cost_price * 100.0) if cost_price else None


def edit_holding(
    *,
    symbol: str,
    quantity: float | None = None,
    cost_price: float | None = None,
    path: Path | None = None,
) -> PortfolioState:
    """Manually correct the current quantity and/or cost without creating a trade."""
    normalized_symbol = normalize_symbol(symbol)
    if not normalized_symbol:
        raise ValueError("Holding requires a symbol.")
    if quantity is None and cost_price is None:
        raise ValueError("Provide quantity or cost price to update.")

    next_quantity = _number(quantity) if quantity is not None else None
    next_cost = _number(cost_price) if cost_price is not None else None
    if next_quantity is not None and next_quantity <= 0:
        raise ValueError("Holding quantity must be greater than zero.")
    if next_cost is not None and next_cost <= 0:
        raise ValueError("Holding cost price must be greater than zero.")

    state = load_state(path)
    holding = next(
        (
            item
            for item in state.holdings
            if normalize_symbol(str(item.get("symbol") or item.get("code") or "")) == normalized_symbol
        ),
        None,
    )
    if holding is None:
        raise ValueError(f"Holding {normalized_symbol} was not found.")

    if next_quantity is not None:
        holding["quantity"] = next_quantity
    if next_cost is not None:
        holding["cost_price"] = next_cost
    holding["updated_at"] = _now()
    holding["manual_adjustment_at"] = holding["updated_at"]
    _recalculate_holding_totals(holding)
    save_state(
        state,
        path,
        event_type="manual_adjustment",
        event_payload={
            "symbol": normalized_symbol,
            "quantity": next_quantity,
            "cost_price": next_cost,
            "cost_basis_kind": "broker_adjusted" if next_cost is not None else holding.get("cost_basis_kind"),
            "recorded_at": _now(),
        },
        source="user_holding_correction",
    )
    return state


def delete_trade(*, trade_id: str, path: Path | None = None) -> PortfolioState:
    """Append a reversal marker; immutable applied events are never deleted."""
    target = str(trade_id or "").strip()
    if not target:
        raise ValueError("Trade id is required.")

    projection = path or state_path()
    return PortfolioState.from_dict(get_ledger(projection).reverse_event(target))


def record_trade(
    *,
    trade: dict[str, Any],
    path: Path | None = None,
    attempt_id: str | None = None,
    expected_revision: int | None = None,
    idempotency_key: str | None = None,
) -> PortfolioState:
    projection = path or state_path()
    ledger = get_ledger(projection)
    pending = ledger.pending_snapshot(attempt_id) if attempt_id else None
    state = PortfolioState.from_dict(pending) if pending else load_state(projection)
    entry = dict(trade)
    code = str(entry.get("code") or "").strip().upper()
    name = str(entry.get("name") or "").strip()
    symbol = str(entry.get("symbol") or "").strip()
    if not (re.fullmatch(r"\d{6}", code) or _BARE_US_TICKER_RE.fullmatch(code.upper())):
        raise ValueError("Trade requires a complete A-share code or U.S. ticker.")
    if not name:
        raise ValueError("Trade requires a security name.")
    if not symbol:
        raise ValueError("Trade requires a complete security symbol.")
    normalized_symbol = normalize_symbol(str(symbol))
    if normalized_symbol != normalize_symbol(code):
        raise ValueError("Trade symbol does not match the security code.")

    side_aliases = {"buy": "buy", "买": "buy", "买入": "buy", "sell": "sell", "卖": "sell", "卖出": "sell"}
    side = side_aliases.get(str(entry.get("side") or "").strip().lower())
    if side is None:
        raise ValueError("Trade side must be buy or sell.")

    quantity = _number(entry.get("quantity"))
    price = _number(entry.get("price"))
    if quantity is None or quantity <= 0:
        raise ValueError("Trade quantity must be greater than zero.")
    if price is None or price <= 0:
        raise ValueError("Trade price must be greater than zero.")

    entry["trade_id"] = str(entry.get("trade_id") or uuid.uuid4().hex)
    entry["code"] = code
    entry["name"] = name
    entry["symbol"] = normalized_symbol
    entry["side"] = side
    entry["quantity"] = quantity
    entry["price"] = price
    entry["fees"] = _number(entry.get("fees")) or 0.0
    entry["taxes"] = _number(entry.get("taxes")) or 0.0
    entry["broker_reported_pnl"] = _number(entry.get("broker_reported_pnl"))
    entry["exactness"] = (
        "broker_reported" if entry["broker_reported_pnl"] is not None else "unavailable"
    )
    entry.setdefault("recorded_at", _now())

    holding_index = next(
        (
            index
            for index, holding in enumerate(state.holdings)
            if normalize_symbol(str(holding.get("symbol") or holding.get("code") or "")) == normalized_symbol
        ),
        None,
    )
    entry["pre_trade_lot_completeness"] = (
        str(state.holdings[holding_index].get("lot_completeness") or "incomplete")
        if holding_index is not None
        else "complete"
    )

    if side == "buy":
        if holding_index is None:
            holding = {
                "name": entry.get("name") or normalized_symbol,
                "code": str(entry.get("code") or normalized_symbol).split(".")[0],
                "symbol": normalized_symbol,
                "quantity": quantity,
                "cost_price": price,
                "last_price": None,
                "market_value": None,
                "pnl": None,
                "pnl_pct": None,
                "market_status": "unresolved",
                "source": "recorded_trade",
                "cost_basis_kind": "trade_lot",
                "lot_completeness": "complete",
                "provenance": {"source": "recorded_trade", "as_of": entry["recorded_at"]},
                "updated_at": _now(),
            }
            state.holdings.append(holding)
        else:
            holding = state.holdings[holding_index]
            current_quantity = _number(holding.get("quantity")) or 0.0
            current_cost = _number(holding.get("cost_price"))
            next_quantity = current_quantity + quantity
            holding["quantity"] = next_quantity
            holding["cost_price"] = (
                ((current_quantity * current_cost) + (quantity * price)) / next_quantity
                if current_cost is not None and current_quantity > 0
                else price
            )
            if entry.get("name") and not holding.get("name"):
                holding["name"] = entry["name"]
            holding["updated_at"] = _now()
            holding["last_trade_at"] = entry["recorded_at"]
            holding["lot_completeness"] = entry["pre_trade_lot_completeness"]
            _recalculate_holding_totals(holding)
    else:
        if holding_index is None:
            raise ValueError(f"Cannot sell {normalized_symbol}: it is not in current holdings.")
        holding = state.holdings[holding_index]
        current_quantity = _number(holding.get("quantity")) or 0.0
        if quantity > current_quantity:
            raise ValueError(
                f"Cannot sell {quantity:g} of {normalized_symbol}: current holding is {current_quantity:g}."
            )
        next_quantity = current_quantity - quantity
        if next_quantity <= 1e-12:
            state.holdings.pop(holding_index)
        else:
            holding["quantity"] = next_quantity
            holding["updated_at"] = _now()
            holding["last_trade_at"] = entry["recorded_at"]
            _recalculate_holding_totals(holding)

    entry["applied_to_holdings"] = True
    state.recent_trades.insert(0, entry)
    state.recent_trades = state.recent_trades[:200]
    if attempt_id:
        staged = ledger.stage(
            attempt_id=attempt_id,
            action="record_trade",
            payload=entry,
            preview=state.to_dict(),
            expected_revision=state.revision if expected_revision is None else expected_revision,
            idempotency_key=idempotency_key or f"{attempt_id}:record_trade:{entry['trade_id']}",
        )
        preview_state = PortfolioState.from_dict(staged["preview"])
        preview_state.provenance["pending_mutation_id"] = staged["mutation_id"]
        preview_state.provenance["pending_attempt_id"] = attempt_id
        return preview_state
    save_state(
        state,
        projection,
        event_type="trade",
        event_payload=entry,
        source=str(entry.get("source") or "user_recorded_trade"),
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
    )
    return state


def clear_state(
    path: Path | None = None,
    *,
    attempt_id: str | None = None,
    expected_revision: int | None = None,
    idempotency_key: str | None = None,
) -> Path:
    """Clear through an auditable adjustment instead of deleting ledger files."""

    projection = path or state_path()
    current = load_state(projection)
    empty = PortfolioState(
        cash_currency=current.cash_currency,
        revision=current.revision,
        provenance=dict(current.provenance),
        updated_at=_now(),
    )
    ledger = get_ledger(projection)
    payload = {"reason": "explicit_clear", "recorded_at": _now()}
    if attempt_id:
        ledger.stage(
            attempt_id=attempt_id,
            action="clear",
            payload=payload,
            preview=empty.to_dict(),
            expected_revision=current.revision if expected_revision is None else expected_revision,
            idempotency_key=idempotency_key or f"{attempt_id}:clear",
        )
        return projection
    save_state(
        empty,
        projection,
        event_type="manual_adjustment",
        event_payload=payload,
        source="explicit_clear",
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
    )
    return projection


def commit_attempt_mutations(attempt_id: str, path: Path | None = None) -> PortfolioState:
    projection = path or state_path()
    return PortfolioState.from_dict(get_ledger(projection).commit_attempt(attempt_id))


def discard_attempt_mutations(attempt_id: str, path: Path | None = None) -> int:
    projection = path or state_path()
    return get_ledger(projection).discard_attempt(attempt_id)


def preview_reconciliation(
    *,
    raw_text: str | None = None,
    holdings: list[dict[str, Any]] | None = None,
    trades: list[dict[str, Any]] | None = None,
    cash: float | None = None,
    cash_currency: str = "CNY",
    broker_reported_pnl: float | None = None,
    source_label: str = "broker_snapshot",
    path: Path | None = None,
) -> dict[str, Any]:
    """Create a read-only diff; no portfolio fact changes before commit."""

    projection = path or state_path()
    ledger = get_ledger(projection)
    current = load_state(projection)
    parsed = parse_holdings_text(raw_text or "") if raw_text else []
    supplied: list[dict[str, Any]] = []
    for item in holdings or []:
        row = dict(item)
        code = str(row.get("code") or row.get("symbol") or "")
        row["symbol"] = normalize_symbol(code)
        row.setdefault("code", code.split(".")[0])
        row.setdefault("source", "broker_reconciliation")
        row.setdefault("updated_at", _now())
        supplied.append(row)
    target_holdings = parsed + supplied
    if not target_holdings:
        raise ValueError("No recognizable broker holdings were supplied.")
    for row in target_holdings:
        row["cost_basis_kind"] = "broker_adjusted"
        row["lot_completeness"] = "incomplete"
        row["provenance"] = {"source": source_label, "as_of": row.get("updated_at") or _now()}

    current_by_symbol = {
        normalize_symbol(str(item.get("symbol") or item.get("code") or "")): item
        for item in current.holdings
    }
    target_by_symbol = {
        normalize_symbol(str(item.get("symbol") or item.get("code") or "")): item
        for item in target_holdings
    }
    diffs: list[dict[str, Any]] = []
    for symbol in sorted(set(current_by_symbol) | set(target_by_symbol)):
        before = current_by_symbol.get(symbol)
        after = target_by_symbol.get(symbol)
        changes: dict[str, Any] = {}
        for field_name in ("quantity", "cost_price"):
            old = _number((before or {}).get(field_name))
            new = _number((after or {}).get(field_name))
            if old != new:
                changes[field_name] = {"current": old, "broker": new}
        if before is None or after is None or changes:
            diffs.append(
                {
                    "symbol": symbol,
                    "status": "added" if before is None else "removed" if after is None else "changed",
                    "changes": changes,
                }
            )

    existing_ids = {
        str(item.get("event_id") or item.get("trade_id") or "")
        for item in current.ledger_events
    }
    incoming_ids = {
        str(item.get("event_id") or item.get("trade_id") or "")
        for item in (trades or [])
        if item.get("event_id") or item.get("trade_id")
    }
    suspicious = [
        {
            "event_id": str(item.get("event_id") or item.get("trade_id") or ""),
            "reason": "known_cancelled_attempt_candidate",
            "action": "review_only_no_auto_delete",
        }
        for item in current.ledger_events
        if str(item.get("event_id") or item.get("trade_id") or "").startswith("553a08a")
    ]
    reported = _number(broker_reported_pnl)
    computed = _number(current.performance.get("realized_pnl"))
    unexplained = (reported - computed) if reported is not None and computed is not None else None
    target_state = {
        **current.to_dict(),
        "holdings": target_holdings,
        "cash": current.cash if cash is None else _number(cash),
        "cash_currency": cash_currency or current.cash_currency,
    }
    preview = {
        "base_revision": current.revision,
        "holding_diffs": diffs,
        "missing_ledger_event_ids": sorted(incoming_ids - existing_ids),
        "extra_ledger_event_ids": sorted(existing_ids - incoming_ids) if incoming_ids else [],
        "suspicious_events": suspicious,
        "broker_reported_pnl": reported,
        "computed_realized_pnl": computed,
        "unexplained_pnl": unexplained,
        "pnl_status": "broker_reported" if reported is not None else "unavailable",
        "requires_explicit_commit": True,
        "target_state": target_state,
    }
    request = {
        "raw_text": raw_text,
        "holdings": holdings or [],
        "trades": trades or [],
        "cash": cash,
        "cash_currency": cash_currency,
        "broker_reported_pnl": reported,
        "source_label": source_label,
    }
    return ledger.create_reconciliation(request, preview)


def commit_reconciliation(
    reconciliation_id: str,
    *,
    expected_revision: int,
    path: Path | None = None,
) -> dict[str, Any]:
    projection = path or state_path()
    return get_ledger(projection).commit_reconciliation(
        reconciliation_id,
        expected_revision,
    )
