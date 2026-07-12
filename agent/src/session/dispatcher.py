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
    source_metadata: Dict[str, Any] = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    attempt_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "pending"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None


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
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_dispatch_pending ON dispatch_jobs(status, created_at)"
            )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> DispatchJob:
        return DispatchJob(
            job_id=row["job_id"],
            session_id=row["session_id"],
            content=row["content"],
            source=row["source"],
            source_metadata=json.loads(row["source_metadata"] or "{}"),
            message_id=row["message_id"],
            attempt_id=row["attempt_id"],
            status=row["status"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            error=row["error"],
        )

    def add(self, job: DispatchJob) -> DispatchJob:
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO dispatch_jobs
                (job_id, session_id, content, source, source_metadata, message_id,
                 attempt_id, status, created_at, started_at, completed_at, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job.job_id,
                    job.session_id,
                    job.content,
                    job.source,
                    json.dumps(job.source_metadata, ensure_ascii=False),
                    job.message_id,
                    job.attempt_id,
                    job.status,
                    job.created_at,
                    job.started_at,
                    job.completed_at,
                    job.error,
                ),
            )
        return job

    def get(self, job_id: str) -> Optional[DispatchJob]:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM dispatch_jobs WHERE job_id = ?", (job_id,)).fetchone()
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

    def recover_interrupted(self) -> int:
        """Fail jobs that were executing at process exit; pending jobs remain queued."""
        now = datetime.now().isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """UPDATE dispatch_jobs
                SET status = 'failed', completed_at = ?, error = 'service restarted during execution'
                WHERE status = 'running'""",
                (now,),
            )
            return cursor.rowcount

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
        self.store.recover_interrupted()
        self._stopping = False
        self._supervisor = asyncio.create_task(self._run(), name="session-dispatcher")
        self._wake.set()

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._supervisor:
            await self._supervisor
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def submit(
        self,
        session_id: str,
        content: str,
        *,
        source: str = "api",
        source_metadata: Optional[Dict[str, Any]] = None,
        include_shell_tools: bool = False,
    ) -> Dict[str, Any]:
        if not self.service.get_session(session_id):
            raise ValueError(f"Session {session_id} not found")
        job = DispatchJob(
            session_id=session_id,
            content=content,
            source=source,
            source_metadata={**(source_metadata or {}), "include_shell_tools": bool(include_shell_tools)},
        )
        self.store.add(job)
        await self._notify(job)
        self._wake.set()
        return {
            "job_id": job.job_id,
            "message_id": job.message_id,
            "attempt_id": job.attempt_id,
            "status": "queued",
            "queue_position": self.store.queue_position(job),
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
