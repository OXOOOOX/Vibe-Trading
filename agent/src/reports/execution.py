"""Deep-research execution mode selection with legacy compatibility."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal


DeepResearchEngine = Literal["provider", "codex_cli"]

DEEP_RESEARCH_ENGINE_ENV = "VIBE_TRADING_DEEP_RESEARCH_ENGINE"
LEGACY_CODEX_CLI_ENABLED_ENV = "VIBE_TRADING_CODEX_DEEP_RESEARCH_ENABLED"
DEEP_RESEARCH_ENGINE_PROVIDER: DeepResearchEngine = "provider"
DEEP_RESEARCH_ENGINE_CODEX_CLI: DeepResearchEngine = "codex_cli"
DEEP_RESEARCH_ENGINES = (
    DEEP_RESEARCH_ENGINE_PROVIDER,
    DEEP_RESEARCH_ENGINE_CODEX_CLI,
)


def resolve_deep_research_engine(
    values: Mapping[str, str] | None = None,
) -> DeepResearchEngine:
    """Resolve the explicit engine first, then the historical boolean flag."""

    source = values if values is not None else os.environ
    configured = str(source.get(DEEP_RESEARCH_ENGINE_ENV, "")).strip().lower()
    if configured in DEEP_RESEARCH_ENGINES:
        return configured  # type: ignore[return-value]

    legacy_enabled = str(source.get(LEGACY_CODEX_CLI_ENABLED_ENV, "0")).strip().lower()
    if legacy_enabled in {"1", "true", "yes", "on"}:
        return DEEP_RESEARCH_ENGINE_CODEX_CLI
    return DEEP_RESEARCH_ENGINE_PROVIDER


def engine_env_updates(engine: DeepResearchEngine) -> dict[str, str]:
    """Persist the semantic engine and the old flag for downgrade compatibility."""

    if engine not in DEEP_RESEARCH_ENGINES:
        raise ValueError(f"unsupported deep research engine: {engine}")
    return {
        DEEP_RESEARCH_ENGINE_ENV: engine,
        LEGACY_CODEX_CLI_ENABLED_ENV: (
            "1" if engine == DEEP_RESEARCH_ENGINE_CODEX_CLI else "0"
        ),
    }
