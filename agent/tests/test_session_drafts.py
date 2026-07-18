from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.session.dispatcher import DispatchStore, SessionDispatcher
from src.session.events import EventBus
from src.session.service import SessionService
from src.session.store import SessionStore


class _DummyIndex:
    def index_session(self, *_args, **_kwargs) -> None:
        return None

    def index_message(self, *_args, **_kwargs) -> None:
        return None


def _service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SessionService:
    monkeypatch.setattr("src.session.service.get_shared_index", lambda: _DummyIndex())
    return SessionService(
        store=SessionStore(tmp_path / "sessions"),
        event_bus=EventBus(),
        runs_dir=tmp_path / "runs",
    )


def test_web_chat_draft_is_not_a_session_until_first_message_is_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        service = _service(tmp_path, monkeypatch)
        draft = service.create_draft_session(title="first prompt")

        assert service.get_session(draft.session_id) is draft
        assert service.store.get_session(draft.session_id) is None
        assert service.list_sessions() == []
        assert not (tmp_path / "sessions" / draft.session_id).exists()

        dispatcher = SessionDispatcher(
            service,
            store=DispatchStore(tmp_path / "dispatch.db"),
        )
        result = await dispatcher.submit(draft.session_id, "hello")

        assert result["status"] == "queued"
        assert service.is_draft_session(draft.session_id) is False
        assert service.store.get_session(draft.session_id) is not None
        assert [item.session_id for item in service.list_sessions()] == [draft.session_id]

    asyncio.run(scenario())


def test_failed_first_message_queue_does_not_leave_an_empty_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        service = _service(tmp_path, monkeypatch)
        draft = service.create_draft_session(title="will fail")
        dispatcher = SessionDispatcher(
            service,
            store=DispatchStore(tmp_path / "dispatch.db"),
        )

        def fail_add(_job):
            raise RuntimeError("queue unavailable")

        monkeypatch.setattr(dispatcher.store, "add", fail_add)
        with pytest.raises(RuntimeError, match="queue unavailable"):
            await dispatcher.submit(draft.session_id, "hello")

        assert service.get_session(draft.session_id) is None
        assert service.list_sessions() == []
        assert not (tmp_path / "sessions" / draft.session_id).exists()

    asyncio.run(scenario())
