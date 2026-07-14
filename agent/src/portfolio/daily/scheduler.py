"""Durable 09:12 scheduler for the portfolio morning meeting."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import sqlite3
import time as monotonic_time
from contextlib import suppress
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from src.config.paths import get_runtime_root
from src.data_layer.prewarm import ChinaMarketCalendar


_CN_TZ = ZoneInfo("Asia/Shanghai")
_TRUE_VALUES = {"1", "true", "yes", "on"}
_SUCCESS_RUN_STATUSES = {"completed", "completed_with_warnings"}
_FAILED_RUN_STATUSES = {"failed", "cancelled"}
_PREWARM_SUCCESS_STATUSES = {
    "completed",
    "completed_with_warnings",
    "live",
    "ok",
    "success",
}
_FINAL_DELIVERY_STATUSES = {
    "delivered",
    "shadow_suppressed",
    "delivery_uncertain",
    "origin_delivery_reused",
}
_DELIVERY_CLAIMABLE_STATUSES = {
    "pending",
    "delivery_waiting_target",
    "delivery_waiting_record",
}

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    from datetime import timezone

    return datetime.now(timezone.utc).isoformat()


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _append_error(current: Any, message: str) -> str:
    value = str(current or "").strip()
    if message in value:
        return value
    return f"{value}; {message}" if value else message


class DailyScheduleStore:
    """SQLite ledger that makes one scheduled claim durable across restarts."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (
            get_runtime_root() / "portfolio" / "daily_scheduler" / "scheduler.sqlite3"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS daily_jobs (
                    market_date TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    run_id TEXT,
                    refresh_policy TEXT,
                    prewarm_status TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    recovery_attempts INTEGER NOT NULL DEFAULT 0,
                    delivery_status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    delivered_at TEXT
                );
                CREATE TABLE IF NOT EXISTS scheduler_settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def claim(self, market_date: str, *, mode: str) -> tuple[dict[str, Any], bool]:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO daily_jobs (
                    market_date, state, mode, delivery_status, created_at, updated_at
                ) VALUES (?, 'claimed', ?, 'pending', ?, ?)
                """,
                (market_date, mode, now, now),
            )
            row = connection.execute(
                "SELECT * FROM daily_jobs WHERE market_date = ?", (market_date,)
            ).fetchone()
            connection.commit()
        value = _row_dict(row)
        if value is None:  # pragma: no cover - protected by the transaction above
            raise RuntimeError("scheduled job claim was not persisted")
        return value, cursor.rowcount == 1

    def get(self, market_date: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            return _row_dict(
                connection.execute(
                    "SELECT * FROM daily_jobs WHERE market_date = ?", (market_date,)
                ).fetchone()
            )

    def latest(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            return _row_dict(
                connection.execute(
                    "SELECT * FROM daily_jobs ORDER BY market_date DESC LIMIT 1"
                ).fetchone()
            )

    def update(self, market_date: str, **updates: Any) -> dict[str, Any]:
        allowed = {
            "state",
            "mode",
            "run_id",
            "refresh_policy",
            "prewarm_status",
            "attempts",
            "recovery_attempts",
            "delivery_status",
            "error",
            "completed_at",
            "delivered_at",
        }
        values = {key: value for key, value in updates.items() if key in allowed}
        values["updated_at"] = _utc_now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE daily_jobs SET {assignments} WHERE market_date = ?",
                (*values.values(), market_date),
            )
        value = self.get(market_date)
        if value is None:
            raise KeyError(market_date)
        return value

    def begin_delivery(self, market_date: str) -> bool:
        """Claim delivery once; an ambiguous restart is never sent twice."""

        placeholders = ", ".join("?" for _ in _DELIVERY_CLAIMABLE_STATUSES)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE daily_jobs
                SET delivery_status = 'delivering', updated_at = ?
                WHERE market_date = ? AND delivery_status IN ({placeholders})
                """,
                (
                    _utc_now(),
                    market_date,
                    *sorted(_DELIVERY_CLAIMABLE_STATUSES),
                ),
            )
            return cursor.rowcount == 1

    def remember_delivery_target(
        self,
        *,
        channel: str,
        chat_id: str,
        chat_type: str = "p2p",
        session_key: str = "",
    ) -> None:
        if not channel.strip() or not chat_id.strip():
            return
        value = {
            "channel": channel.strip(),
            "chat_id": chat_id.strip(),
            "chat_type": chat_type.strip() or "p2p",
            "session_key": session_key.strip(),
        }
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO scheduler_settings (key, value_json, updated_at)
                VALUES ('delivery_target', ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (json.dumps(value, ensure_ascii=False, sort_keys=True), now),
            )

    def delivery_target(self) -> dict[str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM scheduler_settings WHERE key = 'delivery_target'"
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(str(row["value_json"]))
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) and value.get("chat_id") else None


