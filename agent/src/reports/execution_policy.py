"""Host-owned execution limits for manual and autonomous report runs."""

from __future__ import annotations

from dataclasses import dataclass


STANDARD_AGENT_MAX_ITERATIONS = 50
EQUITY_DEEP_REPORT_MAX_ITERATIONS = 100

# Monitoring refreshes are unattended repair work.  They get enough room to
# rebuild structured evidence, but must never inherit the open-ended budget of
# a user-started Deep Report.
MONITOR_STRUCTURAL_REFRESH_MAX_ITERATIONS = 20
MONITOR_STRUCTURAL_REFRESH_MAX_TOTAL_TOKENS = 240_000
MONITOR_STRUCTURAL_REFRESH_GENERATION_SOURCE = "portfolio_monitor_structural_refresh"


@dataclass(frozen=True)
class AgentExecutionLimits:
    max_iterations: int
    max_total_tokens: int | None = None


def resolve_agent_execution_limits(
    *,
    is_deep_report: bool,
    generation_source: str | None,
) -> AgentExecutionLimits:
    """Return limits derived from trusted attempt metadata.

    The caller, not request input, decides whether the attempt is a Deep Report.
    Autonomous monitor refreshes receive both a hard token ceiling (when the
    provider reports usage) and an iteration ceiling (the fallback when it does
    not).
    """

    if (
        is_deep_report
        and str(generation_source or "") == MONITOR_STRUCTURAL_REFRESH_GENERATION_SOURCE
    ):
        return AgentExecutionLimits(
            max_iterations=MONITOR_STRUCTURAL_REFRESH_MAX_ITERATIONS,
            max_total_tokens=MONITOR_STRUCTURAL_REFRESH_MAX_TOTAL_TOKENS,
        )
    return AgentExecutionLimits(
        max_iterations=(
            EQUITY_DEEP_REPORT_MAX_ITERATIONS
            if is_deep_report
            else STANDARD_AGENT_MAX_ITERATIONS
        )
    )
