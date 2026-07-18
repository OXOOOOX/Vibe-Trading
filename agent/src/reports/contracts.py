"""Serializable contracts for evidence-backed equity deep research.

The report pipeline keeps source evidence, extracted facts, deterministic
calculations, and narrative claims as separate records.  These dataclasses are
intentionally dependency-free so they can be used by tools, API routes, and
tests without importing the LLM runtime.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


ValidationStatus = Literal[
    "pass", "warning", "fail", "not_comparable", "insufficient_data"
]
QualityStatus = Literal["passed", "passed_with_gaps", "failed_validation"]
ModuleStatus = Literal[
    "pending", "running", "passed", "warning", "failed_validation",
    "insufficient_evidence", "not_requested",
]
ClaimType = Literal["fact", "calculation", "inference", "opinion", "data_gap"]
RevisionMode = Literal["initial", "full_refresh", "section_revision", "repair"]
PipelineState = Literal[
    "collecting", "drafting_sections", "compiling", "auditing", "repairing",
    "published", "diagnostic", "technical_failed", "cancelled",
]
DeliveryKind = Literal["report", "diagnostic"]


def utc_now() -> str:
    """Return a compact, timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class EvidenceItem:
    evidence_id: str
    symbol: str
    domain: str
    source: str
    source_locator: str
    retrieved_at: str
    published_at: str | None
    content_hash: str
    summary: str
    status: str = "verified"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DerivedFinancialFact:
    fact_id: str
    symbol: str
    metric: str
    value: str | None
    unit: str
    period: str
    formula: str | None
    input_fact_ids: list[str]
    evidence_ids: list[str]
    calculation_version: str
    validation_status: ValidationStatus
    statement_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ClaimItem:
    claim_id: str
    text: str
    claim_type: ClaimType
    evidence_ids: list[str] = field(default_factory=list)
    fact_ids: list[str] = field(default_factory=list)
    material: bool = True
    section_id: str | None = None


@dataclass(slots=True)
class ReportSection:
    """One compiler-owned section body in a Deep Report workspace."""

    section_id: str
    body_markdown: str
    source_report_id: str | None = None
    source_revision: int | None = None
    content_hash: str = ""
    fact_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    status: Literal["draft", "passed", "stale", "failed_validation"] = "draft"
    validation_issues: list[str] = field(default_factory=list)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReportSection":
        return cls(**data)


@dataclass(slots=True)
class FinancialPeriod:
    period_end: str
    period_type: Literal["annual", "quarter", "ytd"]
    fiscal_year: int | None
    statement_type: Literal["balance", "income", "cashflow", "indicators"]
    source_evidence_id: str
    values: dict[str, str | None]
    raw_update_at: str | None = None


@dataclass(slots=True)
class FinancialCoverage:
    required_fields: list[str]
    present_fields: list[str]
    missing_fields: list[str]
    comparable_periods: dict[str, int]
    provider_status: str
    coverage_ratio: float


@dataclass(slots=True)
class FinancialSnapshot:
    symbol: str
    security_name: str
    market: str
    report_currency: str
    data_as_of: str
    periods: list[FinancialPeriod]
    coverage: FinancialCoverage
    superseded_evidence_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FinancialAlert:
    alert_id: str
    rule: str
    severity: Literal["review", "elevated"]
    periods: list[str]
    fact_ids: list[str]
    consecutive: bool
    finding: str
    normal_explanations: list[str]
    next_checks: list[str]
    wording_guard: str = "异常信号仅用于进一步核查，不构成财务造假判断。"


@dataclass(slots=True)
class ModuleResult:
    status: ModuleStatus
    coverage: float | None = None
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DeepReportRecord:
    schema_version: int = 2
    report_id: str = field(default_factory=lambda: f"report_{uuid.uuid4().hex[:16]}")
    session_id: str = ""
    attempt_id: str = ""
    profile: str = "equity_deep_research"
    symbol: str = ""
    security_name: str = ""
    report_date: str = ""
    data_as_of: str = ""
    quality_status: QualityStatus = "passed_with_gaps"
    status: Literal["running", "completed", "failed", "cancelled"] = "running"
    analysis_modules: dict[str, ModuleResult] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    validation_issues: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    revision: int = 1
    parent_report_id: str | None = None
    request_content: str = ""
    generation_source: str = "manual"
    generation_reason: str = ""
    revision_mode: RevisionMode = "initial"
    revision_sections: list[str] = field(default_factory=list)
    pipeline_state: PipelineState = "collecting"
    repair_round: int = 0
    delivery_kind: DeliveryKind = "report"
    latest_revision_id: str | None = None
    research_coverage: dict[str, Any] = field(default_factory=dict)
    history_delta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeepReportRecord":
        payload = dict(data)
        legacy_failed_validation = (
            payload.get("quality_status") == "failed_validation"
            and "delivery_kind" not in payload
        )
        if legacy_failed_validation:
            # Pre-v2 quality failures stored the diagnostic text in report.md and
            # advertised it as a formal Markdown/PDF. Normalize the public view
            # without rewriting the immutable historical manifest.
            payload["delivery_kind"] = "diagnostic"
            payload.setdefault("pipeline_state", "diagnostic")
            normalized_artifacts: list[dict[str, Any]] = []
            for raw in payload.get("artifacts") or []:
                item = dict(raw) if isinstance(raw, dict) else {}
                if item.get("artifact_id") == "markdown":
                    item["artifact_id"] = "diagnostic"
                    item["artifact_role"] = "diagnostic"
                    item["previewable"] = True
                    normalized_artifacts.append(item)
            payload["artifacts"] = normalized_artifacts
        payload.setdefault("latest_revision_id", payload.get("report_id"))
        payload["analysis_modules"] = {
            str(key): value if isinstance(value, ModuleResult) else ModuleResult(**value)
            for key, value in dict(payload.get("analysis_modules") or {}).items()
        }
        return cls(**payload)
