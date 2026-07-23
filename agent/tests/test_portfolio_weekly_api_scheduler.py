from __future__ import annotations

import asyncio
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.portfolio_weekly_routes import register_portfolio_weekly_routes
from src.portfolio.weekly.scheduler import WeeklyReportScheduler, WeeklyScheduleStore


SHANGHAI = ZoneInfo("Asia/Shanghai")


class Calendar:
    mode = "test_exchange_calendar"

    def is_trading_day(self, value: date) -> bool:
        return value.weekday() < 5


class ArtifactStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def resolve_artifact(self, run_id: str, artifact_id: str):
        if run_id != "weekly-run-1" or artifact_id != "weekly-json":
            return None
        return (
            {
                "artifact_id": artifact_id,
                "filename": "2026-07-17_588870.SH_科创板芯片ETF_周度复盘.json",
                "media_type": "application/json",
            },
            self.path,
        )


class ApiService:
    def __init__(self, artifact_path: Path, *, disabled: bool = False) -> None:
        self.disabled = disabled
        self.start_kwargs = None
        self.store = ArtifactStore(artifact_path)
        self.record = {
            "run_id": "weekly-run-1",
            "report_id": "weekly-report-1",
            "symbol": "588870.SH",
            "week_start": "2026-07-13",
            "week_end": "2026-07-17",
            "status": "queued",
            "stage": "queued",
            "progress": {"completed": 0, "total": 1, "percent": 0},
            "artifacts": [],
        }

    def list_runs(self, _limit=30):
        return [self.record]

    def get_run(self, run_id: str):
        return self.record if run_id == self.record["run_id"] else None

    def metrics(self):
        return {"total_runs": 1, "model_calls": 0}

    async def start(self, **kwargs):
        if self.disabled:
            raise ValueError("weekly report feature is disabled")
        self.start_kwargs = kwargs
        return [self.record]

    async def cancel(self, run_id: str):
        if run_id != self.record["run_id"]:
            raise KeyError(run_id)
        return {**self.record, "status": "cancelled"}

    async def retry(self, run_id: str):
        if run_id != self.record["run_id"]:
            raise KeyError(run_id)
        return {**self.record, "run_id": "weekly-run-2", "revision": 2}


class SchedulerService:
    def __init__(self) -> None:
        self.start_calls = 0
        self.records = {"weekly-a": {"run_id": "weekly-a", "status": "running"}}

    def enabled(self) -> bool:
        return True

    async def start(self, **kwargs):
        self.start_calls += 1
        assert kwargs["week_end"] == "2026-07-17"
        assert kwargs["trigger"] == "scheduled"
        return [self.records["weekly-a"]]

    def get_run(self, run_id: str):
        return self.records.get(run_id)


def app_for(service: ApiService) -> TestClient:
    app = FastAPI()
    register_portfolio_weekly_routes(
        app,
        lambda: None,
        get_service=lambda: service,
        get_scheduler=lambda: type("Scheduler", (), {"status": lambda self: {"enabled": False}})(),
    )
    return TestClient(app)


def test_weekly_api_exposes_run_lifecycle_metrics_and_artifact(tmp_path) -> None:
    artifact = tmp_path / "weekly.json"
    artifact.write_text('{"report_kind":"weekly_review"}', encoding="utf-8")
    service = ApiService(artifact)
    client = app_for(service)

    started = client.post(
        "/portfolio/weekly-runs",
        json={"week_end": "2026-07-17", "symbols": ["588870.SH"]},
    )
    assert started.status_code == 202
    assert started.json()["runs"][0]["run_id"] == "weekly-run-1"
    assert service.start_kwargs["report_audience"] == "user"
    assert client.get("/portfolio/weekly-runs").json()["runs"][0]["symbol"] == "588870.SH"
    assert client.get("/portfolio/weekly-runs/metrics").json()["model_calls"] == 0
    capabilities = client.get("/portfolio/weekly-runs/capabilities").json()
    assert capabilities["report_audiences"] == [
        {
            "id": "user",
            "status": "available",
            "default_profile": "weekly_review_v1",
        },
        {
            "id": "monitor",
            "status": "reserved",
            "default_profile": None,
        },
    ]
    assert capabilities["deterministic_methods"][0]["method_version"] == (
        "etf-tracking-metrics/1.0"
    )
    registry = capabilities["data_gap_registry"]
    assert any(
        item["reason_code"] == "etf_tracking_error_scope_unavailable"
        and item["label_zh"] == "缺少基金官方跟踪误差或跟踪偏离度"
        for item in registry
    )
    assert client.post("/portfolio/weekly-runs/weekly-run-1/cancel").json()["status"] == "cancelled"
    assert client.post("/portfolio/weekly-runs/weekly-run-1/retry").json()["revision"] == 2
    response = client.get("/portfolio/weekly-runs/weekly-run-1/artifacts/weekly-json?download=false")
    assert response.status_code == 200
    assert response.json()["report_kind"] == "weekly_review"


def test_weekly_api_is_fail_closed_when_feature_disabled(tmp_path) -> None:
    artifact = tmp_path / "unused.json"
    artifact.write_text("{}", encoding="utf-8")
    response = app_for(ApiService(artifact, disabled=True)).post(
        "/portfolio/weekly-runs",
        json={"symbols": ["588870.SH"]},
    )
    assert response.status_code == 503
    assert "disabled" in response.json()["detail"]


def test_weekly_scheduler_claims_whole_week_once_across_repeated_ticks(tmp_path) -> None:
    service = SchedulerService()
    scheduler = WeeklyReportScheduler(
        lambda: service,
        store=WeeklyScheduleStore(tmp_path / "weekly-scheduler.sqlite3"),
        calendar=Calendar(),
        scheduled_time=time(15, 40),
        enabled_override=True,
    )
    now = datetime(2026, 7, 17, 15, 45, tzinfo=SHANGHAI)

    first = asyncio.run(scheduler.run_due_once(now))
    service.records["weekly-a"]["status"] = "completed"
    second = asyncio.run(scheduler.run_due_once(now))

    assert first["state"] == "running"
    assert second["state"] == "completed"
    assert second["duplicate_suppressed"] == 1
    assert service.start_calls == 1


def test_weekly_scheduler_respects_close_time_and_non_trading_days(tmp_path) -> None:
    service = SchedulerService()
    scheduler = WeeklyReportScheduler(
        lambda: service,
        store=WeeklyScheduleStore(tmp_path / "weekly-scheduler.sqlite3"),
        calendar=Calendar(),
        scheduled_time=time(15, 40),
        enabled_override=True,
    )
    assert asyncio.run(
        scheduler.run_due_once(datetime(2026, 7, 17, 15, 39, tzinfo=SHANGHAI))
    ) is None
    assert scheduler.last_check["decision"] == "not_due"
    assert asyncio.run(
        scheduler.run_due_once(datetime(2026, 7, 18, 16, 0, tzinfo=SHANGHAI))
    ) is None
    assert scheduler.last_check["decision"] == "non_trading_day"
    assert service.start_calls == 0
