"""Shared Deep Report workspace and persistence operations for all executors."""

from __future__ import annotations

import ast
from typing import Any, Protocol


class DeepReportRuntimeService(Protocol):
    def inspect_workspace(self, report_id: str, **kwargs: Any) -> dict[str, Any]: ...
    def subject_profile(self, report_id: str) -> dict[str, Any] | None: ...

    def submit_section(
        self,
        report_id: str,
        *,
        section_id: str,
        body_markdown: str,
    ) -> Any: ...

    def submit_monitoring_bundle(
        self,
        report_id: str,
        *,
        monitoring_bundle: dict[str, Any],
    ) -> dict[str, Any]: ...

    def record_research_attempt(self, report_id: str, **kwargs: Any) -> dict[str, Any]: ...

    def attach_analysis(self, report_id: str, analysis: dict[str, Any]) -> Any: ...

    def attach_etf_analysis(self, report_id: str, analysis: dict[str, Any]) -> Any: ...

    def attach_etf_component_selection(
        self, report_id: str, selection: dict[str, Any]
    ) -> Any: ...

    def attach_component_digest_resolution(
        self,
        report_id: str,
        resolution: dict[str, Any],
        materialization: dict[str, Any] | None = None,
    ) -> Any: ...

    def attach_external_evidence(self, report_id: str, bundle: dict[str, Any]) -> Any: ...

    def attach_deterministic_result(
        self,
        report_id: str,
        command: str,
        result: dict[str, Any],
    ) -> Any: ...

    def attach_audit_result(self, report_id: str, result: dict[str, Any]) -> Any: ...


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.startswith("[") and raw.endswith("]"):
            try:
                parsed = ast.literal_eval(raw)
            except (SyntaxError, ValueError):
                parsed = None
            if isinstance(parsed, list):
                return [str(item) for item in parsed if str(item).strip()]
        return [item.strip(" \t'\"") for item in raw.split(",") if item.strip(" \t'\"")]
    return []


def _bool_value(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def handle_report_workspace_command(
    reports: DeepReportRuntimeService,
    report_id: str,
    command: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Execute the report-scoped workspace contract shared by both engines."""

    if command == "inspect":
        fact_metrics = _string_list(payload.get("fact_metrics"))
        evidence_domains = _string_list(payload.get("evidence_domains"))
        return {
            "status": "ok",
            **reports.inspect_workspace(
                report_id,
                section_ids=_string_list(payload.get("section_ids")) or None,
                fact_metrics=fact_metrics or ["__catalog_only__"],
                evidence_domains=evidence_domains or ["__catalog_only__"],
                include_module_statuses=_bool_value(
                    payload.get("include_module_statuses"),
                    True,
                ),
                include_section_bodies=(
                    _bool_value(payload.get("include_section_bodies"))
                    if payload.get("include_section_bodies") is not None
                    else None
                ),
            ),
        }
    if command == "submit_section":
        section = reports.submit_section(
            report_id,
            section_id=str(payload.get("section_id") or ""),
            body_markdown=str(payload.get("body_markdown") or ""),
        )
        return {"status": "ok", "section": section.to_dict()}
    if command == "record_research_attempt":
        return {
            "status": "ok",
            **reports.record_research_attempt(
                report_id,
                task_id=str(payload.get("task_id") or ""),
                outcome=str(payload.get("outcome") or ""),
                query=str(payload.get("query") or ""),
                document_refs=_string_list(payload.get("document_refs")),
                fact_ids=_string_list(payload.get("fact_ids")),
                evidence_ids=_string_list(payload.get("evidence_ids")),
                independence_groups=_string_list(payload.get("independence_groups")),
                covered_years=[
                    int(value) for value in (payload.get("covered_years") or [])
                ],
                detail=str(payload.get("detail") or ""),
            ),
        }
    if command == "submit_monitoring_bundle":
        raw_bundle = payload.get("monitoring_bundle")
        if not isinstance(raw_bundle, dict):
            raise ValueError("monitoring_bundle must be an object")
        return {
            "status": "ok",
            **reports.submit_monitoring_bundle(
                report_id,
                monitoring_bundle=raw_bundle,
            ),
        }
    raise ValueError(f"unknown report workspace command: {command}")


def persist_report_event(
    reports: DeepReportRuntimeService,
    report_id: str,
    profile: str,
    event_type: str,
    data: dict[str, Any],
    *,
    allowed_deterministic_commands: set[str] | None = None,
) -> bool:
    """Persist one structured report event and report whether it was handled."""

    if event_type == "report.analysis_snapshot":
        analysis = data.get("analysis")
        if not isinstance(analysis, dict):
            return False
        if profile == "etf_deep_research":
            reports.attach_etf_analysis(report_id, analysis)
        else:
            reports.attach_analysis(report_id, analysis)
        return True

    if event_type == "report.etf_component_selection":
        selection = data.get("selection")
        if profile != "etf_deep_research" or not isinstance(selection, dict):
            return False
        reports.attach_etf_component_selection(report_id, selection)
        return True

    if event_type == "report.component_digest_resolution":
        resolution = data.get("resolution")
        materialization = data.get("materialization")
        if profile != "etf_deep_research" or not isinstance(resolution, dict):
            return False
        reports.attach_component_digest_resolution(
            report_id,
            resolution,
            materialization if isinstance(materialization, dict) else None,
        )
        return True

    if event_type == "report.external_evidence":
        bundle = data.get("bundle")
        if not isinstance(bundle, dict):
            return False
        reports.attach_external_evidence(report_id, bundle)
        return True

    if event_type == "report.deterministic_result":
        command = str(data.get("command") or "")
        result = data.get("result")
        if not command or not isinstance(result, dict):
            return False
        if (
            allowed_deterministic_commands is not None
            and command not in allowed_deterministic_commands
        ):
            raise ValueError(f"deterministic command is not allowed for {profile}: {command}")
        reports.attach_deterministic_result(report_id, command, result)
        return True

    if event_type == "report.audit_result":
        result = data.get("result")
        if not isinstance(result, dict):
            return False
        reports.attach_audit_result(report_id, result)
        return True

    return False
