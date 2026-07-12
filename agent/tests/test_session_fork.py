"""Session fork behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.session.events import EventBus
from src.session.models import Message
from src.session.service import SessionService
from src.session.store import SessionStore


class _DummyIndex:
    def index_session(self, session_id: str, title: str) -> None:
        del session_id, title

    def index_message(self, session_id: str, role: str, content: str) -> None:
        del session_id, role, content

    def reindex_from_store(self, store_base_dir: Path) -> int:
        del store_base_dir
        return 0


def _service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SessionService:
    monkeypatch.setattr("src.session.service.get_shared_index", lambda: _DummyIndex())
    return SessionService(
        store=SessionStore(tmp_path / "sessions"),
        event_bus=EventBus(),
        runs_dir=tmp_path / "runs",
    )


def test_fork_session_copies_history_through_selected_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path, monkeypatch)
    source = service.create_session(title="Original chat", config={"model": "test-model"})
    first = Message(session_id=source.session_id, role="user", content="first")
    second = Message(session_id=source.session_id, role="assistant", content="second")
    third = Message(session_id=source.session_id, role="user", content="third")
    for message in (first, second, third):
        service.store.append_message(message)

    fork = service.fork_session(source.session_id, after_message_id=second.message_id)

    copied = service.get_messages(fork.session_id, limit=10)
    assert fork.session_id != source.session_id
    assert fork.title == "Original chat (fork)"
    assert fork.config == {"model": "test-model"}
    assert [(msg.role, msg.content) for msg in copied] == [
        ("user", "first"),
        ("assistant", "second"),
    ]
    assert [msg.message_id for msg in copied] != [first.message_id, second.message_id]
    assert copied[0].metadata["forked_from_session_id"] == source.session_id
    assert copied[0].metadata["forked_from_message_id"] == first.message_id


def test_fork_session_does_not_modify_source_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path, monkeypatch)
    source = service.create_session(title="Original chat")
    first = Message(session_id=source.session_id, role="user", content="first")
    second = Message(session_id=source.session_id, role="assistant", content="second")
    for message in (first, second):
        service.store.append_message(message)

    service.fork_session(source.session_id, after_message_id=second.message_id)

    source_messages = service.get_messages(source.session_id, limit=10)
    assert [msg.message_id for msg in source_messages] == [first.message_id, second.message_id]
    assert [msg.content for msg in source_messages] == ["first", "second"]


def test_fork_session_rejects_user_message_as_branch_point(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path, monkeypatch)
    source = service.create_session(title="Original chat")
    user_message = Message(session_id=source.session_id, role="user", content="first")
    service.store.append_message(user_message)

    with pytest.raises(PermissionError, match="only start from assistant outputs"):
        service.fork_session(source.session_id, after_message_id=user_message.message_id)


def test_fork_session_requires_target_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path, monkeypatch)
    source = service.create_session(title="Original chat")
    service.store.append_message(Message(session_id=source.session_id, role="user", content="first"))

    with pytest.raises(ValueError, match="Message missing not found"):
        service.fork_session(source.session_id, after_message_id="missing")


def test_edit_latest_user_message_prunes_later_response_and_reruns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path, monkeypatch)
    session = service.create_session(title="Editable chat")
    first = Message(session_id=session.session_id, role="user", content="old prompt", linked_attempt_id="old_attempt")
    failed = Message(session_id=session.session_id, role="assistant", content="Execution failed: cancelled by user", linked_attempt_id="old_attempt")
    for message in (first, failed):
        service.store.append_message(message)

    async def fake_run_attempt(session_arg, attempt, *, include_shell_tools=False):
        del session_arg, attempt, include_shell_tools

    monkeypatch.setattr(service, "_run_attempt", fake_run_attempt)

    result = asyncio.run(service.edit_user_message(session.session_id, first.message_id, "new prompt"))

    messages = service.get_messages(session.session_id, limit=10)
    assert result["message_id"] == first.message_id
    assert result["attempt_id"]
    assert [(msg.role, msg.content) for msg in messages] == [("user", "new prompt")]
    assert messages[0].linked_attempt_id == result["attempt_id"]
    assert messages[0].metadata["original_content"] == "old prompt"


def test_edit_user_message_requires_latest_user_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path, monkeypatch)
    session = service.create_session(title="Editable chat")
    first = Message(session_id=session.session_id, role="user", content="first")
    second = Message(session_id=session.session_id, role="user", content="second")
    for message in (first, second):
        service.store.append_message(message)

    with pytest.raises(ValueError, match="Only the latest user message"):
        asyncio.run(service.edit_user_message(session.session_id, first.message_id, "new prompt"))
