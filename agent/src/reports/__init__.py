"""Deep research report contracts, deterministic analysis, and persistence."""

from .contracts import (
    ClaimItem,
    DeepReportRecord,
    DerivedFinancialFact,
    EvidenceItem,
    FinancialAlert,
    FinancialCoverage,
    FinancialPeriod,
    FinancialSnapshot,
    ModuleResult,
)
from .profile import EQUITY_DEEP_RESEARCH_PROFILE, build_equity_deep_research_prompt
from .service import DeepReportService

__all__ = [
    "ClaimItem",
    "DeepReportRecord",
    "DeepReportService",
    "DerivedFinancialFact",
    "EQUITY_DEEP_RESEARCH_PROFILE",
    "EvidenceItem",
    "FinancialAlert",
    "FinancialCoverage",
    "FinancialPeriod",
    "FinancialSnapshot",
    "ModuleResult",
    "build_equity_deep_research_prompt",
]
