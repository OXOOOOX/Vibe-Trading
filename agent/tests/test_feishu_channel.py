from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from src.channels.feishu import FeishuBindingStore, FeishuBot
from src.session.models import Session


class _FakeTransport:
    def __init__(self) -> None:
        self.cards = []
        self.updates = []
        self.texts = []

    def reply_card(self, message_id, card):
        self.cards.append((message_id, card))
        return f"ack-{message_id}"

    def update_card(self, message_id, card):
        self.updates.append((message_id, card))

    def reply_text(self, message_id, text):
        self.texts.append((message_id, text))
        return f"text-{message_id}"


class _FakeDispatcher:
    def __init__(self) -> None:
        self.listeners = []
        self.submissions = []

    def add_listener(self, listener):
        self.listeners.append(listener)

    async def submit(self, session_id, content, **kwargs):
        self.submissions.append((session_id, content, kwargs))
        return {"job_id": "j1", "message_id": "m1", "attempt_id": "a1", "queue_position": 1}

    async def cancel_session(self, session_id):
        return {"status": "cancelled", "running": True, "queued": 0}


class _FakeService:
    def __init__(self) -> None:
        self.sessions = {}
        self.store = SimpleNamespace(get_attempt=lambda *args: None)

    def create_session(self, title="", config=None):
        session = Session(title=title, config=config or {})
        self.sessions[session.session_id] = session
        return session

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def list_sessions(self, limit=50):
        return list(self.sessions.values())[:limit]


def _event(message_id: str, text: str, *, root_id: str = ""):
    mention = SimpleNamespace(key="@_user_1")
    message = SimpleNamespace(
        message_id=message_id,
        root_id=root_id,
        thread_id="",
        chat_id="oc_allowed",
        chat_type="group",
        message_type="text",
        content=json.dumps({"text": f"@_user_1 {text}"}),
        mentions=[mention],
    )
    sender = SimpleNamespace(sender_type="user", sender_id=SimpleNamespace(open_id="ou_owner"))
    return SimpleNamespace(
        header=SimpleNamespace(tenant_key="tenant"),
        event=SimpleNamespace(message=message, sender=sender),
    )


def test_group_topic_maps_to_one_session_and_deduplicates(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FEISHU_ALLOWED_CHAT_IDS", "oc_allowed")
    service = _FakeService()
    dispatcher = _FakeDispatcher()
    transport = _FakeTransport()
    bot = FeishuBot(
        service,
        dispatcher,
        binding_store=FeishuBindingStore(tmp_path / "feishu.db"),
        transport=transport,
    )

    async def scenario() -> None:
        await bot.handle_event(_event("root-1", "研究贵州茅台"))
        await bot.handle_event(_event("reply-1", "继续做估值", root_id="root-1"))
        await bot.handle_event(_event("reply-1", "重复推送", root_id="root-1"))

    asyncio.run(scenario())
    assert len(service.sessions) == 1
    assert [item[1] for item in dispatcher.submissions] == ["研究贵州茅台", "继续做估值"]
    assert dispatcher.submissions[0][0] == dispatcher.submissions[1][0]
    assert len(transport.cards) == 2


def test_group_live_control_request_is_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FEISHU_ALLOWED_CHAT_IDS", "oc_allowed")
    service = _FakeService()
    dispatcher = _FakeDispatcher()
    transport = _FakeTransport()
    bot = FeishuBot(
        service,
        dispatcher,
        binding_store=FeishuBindingStore(tmp_path / "feishu.db"),
        transport=transport,
    )

    asyncio.run(bot.handle_event(_event("root-live", "立即实盘下单买入 600519")))
    assert dispatcher.submissions == []
    assert any("只开放研究与回测" in text for _, text in transport.texts)
