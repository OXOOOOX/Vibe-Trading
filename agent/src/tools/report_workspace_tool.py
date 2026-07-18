"""Compiler-owned section workspace for equity Deep Reports."""

from __future__ import annotations

import json
from typing import Any, Callable

from src.agent.tools import BaseTool


class ReportWorkspaceTool(BaseTool):
    """Inspect and submit sections without letting the model own report files."""

    name = "report_workspace"
    description = (
        "Inspect the active Deep Report workspace or submit one validated section. "
        "The service owns headings, compilation, numeric audit, artifacts, and revisions. "
        "Section bodies must not contain H1/H2 headings and every material number must "
        "cite a matching [Fact:<id>] on the same line."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "enum": ["inspect", "submit_section"]},
            "section_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional section filters for inspect.",
            },
            "fact_metrics": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional metric-name filters for inspect.",
            },
            "evidence_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional Evidence domain filters for inspect.",
            },
            "include_module_statuses": {"type": "boolean"},
            "include_section_bodies": {
                "type": "boolean",
                "description": (
                    "Include stored section prose during inspect. Defaults to false for "
                    "full_refresh so stale parent prose does not pollute a new draft."
                ),
            },
            "section_id": {
                "type": "string",
                "enum": [
                    "executive_summary", "business_position", "financial_quality",
                    "accounting_review", "implied_expectations", "terminal_narrative",
                    "counter_thesis", "conclusion_watchlist",
                ],
            },
            "body_markdown": {
                "type": "string",
                "description": "Section body only; H1 and H2 headings are forbidden.",
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
            return json.dumps({
                "status": "error",
                "error": "report workspace is unavailable outside an active Deep Report",
            }, ensure_ascii=False)
        command = str(kwargs.get("command") or "")
        try:
            payload = self.handler(command, dict(kwargs))
        except (KeyError, TypeError, ValueError) as exc:
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
        if command == "submit_section" and self.event_callback is not None:
            self.event_callback("report.workspace_section", {
                "section_id": kwargs.get("section_id"),
                "status": payload.get("status"),
            })
        return json.dumps(payload, ensure_ascii=False, default=str)
