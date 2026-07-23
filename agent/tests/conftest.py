"""Shared fixtures and sys.path setup for all tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure agent/ is on sys.path so imports like `backtest.*` and `src.*` work.
AGENT_DIR = Path(__file__).resolve().parent.parent
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))


@pytest.fixture(autouse=True)
def disable_live_report_library_writes(monkeypatch: pytest.MonkeyPatch):
    """Keep producer hooks from writing test artifacts into the live catalog."""

    monkeypatch.setenv("VIBE_TRADING_REPORT_LIBRARY_ENABLED", "0")
