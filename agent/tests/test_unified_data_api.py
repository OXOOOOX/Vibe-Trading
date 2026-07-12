from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

import api_server
import src.api.data_routes as data_routes


class _FakeControl:
    def __init__(self) -> None:
        self.watchlist = []

    def list_watchlist(self):
        return list(self.watchlist)

    def add_watchlist(self, symbol, note=None):
        row = {"symbol": symbol.upper(), "note": note, "added_at": "2026-07-13T00:00:00+00:00"}
        self.watchlist = [item for item in self.watchlist if item["symbol"] != row["symbol"]] + [row]
        return row

    def remove_watchlist(self, symbol):
        before = len(self.watchlist)
        self.watchlist = [item for item in self.watchlist if item["symbol"] != symbol.upper()]
        return len(self.watchlist) != before

    def get_request(self, request_id):
        return {"request_id": request_id, "status": "completed"} if request_id == "request-1" else None


class _FakeDataService:
    def __init__(self) -> None:
        self.control = _FakeControl()

    def get_context(self, **kwargs):
        return {"request_id": "request-1", "status": "live", **kwargs}

    def read_bars(self, handle, cursor):
        if handle != "valid-handle":
            raise KeyError("unknown or expired data handle")
        return {"handle": handle, "cursor": cursor, "bars": []}

    def coverage(self):
        return {"status": "ok", "coverage": [], "watchlist": self.control.list_watchlist(), "retention": {}}

    def sources(self):
        return {"status": "ok", "sources": [], "quorum": "two sources"}

    def storage(self):
        return {"status": "ok", "entries": [], "total_bytes": 0, "soft_limit_bytes": 1, "evict_at_bytes": 1, "retention": {}}

    def prewarm(self, *, phase):
        return {"status": "live", "phase": phase}


def test_unified_data_routes_expose_context_paging_and_manual_watchlist(monkeypatch) -> None:
    service = _FakeDataService()
    monkeypatch.setattr(data_routes, "get_unified_data_service", lambda: service)
    client = TestClient(api_server.app, client=("127.0.0.1", 50000))

    context = client.post("/data/context", json={"symbols": ["588870.SH"], "purpose": "holding"})
    assert context.status_code == 200
    assert context.json()["request_id"] == "request-1"
    assert client.get("/data/bars", params={"handle": "valid-handle", "cursor": 0}).status_code == 200
    assert client.get("/data/bars", params={"handle": "missing-handle"}).status_code == 404

    assert client.post("/data/watchlist", json={"symbol": "510300.SH"}).json()["entry"]["symbol"] == "510300.SH"
    assert client.get("/data/watchlist").json()["watchlist"][0]["symbol"] == "510300.SH"
    assert client.delete("/data/watchlist/510300.SH").status_code == 200
    assert client.post("/data/prewarm", json={"phase": "intraday"}).json()["phase"] == "intraday"
