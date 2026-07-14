from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.portfolio.daily.scheduler import DailyPortfolioScheduler, DailyScheduleStore


_TZ = ZoneInfo("Asia/Shanghai")
_DUE = datetime(2026, 7, 14, 9, 12, tzinfo=_TZ)


class _Calendar:
    def __init__(self, trading: bool = True) -> None:
        self.trading = trading

    def is_trading_day(self, _value) -> bool:
        return self.trading


class _RunService:
    def __init__(self, *, terminal_status: str = "completed") -> None:
        self.terminal_status = terminal_status
        self.records: dict[str, dict] = {}
        self.starts: list[dict] = []
        self.retries: list[str] = []

    async def start(self, **kwargs):
        self.starts.append(kwargs)
        run_id = f"run-{len(self.starts)}"
        record = {
            "run_id": run_id,
            "market_date": kwargs["market_date"],
            "status": self.terminal_status,
            "stage": "completed" if self.terminal_status.startswith("completed") else "failed",
            "revision": 1,
            "artifacts": [],
            "error": "upstream failed" if self.terminal_status == "failed" else None,
        }
        self.records[run_id] = record
        return record

    def get_run(self, run_id: str):
        return self.records.get(run_id)

    async def wait(self, run_id: str):
        return self.records[run_id]

    async def retry(self, run_id: str):
        self.retries.append(run_id)
        recovered = {
            "run_id": f"{run_id}-recovered",
            "market_date": "2026-07-14",
            "status": "completed",
            "stage": "completed",
            "revision": 2,
            "artifacts": [],
            "error": None,
        }
        self.records[recovered["run_id"]] = recovered
        return recovered


def _store(tmp_path: Path) -> DailyScheduleStore:
    return DailyScheduleStore(tmp_path / "scheduler.sqlite3")


def _prewarm(status: str = "completed"):
    return lambda: {
        "enabled": True,
        "active_slots": [],
        "last_run": {
            "slot": "2026-07-14:premarket:09:10:00",
            "status": status,
        },
    }


def test_trading_day_runs_once_and_restart_deduplicates(tmp_path: Path) -> None:
    service = _RunService()
    store = _store(tmp_path)
    scheduler = DailyPortfolioScheduler(
        lambda: service,
        store=store,
        calendar=_Calendar(),
        prewarm_status_provider=_prewarm(),
        prewarm_wait_seconds=0,
        mode_override="shadow",
    )

    first = asyncio.run(scheduler.run_due_once(_DUE))
    restarted = DailyPortfolioScheduler(
        lambda: service,
        store=DailyScheduleStore(store.path),
        calendar=_Calendar(),
        prewarm_status_provider=_prewarm(),
        prewarm_wait_seconds=0,
        mode_override="shadow",
    )
    second = asyncio.run(restarted.run_due_once(_DUE))

    assert len(service.starts) == 1
    assert service.starts[0]["trigger"] == "scheduled_0912"
    assert service.starts[0]["refresh_policy"] == "ensure_fresh"
    assert first["state"] == second["state"] == "completed"
    assert second["delivery_status"] == "shadow_suppressed"


def test_weekend_does_not_claim_or_start(tmp_path: Path) -> None:
    service = _RunService()
    store = _store(tmp_path)
    scheduler = DailyPortfolioScheduler(
        lambda: service,
        store=store,
        calendar=_Calendar(False),
        prewarm_wait_seconds=0,
    )

    assert asyncio.run(scheduler.run_due_once(_DUE)) is None
    assert service.starts == []
    assert store.latest() is None
    assert scheduler.status()["last_check"]["decision"] == "non_trading_day"


