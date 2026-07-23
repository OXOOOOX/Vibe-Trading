"""Compiler-owned section workspace for registered Deep Report Profiles."""

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
            "command": {
                "type": "string",
                "enum": [
                    "inspect",
                    "record_research_attempt",
                    "submit_section",
                    "submit_monitoring_bundle",
                ],
            },
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
                "description": "Registered section ID returned by inspect for the active Profile.",
            },
            "body_markdown": {
                "type": "string",
                "description": "Section body only; H1 and H2 headings are forbidden.",
            },
            "monitoring_bundle": {
                "type": "object",
                "description": (
                    "Optional structural monitoring context and 0-6 research/watch candidates. "
                    "It never activates monitoring or permits trade execution."
                ),
            },
            "task_id": {
                "type": "string",
                "description": "Server-owned task ID from research_enrichment in inspect.",
            },
            "outcome": {
                "type": "string",
                "enum": [
                    "evidence_accepted",
                    "no_results",
                    "retrieval_failed",
                    "evidence_rejected",
                    "source_unavailable",
                ],
            },
            "query": {"type": "string"},
            "document_refs": {"type": "array", "items": {"type": "string"}},
            "fact_ids": {"type": "array", "items": {"type": "string"}},
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
            "independence_groups": {"type": "array", "items": {"type": "string"}},
            "covered_years": {"type": "array", "items": {"type": "integer"}},
            "detail": {"type": "string"},
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
        if self.event_callback is not None:
            if command == "submit_section":
                self.event_callback("report.workspace_section", {
                    "section_id": kwargs.get("section_id"),
                    "status": payload.get("status"),
                })
            elif command == "submit_monitoring_bundle":
                self.event_callback("report.monitoring_bundle", {
                    "status": payload.get("status"),
                    "candidate_count": payload.get("candidate_count", 0),
                })
            elif command == "record_research_attempt":
                self.event_callback("report.research_enrichment", {
                    "task_id": kwargs.get("task_id"),
                    "outcome": kwargs.get("outcome"),
                    "status": payload.get("task", {}).get("status"),
                })
        return json.dumps(payload, ensure_ascii=False, default=str)
