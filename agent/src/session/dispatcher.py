"""Persistent, session-aware message dispatch for API and chat channels."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from src.config.paths import get_runtime_root
from src.session.models import AttemptStatus

logger = logging.getLogger(__name__)

TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled"})


@dataclass
class DispatchJob:
    """One queued user turn reserved for a Vibe-Trading session."""

    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    session_id: str = ""
    content: str = ""
    source: str = "api"
    source_event_id: Optional[str] = None
    source_metadata: Dict[str, Any] = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    attempt_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "pending"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None
    delivery_status: str = "not_required"
    delivery_attempts: int = 0
    delivered_at: Optional[str] = None
    delivery_error: Optional[str] = None


class DispatchStore:
    """Small SQLite queue that survives process restarts."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or (get_runtime_root() / "channels" / "dispatch.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS dispatch_jobs (
                    job_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_metadata TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    error TEXT
                )
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(dispatch_jobs)").fetchall()
            }
            migrations = {
                "source_event_id": "ALTER TABLE dispatch_jobs ADD COLUMN source_event_id TEXT",
                "delivery_status": (
                    "ALTER TABLE dispatch_jobs ADD COLUMN delivery_status "
                    "TEXT NOT NULL DEFAULT 'not_required'"
                ),
                "delivery_attempts": (
                    "ALTER TABLE dispatch_jobs ADD COLUMN delivery_attempts "
                    "INTEGER NOT NULL DEFAULT 0"
                ),
                "delivered_at": "ALTER TABLE dispatch_jobs ADD COLUMN delivered_at TEXT",
                "delivery_error": "ALTER TABLE dispatch_jobs ADD COLUMN delivery_error TEXT",
            }
            for name, statement in migrations.items():
                if name not in columns:
                    connection.execute(statement)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_dispatch_pending ON dispatch_jobs(status, created_at)"
            )
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_dispatch_source_event
                ON dispatch_jobs(source, source_event_id)
                WHERE source_event_id IS NOT NULL"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_dispatch_delivery
                ON dispatch_jobs(delivery_status, status, completed_at)"""
            )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> DispatchJob:
        return DispatchJob(
            job_id=row["job_id"],
            session_id=row["session_id"],
            content=row["content"],
            source=row["source"],
            source_event_id=row["source_event_id"],
            source_metadata=json.loads(row["source_metadata"] or "{}"),
            message_id=row["message_id"],
            attempt_id=row["attempt_id"],
            status=row["status"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            error=row["error"],
            delivery_status=row["delivery_status"],
            delivery_attempts=int(row["delivery_attempts"] or 0),
            delivered_at=row["delivered_at"],
            delivery_error=row["delivery_error"],
        )

    def add(self, job: DispatchJob) -> DispatchJob:
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO dispatch_jobs
                (job_id, session_id, content, source, source_event_id, source_metadata,
                 message_id, attempt_id, status, created_at, started_at, completed_at,
                 error, delivery_status, delivery_attempts, delivered_at, delivery_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job.job_id,
                    job.session_id,
                    job.content,
                    job.source,
                    job.source_event_id,
                    json.dumps(job.source_metadata, ensure_ascii=False),
                    job.message_id,
                    job.attempt_id,
                    job.status,
                    job.created_at,
                    job.started_at,
                    job.completed_at,
                    job.error,
                    job.delivery_status,
                    job.delivery_attempts,
                    job.delivered_at,
                    job.delivery_error,
                ),
            )
        return job

    def get(self, job_id: str) -> Optional[DispatchJob]:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM dispatch_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._from_row(row) if row else None

    def get_by_source_event(self, source: str, source_event_id: str) -> Optional[DispatchJob]:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM dispatch_jobs WHERE source = ? AND source_event_id = ?",
                (source, source_event_id),
            ).fetchone()
        return self._from_row(row) if row else None

    def pending(self, limit: int = 200) -> list[DispatchJob]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM dispatch_jobs WHERE status = 'pending' ORDER BY created_at, rowid LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def update(self, job_id: str, status: str, *, error: Optional[str] = None) -> Optional[DispatchJob]:
        now = datetime.now().isoformat()
        fields = ["status = ?", "error = ?"]
        values: list[Any] = [status, error]
        if status == "running":
            fields.append("started_at = ?")
            values.append(now)
        if status in TERMINAL_JOB_STATUSES:
            fields.append("completed_at = ?")
            values.append(now)
        values.append(job_id)
        with self._lock, self._connect() as connection:
            connection.execute(f"UPDATE dispatch_jobs SET {', '.join(fields)} WHERE job_id = ?", values)
        return self.get(job_id)

    def recover_interrupted_jobs(self) -> list[DispatchJob]:
        """Fail jobs that were executing at process exit; pending jobs remain queued."""
        now = datetime.now().isoformat()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id FROM dispatch_jobs WHERE status = 'running'"
            ).fetchall()
            connection.execute(
                """UPDATE dispatch_jobs
                SET status = 'failed', completed_at = ?, error = 'service restarted during execution'
                WHERE status = 'running'""",
                (now,),
            )
        return [job for row in rows if (job := self.get(str(row["job_id"]))) is not None]

    def recover_interrupted(self) -> int:
        return len(self.recover_interrupted_jobs())

    def undelivered_terminal(self, limit: int = 200) -> list[DispatchJob]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM dispatch_jobs
                WHERE status IN ('completed', 'failed', 'cancelled')
                  AND delivery_status IN ('pending', 'delivering', 'retrying')
                  AND (delivery_attempts < 3 OR delivery_status = 'delivering')
                ORDER BY completed_at, rowid LIMIT ?""",
                (limit,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def pending_delivery_count(self) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS count FROM dispatch_jobs
                WHERE status IN ('completed', 'failed', 'cancelled')
                  AND delivery_status IN ('pending', 'delivering', 'retrying')
                  AND (delivery_attempts < 3 OR delivery_status = 'delivering')"""
            ).fetchone()
        return int(row["count"] if row else 0)

    def latest_persisted_at(self, source: Optional[str] = None) -> Optional[str]:
        query = "SELECT created_at FROM dispatch_jobs"
        values: tuple[Any, ...] = ()
        if source:
            query += " WHERE source = ? AND source_event_id IS NOT NULL"
            values = (source,)
        query += " ORDER BY created_at DESC, rowid DESC LIMIT 1"
        with self._lock, self._connect() as connection:
            row = connection.execute(query, values).fetchone()
        return str(row["created_at"]) if row else None

    def latest_error(self, source: Optional[str] = None) -> Optional[dict[str, str]]:
        source_clause = " AND source = ?" if source else ""
        values: tuple[Any, ...] = (source,) if source else ()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                f"""SELECT job_id, source_event_id, error, delivery_error,
                          COALESCE(completed_at, created_at) AS error_at
                FROM dispatch_jobs
                WHERE (error IS NOT NULL OR delivery_error IS NOT NULL)
                {source_clause}
                ORDER BY COALESCE(completed_at, created_at) DESC, rowid DESC LIMIT 1""",
                values,
            ).fetchone()
        if not row:
            return None
        return {
            "job_id": str(row["job_id"]),
            "source_event_id": str(row["source_event_id"] or ""),
            "error": str(row["delivery_error"] or row["error"] or ""),
            "at": str(row["error_at"] or ""),
        }

    def start_delivery_attempt(self, job_id: str) -> Optional[DispatchJob]:
        with self._lock, self._connect() as connection:
            connection.execute(
                """UPDATE dispatch_jobs
                SET delivery_status = 'delivering',
                    delivery_attempts = CASE
                        WHEN delivery_status = 'delivering' AND delivery_attempts > 0
                        THEN delivery_attempts
                        ELSE delivery_attempts + 1
                    END,
                    delivery_error = NULL
                WHERE job_id = ?
                  AND (delivery_attempts < 3 OR delivery_status = 'delivering')
                  AND delivery_status != 'delivered'""",
                (job_id,),
            )
        return self.get(job_id)

    def mark_delivery_retry(self, job_id: str, error: str) -> Optional[DispatchJob]:
        with self._lock, self._connect() as connection:
            connection.execute(
                """UPDATE dispatch_jobs
                SET delivery_status = CASE WHEN delivery_attempts >= 3 THEN 'failed' ELSE 'retrying' END,
                    delivery_error = ?
                WHERE job_id = ? AND delivery_status != 'delivered'""",
                (error, job_id),
            )
        return self.get(job_id)

    def mark_delivered(self, job_id: str) -> Optional[DispatchJob]:
        now = datetime.now().isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                """UPDATE dispatch_jobs
                SET delivery_status = 'delivered', delivered_at = ?, delivery_error = NULL
                WHERE job_id = ?""",
                (now, job_id),
            )
        return self.get(job_id)

    def cancel_session(self, session_id: str) -> list[DispatchJob]:
        now = datetime.now().isoformat()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM dispatch_jobs WHERE session_id = ? AND status = 'pending'",
                (session_id,),
            ).fetchall()
            connection.execute(
                """UPDATE dispatch_jobs
                SET status = 'cancelled', completed_at = ?, error = 'cancelled by user'
                WHERE session_id = ? AND status = 'pending'""",
                (now, session_id),
            )
        return [self._from_row(row) for row in rows]

    def queue_position(self, job: DispatchJob) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS count FROM dispatch_jobs
                WHERE session_id = ? AND status = 'pending' AND (created_at < ? OR (created_at = ? AND rowid <= (
                    SELECT rowid FROM dispatch_jobs WHERE job_id = ?
                )))""",
                (job.session_id, job.created_at, job.created_at, job.job_id),
            ).fetchone()
        return int(row["count"] if row else 0)


