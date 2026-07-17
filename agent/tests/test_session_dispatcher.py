from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from src.session.dispatcher import DispatchJob, DispatchStore, SessionDispatcher
from src.session.models import AttemptStatus


class _AttemptStore:
    def __init__(self) -> None:
        self.attempts = {}

    def get_attempt(self, session_id: str, attempt_id: str):
        return self.attempts.get((session_id, attempt_id))


class _FakeService:
    def __init__(self, delay: float = 0.03) -> None:
        self.sessions = {"s1": object(), "s2": object()}
        self.store = _AttemptStore()
        self.delay = delay
        self.events: list[tuple[str, str, str]] = []
        self.active_by_session: dict[str, int] = {}
        self.max_global_active = 0
        self.global_active = 0

    def get_session(self, session_id: str):
        return self.sessions.get(session_id)

    async def execute_message(self, session_id: str, content: str, **kwargs):
        attempt_id = kwargs["attempt_id"]
        self.active_by_session[session_id] = self.active_by_session.get(session_id, 0) + 1
        assert self.active_by_session[session_id] == 1
        self.global_active += 1
        self.max_global_active = max(self.max_global_active, self.global_active)
        self.events.append(("start", session_id, content))
        await asyncio.sleep(self.delay)
        self.events.append(("end", session_id, content))
        self.global_active -= 1
        self.active_by_session[session_id] -= 1
        self.store.attempts[(session_id, attempt_id)] = SimpleNamespace(
            status=AttemptStatus.COMPLETED,
            error=None,
        )

    def cancel_current(self, session_id: str) -> bool:
        return False


async def _wait_terminal(store: DispatchStore, job_ids: list[str]) -> None:
    for _ in range(200):
        if all(store.get(job_id).status in {"completed", "failed", "cancelled"} for job_id in job_ids):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("dispatcher jobs did not finish")


def test_dispatcher_serializes_each_session_and_runs_sessions_in_parallel(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = _FakeService()
        store = DispatchStore(tmp_path / "dispatch.db")
        dispatcher = SessionDispatcher(service, store=store, max_concurrency=2)
        dispatcher.start()
        first = await dispatcher.submit("s1", "one")
        second = await dispatcher.submit("s1", "two")
        other = await dispatcher.submit("s2", "other")
        await _wait_terminal(store, [first["job_id"], second["job_id"], other["job_id"]])
        await dispatcher.stop()

        s1_events = [(kind, content) for kind, sid, content in service.events if sid == "s1"]
        assert s1_events == [("start", "one"), ("end", "one"), ("start", "two"), ("end", "two")]
        assert service.max_global_active == 2

    asyncio.run(scenario())


def test_dispatch_store_recovers_running_without_replaying(tmp_path: Path) -> None:
    store = DispatchStore(tmp_path / "dispatch.db")
    pending = store.add(DispatchJob(session_id="s1", content="pending"))
    running = store.add(DispatchJob(session_id="s2", content="running"))
    store.update(running.job_id, "running")

    assert store.recover_interrupted() == 1
    assert store.get(pending.job_id).status == "pending"
    assert store.get(running.job_id).status == "failed"
    assert "restarted" in store.get(running.job_id).error


def test_cancel_session_clears_all_pending_jobs(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = _FakeService()
        store = DispatchStore(tmp_path / "dispatch.db")
        dispatcher = SessionDispatcher(service, store=store, max_concurrency=1)
        first = await dispatcher.submit("s1", "one")
        second = await dispatcher.submit("s1", "two")
        result = await dispatcher.cancel_session("s1")
        assert result == {"status": "cancelled", "running": False, "queued": 2}
        assert store.get(first["job_id"]).status == "cancelled"
        assert store.get(second["job_id"]).status == "cancelled"

    asyncio.run(scenario())


def test_feishu_source_event_is_durable_and_idempotent_for_burst_messages(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        service = _FakeService(delay=0.01)
        store = DispatchStore(tmp_path / "dispatch.db")
        dispatcher = SessionDispatcher(service, store=store, max_concurrency=1)

        payloads = ["你好", "请详细分析 513120 的风险与机会", "继续，重点说估值"]
        submitted = [
            await dispatcher.submit(
                "s1",
                content,
                source="feishu",
                source_event_id=f"om-{index}",
                source_metadata={"channel_chat_id": "ou-user"},
            )
            for index, content in enumerate(payloads)
        ]
        duplicate = await dispatcher.submit(
            "s1",
            payloads[1],
            source="feishu",
            source_event_id="om-1",
            source_metadata={"channel_chat_id": "ou-user"},
        )

        assert len({item["job_id"] for item in submitted}) == 3
        assert all(item["deduplicated"] is False for item in submitted)
        assert duplicate["deduplicated"] is True
        assert duplicate["job_id"] == submitted[1]["job_id"]
        assert store.get(submitted[1]["job_id"]).source_event_id == "om-1"
        assert store.get(submitted[1]["job_id"]).delivery_status == "pending"

        dispatcher.start()
        await _wait_terminal(store, [item["job_id"] for item in submitted])
        await dispatcher.stop()
        starts = [content for kind, _sid, content in service.events if kind == "start"]
        assert starts == payloads

    asyncio.run(scenario())


def test_dispatch_store_migrates_existing_sqlite_in_place(tmp_path: Path) -> None:
    path = tmp_path / "dispatch.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE dispatch_jobs (
            job_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, content TEXT NOT NULL,
            source TEXT NOT NULL, source_metadata TEXT NOT NULL, message_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
            started_at TEXT, completed_at TEXT, error TEXT)"""
        )
        connection.execute(
            """INSERT INTO dispatch_jobs VALUES
            ('legacy', 's1', 'old', 'api', '{}', 'm1', 'a1', 'pending',
             '2026-01-01T00:00:00', NULL, NULL, NULL)"""
        )

    store = DispatchStore(path)
    legacy = store.get("legacy")

    assert legacy is not None
    assert legacy.content == "old"
    assert legacy.source_event_id is None
    assert legacy.delivery_status == "not_required"
    assert legacy.delivery_attempts == 0


def test_dispatch_store_resumes_an_uncertain_delivery_with_same_logical_attempt(
    tmp_path: Path,
) -> None:
    store = DispatchStore(tmp_path / "dispatch.db")
    job = store.add(
        DispatchJob(
            session_id="s1",
            content="answer",
            source="feishu",
            source_event_id="om-uncertain",
            status="completed",
            delivery_status="delivering",
            delivery_attempts=3,
        )
    )

    assert [item.job_id for item in store.undelivered_terminal()] == [job.job_id]
    resumed = store.start_delivery_attempt(job.job_id)
    assert resumed is not None
    assert resumed.delivery_attempts == 3
    assert resumed.delivery_status == "delivering"
