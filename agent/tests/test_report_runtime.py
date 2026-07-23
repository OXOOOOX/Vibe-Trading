"""Shared Deep Report execution and runtime regressions."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.reports.execution import (
    engine_env_updates,
    resolve_deep_research_engine,
)
from src.reports.runtime import handle_report_workspace_command, persist_report_event


@dataclass
class _Section:
    section_id: str

    def to_dict(self) -> dict[str, str]:
        return {"section_id": self.section_id}


class _Reports:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def inspect_workspace(self, report_id: str, **kwargs):
        self.calls.append(("inspect", report_id, kwargs))
        return {"report_id": report_id}

    def submit_section(self, report_id: str, *, section_id: str, body_markdown: str):
        self.calls.append(("submit", report_id, section_id, body_markdown))
        return _Section(section_id)

    def attach_analysis(self, report_id: str, analysis: dict):
        self.calls.append(("analysis", report_id, analysis))

    def attach_etf_analysis(self, report_id: str, analysis: dict):
        self.calls.append(("etf_analysis", report_id, analysis))

    def attach_external_evidence(self, report_id: str, bundle: dict):
        self.calls.append(("evidence", report_id, bundle))

    def attach_deterministic_result(self, report_id: str, command: str, result: dict):
        self.calls.append(("deterministic", report_id, command, result))

    def attach_audit_result(self, report_id: str, result: dict):
        self.calls.append(("audit", report_id, result))


def test_explicit_engine_wins_and_legacy_flag_remains_compatible() -> None:
    assert resolve_deep_research_engine({}) == "provider"
    assert resolve_deep_research_engine({
        "VIBE_TRADING_CODEX_DEEP_RESEARCH_ENABLED": "1",
    }) == "codex_cli"
    assert resolve_deep_research_engine({
        "VIBE_TRADING_DEEP_RESEARCH_ENGINE": "provider",
        "VIBE_TRADING_CODEX_DEEP_RESEARCH_ENABLED": "1",
    }) == "provider"
    assert engine_env_updates("codex_cli") == {
        "VIBE_TRADING_DEEP_RESEARCH_ENGINE": "codex_cli",
        "VIBE_TRADING_CODEX_DEEP_RESEARCH_ENABLED": "1",
    }


def test_workspace_contract_is_shared_and_catalog_first() -> None:
    reports = _Reports()

    inspected = handle_report_workspace_command(
        reports,
        "report_1",
        "inspect",
        {"section_ids": "['summary', 'risks']"},
    )
    submitted = handle_report_workspace_command(
        reports,
        "report_1",
        "submit_section",
        {"section_id": "summary", "body_markdown": "Body"},
    )

    assert inspected == {"status": "ok", "report_id": "report_1"}
    inspect_kwargs = reports.calls[0][2]
    assert inspect_kwargs["section_ids"] == ["summary", "risks"]
    assert inspect_kwargs["fact_metrics"] == ["__catalog_only__"]
    assert inspect_kwargs["evidence_domains"] == ["__catalog_only__"]
    assert submitted["section"] == {"section_id": "summary"}


def test_event_persistence_applies_profile_and_command_policy() -> None:
    reports = _Reports()

    handled = persist_report_event(
        reports,
        "report_1",
        "etf_deep_research",
        "report.analysis_snapshot",
        {"analysis": {"quality_status": "passed"}},
    )

    assert handled is True
    assert reports.calls[-1][0] == "etf_analysis"

    with pytest.raises(ValueError, match="not allowed"):
        persist_report_event(
            reports,
            "report_1",
            "equity_deep_research",
            "report.deterministic_result",
            {"command": "forbidden", "result": {"value": 1}},
            allowed_deterministic_commands={"allowed"},
        )