DeliveryCallback = Callable[
    [dict[str, Any] | None, dict[str, Any], dict[str, str]], Awaitable[None]
]


class DailyPortfolioScheduler:
    """Cooperative, restart-safe scheduler for the 09:12 daily report."""

    def __init__(
        self,
        run_service_factory: Callable[[], Any],
        *,
        store: DailyScheduleStore | None = None,
        calendar: Any | None = None,
        prewarm_status_provider: Callable[[], dict[str, Any]] | None = None,
        delivery_callback: DeliveryCallback | None = None,
        now_factory: Callable[[], datetime] | None = None,
        interval_seconds: float = 30.0,
        scheduled_time: time = time(9, 12),
        latest_start_time: time = time(10, 0),
        prewarm_wait_seconds: float = 180.0,
        prewarm_poll_seconds: float = 5.0,
        enabled_override: bool | None = None,
        mode_override: str | None = None,
    ) -> None:
        self.run_service_factory = run_service_factory
        self.store = store or DailyScheduleStore()
        self.calendar = calendar or ChinaMarketCalendar()
        self.prewarm_status_provider = prewarm_status_provider or (lambda: {})
        self.delivery_callback = delivery_callback
        self.now_factory = now_factory or (lambda: datetime.now(_CN_TZ))
        self.interval_seconds = max(0.05, float(interval_seconds))
        self.scheduled_time = scheduled_time
        self.latest_start_time = latest_start_time
        self.prewarm_wait_seconds = max(0.0, float(prewarm_wait_seconds))
        self.prewarm_poll_seconds = max(0.01, float(prewarm_poll_seconds))
        self.enabled_override = enabled_override
        self.mode_override = mode_override
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._tick_lock = asyncio.Lock()
        self.last_check: dict[str, Any] | None = None

    def enabled(self) -> bool:
        if self.enabled_override is not None:
            return self.enabled_override
        return (
            os.getenv("VIBE_TRADING_PORTFOLIO_AUTO_RUN_ENABLED", "0").strip().lower()
            in _TRUE_VALUES
        )

    def mode(self) -> str:
        value = (
            self.mode_override
            or os.getenv("VIBE_TRADING_PORTFOLIO_AUTO_RUN_MODE", "shadow")
        ).strip().lower()
        return value if value in {"shadow", "deliver"} else "shadow"

    async def start(self) -> None:
        if not self.enabled() or (self._task is not None and not self._task.done()):
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="portfolio-daily-0912")

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
            except Exception:  # noqa: BLE001 - the next scheduler tick must survive
                logger.exception("portfolio daily scheduler tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                continue

    def _target(self) -> dict[str, str] | None:
        chat_id = os.getenv("VIBE_TRADING_PORTFOLIO_AUTO_RUN_CHAT_ID", "").strip()
        if chat_id:
            channel = os.getenv(
                "VIBE_TRADING_PORTFOLIO_AUTO_RUN_CHANNEL", "feishu"
            ).strip() or "feishu"
            return {
                "channel": channel,
                "chat_id": chat_id,
                "chat_type": "group" if chat_id.startswith("oc_") else "p2p",
                "session_key": f"{channel}:{chat_id}",
            }
        return self.store.delivery_target()

    def status(self) -> dict[str, Any]:
        target = self._target()
        return {
            "enabled": self.enabled(),
            "mode": self.mode(),
            "running": self._task is not None and not self._task.done(),
            "timezone": "Asia/Shanghai",
            "scheduled_time": self.scheduled_time.strftime("%H:%M"),
            "latest_start_time": self.latest_start_time.strftime("%H:%M"),
            "delivery_target": {
                "configured": bool(target),
                "channel": (target or {}).get("channel"),
                "chat_type": (target or {}).get("chat_type"),
            },
            "last_check": self.last_check,
            "latest_job": self.store.latest(),
        }

    async def run_due_once(self, now: datetime | None = None) -> dict[str, Any] | None:
        local_now = (now or self.now_factory()).astimezone(_CN_TZ)
        market_date = local_now.date().isoformat()
        existing = self.store.get(market_date)
        if local_now.time() < self.scheduled_time and existing is None:
            self.last_check = {
                "at": local_now.isoformat(),
                "market_date": market_date,
                "decision": "not_due",
            }
            return None
        if local_now.time() > self.latest_start_time and existing is None:
            self.last_check = {
                "at": local_now.isoformat(),
                "market_date": market_date,
                "decision": "window_closed",
            }
            return None
        if existing is None and not self.calendar.is_trading_day(local_now.date()):
            self.last_check = {
                "at": local_now.isoformat(),
                "market_date": market_date,
                "decision": "non_trading_day",
            }
            return None

        async with self._tick_lock:
            if existing is None:
                job, created = self.store.claim(market_date, mode=self.mode())
            else:
                job, created = existing, False
            self.last_check = {
                "at": local_now.isoformat(),
                "market_date": market_date,
                "decision": "claimed" if created else "deduplicated",
            }
            return await self._process_job(job, local_now)

    async def _process_job(
        self, job: dict[str, Any], local_now: datetime
    ) -> dict[str, Any]:
        market_date = str(job["market_date"])
        try:
            if job["state"] in {"completed", "failed"}:
                if str(job.get("mode") or "shadow") == "shadow":
                    return await self._deliver_if_needed(job)
                run_id = str(job.get("run_id") or "")
                record = None
                record_error: Exception | None = None
                if run_id:
                    try:
                        record = self.run_service_factory().get_run(run_id)
                    except Exception as exc:  # retry completed-record lookup next tick
                        record_error = exc
                if job["state"] == "completed" and record is None:
                    return self.store.update(
                        market_date,
                        delivery_status="delivery_waiting_record",
                        error=_append_error(
                            job.get("error"),
                            "completed run record is temporarily unavailable"
                            + (
                                f": {type(record_error).__name__}: {record_error}"
                                if record_error is not None
                                else ""
                            ),
                        ),
                    )
                return await self._deliver_if_needed(job, record=record)

            service = self.run_service_factory()
            run_id = str(job.get("run_id") or "")
            record = service.get_run(run_id) if run_id else None

            if record and record.get("status") == "interrupted":
                recovery_attempts = int(job.get("recovery_attempts") or 0)
                if recovery_attempts >= 1:
                    raise RuntimeError("scheduled run was interrupted more than once")
                record = await service.retry(run_id)
                job = self.store.update(
                    market_date,
                    state="running",
                    run_id=str(record["run_id"]),
                    recovery_attempts=recovery_attempts + 1,
                    attempts=int(job.get("attempts") or 0) + 1,
                    error=None,
                )
            elif record is None:
                refresh_policy = str(job.get("refresh_policy") or "")
                prewarm_status = str(job.get("prewarm_status") or "")
                if not refresh_policy:
                    refresh_policy, prewarm_status = await self._refresh_policy(
                        local_now.date()
                    )
                job = self.store.update(
                    market_date,
                    state="starting",
                    refresh_policy=refresh_policy,
                    prewarm_status=prewarm_status,
                    attempts=int(job.get("attempts") or 0) + 1,
                    error=None,
                )
                record = await service.start(
                    market_date=market_date,
                    refresh_policy=refresh_policy,
                    report_profile="master_with_holding_appendices",
                    trigger="scheduled_0912",
                    force_new=False,
                )
                job = self.store.update(
                    market_date, state="running", run_id=str(record["run_id"])
                )
                target = self._target()
                if (
                    record.get("deduplicated") is True
                    and target is not None
                    and str(record.get("trigger") or "")
                    == str(target.get("channel") or "")
                ):
                    # The same channel is already monitoring and delivering the
                    # reused interactive run. Persist suppression before waiting
                    # so a restart cannot send the same result a second time.
                    job = self.store.update(
                        market_date,
                        delivery_status="origin_delivery_reused",
                    )

            if record.get("status") not in (
                _SUCCESS_RUN_STATUSES | _FAILED_RUN_STATUSES | {"interrupted"}
            ):
                record = await service.wait(str(record["run_id"]))

            if record.get("status") in _SUCCESS_RUN_STATUSES:
                job = self.store.update(
                    market_date,
                    state="completed",
                    run_id=str(record["run_id"]),
                    completed_at=_utc_now(),
                    error=None,
                )
                return await self._deliver_if_needed(job, record=record)

            if record.get("status") == "interrupted":
                # A process restart marks the run interrupted. Re-enter once so
                # the persisted recovery branch above creates exactly one retry.
                return await self._process_job(self.store.get(market_date) or job, local_now)

            raise RuntimeError(
                str(record.get("error") or f"daily run ended as {record.get('status')}")
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - persist and surface the failure
            job = self.store.update(
                market_date,
                state="failed",
                error=f"{type(exc).__name__}: {exc}",
                completed_at=_utc_now(),
            )
            record = None
            run_id = str(job.get("run_id") or "")
            if run_id:
                try:
                    record = self.run_service_factory().get_run(run_id)
                except Exception:  # noqa: BLE001 - failure delivery is best effort
                    record = None
            return await self._deliver_if_needed(job, record=record)

    async def _refresh_policy(self, market_date: date) -> tuple[str, str]:
        deadline = monotonic_time.monotonic() + self.prewarm_wait_seconds
        while True:
            status = self.prewarm_status_provider() or {}
            outcome = self._prewarm_outcome(status, market_date)
            if outcome == "completed":
                return "ensure_fresh", outcome
            if outcome in {"failed", "disabled"}:
                return "force", outcome
            if monotonic_time.monotonic() >= deadline:
                return "force", "timeout"
            await asyncio.sleep(self.prewarm_poll_seconds)

    @staticmethod
    def _prewarm_outcome(status: dict[str, Any], market_date: date) -> str:
        if status.get("enabled") is False:
            return "disabled"
        prefix = f"{market_date.isoformat()}:premarket:"
        active_slots = [str(item) for item in status.get("active_slots") or []]
        if any(item.startswith(prefix) for item in active_slots):
            return "running"
        last = status.get("last_run") or {}
        if not str(last.get("slot") or "").startswith(prefix):
            return "missing"
        last_status = str(last.get("status") or "").lower()
        if last_status in _PREWARM_SUCCESS_STATUSES:
            return "completed"
        if last_status == "running":
            return "running"
        return "failed"

    async def _deliver_if_needed(
        self,
        job: dict[str, Any],
        *,
        record: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        market_date = str(job["market_date"])
        delivery_status = str(job.get("delivery_status") or "pending")
        if delivery_status in _FINAL_DELIVERY_STATUSES:
            return job
        if delivery_status == "delivery_failed":
            # Migrate scheduler rows created before retryable configuration
            # failures had their own state. An external callback failure remains
            # ambiguous and must never be resent blindly.
            if "delivery target is not configured" in str(job.get("error") or ""):
                job = self.store.update(
                    market_date, delivery_status="delivery_waiting_target"
                )
                delivery_status = "delivery_waiting_target"
            else:
                return self.store.update(
                    market_date,
                    delivery_status="delivery_uncertain",
                    error=_append_error(
                        job.get("error"),
                        "legacy delivery failure may already have been sent; not resent",
                    ),
                )
        if str(job.get("mode") or "shadow") == "shadow":
            return self.store.update(
                market_date, delivery_status="shadow_suppressed"
            )
        if delivery_status == "delivering":
            return self.store.update(
                market_date,
                delivery_status="delivery_uncertain",
                error=_append_error(
                    job.get("error"),
                    "delivery state was ambiguous after restart; not resent",
                ),
            )
        target = self._target()
        if target is None or self.delivery_callback is None:
            return self.store.update(
                market_date,
                delivery_status="delivery_waiting_target",
                error=_append_error(
                    job.get("error"), "delivery target is not configured"
                ),
            )
        if not self.store.begin_delivery(market_date):
            current = self.store.get(market_date) or job
            if current.get("delivery_status") == "delivering":
                return self.store.update(
                    market_date, delivery_status="delivery_uncertain"
                )
            return current
        try:
            result = self.delivery_callback(record, self.store.get(market_date) or job, target)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 - never blindly resend an external message
            return self.store.update(
                market_date,
                delivery_status="delivery_uncertain",
                error=_append_error(
                    job.get("error"),
                    f"delivery failed ambiguously: {type(exc).__name__}: {exc}",
                ),
            )
        return self.store.update(
            market_date,
            delivery_status="delivered",
            delivered_at=_utc_now(),
            error=None,
        )
