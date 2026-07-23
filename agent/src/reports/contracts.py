"""Serializable contracts for evidence-backed equity and ETF deep research.

The report pipeline keeps source evidence, extracted facts, deterministic
calculations, and narrative claims as separate records.  These dataclasses are
intentionally dependency-free so they can be used by tools, API routes, and
tests without importing the LLM runtime.
"""

from __future__ import annotations

import uuid
import re
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
ModuleAvailability = Literal["complete", "partial", "missing", "not_applicable"]
ModuleValidation = Literal["passed", "warning", "failed"]
ClaimType = Literal["fact", "calculation", "inference", "opinion", "data_gap"]
RevisionMode = Literal["initial", "full_refresh", "section_revision", "repair"]
PipelineState = Literal[
    "collecting", "drafting_sections", "compiling", "auditing", "repairing",
    "published", "diagnostic", "technical_failed", "cancelled",
]
DeliveryKind = Literal["report", "diagnostic"]
ReportKind = Literal[
    "deep_research", "daily_holding", "daily_portfolio", "weekly_review",
    "monitor_research", "component_research",
]
ReportSubjectType = Literal["symbol", "portfolio"]
ReportCatalogStatus = Literal["published", "diagnostic", "archived"]
ReportCoverageStatus = Literal["complete", "partial", "insufficient", "unknown"]
ReportHorizon = Literal["intraday", "daily", "weekly", "structural"]
ReportStance = Literal["bullish", "neutral", "bearish", "mixed", "unknown"]
ReportAction = Literal["observe", "add", "reduce", "exit", "not_applicable"]
ReportConfidence = Literal["low", "medium", "high", "unknown"]
ReportRelationType = Literal["revision_of", "supersedes"]
ETFSnapshotType = Literal[
    "identity",
    "universe",
    "market",
    "holder",
    "index_methodology",
    "product_metrics",
    "share_history",
    "peer_group",
]
ETFAnalysisMode = Literal[
    "reuse", "monitor_delta", "partial_refresh", "section_revision", "full_refresh",
]
ETFConcentrationClass = Literal[
    "highly_diversified", "moderately_diversified", "focused", "concentrated",
]
ETFSelectionQuality = Literal["complete", "partial", "insufficient"]
ComponentResearchStatus = Literal[
    "reusable", "partial_reusable", "stale", "missing", "conflicted",
]
ComponentResearchQuality = Literal["complete", "partial", "insufficient"]
ComponentResearchDimension = Literal[
    "business_exposure",
    "earnings_trend",
    "valuation",
    "catalysts",
    "risks",
    "holder_governance",
    "material_events",
]
ComponentResearchGenerationJobStatus = Literal[
    "planned", "blocked", "approved", "running", "published", "failed",
    "skipped", "cancelled",
]
ComponentResearchEvidenceQuality = Literal["complete", "partial", "insufficient"]


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
    availability: ModuleAvailability | None = None
    validation: ModuleValidation | None = None
    reason_code: str | None = None
    missing_items: list[str] = field(default_factory=list)
    narrative_result: dict[str, Any] | None = None
    deterministic_result: dict[str, Any] | None = None
    module_id: str | None = None
    selection_id: str | None = None
    resolution_id: str | None = None
    universe_snapshot_id: str | None = None
    selected_count: int | None = None
    selected_weight_coverage: float | None = None
    explanation_coverage: float | None = None
    research_coverage: float | None = None
    fully_supported_coverage: float | None = None
    reusable_count: int | None = None
    partial_reusable_count: int | None = None
    stale_count: int | None = None
    missing_count: int | None = None
    conflicted_count: int | None = None
    selected_components: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        def legacy_projection(raw: Any) -> dict[str, Any] | None:
            if not isinstance(raw, dict):
                return None
            payload = dict(raw)
            status = str(payload.get("status") or "pending")
            availability = payload.get("availability") or {
                "passed": "complete",
                "warning": "partial",
                "insufficient_evidence": "partial",
                "failed_validation": "missing",
                "not_requested": "not_applicable",
            }.get(status, "missing")
            validation = payload.get("validation") or {
                "passed": "passed",
                "not_requested": "passed",
                "failed_validation": "failed",
            }.get(status, "warning")
            details = dict(payload.get("details") or {})
            for key in ("deterministic_analysis", "narrative_section"):
                nested = legacy_projection(details.pop(key, None))
                if nested:
                    for detail_key, detail_value in dict(nested.get("details") or {}).items():
                        details.setdefault(detail_key, detail_value)
            return {
                "availability": availability,
                "validation": validation,
                "coverage": payload.get("coverage", payload.get("coverage_ratio")),
                "reason_code": payload.get("reason_code") or payload.get("reason"),
                "missing_items": list(payload.get("missing_items") or []),
                "details": details,
            }

        legacy_narrative = self.details.pop("narrative_section", None)
        legacy_deterministic = self.details.pop("deterministic_analysis", None)
        if self.narrative_result is None:
            self.narrative_result = legacy_projection(legacy_narrative)
        if self.deterministic_result is None:
            self.deterministic_result = legacy_projection(legacy_deterministic)
        if self.availability is None:
            self.availability = {
                "passed": "complete",
                "warning": "partial",
                "insufficient_evidence": "partial",
                "failed_validation": "missing",
                "not_requested": "not_applicable",
                "pending": "missing",
                "running": "partial",
            }.get(self.status, "missing")  # type: ignore[assignment]
        if self.validation is None:
            self.validation = {
                "passed": "passed",
                "not_requested": "passed",
                "warning": "warning",
                "insufficient_evidence": "warning",
                "pending": "warning",
                "running": "warning",
                "failed_validation": "failed",
            }.get(self.status, "warning")  # type: ignore[assignment]
        if self.reason_code is None and self.reason:
            self.reason_code = self.reason
        projections = [
            self.details,
            dict((self.deterministic_result or {}).get("details") or {}),
            dict((self.narrative_result or {}).get("details") or {}),
        ]
        for field_name in (
            "module_id", "selection_id", "resolution_id", "universe_snapshot_id",
            "selected_count", "selected_weight_coverage", "explanation_coverage",
            "research_coverage", "fully_supported_coverage", "reusable_count",
            "partial_reusable_count", "stale_count", "missing_count",
            "conflicted_count",
        ):
            if getattr(self, field_name) is not None:
                continue
            for source in projections:
                if source.get(field_name) is not None:
                    setattr(self, field_name, source[field_name])
                    break
        if not self.selected_components:
            for source in projections:
                raw = source.get("selected_components") or source.get("selected")
                if isinstance(raw, list):
                    self.selected_components = [
                        dict(item) for item in raw if isinstance(item, dict)
                    ]
                    break


