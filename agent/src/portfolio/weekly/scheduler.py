"""Durable, exchange-calendar-aware scheduler for formal weekly reports."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from contextlib import suppress
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.config.paths import get_runtime_root
from src.data_layer.prewarm import ChinaMarketCalendar

from .verification import resolve_completed_trading_week


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_TRUE_VALUES = {"1", "true", "yes", "on"}
_TERMINAL = {"completed", "completed_with_warnings", "failed", "cancelled", "interrupted"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WeeklyScheduleStore:
    """A SQLite claim makes one whole-portfolio weekly schedule single-writer."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (
            get_runtime_root() / "portfolio" / "weekly_scheduler" / "scheduler.sqlite3"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS weekly_jobs (
                   week_end TEXT PRIMARY KEY,
                   state TEXT NOT NULL,
                   run_ids_json TEXT NOT NULL DEFAULT '[]',
                   attempts INTEGER NOT NULL DEFAULT 0,
                   duplicate_suppressed INTEGER NOT NULL DEFAULT 0,
                   error TEXT,
                   created_at TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   completed_at TEXT
                   )"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        try:
            value["run_ids"] = json.loads(str(value.pop("run_ids_json")))
        except json.JSONDecodeError:
            value["run_ids"] = []
        return value

    def claim(self, week_end: str) -> tuple[dict[str, Any], bool]:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """INSERT OR IGNORE INTO weekly_jobs(
                   week_end,state,run_ids_json,attempts,created_at,updated_at
                   ) VALUES (?, 'claimed', '[]', 0, ?, ?)""",
                (week_end, now, now),
            )
            if cursor.rowcount == 0:
                connection.execute(
                    "UPDATE weekly_jobs SET duplicate_suppressed=duplicate_suppressed+1,updated_at=? WHERE week_end=?",
                    (now, week_end),
                )
            row = connection.execute(
                "SELECT * FROM weekly_jobs WHERE week_end=?", (week_end,)
            ).fetchone()
            connection.commit()
        value = self._decode(row)
        if value is None:
            raise RuntimeError("weekly scheduler claim was not persisted")
        return value, cursor.rowcount == 1

    def get(self, week_end: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            return self._decode(
                connection.execute(
                    "SELECT * FROM weekly_jobs WHERE week_end=?", (week_end,)
                ).fetchone()
            )

    def latest(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            return self._decode(
                connection.execute(
                    "SELECT * FROM weekly_jobs ORDER BY week_end DESC LIMIT 1"
                ).fetchone()
            )

    def update(self, week_end: str, **updates: Any) -> dict[str, Any]:
        allowed = {"state", "run_ids", "attempts", "error", "completed_at"}
        values: dict[str, Any] = {}
        for key, value in updates.items():
            if key not in allowed:
                continue
            values["run_ids_json" if key == "run_ids" else key] = (
                json.dumps(value, ensure_ascii=False) if key == "run_ids" else value
            )
        values["updated_at"] = _utc_now()
        with self._connect() as connection:
            connection.execute(
                f"UPDATE weekly_jobs SET {', '.join(f'{key}=?' for key in values)} WHERE week_end=?",
                (*values.values(), week_end),
            )
        value = self.get(week_end)
        if value is None:
            raise KeyError(week_end)
        return value


class WeeklyReportScheduler:
    """Generate current holdings after the week's last exchange close."""

    def __init__(
        self,
        service_factory: Callable[[], Any],
        *,
        store: WeeklyScheduleStore | None = None,
        calendar: Any | None = None,
        now_factory: Callable[[], datetime] | None = None,
        interval_seconds: float = 30.0,
        scheduled_time: time | None = None,
        enabled_override: bool | None = None,
    ) -> None:
        self.service_factory = service_factory
        self.store = store or WeeklyScheduleStore()
        self.calendar = calendar or ChinaMarketCalendar()
        self.now_factory = now_factory or (lambda: datetime.now(_SHANGHAI))
        self.interval_seconds = max(0.05, float(interval_seconds))
        configured = os.getenv("VIBE_TRADING_WEEKLY_REPORT_SCHEDULE_TIME", "15:40")
        try:
            hour, minute = (int(item) for item in configured.split(":", 1))
            default_time = time(hour, minute)
        except (TypeError, ValueError):
            default_time = time(15, 40)
        self.scheduled_time = scheduled_time or default_time
        self.enabled_override = enabled_override
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._tick_lock = asyncio.Lock()
        self.last_check: dict[str, Any] | None = None

    def enabled(self) -> bool:
        if self.enabled_override is not None:
            return self.enabled_override
        return (
            os.getenv("VIBE_TRADING_WEEKLY_REPORT_SCHEDULER_ENABLED", "0").strip().lower()
            in _TRUE_VALUES
            and self.service_factory().enabled()
        )

    async def start(self) -> None:
        if not self.enabled() or (self._task is not None and not self._task.done()):
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="portfolio-weekly-report")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_due_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                continue

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled(),
            "running": self._task is not None and not self._task.done(),
            "timezone": "Asia/Shanghai",
            "scheduled_time": self.scheduled_time.strftime("%H:%M"),
            "calendar_mode": getattr(self.calendar, "mode", "unknown"),
            "last_check": self.last_check,
            "latest_job": self.store.latest(),
        }

    async def run_due_once(self, now: datetime | None = None) -> dict[str, Any] | None:
        local_now = (now or self.now_factory()).astimezone(_SHANGHAI)
        if local_now.time() < self.scheduled_time:
            self.last_check = {"at": local_now.isoformat(), "decision": "not_due"}
            return None
        if not self.calendar.is_trading_day(local_now.date()):
            self.last_check = {"at": local_now.isoformat(), "decision": "non_trading_day"}
            return None
        _, week_end, _ = resolve_completed_trading_week(
            self.calendar,
            requested_week_end=local_now.date().isoformat(),
            now=local_now,
        )
        async with self._tick_lock:
            job, created = self.store.claim(week_end)
            self.last_check = {
                "at": local_now.isoformat(),
                "week_end": week_end,
                "decision": "claimed" if created else "deduplicated",
            }
            if created:
                try:
                    records = await self.service_factory().start(
                        week_end=week_end,
                        refresh_policy="ensure_fresh",
                        force_new=False,
                        trigger="scheduled",
                    )
                    return self.store.update(
                        week_end,
                        state="running",
                        run_ids=[str(item["run_id"]) for item in records],
                        attempts=1,
                    )
                except Exception as exc:
                    return self.store.update(
                        week_end,
                        state="failed",
                        attempts=1,
                        error=f"{type(exc).__name__}: {exc}",
                        completed_at=_utc_now(),
                    )
            if job["state"] == "running":
                service = self.service_factory()
                records = [service.get_run(run_id) for run_id in job.get("run_ids") or []]
                if records and all(record and record.get("status") in _TERMINAL for record in records):
                    state = "completed" if all(record.get("status") in {"completed", "completed_with_warnings"} for record in records if record) else "failed"
                    return self.store.update(
                        week_end,
                        state=state,
                        completed_at=_utc_now(),
                    )
            return self.store.get(week_end)