JobListener = Callable[[DispatchJob], Optional[Awaitable[None]]]


class SessionDispatcher:
    """Run one turn per session while sharing a bounded global worker pool."""

    def __init__(self, service: Any, store: Optional[DispatchStore] = None, max_concurrency: int = 4) -> None:
        self.service = service
        self.store = store or DispatchStore()
        self.max_concurrency = max(1, min(int(max_concurrency), 32))
        self._active_sessions: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._listeners: list[JobListener] = []
        self._notification_tasks: set[asyncio.Task] = set()
        self._wake = asyncio.Event()
        self._supervisor: Optional[asyncio.Task] = None
        self._stopping = False

    def add_listener(self, listener: JobListener) -> None:
        self._listeners.append(listener)

    async def _notify(self, job: DispatchJob) -> None:
        for listener in list(self._listeners):
            try:
                result = listener(job)
                if result is not None:
                    await result
            except Exception:
                logger.warning("Dispatch listener failed for job %s", job.job_id, exc_info=True)

    def start(self) -> None:
        if self._supervisor and not self._supervisor.done():
            return
        recovered = self.store.recover_interrupted_jobs()
        self._stopping = False
        self._supervisor = asyncio.create_task(self._run(), name="session-dispatcher")
        for job in recovered:
            task = asyncio.create_task(self._notify(job), name=f"dispatch-recovered-{job.job_id}")
            self._notification_tasks.add(task)
            task.add_done_callback(self._notification_tasks.discard)
        self._wake.set()

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._supervisor:
            await self._supervisor
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._notification_tasks:
            await asyncio.gather(*self._notification_tasks, return_exceptions=True)

    async def submit(
        self,
        session_id: str,
        content: str,
        *,
        source: str = "api",
        source_event_id: Optional[str] = None,
        source_metadata: Optional[Dict[str, Any]] = None,
        include_shell_tools: bool = False,
    ) -> Dict[str, Any]:
        if not self.service.get_session(session_id):
            raise ValueError(f"Session {session_id} not found")
        if source_event_id:
            existing = self.store.get_by_source_event(source, source_event_id)
            if existing is not None:
                return self._submission_result(existing, deduplicated=True)
        job = DispatchJob(
            session_id=session_id,
            content=content,
            source=source,
            source_event_id=source_event_id,
            source_metadata={**(source_metadata or {}), "include_shell_tools": bool(include_shell_tools)},
            delivery_status=(
                "pending" if source != "api" and bool(source_event_id) else "not_required"
            ),
        )
        is_draft = getattr(self.service, "is_draft_session", None)
        persist_draft = getattr(self.service, "persist_draft_session", None)
        committed_draft = bool(callable(is_draft) and is_draft(session_id))
        try:
            if committed_draft and callable(persist_draft):
                # The first real user message is now being accepted, so the
                # web-chat draft may become a session. Queue failure rolls it
                # back below.
                persist_draft(session_id)
            self.store.add(job)
        except sqlite3.IntegrityError:
            if committed_draft:
                self.service.delete_session(session_id)
            if not source_event_id:
                raise
            existing = self.store.get_by_source_event(source, source_event_id)
            if existing is None:
                raise
            return self._submission_result(existing, deduplicated=True)
        except Exception:
            if committed_draft:
                self.service.delete_session(session_id)
            raise
        await self._notify(job)
        self._wake.set()
        return self._submission_result(job, deduplicated=False)

    def _submission_result(self, job: DispatchJob, *, deduplicated: bool) -> Dict[str, Any]:
        return {
            "job_id": job.job_id,
            "message_id": job.message_id,
            "attempt_id": job.attempt_id,
            "status": "queued" if job.status == "pending" else job.status,
            "queue_position": self.store.queue_position(job) if job.status == "pending" else 0,
            "deduplicated": deduplicated,
        }

    async def cancel_session(self, session_id: str) -> Dict[str, Any]:
        pending = self.store.cancel_session(session_id)
        for job in pending:
            cancelled = self.store.get(job.job_id)
            if cancelled:
                await self._notify(cancelled)
        running_cancelled = self.service.cancel_current(session_id)
        self._wake.set()
        return {"status": "cancelled", "running": running_cancelled, "queued": len(pending)}

    async def _run(self) -> None:
        while True:
            launched = False
            if not self._stopping:
                for job in self.store.pending():
                    if len(self._tasks) >= self.max_concurrency:
                        break
                    if job.session_id in self._active_sessions:
                        continue
                    self._active_sessions.add(job.session_id)
                    task = asyncio.create_task(self._execute(job), name=f"dispatch-{job.job_id}")
                    self._tasks.add(task)
                    task.add_done_callback(self._task_done)
                    launched = True
            if self._stopping and not self._tasks:
                return
            if launched:
                await asyncio.sleep(0)
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        self._wake.set()

    async def _execute(self, job: DispatchJob) -> None:
        running = self.store.update(job.job_id, "running")
        if running:
            await self._notify(running)
        try:
            await self.service.execute_message(
                session_id=job.session_id,
                content=job.content,
                include_shell_tools=bool(job.source_metadata.get("include_shell_tools", False)),
                message_id=job.message_id,
                attempt_id=job.attempt_id,
                message_metadata={"source": job.source, **job.source_metadata},
            )
            attempt = self.service.store.get_attempt(job.session_id, job.attempt_id)
            attempt_status = getattr(getattr(attempt, "status", None), "value", None)
            if attempt_status == AttemptStatus.COMPLETED.value:
                status, error = "completed", None
            elif attempt_status == AttemptStatus.CANCELLED.value:
                status, error = "cancelled", getattr(attempt, "error", None)
            else:
                status, error = "failed", getattr(attempt, "error", "execution failed")
            terminal = self.store.update(job.job_id, status, error=error)
            if terminal:
                await self._notify(terminal)
        except Exception as exc:
            terminal = self.store.update(job.job_id, "failed", error=str(exc))
            if terminal:
                await self._notify(terminal)
        finally:
            self._active_sessions.discard(job.session_id)
            self._wake.set()