@dataclass(frozen=True, slots=True)
class ETFResearchSnapshot:
    """Immutable ETF input snapshot stored beside the shared research ledger.

    ``payload`` contains normalized ETF observations only.  Research prose and
    source bodies stay in the existing Fact/Evidence stores and are referenced
    through IDs, avoiding a second fact base.
    """

    snapshot_id: str
    symbol: str
    snapshot_type: ETFSnapshotType
    data_as_of: str
    retrieved_at: str
    coverage_ratio: float
    quality_status: QualityStatus
    content_hash: str
    payload: dict[str, Any] = field(default_factory=dict)
    source_ids: list[str] = field(default_factory=list)
    fact_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    freshness_expires_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ETFResearchSnapshot":
        return cls(**data)


@dataclass(frozen=True, slots=True)
class ETFModuleCacheResult:
    """One reusable deterministic or model-backed ETF analysis module."""

    cache_id: str
    symbol: str
    module_id: str
    input_fingerprint: str
    status: ModuleStatus
    result: dict[str, Any] = field(default_factory=dict)
    profile_version: str = "1.0"
    prompt_version: str = "1.0"
    model_id: str = "deterministic"
    input_tokens: int = 0
    output_tokens: int = 0
    created_at: str = field(default_factory=utc_now)
    expires_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ETFAnalysisDecision:
    """Auditable decision describing whether and how ETF research is refreshed."""

    decision_id: str
    symbol: str
    input_fingerprint: str
    mode: ETFAnalysisMode
    reasons: list[str] = field(default_factory=list)
    changed_snapshot_types: list[ETFSnapshotType] = field(default_factory=list)
    refresh_modules: list[str] = field(default_factory=list)
    stale_sections: list[str] = field(default_factory=list)
    reused_report_id: str | None = None
    token_budget: dict[str, int] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ETFComponentObservation:
    """Normalized, model-free observation for one ETF component."""

    symbol: str
    name: str
    weight: float
    price_contribution: float | None = None
    earnings_contribution: float | None = None
    major_event: bool = False
    evidence_conflict: bool = False
    research_stale: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ETFConcentrationMetrics:
    concentration_class: ETFConcentrationClass
    expected_component_count: int
    observed_component_count: int
    observed_weight_coverage: float
    top1_weight: float
    top3_weight: float
    top5_weight: float
    top10_weight: float
    hhi_lower_bound: float
    hhi_upper_bound: float
    effective_component_count_lower_bound: float
    min_penetration_count: int
    max_penetration_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ETFSelectedComponent:
    symbol: str
    name: str
    weight: float
    score: float
    marginal_explanation_gain: float
    forced: bool
    reasons: list[str] = field(default_factory=list)
    price_contribution: float | None = None
    earnings_contribution: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ETFComponentSelection:
    selection_id: str
    etf_symbol: str
    input_fingerprint: str
    quality: ETFSelectionQuality
    concentration: ETFConcentrationMetrics
    selected: list[ETFSelectedComponent]
    selected_weight_coverage: float
    explanation_coverage: float
    stop_reason: str
    warnings: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ComponentResearchDigest:
    """Canonical, ETF-agnostic references to reusable component knowledge."""

    digest_id: str
    schema_version: int
    component_symbol: str
    security_name: str
    entity_id: str | None
    analysis_as_of: str
    research_data_as_of: str | None
    created_at: str
    freshness_expires_at: str | None
    status: ComponentResearchStatus
    quality: ComponentResearchQuality
    coverage_dimensions: list[ComponentResearchDimension]
    missing_dimensions: list[ComponentResearchDimension]
    stale_dimensions: list[ComponentResearchDimension]
    source_report_ids: list[str]
    claim_ids_by_dimension: dict[str, list[str]]
    fact_ids: list[str]
    evidence_ids: list[str]
    conflict_ids: list[str]
    knowledge_fingerprint: str
    input_fingerprint: str
    claim_selection_reasons: dict[str, list[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    model_id: str = "deterministic"
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ComponentResearchDigest":
        return cls(**data)


@dataclass(frozen=True, slots=True)
class ETFComponentDigestBinding:
    """ETF- and selection-specific context pointing at a global digest."""

    binding_id: str
    etf_symbol: str
    selection_id: str
    component_symbol: str
    component_name: str
    digest_id: str | None
    digest_status: ComponentResearchStatus
    component_weight: float
    selection_score: float
    marginal_explanation_gain: float
    forced: bool
    selection_reasons: list[str]
    price_contribution: float | None
    earnings_contribution: float | None
    selected_rank: int
    selection_data_as_of: str
    created_at: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ETFComponentDigestBinding":
        return cls(**data)


@dataclass(frozen=True, slots=True)
class ComponentDigestResolution:
    """Deterministic P4A-to-knowledge resolution with zero model budget."""

    resolution_id: str
    etf_symbol: str
    selection_id: str
    analysis_as_of: str
    selected_count: int
    reusable_count: int
    partial_reusable_count: int
    stale_count: int
    missing_count: int
    conflicted_count: int
    bindings: list[ETFComponentDigestBinding]
    digest_ids: list[str]
    reuse_ratio: float
    estimated_avoided_model_calls: int
    estimated_avoided_input_tokens: int
    estimated_avoided_output_tokens: int
    estimation_basis: str
    knowledge_fingerprint: str
    input_fingerprint: str
    warnings: list[str] = field(default_factory=list)
    cache_hit: bool = False
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ComponentDigestResolution":
        payload = dict(data)
        payload["bindings"] = [
            item if isinstance(item, ETFComponentDigestBinding)
            else ETFComponentDigestBinding.from_dict(dict(item))
            for item in payload.get("bindings") or []
        ]
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class ComponentResearchGenerationPolicy:
    """Central P4B2 feature gates and hard model/token budgets."""

    policy_version: str
    enabled: bool
    live_run_enabled: bool
    eligible_statuses: list[ComponentResearchStatus]
    allow_partial_reusable: bool
    max_components_per_etf_run: int
    max_components_per_day: int
    max_model_calls_per_component: int
    max_model_calls_per_day: int
    max_input_tokens_per_component: int
    max_output_tokens_per_component: int
    max_input_tokens_per_day: int
    max_output_tokens_per_day: int
    max_auto_repairs: int
    digest_reuse_days: int
    allowed_report_kinds: list[str]
    allowed_security_markets: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ComponentResearchGenerationPolicy":
        return cls(**data)


@dataclass(frozen=True, slots=True)
class ComponentResearchEvidencePack:
    """Frozen P4B2 references to pre-existing unified knowledge records."""

    evidence_pack_id: str
    component_symbol: str
    security_name: str
    analysis_as_of: str
    selection_id: str
    resolution_id: str
    source_ids: list[str]
    fact_ids: list[str]
    evidence_ids: list[str]
    existing_claim_ids: list[str]
    conflict_ids: list[str]
    coverage_dimensions: list[ComponentResearchDimension]
    missing_dimensions: list[ComponentResearchDimension]
    market_data_status: str
    financial_period: str | None
    latest_event_at: str | None
    required_field_coverage: float
    quality: ComponentResearchEvidenceQuality
    warnings: list[str]
    input_fingerprint: str
    evidence_data_as_of: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ComponentResearchEvidencePack":
        return cls(**data)


@dataclass(frozen=True, slots=True)
class ComponentResearchGenerationJob:
    """One bounded component-research task within an exact P4B2 plan."""

    job_id: str
    idempotency_key: str
    etf_symbol: str
    selection_id: str
    resolution_id: str
    component_symbol: str
    component_name: str
    digest_status_before: ComponentResearchStatus
    priority: int
    depth: str
    evidence_pack_id: str
    evidence_pack_fingerprint: str
    policy_version: str
    prompt_version: str
    model_id: str
    selection_data_as_of: str
    analysis_as_of: str
    status: ComponentResearchGenerationJobStatus
    blocked_reasons: list[str]
    estimated_input_tokens: int
    estimated_output_tokens: int
    actual_input_tokens: int
    actual_output_tokens: int
    model_calls: int
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    publish_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ComponentResearchGenerationJob":
        return cls(**data)


@dataclass(frozen=True, slots=True)
class ComponentResearchGenerationPlan:
    """Stable, exact-scope P4B2 plan; dry-run creation never calls a model."""

    plan_id: str
    etf_symbol: str
    selection_id: str
    resolution_id: str
    analysis_as_of: str
    dry_run: bool
    authorized: bool
    authorization_scope: list[str]
    candidate_count: int
    eligible_count: int
    planned_count: int
    skipped_reusable_count: int
    skipped_budget_count: int
    blocked_count: int
    estimated_model_calls: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    budget_remaining: dict[str, int]
    jobs: list[ComponentResearchGenerationJob]
    warnings: list[str]
    knowledge_fingerprint: str
    policy_version: str
    created_at: str
    expires_at: str
    status: str = "planned"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ComponentResearchGenerationPlan":
        payload = dict(data)
        payload["jobs"] = [
            item if isinstance(item, ComponentResearchGenerationJob)
            else ComponentResearchGenerationJob.from_dict(dict(item))
            for item in payload.get("jobs") or []
        ]
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class ComponentResearchPublishResult:
    """Unified knowledge publication plus deterministic P4B1 re-resolution."""

    publish_id: str
    job_id: str
    component_symbol: str
    report_id: str
    claim_ids: list[str]
    fact_ids: list[str]
    evidence_ids: list[str]
    quality_status: QualityStatus
    coverage_status: ReportCoverageStatus
    published_at: str
    p4b1_resolution_id_after: str | None
    p4b1_digest_id_after: str | None
    p4b1_digest_status_after: ComponentResearchStatus | None
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ComponentResearchPublishResult":
        return cls(**data)


@dataclass(frozen=True, slots=True)
class ComponentResearchPreflightResult:
    """Machine-readable preflight evidence for a P4B2 dry or live run."""

    preflight_id: str
    checked_at: str
    healthy_service: bool
    service_detail: str
    workspace_dirty: bool
    workspace_changes: list[str]
    runtime_database_path: str
    runtime_database_size: int
    runtime_database_mtime_ns: int
    runtime_tables: dict[str, int]
    p4b1_initialized: bool
    p4b2_initialized: bool
    selection_current: bool
    resolution_current: bool
    authorized: bool
    authorization_scope: list[str]
    budget_used: dict[str, int]
    budget_remaining: dict[str, int]
    planned_budget: dict[str, int]
    budget_sufficient: bool
    dry_run_only: bool
    blocked_reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ReportViewpoint:
    viewpoint_id: str
    report_id: str
    horizon: ReportHorizon
    stance: ReportStance = "unknown"
    action: ReportAction = "not_applicable"
    confidence: ReportConfidence = "unknown"
    summary_claim_id: str | None = None
    reason_claim_ids: list[str] = field(default_factory=list)
    risk_claim_ids: list[str] = field(default_factory=list)
    condition_claim_ids: list[str] = field(default_factory=list)
    invalidation_claim_ids: list[str] = field(default_factory=list)
    valid_from: str | None = None
    valid_until: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ReportArtifactLink:
    artifact_id: str
    artifact_role: str
    filename: str
    media_type: str
    source_locator: str
    sha256: str | None = None
    available: bool = True
    revision: int = 1
    materialization_status: Literal["generatable", "materialized", "failed"] | None = None
    materialization_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ReportRelation:
    relation_id: str
    from_report_id: str
    to_report_id: str
    relation_type: ReportRelationType
    horizon: ReportHorizon | None = None
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ReportEnvelope:
    report_id: str
    family_id: str
    report_kind: ReportKind
    subject_type: ReportSubjectType
    subject_key: str
    status: ReportCatalogStatus
    report_quality_status: QualityStatus
    coverage_status: ReportCoverageStatus
    generated_at: str
    data_as_of: str
    source_type: str
    source_id: str
    source_revision: int = 1
    symbol: str | None = None
    security_name: str = ""
    knowledge_link: dict[str, Any] = field(default_factory=dict)
    viewpoints: list[ReportViewpoint] = field(default_factory=list)
    artifacts: list[ReportArtifactLink] = field(default_factory=list)
    relations: list[ReportRelation] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DeepReportRecord:
    schema_version: int = 3
    report_id: str = field(default_factory=lambda: f"report_{uuid.uuid4().hex[:16]}")
    session_id: str = ""
    attempt_id: str = ""
    profile: str = "equity_deep_research"
    instrument_type: Literal["company_equity", "etf", "index"] = "company_equity"
    symbol: str = ""
    security_name: str = ""
    security_name_source: str = ""
    security_name_aliases: list[str] = field(default_factory=list)
    identity_snapshot_id: str | None = None
    report_date: str = ""
    data_as_of: str = ""
    quality_status: QualityStatus = "passed_with_gaps"
    status: Literal["running", "completed", "failed", "cancelled"] = "running"
    analysis_modules: dict[str, ModuleResult] = field(default_factory=dict)
    pipeline_checks: dict[str, ModuleResult] = field(default_factory=dict)
    report_sections: dict[str, ModuleResult] = field(default_factory=dict)
    etf_readiness: dict[str, Any] = field(default_factory=dict)
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
        if not payload.get("symbol"):
            confirmed = re.search(
                r"研究对象已由用户确认：\s*(.+?)（(\d{6}\.(?:SH|SZ|BJ|HK|US))）",
                str(payload.get("request_content") or ""),
                re.I,
            )
            if confirmed:
                payload["symbol"] = confirmed.group(2).upper()
                payload["security_name"] = (
                    payload.get("security_name") or confirmed.group(1).strip()
                )
                payload["security_name_source"] = (
                    payload.get("security_name_source") or "user_confirmed"
                )
        payload.setdefault(
            "instrument_type",
            "etf" if payload.get("profile") == "etf_deep_research"
            else "index" if payload.get("profile") == "index_deep_research"
            else "company_equity",
        )
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
        for field_name in ("pipeline_checks", "report_sections"):
            payload[field_name] = {
                str(key): value if isinstance(value, ModuleResult) else ModuleResult(**value)
                for key, value in dict(payload.get(field_name) or {}).items()
            }
        payload["etf_readiness"] = dict(payload.get("etf_readiness") or {})
        return cls(**payload)
