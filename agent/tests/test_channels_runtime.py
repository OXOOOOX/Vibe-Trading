"""Contracts for the IM channel runtime wiring."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import pytest

from src.channels.bus.events import InboundMessage, OutboundMessage
from src.channels.bus.queue import MessageBus
from src.channels.manager import ChannelManager
from src.channels.registry import discover_channel_names, inspect_channels
from src.config.schema import ChannelsConfig
from src.channelsui.cli_apps_api import normalize_cli_app_mentions
from src.channelsui.gateway_services import build_gateway_services
from src.channelsui.mcp_presets_api import normalize_mcp_preset_mentions
from src.channelsui.transcription_ws import webui_transcription_event
from src.session.goal_state import goal_state_ws_blob
from src.session.models import Message, Session
from src.session.webui_turns import (
    clear_websocket_turn_started,
    mark_websocket_turn_started,
    websocket_turn_wall_started_at,
)
from src.utils.media_decode import FileSizeExceeded, save_base64_data_url


class FakeSessionService:
    """Small SessionService stand-in for channel runtime tests."""

    def __init__(self) -> None:
        self.created: list[Session] = []
        self.sent: list[tuple[str, str]] = []
        self.messages: dict[str, list[Message]] = {}

    def create_session(self, title: str = "", config: dict[str, Any] | None = None) -> Session:
        session = Session(session_id=f"session-{len(self.created) + 1}", title=title, config=config or {})
        self.created.append(session)
        self.messages[session.session_id] = []
        return session

    def get_session(self, session_id: str) -> Session | None:
        return next((session for session in self.created if session.session_id == session_id), None)

    def list_sessions(self, limit: int = 50) -> list[Session]:
        return list(reversed(self.created))[:limit]

    async def send_message(
        self,
        session_id: str,
        content: str,
        *,
        include_shell_tools: bool = False,
        parent_attempt_id: str | None = None,
    ) -> dict[str, str]:
        del include_shell_tools, parent_attempt_id
        self.sent.append((session_id, content))
        attempt_id = f"attempt-{len(self.sent)}"
        self.messages[session_id].append(
            Message(
                session_id=session_id,
                role="assistant",
                content=getattr(self, "reply_content", f"agent reply: {content}"),
                linked_attempt_id=attempt_id,
            )
        )
        return {"message_id": "msg-1", "attempt_id": attempt_id}

    def get_messages(self, session_id: str, limit: int = 100) -> list[Message]:
        del limit
        return list(self.messages.get(session_id, []))


class FakeDispatcher:
    """Record channel submissions while reusing the fake service reply path."""

    def __init__(self, service: FakeSessionService) -> None:
        self.service = service
        self.submitted: list[dict[str, Any]] = []
        self.cancelled: list[str] = []

    async def submit(
        self,
        session_id: str,
        content: str,
        *,
        source: str,
        source_metadata: dict[str, Any],
        include_shell_tools: bool,
    ) -> dict[str, str]:
        self.submitted.append(
            {
                "session_id": session_id,
                "content": content,
                "source": source,
                "source_metadata": source_metadata,
                "include_shell_tools": include_shell_tools,
            }
        )
        return await self.service.send_message(
            session_id,
            content,
            include_shell_tools=include_shell_tools,
        )

    async def cancel_session(self, session_id: str) -> dict[str, Any]:
        self.cancelled.append(session_id)
        return {"status": "cancelled", "running": True, "queued": 2}


def test_channel_manager_can_construct_websocket_with_default_gateway() -> None:
    bus = MessageBus()
    manager = ChannelManager(
        {"websocket": {"enabled": True, "host": "127.0.0.1", "port": 0, "allow_from": ["*"]}},
        bus,
    )

    assert "websocket" in manager.channels
    assert manager.channels["websocket"].name == "websocket"


def test_registry_reports_all_built_in_channels_with_dependency_recovery() -> None:
    expected = {
        "dingtalk",
        "discord",
        "email",
        "feishu",
        "matrix",
        "mochat",
        "msteams",
        "napcat",
        "qq",
        "signal",
        "slack",
        "telegram",
        "websocket",
        "wecom",
        "weixin",
        "whatsapp",
    }

    assert expected.issubset(set(discover_channel_names()))
    assert {"config", "runtime"}.isdisjoint(set(discover_channel_names()))

    statuses = inspect_channels({"telegram": {"enabled": True}, "slack": {"enabled": True}})

    for name in expected:
        assert name in statuses
        assert statuses[name]["name"] == name
        assert "install_hint" in statuses[name]
        assert "available" in statuses[name]

    missing = [item for item in statuses.values() if not item["available"]]
    assert all(item["install_hint"].startswith("pip install") for item in missing)


def test_registry_ignores_global_channel_settings() -> None:
    statuses = inspect_channels(
        ChannelsConfig.model_validate(
            {
                "replyTimeoutS": 120,
                "sendMaxRetries": 1,
                "telegram": {"enabled": True},
            }
        )
    )

    assert "telegram" in statuses
    assert "reply_timeout_s" not in statuses
    assert "send_max_retries" not in statuses
    assert "model_dump" not in statuses


def test_channel_manager_status_includes_every_configured_adapter() -> None:
    bus = MessageBus()
    manager = ChannelManager(
        {
            "send_max_retries": 1,
            "reply_timeout_s": 600.0,
            "websocket": {"enabled": True, "host": "127.0.0.1", "port": 0, "allow_from": ["*"]},
            "telegram": {"enabled": False},
            "slack": {"enabled": True},
        },
        bus,
    )

    status = manager.get_status()

    assert "send_max_retries" not in status
    assert "reply_timeout_s" not in status
    assert status["websocket"]["loaded"] is True
    assert status["websocket"]["enabled"] is True
    assert status["telegram"]["configured"] is True
    assert status["telegram"]["enabled"] is False
    assert status["slack"]["configured"] is True
    assert status["slack"]["enabled"] is True
    assert "available" in status["slack"]
    if not status["slack"]["loaded"]:
        assert status["slack"]["error"]
        assert status["slack"]["install_hint"].startswith("pip install")


def test_channel_manager_direct_send_surfaces_failure_without_blind_retry() -> None:
    class FailingChannel:
        is_running = True

        def __init__(self) -> None:
            self.calls = 0

        async def send(self, message: OutboundMessage) -> None:
            self.calls += 1
            assert message.metadata["_require_delivery_receipt"] is True
            raise RuntimeError("connection closed after send")

    manager = ChannelManager(
        {"send_max_retries": 3},
        MessageBus(),
    )
    channel = FailingChannel()
    manager.channels["test"] = channel  # type: ignore[assignment]

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="connection closed after send"):
            await manager.send_direct(
                OutboundMessage(channel="test", chat_id="chat", content="report")
            )

    asyncio.run(scenario())
    assert channel.calls == 1


def test_registry_marks_lazy_sdk_adapter_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.channels.discord as discord_channel

    from src.channels.registry import inspect_channel

    monkeypatch.setattr(discord_channel, "DISCORD_AVAILABLE", False)

    status = inspect_channel("discord").to_dict()

    assert status["available"] is False
    assert status["install_hint"] == "pip install 'vibe-trading-ai[discord]'"


def test_channel_manager_skips_enabled_adapter_when_lazy_sdk_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.channels.discord as discord_channel

    monkeypatch.setattr(discord_channel, "DISCORD_AVAILABLE", False)

    manager = ChannelManager({"discord": {"enabled": True, "token": "x"}}, MessageBus())
    status = manager.get_status()["discord"]

    assert "discord" not in manager.channels
    assert status["enabled"] is True
    assert status["available"] is False
    assert status["loaded"] is False
    assert status["install_hint"] == "pip install 'vibe-trading-ai[discord]'"


def test_websocket_turn_wall_accepts_optional_chat_id() -> None:
    assert websocket_turn_wall_started_at("chat-1") is None
    mark_websocket_turn_started("chat-1", 123.0)
    assert websocket_turn_wall_started_at("chat-1") == 123.0
    clear_websocket_turn_started("chat-1")
    assert websocket_turn_wall_started_at("chat-1") is None


def test_websocket_compatibility_helpers_have_structured_behavior() -> None:
    assert goal_state_ws_blob({"active_goal": {"goal_id": "g1"}}) == {
        "active": True,
        "active_goals": [{"goal_id": "g1"}],
        "completed_goals": [],
    }
    assert normalize_cli_app_mentions("@codex, terminal") == ["codex", "terminal"]
    assert normalize_mcp_preset_mentions(["@research", "market"]) == ["research", "market"]


def test_gateway_services_adapt_session_service_for_hydration() -> None:
    service = FakeSessionService()
    session = service.create_session(title="chat", config={"metadata": {"active_goal": {"goal_id": "g1"}}})
    gateway = build_gateway_services(session_manager=service)

    row = gateway.session_manager.read_session_file(session.session_id)

    assert row["metadata"] == {"active_goal": {"goal_id": "g1"}}


def test_transcription_event_returns_structured_error() -> None:
    async def scenario() -> None:
        event, payload = await webui_transcription_event({"type": "transcribe_audio"})
        assert event == "error"
        assert "not configured" in payload["detail"]

    asyncio.run(scenario())


def test_save_base64_data_url_decodes_and_limits_size(tmp_path: Path) -> None:
    payload = base64.b64encode(b"hello").decode("ascii")

    saved = save_base64_data_url(f"data:text/plain;base64,{payload}", tmp_path, max_bytes=10)

    assert saved.read_text(encoding="utf-8") == "hello"
    assert saved.suffix == ".txt"

    with pytest.raises(FileSizeExceeded):
        save_base64_data_url(f"data:text/plain;base64,{payload}", tmp_path, max_bytes=4)

    with pytest.raises(ValueError):
        save_base64_data_url("not-a-data-url", tmp_path, max_bytes=10)


def test_channel_runtime_routes_inbound_to_session_and_outbound(tmp_path: Path) -> None:
    async def scenario() -> None:
        from src.channels.runtime import ChannelRuntime

        bus = MessageBus()
        service = FakeSessionService()
        runtime = ChannelRuntime(
            bus=bus,
            session_service=service,
            manager=None,
            session_map_path=tmp_path / "channel_sessions.json",
            reply_timeout_s=1,
            poll_interval_s=0.01,
        )
        await runtime.start(start_manager=False)
        try:
            await bus.publish_inbound(
                InboundMessage(
                    channel="websocket",
                    sender_id="user-1",
                    chat_id="chat-1",
                    content="hello from IM",
                )
            )

            outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        finally:
            await runtime.stop()

        assert service.sent == [("session-1", "hello from IM")]
        assert outbound == OutboundMessage(
            channel="websocket",
            chat_id="chat-1",
            content="agent reply: hello from IM",
            metadata={
                "_channel_runtime": True,
                "attempt_id": "attempt-1",
                "session_id": "session-1",
            },
        )

    asyncio.run(scenario())


def test_channel_runtime_uses_dispatcher_research_policy_and_preserves_routing_metadata(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        from src.channels.runtime import ChannelRuntime

        bus = MessageBus()
        service = FakeSessionService()
        dispatcher = FakeDispatcher(service)
        runtime = ChannelRuntime(
            bus=bus,
            session_service=service,
            manager=None,
            dispatcher=dispatcher,
            session_map_path=tmp_path / "channel_sessions.json",
            reply_timeout_s=1,
            poll_interval_s=0.01,
        )
        await runtime.start(start_manager=False)
        try:
            await bus.publish_inbound(
                InboundMessage(
                    channel="feishu",
                    sender_id="user-1",
                    chat_id="chat-1",
                    content="research this portfolio",
                    metadata={"message_id": "m-1", "root_id": "root-1", "thread_id": "thread-1"},
                    session_key_override="feishu:chat-1:root-1",
                )
            )
            outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1)

            await bus.publish_inbound(
                InboundMessage(
                    channel="feishu",
                    sender_id="user-1",
                    chat_id="chat-1",
                    content="/cancel",
                    metadata={"message_id": "m-2", "root_id": "root-1"},
                    session_key_override="feishu:chat-1:root-1",
                )
            )
            cancelled = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        finally:
            await runtime.stop()

        assert service.created[0].config["channel_policy"] == {
            "research_only": True,
            "allow_shell_tools": False,
            "allow_trading_tools": False,
        }
        assert dispatcher.submitted == [
            {
                "session_id": "session-1",
                "content": "research this portfolio",
                "source": "feishu",
                "source_metadata": {
                    "channel_session_key": "feishu:chat-1:root-1",
                    "channel_chat_id": "chat-1",
                    "message_id": "m-1",
                    "root_id": "root-1",
                    "thread_id": "thread-1",
                },
                "include_shell_tools": False,
            }
        ]
        assert outbound.metadata["message_id"] == "m-1"
        assert outbound.metadata["root_id"] == "root-1"
        assert outbound.metadata["thread_id"] == "thread-1"
        assert outbound.metadata["session_id"] == "session-1"
        assert outbound.media == []
        assert outbound.content == "agent reply: research this portfolio"
        assert dispatcher.cancelled == ["session-1"]
        assert cancelled.metadata["message_id"] == "m-2"
        assert cancelled.metadata["session_cancel"] is True
        assert "running=1" in cancelled.content
        assert "queued=2" in cancelled.content

    asyncio.run(scenario())


def test_channel_runtime_handles_pairing_commands_without_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        from src.channels.runtime import ChannelRuntime

        monkeypatch.setenv("VIBE_TRADING_DATA_DIR", str(tmp_path / "data"))
        bus = MessageBus()
        service = FakeSessionService()
        runtime = ChannelRuntime(
            bus=bus,
            session_service=service,
            manager=None,
            session_map_path=tmp_path / "channel_sessions.json",
            reply_timeout_s=1,
            poll_interval_s=0.01,
        )
        await runtime.start(start_manager=False)
        try:
            await bus.publish_inbound(
                InboundMessage(
                    channel="telegram",
                    sender_id="owner",
                    chat_id="chat-1",
                    content="/PAIRING LIST",
                )
            )
            outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        finally:
            await runtime.stop()

        assert service.sent == []
        assert outbound.channel == "telegram"
        assert outbound.chat_id == "chat-1"
        assert "No pending pairing requests" in outbound.content
        assert outbound.metadata["_pairing_command"] is True

    asyncio.run(scenario())


def test_channel_runtime_new_command_resets_session_and_creates_fresh_one(tmp_path: Path) -> None:
    async def scenario() -> None:
        from src.channels.runtime import ChannelRuntime

        bus = MessageBus()
        service = FakeSessionService()
        runtime = ChannelRuntime(
            bus=bus,
            session_service=service,
            manager=None,
            session_map_path=tmp_path / "channel_sessions.json",
            reply_timeout_s=1,
            poll_interval_s=0.01,
        )
        await runtime.start(start_manager=False)
        try:
            await bus.publish_inbound(
                InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="hello")
            )
            reply1 = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
            assert reply1.metadata["session_id"] == "session-1"

            await bus.publish_inbound(
                InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="/new")
            )
            reset_reply = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
            assert "开启新对话" in reset_reply.content
            assert reset_reply.metadata.get("session_reset") is True

            await bus.publish_inbound(
                InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="after reset")
            )
            reply2 = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
            assert reply2.metadata["session_id"] == "session-2"
        finally:
            await runtime.stop()

        assert service.sent == [("session-1", "hello"), ("session-2", "after reset")]

    asyncio.run(scenario())


def test_channel_runtime_new_command_with_no_existing_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        from src.channels.runtime import ChannelRuntime

        bus = MessageBus()
        service = FakeSessionService()
        runtime = ChannelRuntime(
            bus=bus,
            session_service=service,
            manager=None,
            session_map_path=tmp_path / "channel_sessions.json",
            reply_timeout_s=1,
            poll_interval_s=0.01,
        )
        await runtime.start(start_manager=False)
        try:
            await bus.publish_inbound(
                InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content="/new")
            )
            reply = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        finally:
            await runtime.stop()

        assert "开启新对话" in reply.content
        assert reply.metadata.get("session_reset") is True
        assert service.sent == []
        assert len(service.created) == 1

    asyncio.run(scenario())


def test_channel_runtime_reset_and_newsession_aliases_work(tmp_path: Path) -> None:
    async def scenario() -> None:
        from src.channels.runtime import ChannelRuntime

        bus = MessageBus()
        service = FakeSessionService()
        runtime = ChannelRuntime(
            bus=bus,
            session_service=service,
            manager=None,
            session_map_path=tmp_path / "channel_sessions.json",
            reply_timeout_s=1,
            poll_interval_s=0.01,
        )
        await runtime.start(start_manager=False)
        try:
            await bus.publish_inbound(
                InboundMessage(channel="discord", sender_id="u1", chat_id="c1", content="hi")
            )
            await asyncio.wait_for(bus.consume_outbound(), timeout=1)

            await bus.publish_inbound(
                InboundMessage(channel="discord", sender_id="u1", chat_id="c1", content="/reset")
            )
            reply = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
            assert "开启新对话" in reply.content

            await bus.publish_inbound(
                InboundMessage(channel="discord", sender_id="u1", chat_id="c1", content="hi again")
            )
            await asyncio.wait_for(bus.consume_outbound(), timeout=1)

            await bus.publish_inbound(
                InboundMessage(channel="discord", sender_id="u1", chat_id="c1", content="/newsession")
            )
            reply2 = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
            assert "开启新对话" in reply2.content
        finally:
            await runtime.stop()

    asyncio.run(scenario())


def test_channel_runtime_research_topics_switch_and_resume_for_followups(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        from src.channels.runtime import ChannelRuntime
        from src.channels.research_sessions import build_research_session_route

        map_path = tmp_path / "channel_sessions.json"
        bus = MessageBus()
        service = FakeSessionService()
        runtime = ChannelRuntime(
            bus=bus,
            session_service=service,
            manager=None,
            session_map_path=map_path,
            reply_timeout_s=1,
            poll_interval_s=0.01,
        )
        await runtime.start(start_manager=False)
        try:
            await bus.publish_inbound(
                InboundMessage(
                    channel="feishu",
                    sender_id="u1",
                    chat_id="c1",
                    content="old context",
                )
            )
            await asyncio.wait_for(bus.consume_outbound(), timeout=1)

            await bus.publish_inbound(
                InboundMessage(
                    channel="feishu",
                    sender_id="u1",
                    chat_id="c1",
                    content="research A",
                    metadata={
                        "research_label": "招商银行个股分析",
                        **build_research_session_route(
                            base_key="feishu:c1",
                            action="holding",
                            symbol="600036.SH",
                            name="招商银行",
                        ).metadata(),
                    },
                    session_key_override="feishu:c1:research:symbol:600036.SH",
                )
            )
            first_a = await asyncio.wait_for(bus.consume_outbound(), timeout=1)

            await bus.publish_inbound(
                InboundMessage(
                    channel="feishu",
                    sender_id="u1",
                    chat_id="c1",
                    content="follow-up A",
                )
            )
            followup_a = await asyncio.wait_for(bus.consume_outbound(), timeout=1)

            route_b = build_research_session_route(
                base_key="feishu:c1",
                action="holding",
                symbol="000651.SZ",
                name="格力电器",
            )
            await bus.publish_inbound(
                InboundMessage(
                    channel="feishu",
                    sender_id="u1",
                    chat_id="c1",
                    content="research B",
                    metadata={"research_label": route_b.label, **route_b.metadata()},
                    session_key_override=route_b.route_key,
                )
            )
            first_b = await asyncio.wait_for(bus.consume_outbound(), timeout=1)

            route_a = build_research_session_route(
                base_key="feishu:c1",
                action="custom_stock",
                symbol="600036",
                name="招商银行",
            )
            await bus.publish_inbound(
                InboundMessage(
                    channel="feishu",
                    sender_id="u1",
                    chat_id="c1",
                    content="resume A",
                    metadata={"research_label": route_a.label, **route_a.metadata()},
                    session_key_override=route_a.route_key,
                )
            )
            resumed_a = await asyncio.wait_for(bus.consume_outbound(), timeout=1)

            await bus.publish_inbound(
                InboundMessage(
                    channel="feishu",
                    sender_id="u1",
                    chat_id="c1",
                    content="",
                    metadata={
                        "research_control": "new_conversation",
                        "research_base_session_key": "feishu:c1",
                    },
                )
            )
            new_a = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
            await bus.publish_inbound(
                InboundMessage(
                    channel="feishu",
                    sender_id="u1",
                    chat_id="c1",
                    content="fresh A follow-up",
                )
            )
            fresh_a = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        finally:
            await runtime.stop()

        assert service.sent == [
            ("session-1", "old context"),
            ("session-2", "research A"),
            ("session-2", "follow-up A"),
            ("session-3", "research B"),
            ("session-2", "resume A"),
            ("session-4", "fresh A follow-up"),
        ]
        assert first_a.metadata["session_id"] == "session-2"
        assert followup_a.metadata["session_id"] == "session-2"
        assert first_b.metadata["session_id"] == "session-3"
        assert resumed_a.metadata["session_id"] == "session-2"
        assert new_a.metadata["session_id"] == "session-4"
        assert fresh_a.metadata["session_id"] == "session-4"
        mapping = json.loads(map_path.read_text(encoding="utf-8"))
        assert mapping["feishu:c1"] == "session-4"
        assert mapping["feishu:c1:general"] == "session-1"
        assert mapping["feishu:c1:research:symbol:600036.SH"] == "session-4"
        assert mapping["feishu:c1:research:symbol:000651.SZ"] == "session-3"

    asyncio.run(scenario())


def test_channel_runtime_refreshes_active_portfolio_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        from src.channels import runtime as runtime_module
        from src.channels.research_sessions import build_research_session_route
        from src.channels.runtime import ChannelRuntime

        def fake_render(title: str, content: str, target: Path) -> Path:
            del title, content
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"%PDF-refresh")
            return target

        monkeypatch.setattr(runtime_module, "_render_research_pdf", fake_render)
        bus = MessageBus()
        service = FakeSessionService()
        runtime = ChannelRuntime(
            bus=bus,
            session_service=service,
            manager=None,
            session_map_path=tmp_path / "channel_sessions.json",
            reply_timeout_s=1,
            poll_interval_s=0.01,
        )
        route = build_research_session_route(
            base_key="feishu:c1", action="portfolio"
        )
        await runtime.start(start_manager=False)
        try:
            await bus.publish_inbound(
                InboundMessage(
                    channel="feishu",
                    sender_id="u1",
                    chat_id="c1",
                    content="initial portfolio context",
                    metadata={"research_label": route.label, **route.metadata()},
                    session_key_override=route.route_key,
                )
            )
            await asyncio.wait_for(bus.consume_outbound(), timeout=1)

            await bus.publish_inbound(
                InboundMessage(
                    channel="feishu",
                    sender_id="u1",
                    chat_id="c1",
                    content="",
                    metadata={
                        "research_control": "refresh_report",
                        "research_base_session_key": "feishu:c1",
                    },
                )
            )
            refreshed = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        finally:
            await runtime.stop()

        assert service.sent[0] == ("session-1", "initial portfolio context")
        assert service.sent[1][0] == "session-1"
        assert "生成一份细致的组合报告" in service.sent[1][1]
        assert refreshed.metadata["delivery_mode"] == "report_pdf"
        assert refreshed.metadata["research_refresh"] is True
        assert Path(refreshed.media[0]).read_bytes() == b"%PDF-refresh"

    asyncio.run(scenario())


def test_channel_runtime_refresh_from_general_opens_research_hub(tmp_path: Path) -> None:
    async def scenario() -> None:
        from src.channels.runtime import ChannelRuntime

        bus = MessageBus()
        service = FakeSessionService()
        runtime = ChannelRuntime(
            bus=bus,
            session_service=service,
            manager=None,
            session_map_path=tmp_path / "channel_sessions.json",
            reply_timeout_s=1,
            poll_interval_s=0.01,
        )
        await runtime.start(start_manager=False)
        try:
            await bus.publish_inbound(
                InboundMessage(
                    channel="feishu",
                    sender_id="u1",
                    chat_id="c1",
                    content="hello",
                )
            )
            await asyncio.wait_for(bus.consume_outbound(), timeout=1)
            await bus.publish_inbound(
                InboundMessage(
                    channel="feishu",
                    sender_id="u1",
                    chat_id="c1",
                    content="",
                    metadata={
                        "research_control": "refresh_report",
                        "research_base_session_key": "feishu:c1",
                    },
                )
            )
            reply = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        finally:
            await runtime.stop()

        assert reply.metadata["_research_hub"] is True
        assert "请先从分析菜单选择" in reply.content
        assert service.sent == [("session-1", "hello")]

    asyncio.run(scenario())


def test_channel_runtime_rejects_duplicate_refresh_while_topic_is_busy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        from src.channels.research_sessions import build_research_session_route
        from src.channels.runtime import ChannelRuntime

        bus = MessageBus()
        service = FakeSessionService()
        runtime = ChannelRuntime(
            bus=bus,
            session_service=service,
            manager=None,
            session_map_path=tmp_path / "channel_sessions.json",
            reply_timeout_s=1,
            poll_interval_s=0.01,
        )
        route = build_research_session_route(
            base_key="feishu:c1", action="portfolio"
        )
        await runtime.start(start_manager=False)
        try:
            await bus.publish_inbound(
                InboundMessage(
                    channel="feishu",
                    sender_id="u1",
                    chat_id="c1",
                    content="portfolio",
                    metadata={"research_label": route.label, **route.metadata()},
                    session_key_override=route.route_key,
                )
            )
            await asyncio.wait_for(bus.consume_outbound(), timeout=1)
            monkeypatch.setattr(runtime, "_is_session_busy", lambda _session_id: True)
            await bus.publish_inbound(
                InboundMessage(
                    channel="feishu",
                    sender_id="u1",
                    chat_id="c1",
                    content="",
                    metadata={
                        "research_control": "refresh_report",
                        "research_base_session_key": "feishu:c1",
                    },
                )
            )
            reply = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        finally:
            await runtime.stop()

        assert "仍在生成" in reply.content
        assert reply.metadata["refresh_rejected"] is True
        assert len(service.sent) == 1

    asyncio.run(scenario())


def test_channel_runtime_attaches_pdf_to_completed_feishu_research(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        from src.channels import runtime as runtime_module
        from src.channels.runtime import ChannelRuntime

        def fake_render(title: str, content: str, target: Path) -> Path:
            assert title == "2026-07-13_军工基金（512680.SH）单标的持仓分析"
            assert content == service.reply_content
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"%PDF-test")
            return target

        monkeypatch.setattr(runtime_module, "_render_research_pdf", fake_render)
        bus = MessageBus()
        service = FakeSessionService()
        service.reply_content = (
            "# 🔍 军工基金（512680.SH）单标的持仓分析\n\n"
            "报告日期：2026-07-13\n\n## 一、概览\n\n正文"
        )
        runtime = ChannelRuntime(
            bus=bus,
            session_service=service,
            manager=None,
            session_map_path=tmp_path / "channel_sessions.json",
            reply_timeout_s=1,
            poll_interval_s=0.01,
        )
        await runtime.start(start_manager=False)
        try:
            await bus.publish_inbound(
                InboundMessage(
                    channel="feishu",
                    sender_id="u1",
                    chat_id="c1",
                    content="grounded research prompt",
                    metadata={
                        "research_action": "premarket",
                        "research_label": "盘前分析",
                    },
                )
            )
            outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        finally:
            await runtime.stop()

        assert len(outbound.media) == 1
        pdf_path = Path(outbound.media[0])
        assert pdf_path.name == "2026-07-13_军工基金（512680.SH）单标的持仓分析.pdf"
        assert pdf_path.read_bytes() == b"%PDF-test"
        assert outbound.content == (
            "✅ 盘前分析已完成\n"
            "报告：🔍 军工基金（512680.SH）单标的持仓分析\n"
            "完整正文和表格见 PDF 附件；飞书中仅保留这条简要说明。\n"
            "后续可直接发送文字，在当前专题 Session 中继续讨论。"
        )
        assert "agent reply" not in outbound.content
        assert outbound.metadata["delivery_mode"] == "report_pdf"

    asyncio.run(scenario())


def test_channel_runtime_pdf_report_note_includes_report_title() -> None:
    from src.channels.runtime import _research_delivery_note

    note = _research_delivery_note(
        "个股分析",
        "# 🔍 军工基金（512680.SH）单标的持仓分析\n\n| 项目 | 数值 |",
    )

    assert note == (
        "✅ 个股分析已完成\n"
        "报告：🔍 军工基金（512680.SH）单标的持仓分析\n"
        "完整正文和表格见 PDF 附件；飞书中仅保留这条简要说明。\n"
        "后续可直接发送文字，在当前专题 Session 中继续讨论。"
    )


def test_channel_runtime_pdf_filename_uses_date_and_report_title() -> None:
    from src.channels.runtime import _report_pdf_name

    assert _report_pdf_name(
        "# 组合分析报告：2026年7月13日\n\n## 一、概览",
        "持仓分析",
    ) == "2026-07-13_组合分析报告.pdf"


def test_channel_runtime_honors_direct_pdf_delivery_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        from src.channels import runtime as runtime_module
        from src.channels.runtime import ChannelRuntime

        def fake_render(title: str, content: str, target: Path) -> Path:
            assert title == "2026-07-13_SpaceX (SPCX.US) 深度分析报告"
            assert content == service.reply_content
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"%PDF-direct")
            return target

        monkeypatch.setattr(runtime_module, "_render_research_pdf", fake_render)
        bus = MessageBus()
        service = FakeSessionService()
        service.reply_content = (
            "# SpaceX (SPCX.US) 深度分析报告\n\n"
            "报告日期：2026-07-13\n\n## 一、概览\n\n完整正文"
        )
        runtime = ChannelRuntime(
            bus=bus,
            session_service=service,
            manager=None,
            session_map_path=tmp_path / "channel_sessions.json",
            reply_timeout_s=1,
            poll_interval_s=0.01,
        )
        await runtime.start(start_manager=False)
        try:
            await bus.publish_inbound(
                InboundMessage(
                    channel="feishu",
                    sender_id="u1",
                    chat_id="oc_group",
                    content="帮我分析 SpaceX，给我 PDF 报告",
                    metadata={"pdf_delivery_requested": True},
                )
            )
            outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        finally:
            await runtime.stop()

        assert outbound.metadata["delivery_mode"] == "report_pdf"
        assert "SpaceX (SPCX.US) 深度分析报告" in outbound.content
        assert Path(outbound.media[0]).name == (
            "2026-07-13_SpaceX (SPCX.US) 深度分析报告.pdf"
        )
        assert Path(outbound.media[0]).read_bytes() == b"%PDF-direct"

    asyncio.run(scenario())


def test_channel_runtime_sends_latest_chat_report_pdf_without_model_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        from src.channels import runtime as runtime_module
        from src.channels.runtime import ChannelRuntime

        report = (
            "# 🔴 SpaceX (SPCX.US) 深度分析报告\n\n"
            "**日期**: 2026年7月13日\n\n"
            "## 一、执行摘要\n\n| 项目 | 内容 |\n|---|---|\n| 结论 | 谨慎 |\n\n"
            "## 二、估值分析\n\n| 情景 | 估值 |\n|---|---|\n| 基准 | 偏高 |\n\n"
            + ("完整研究正文。" * 100)
        )

        def fake_render(title: str, content: str, target: Path) -> Path:
            assert title == "2026-07-13_SpaceX (SPCX.US) 深度分析报告"
            assert content == report
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"%PDF-previous")
            return target

        monkeypatch.setattr(runtime_module, "_render_research_pdf", fake_render)
        bus = MessageBus()
        service = FakeSessionService()
        source = service.create_session(
            title="feishu:oc_group",
            config={"channel": "feishu", "channel_chat_id": "oc_group"},
        )
        service.messages[source.session_id].append(
            Message(
                session_id=source.session_id,
                role="assistant",
                content=report,
                created_at="2026-07-13T11:40:00",
            )
        )
        runtime = ChannelRuntime(
            bus=bus,
            session_service=service,
            manager=None,
            session_map_path=tmp_path / "channel_sessions.json",
            reply_timeout_s=1,
            poll_interval_s=0.01,
        )
        await runtime.start(start_manager=False)
        try:
            await bus.publish_inbound(
                InboundMessage(
                    channel="feishu",
                    sender_id="u1",
                    chat_id="oc_group",
                    content="可以把这份报告的 PDF 发送给我吗？",
                    metadata={
                        "pdf_delivery_requested": True,
                        "pdf_from_previous_report": True,
                    },
                )
            )
            outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        finally:
            await runtime.stop()

        assert service.sent == []
        assert outbound.metadata["delivery_mode"] == "report_pdf"
        assert outbound.metadata["report_source_session_id"] == source.session_id
        assert "SpaceX (SPCX.US) 深度分析报告" in outbound.content
        assert Path(outbound.media[0]).name == (
            "2026-07-13_SpaceX (SPCX.US) 深度分析报告.pdf"
        )
        assert Path(outbound.media[0]).read_bytes() == b"%PDF-previous"

    asyncio.run(scenario())

def test_channel_runtime_never_sends_full_report_text_when_pdf_render_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        from src.channels import runtime as runtime_module
        from src.channels.runtime import ChannelRuntime

        def broken_render(title: str, content: str, target: Path) -> Path:
            del title, content, target
            raise RuntimeError("renderer unavailable")

        monkeypatch.setattr(runtime_module, "_render_research_pdf", broken_render)
        bus = MessageBus()
        service = FakeSessionService()
        runtime = ChannelRuntime(
            bus=bus,
            session_service=service,
            manager=None,
            session_map_path=tmp_path / "channel_sessions.json",
            reply_timeout_s=1,
            poll_interval_s=0.01,
        )
        await runtime.start(start_manager=False)
        try:
            await bus.publish_inbound(
                InboundMessage(
                    channel="feishu",
                    sender_id="u1",
                    chat_id="c1",
                    content="grounded research prompt",
                    metadata={
                        "research_action": "portfolio",
                        "research_label": "全仓分析",
                    },
                )
            )
            outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        finally:
            await runtime.stop()

        assert outbound.media == []
        assert outbound.content == (
            "⚠️ 全仓分析已完成，但 PDF 生成失败。\n"
            "为避免在飞书中发送大量表格正文，本次未发送报告内容；请重试生成报告。"
        )
        assert "agent reply" not in outbound.content
        assert "delivery_mode" not in outbound.metadata

    asyncio.run(scenario())


def test_channel_runtime_regular_messages_not_intercepted_as_new_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        from src.channels.runtime import ChannelRuntime

        bus = MessageBus()
        service = FakeSessionService()
        runtime = ChannelRuntime(
            bus=bus,
            session_service=service,
            manager=None,
            session_map_path=tmp_path / "channel_sessions.json",
            reply_timeout_s=1,
            poll_interval_s=0.01,
        )
        await runtime.start(start_manager=False)
        try:
            for text in ["hello /new world", "/new stuff", "/NEW YORK", "type /new to reset"]:
                await bus.publish_inbound(
                    InboundMessage(channel="slack", sender_id="u1", chat_id="c1", content=text)
                )
                reply = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
                assert reply.metadata.get("session_reset") is not True
                assert "agent reply:" in reply.content
        finally:
            await runtime.stop()

        assert len(service.sent) == 4

    asyncio.run(scenario())


def test_channel_runtime_session_map_persisted_after_reset(tmp_path: Path) -> None:
    import json

    async def scenario() -> None:
        from src.channels.runtime import ChannelRuntime

        map_path = tmp_path / "channel_sessions.json"
        bus = MessageBus()
        service = FakeSessionService()
        runtime = ChannelRuntime(
            bus=bus,
            session_service=service,
            manager=None,
            session_map_path=map_path,
            reply_timeout_s=1,
            poll_interval_s=0.01,
        )
        await runtime.start(start_manager=False)
        try:
            await bus.publish_inbound(
                InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="hi")
            )
            await asyncio.wait_for(bus.consume_outbound(), timeout=1)

            data = json.loads(map_path.read_text(encoding="utf-8"))
            assert data["feishu:c1"] == "session-1"

            await bus.publish_inbound(
                InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="/new")
            )
            await asyncio.wait_for(bus.consume_outbound(), timeout=1)

            data = json.loads(map_path.read_text(encoding="utf-8"))
            assert data["feishu:c1"] == "session-2"
            assert data["feishu:c1:general"] == "session-2"
        finally:
            await runtime.stop()

    asyncio.run(scenario())
