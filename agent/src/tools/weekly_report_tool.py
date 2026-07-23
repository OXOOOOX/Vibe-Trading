"""Session-facing control surface for formal user weekly reports."""

from __future__ import annotations

import json
from typing import Any, Callable

from src.agent.tools import BaseTool


class WeeklyReportTool(BaseTool):
    """Start and inspect formal weekly reports without exposing monitor profiles."""

    name = "weekly_report"
    description = (
        "Start, inspect, or list formal persisted user-facing weekly reports. "
        "Use command=start when the user asks to generate or launch a formal weekly report; "
        "do not substitute an ordinary chat summary. Symbols must use exact normalized codes "
        "such as 000651.SZ or 588870.SH; omit symbols only when the user explicitly asks for "
        "all current holdings. This tool never starts a monitor-facing report, activates "
        "monitoring, delivers files, executes trades, or authorizes extended P4B2 research. "
        "Set force_new or single_source_authorized only when the user explicitly requests it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["start", "get", "list"],
            },
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 50,
                "description": (
                    "Exact normalized symbols for start, for example 000651.SZ. "
                    "Omit only for an explicit all-holdings request."
                ),
            },
            "week_end": {
                "type": "string",
                "description": (
                    "Optional completed trading-week end in YYYY-MM-DD. "
                    "Omit to use the latest completed trading week."
                ),
            },
            "refresh_policy": {
                "type": "string",
                "enum": ["ensure_fresh", "force", "reuse"],
                "default": "ensure_fresh",
            },
            "force_new": {
                "type": "boolean",
                "default": False,
                "description": "Create a new revision instead of reusing an existing run.",
            },
            "single_source_authorized": {
                "type": "boolean",
                "default": False,
                "description": "Explicit user consent to continue with one market-data source.",
            },
            "run_id": {
                "type": "string",
                "description": "Required by command=get.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "default": 20,
            },
        },
        "required": ["command"],
    }
    is_readonly = False
    repeatable = True

    def __init__(
        self,
        handler: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.handler = handler
        self.event_callback = event_callback

    def execute(self, **kwargs: Any) -> str:
        if self.handler is None:
            return json.dumps(
                {"status": "error", "error": "weekly report service is unavailable"},
                ensure_ascii=False,
            )
        command = str(kwargs.get("command") or "").strip().lower()
        try:
            payload = self.handler(command, dict(kwargs))
        except (KeyError, TypeError, ValueError, TimeoutError) as exc:
            return json.dumps(
                {"status": "error", "error": str(exc)}, ensure_ascii=False
            )
        if self.event_callback is not None and command == "start":
            self.event_callback(
                "weekly_report.started",
                {
                    "status": payload.get("status"),
                    "run_ids": [
                        str(item.get("run_id") or "")
                        for item in payload.get("runs") or []
                        if item.get("run_id")
                    ],
                    "report_audience": "user",
                },
            )
        return json.dumps(payload, ensure_ascii=False, default=str)