def test_restart_recovers_one_interrupted_run(tmp_path: Path) -> None:
    service = _RunService()
    service.records["old-run"] = {
        "run_id": "old-run",
        "market_date": "2026-07-14",
        "status": "interrupted",
        "stage": "interrupted",
    }
    store = _store(tmp_path)
    store.claim("2026-07-14", mode="shadow")
    store.update(
        "2026-07-14",
        state="running",
        run_id="old-run",
        refresh_policy="ensure_fresh",
        prewarm_status="completed",
        attempts=1,
    )
    scheduler = DailyPortfolioScheduler(
        lambda: service,
        store=store,
        calendar=_Calendar(),
        prewarm_status_provider=_prewarm(),
        prewarm_wait_seconds=0,
        mode_override="shadow",
    )

    after_window = datetime(2026, 7, 14, 10, 30, tzinfo=_TZ)
    result = asyncio.run(scheduler.run_due_once(after_window))

    assert service.starts == []
    assert service.retries == ["old-run"]
    assert result["run_id"] == "old-run-recovered"
    assert result["recovery_attempts"] == 1
    assert result["delivery_status"] == "shadow_suppressed"


def test_prewarm_timeout_forces_data_refresh(tmp_path: Path) -> None:
    service = _RunService()
    scheduler = DailyPortfolioScheduler(
        lambda: service,
        store=_store(tmp_path),
        calendar=_Calendar(),
        prewarm_status_provider=lambda: {
            "enabled": True,
            "active_slots": ["2026-07-14:premarket:09:10:00"],
            "last_run": {"status": "running"},
        },
        prewarm_wait_seconds=0,
        mode_override="shadow",
    )

    result = asyncio.run(scheduler.run_due_once(_DUE))

    assert service.starts[0]["refresh_policy"] == "force"
    assert result["prewarm_status"] == "timeout"


def test_deliver_mode_waits_for_delivery_and_never_resends_after_restart(
    tmp_path: Path,
) -> None:
    service = _RunService()
    store = _store(tmp_path)
    store.remember_delivery_target(channel="feishu", chat_id="ou_user")
    deliveries: list[tuple[str, str]] = []

    async def deliver(record, job, target):
        deliveries.append((record["run_id"], target["chat_id"]))

    scheduler = DailyPortfolioScheduler(
        lambda: service,
        store=store,
        calendar=_Calendar(),
        prewarm_status_provider=_prewarm(),
        delivery_callback=deliver,
        prewarm_wait_seconds=0,
        mode_override="deliver",
    )
    first = asyncio.run(scheduler.run_due_once(_DUE))
    restarted = DailyPortfolioScheduler(
        lambda: service,
        store=DailyScheduleStore(store.path),
        calendar=_Calendar(),
        prewarm_status_provider=_prewarm(),
        delivery_callback=deliver,
        prewarm_wait_seconds=0,
        mode_override="deliver",
    )
    second = asyncio.run(restarted.run_due_once(_DUE))

    assert deliveries == [("run-1", "ou_user")]
    assert first["delivery_status"] == second["delivery_status"] == "delivered"


def test_failed_run_uses_same_single_delivery_path(tmp_path: Path) -> None:
    service = _RunService(terminal_status="failed")
    store = _store(tmp_path)
    store.remember_delivery_target(channel="feishu", chat_id="ou_user")
    delivered: list[str] = []

    async def deliver(record, job, _target):
        assert record["status"] == "failed"
        delivered.append(job["state"])

    scheduler = DailyPortfolioScheduler(
        lambda: service,
        store=store,
        calendar=_Calendar(),
        prewarm_status_provider=_prewarm(),
        delivery_callback=deliver,
        prewarm_wait_seconds=0,
        mode_override="deliver",
    )

    result = asyncio.run(scheduler.run_due_once(_DUE))

    assert result["state"] == "failed"
    assert result["delivery_status"] == "delivered"
    assert delivered == ["failed"]


def test_background_scheduler_stops_promptly(tmp_path: Path) -> None:
    service = _RunService()
    scheduler = DailyPortfolioScheduler(
        lambda: service,
        store=_store(tmp_path),
        calendar=_Calendar(),
        now_factory=lambda: datetime(2026, 7, 14, 8, 0, tzinfo=_TZ),
        interval_seconds=60,
        enabled_override=True,
    )

    async def scenario():
        await scheduler.start()
        await asyncio.sleep(0)
        assert scheduler.status()["running"] is True
        await scheduler.stop()

    asyncio.run(scenario())
    assert scheduler.status()["running"] is False
