from __future__ import annotations

from fastapi.testclient import TestClient

import api_server


class FakeDailyService:
    def __init__(self, tmp_path) -> None:
        self.store = self
        self.master_path = tmp_path / "master.pdf"
        self.holding_path = tmp_path / "holding.pdf"
        self.master_path.write_bytes(b"%PDF-master")
        self.holding_path.write_bytes(b"%PDF-holding")
        self.record = {
            "run_id": "dpr_api",
            "market_date": "2026-07-13",
            "status": "queued",
            "stage": "queued",
            "progress": {"completed": 0, "total": 1, "percent": 0},
            "refresh_policy": "ensure_fresh",
            "report_profile": "master_with_holding_appendices",
            "created_at": "2026-07-13T09:00:00+08:00",
            "artifacts": [
                {
                    "artifact_id": "master",
                    "kind": "master_pdf",
                    "filename": "master.pdf",
                    "media_type": "application/pdf",
                    "path": str(self.master_path),
                    "expired": False,
                    "superseded": False,
                },
                {
                    "artifact_id": "holding",
                    "kind": "holding_daily_pdf",
                    "symbol": "600036.SH",
                    "filename": "holding.pdf",
                    "media_type": "application/pdf",
                    "path": str(self.holding_path),
                    "expired": False,
                    "superseded": False,
                },
            ],
        }

    async def start(self, **kwargs):
        return dict(self.record)

    def list_runs(self, limit):
        return [dict(self.record)]

    def get_run(self, run_id):
        return dict(self.record) if run_id == self.record["run_id"] else None

    async def cancel(self, run_id):
        return {**self.record, "status": "cancelling", "stage": "cancelling"}

    async def retry(self, run_id, *, symbol=None):
        return {
            **self.record,
            "run_id": "dpr_retry",
            "revision": 2,
            "parent_run_id": run_id,
            "retry_symbol": symbol,
        }

    def resolve_artifact(self, run_id, artifact_id):
        artifact = next(
            (item for item in self.record["artifacts"] if item["artifact_id"] == artifact_id),
            None,
        )
        return (artifact, self.master_path if artifact_id == "master" else self.holding_path) if artifact else None


def test_portfolio_mandate_and_daily_run_routes(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_PORTFOLIO_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("VIBE_TRADING_PORTFOLIO_MANDATE_PATH", str(tmp_path / "mandate.json"))
    monkeypatch.setattr(api_server, "_portfolio_daily_service", FakeDailyService(tmp_path))
    client = TestClient(api_server.app, client=("127.0.0.1", 50000))

    seeded = client.post(
        "/portfolio/holdings",
        json={"raw_text": "招商银行 600036 1000 40", "cash": 30000},
    )
    assert seeded.status_code == 200

    mandate_response = client.get("/portfolio/mandate")
    assert mandate_response.status_code == 200
    mandate = mandate_response.json()
    assert mandate["assignments"]["600036.SH"]["active_sleeve_id"] == "defensive"
    assert client.post("/portfolio/mandate/suggest-classifications").status_code == 200

    mandate["cash_policy"].update(
        {"configured": True, "target_amount": 20000, "min_amount": 10000, "max_amount": 30000}
    )
    saved = client.put("/portfolio/mandate", json={"mandate": mandate})
    assert saved.status_code == 200
    assert saved.json()["version"] > mandate["version"]
    patched = client.patch(
        "/portfolio/mandate/assignments/600036.SH",
        json={"sleeve_id": "offensive", "user_locked": True},
    )
    assert patched.status_code == 200
    assert patched.json()["assignments"]["600036.SH"]["user_locked"] is True

    started = client.post("/portfolio/daily-runs", json={"refresh_policy": "ensure_fresh"})
    assert started.status_code == 202
    assert started.json()["run_id"] == "dpr_api"
    fetched = client.get("/portfolio/daily-runs/dpr_api")
    assert fetched.status_code == 200
    assert all("path" not in item for item in fetched.json()["artifacts"])
    assert client.get("/portfolio/daily-runs/latest").json()["run_id"] == "dpr_api"
    assert client.post("/portfolio/daily-runs/dpr_api/cancel").json()["status"] == "cancelling"
    retried = client.post(
        "/portfolio/daily-runs/dpr_api/retry", json={"symbol": "600036.SH"}
    )
    assert retried.status_code == 202
    assert retried.json()["retry_symbol"] == "600036.SH"
    assert client.get("/portfolio/daily-runs/dpr_api/reports/master").status_code == 200
    assert client.get(
        "/portfolio/daily-runs/dpr_api/reports/holdings/600036.SH"
    ).status_code == 200
