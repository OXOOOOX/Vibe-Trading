"""REST contracts for the deterministic ETF universe collection layer."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from fastapi.testclient import TestClient

import api_server
from src.reports import etf_universe_provider


@dataclass
class _Serializable:
    payload: dict

    def to_dict(self) -> dict:
        return dict(self.payload)


class _UniverseService:
    def __init__(self) -> None:
        self.refresh_calls: list[tuple] = []
        self.prewarm_calls: list[bool] = []

    def status(self, symbol: str) -> dict:
        return {"etf_symbol": symbol, "cache_status": "fresh"}

    def latest_snapshot(self, symbol: str):
        if symbol == "000000.SH":
            return None
        return _Serializable({"subject": symbol, "quality_status": "passed"})

    def get_or_refresh(
        self,
        symbol: str,
        force_refresh: bool,
        as_of: str | None,
        event_symbols: list[str],
    ):
        self.refresh_calls.append((symbol, force_refresh, as_of, event_symbols))
        return _Serializable(
            {
                "etf_symbol": symbol,
                "provider": "csi_index_weight",
                "network_fetched": True,
                "model_calls": 0,
                "tokens_used": 0,
            }
        )

    def prewarm_current_holdings(self, *, force_refresh: bool) -> dict:
        self.prewarm_calls.append(force_refresh)
        return {"requested": 2, "succeeded": 2, "failed": 0}


def test_etf_universe_status_snapshot_refresh_and_prewarm(monkeypatch) -> None:
    service = _UniverseService()
    monkeypatch.setattr(
        etf_universe_provider,
        "get_etf_universe_service",
        lambda: service,
    )
    monkeypatch.setattr(api_server, "_session_service", SimpleNamespace(deep_reports=None))
    client = TestClient(api_server.app)

    status = client.get("/research/etf/510300.SH/universe")
    assert status.status_code == 200
    assert status.json() == {"etf_symbol": "510300.SH", "cache_status": "fresh"}

    snapshot = client.get("/research/etf/510300.SH/universe/snapshot")
    assert snapshot.status_code == 200
    assert snapshot.json()["quality_status"] == "passed"
    assert client.get("/research/etf/000000.SH/universe/snapshot").status_code == 404

    refreshed = client.post(
        "/research/etf/510300.SH/universe/refresh",
        json={
            "force_refresh": True,
            "as_of": "2026-07-18",
            "event_symbols": ["600519.SH"],
        },
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["model_calls"] == 0
    assert service.refresh_calls == [
        ("510300.SH", True, "2026-07-18", ["600519.SH"])
    ]

    prewarmed = client.post(
        "/research/etf/universe/prewarm",
        json={"force_refresh": False},
    )
    assert prewarmed.status_code == 200
    assert prewarmed.json()["succeeded"] == 2
    assert service.prewarm_calls == [False]
