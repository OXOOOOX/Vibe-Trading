"""Single Agent-facing entry point for the unified data layer."""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools import BaseTool
from src.data_layer import get_unified_data_service


class DataContextTool(BaseTool):
    """Retrieve policy-governed market, news, and report context."""

    name = "get_data_context"
    description = (
        "The unified data entry point for market bars, fundamentals, verified "
        "latest prices, news, research reports, and ETF product/share-flow context. "
        "Select a purpose; the system enforces the "
        "minimum interval/history, keeps settled history cache-first, revalidates "
        "mutable prices and news live-first, and labels quorum/conflict/stale "
        "fallback explicitly. Returned bars are the latest 120 contiguous bars. "
        "For full contiguous pages, copy a handle from market.bars_handles[] "
        "(never use request_id) and call action='bars'."
    )
    repeatable = True
    is_readonly = False  # A live request may refresh the local continuity cache.
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["context", "bars"], "default": "context"},
            "symbols": {"type": "array", "items": {"type": "string"}},
            "purpose": {
                "type": "string",
                "enum": ["latest_price", "holding", "premarket", "intraday", "long_term", "backtest"],
                "default": "holding",
            },
            "lookback_days": {"type": "integer", "minimum": 1},
            "include": {"type": "array", "items": {"type": "string", "enum": ["market", "fundamentals", "news", "reports", "etf_product"]}},
            "force_live": {"type": "boolean"},
            "handle": {"type": "string"},
            "cursor": {"type": "integer", "minimum": 0, "default": 0},
        },
        "required": [],
    }

    def execute(self, **kwargs: Any) -> str:
        service = get_unified_data_service()
        action = kwargs.get("action", "context")
        if action == "bars":
            handle = str(kwargs.get("handle") or "").strip()
            if not handle:
                return json.dumps({"status": "error", "error": "handle is required for action='bars'"}, ensure_ascii=False)
            try:
                return json.dumps(service.read_bars(handle, int(kwargs.get("cursor") or 0)), ensure_ascii=False)
            except KeyError as exc:
                return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
        try:
            result = service.get_context(
                symbols=list(kwargs.get("symbols") or []),
                purpose=str(kwargs.get("purpose") or "holding"),
                lookback_days=kwargs.get("lookback_days"),
                include=kwargs.get("include"),
                force_live=kwargs.get("force_live"),
            )
        except ValueError as exc:
            result = {"status": "error", "error": str(exc)}
        return json.dumps(result, ensure_ascii=False)
