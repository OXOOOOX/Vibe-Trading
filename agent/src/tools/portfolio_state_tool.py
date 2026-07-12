"""Tool for maintaining authoritative structured portfolio state."""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools import BaseTool
from src.portfolio.state import clear_state, load_state, record_trade, state_path, update_holdings


class PortfolioStateTool(BaseTool):
    """Read and update exact holdings, cash, and recent trades."""

    name = "portfolio_state"
    description = (
        "Maintain the user's authoritative structured portfolio state: exact "
        "holding names/codes, quantities, cost prices, cash, and recent trades. "
        "Use get before portfolio analysis; use update_holdings when the user "
        "pastes holdings; use record_trade when the user describes a trade."
    )
    repeatable = True
    is_readonly = False
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["get", "update_holdings", "record_trade", "clear"],
                "description": "State operation to perform.",
            },
            "raw_text": {
                "type": "string",
                "description": "Broker-style pasted holdings table text.",
            },
            "holdings": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Structured holding rows when already parsed.",
            },
            "cash": {"type": "number", "description": "Cash balance, if known."},
            "cash_currency": {"type": "string", "default": "CNY"},
            "trade": {
                "type": "object",
                "description": "Recent trade object: symbol/code, side, quantity, price, trade_date, notes.",
            },
        },
        "required": ["action"],
    }

    def execute(self, **kwargs: Any) -> str:
        action = str(kwargs.get("action") or "get")
        if action == "get":
            state = load_state()
        elif action == "update_holdings":
            state = update_holdings(
                raw_text=kwargs.get("raw_text"),
                holdings=kwargs.get("holdings"),
                cash=kwargs.get("cash"),
                cash_currency=str(kwargs.get("cash_currency") or "CNY"),
            )
        elif action == "record_trade":
            state = record_trade(trade=dict(kwargs.get("trade") or {}))
        elif action == "clear":
            path = clear_state()
            return json.dumps({"status": "ok", "path": str(path), "cleared": True}, ensure_ascii=False)
        else:
            return json.dumps({"status": "error", "error": f"unknown action: {action}"}, ensure_ascii=False)

        return json.dumps(
            {"status": "ok", "path": str(state_path()), "state": state.to_dict()},
            ensure_ascii=False,
            indent=2,
        )
