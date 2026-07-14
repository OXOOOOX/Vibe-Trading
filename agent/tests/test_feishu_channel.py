from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from src.channels.bus.queue import MessageBus
from src.channels.feishu import (
    FeishuChannel,
    FeishuConfig,
    _direct_research_action,
    _is_new_research_request,
    _pdf_delivery_request,
    _research_control_action,
)


def _mention(open_id: str, *, key: str = "@_user_1", name: str = "Bot") -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        name=name,
        id=SimpleNamespace(open_id=open_id, user_id=""),
    )


def test_feishu_defaults_to_mentions_and_topic_isolation() -> None:
    config = FeishuConfig()

    assert config.group_policy == "mention"
    assert config.topic_isolation is True
    assert config.streaming is True


def test_feishu_group_policy_matches_only_the_bot_mention() -> None:
    channel = FeishuChannel(FeishuConfig(), MessageBus())
    channel._bot_open_id = "ou_bot"

    bot_mention = _mention("ou_bot")
    other_mention = _mention("ou_other")
    addressed = SimpleNamespace(content='{"text":"@_user_1 research"}', mentions=[bot_mention])
    not_addressed = SimpleNamespace(content='{"text":"@_user_1 hello"}', mentions=[other_mention])

    assert channel._is_group_message_for_bot(addressed) is True
    assert channel._is_group_message_for_bot(not_addressed) is False
    assert channel._strip_leading_bot_mention("@_user_1 /status", [bot_mention]) == "/status"


def test_feishu_open_group_policy_accepts_messages_without_mentions() -> None:
    channel = FeishuChannel(FeishuConfig(group_policy="open"), MessageBus())
    message = SimpleNamespace(content='{"text":"research"}', mentions=[])

    assert channel._is_group_message_for_bot(message) is True


def test_feishu_qr_login_persists_credentials(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "agent.json"
    monkeypatch.setattr("src.config.loader.get_config_path", lambda _path=None: config_path)
    monkeypatch.setattr(
        "src.channels.feishu.qr_register",
        lambda initial_domain="feishu": {
            "app_id": "cli_test",
            "app_secret": "secret_test",
            "domain": initial_domain,
        },
    )
    channel = FeishuChannel(FeishuConfig(enabled=True), MessageBus())

    assert asyncio.run(channel.login()) is True

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["channels"]["feishu"]["enabled"] is True
    assert payload["channels"]["feishu"]["appId"] == "cli_test"
    assert payload["channels"]["feishu"]["appSecret"] == "secret_test"
    assert payload["channels"]["feishu"]["topicIsolation"] is True


def _elements_with_tag(value, tag: str) -> list[dict]:
    found: list[dict] = []
    if isinstance(value, dict):
        if value.get("tag") == tag:
            found.append(value)
        for child in value.values():
            found.extend(_elements_with_tag(child, tag))
    elif isinstance(value, list):
        for child in value:
            found.extend(_elements_with_tag(child, tag))
    return found


def test_feishu_new_research_trigger_is_explicit() -> None:
    assert _is_new_research_request("新的研究") is True
    assert _is_new_research_request("开始新的研究！") is True
    assert _is_new_research_request("新研究") is True
    assert _is_new_research_request("分析菜单") is True
    assert _is_new_research_request("研究一下招商银行") is False
    assert _research_control_action("新对话") == "new_conversation"
    assert _research_control_action("更新报告！") == "refresh_report"


def test_feishu_direct_research_shortcuts_are_deterministic() -> None:
    assert _direct_research_action("组合晨会") == "morning_meeting"
    assert _direct_research_action("盘前分析") == "premarket"
    assert _direct_research_action("全仓分析！") == "portfolio"
    assert _direct_research_action("个股分析") == "show_stock_picker"
    assert _direct_research_action("分析一下行情") is None


def test_feishu_detects_new_and_previous_report_pdf_requests() -> None:
    assert _pdf_delivery_request("帮我分析 SpaceX，给我 PDF 的报告") == (True, False)
    assert _pdf_delivery_request("可以把这份报告的pdf发送给我吗？") == (True, True)
    assert _pdf_delivery_request("这个 PDF 主要讲了什么？") == (False, False)


def test_feishu_group_messages_share_chat_session_but_threads_are_isolated(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        bus = MessageBus()
        channel = FeishuChannel(
            FeishuConfig(group_policy="open", allow_from=["ou_user"]),
            bus,
        )
        channel._client = object()

        async def fake_reaction(*_args, **_kwargs):
            return None

        monkeypatch.setattr(channel, "_add_reaction", fake_reaction)
        sender = SimpleNamespace(
            sender_type="user",
            sender_id=SimpleNamespace(open_id="ou_user"),
        )

        async def receive(
            message_id: str,
            *,
            text: str | None = None,
            thread_id: str | None = None,
        ):
            message = SimpleNamespace(
                message_id=message_id,
                chat_id="oc_group",
                chat_type="group",
                message_type="text",
                content=json.dumps({"text": text or f"message {message_id}"}),
                mentions=[],
                parent_id=None,
                root_id=None,
                thread_id=thread_id,
            )
            await channel._on_message(
                SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))
            )
            return await asyncio.wait_for(bus.consume_inbound(), timeout=1)

        first = await receive("om_one", text="帮我分析 SpaceX，给我 PDF 的报告")
        second = await receive("om_two")
        threaded = await receive("om_three", thread_id="omt_topic")

        assert first.session_key == "feishu:oc_group"
        assert second.session_key == first.session_key
        assert threaded.session_key == "feishu:oc_group:thread:omt_topic"
        assert first.metadata["pdf_delivery_requested"] is True
        assert first.metadata["pdf_from_previous_report"] is False
        assert "PDF 附件由飞书频道自动生成和发送" in first.content

    asyncio.run(scenario())


def test_feishu_research_menu_offers_morning_meeting_and_three_scopes() -> None:
    card = FeishuChannel._build_research_menu_card(
        reply_chat_id="ou_user",
        chat_type="p2p",
        session_key=None,
    )
    buttons = _elements_with_tag(card, "button")
    actions = {
        button["behaviors"][0]["value"]["action"]
        for button in buttons
    }

    assert card["schema"] == "2.0"
    assert actions == {"morning_meeting", "premarket", "show_stock_picker", "portfolio"}
    assert all(isinstance(button["behaviors"][0]["value"], dict) for button in buttons)
    assert all(
        button["behaviors"][0]["value"]["base_session_key"] == "feishu:ou_user"
        for button in buttons
    )
    assert _elements_with_tag(card, "select_static") == []


def test_feishu_stock_picker_offers_holdings_and_six_digit_form() -> None:
    card = FeishuChannel._build_stock_picker_card(
        [
            {"name": "招商银行", "symbol": "600036.SH"},
            {"name": "格力电器", "symbol": "000651.SZ"},
        ],
        reply_chat_id="ou_user",
        chat_type="p2p",
        session_key=None,
    )
    buttons = _elements_with_tag(card, "button")
    callbacks = [button["behaviors"][0]["value"] for button in buttons]
    inputs = _elements_with_tag(card, "input")

    assert {item.get("symbol") for item in callbacks if item["action"] == "holding"} == {
        "600036.SH",
        "000651.SZ",
    }
    assert any(item["action"] == "custom_stock" for item in callbacks)
    assert all(item["base_session_key"] == "feishu:ou_user" for item in callbacks)
    assert inputs == [
        {
            "tag": "input",
            "name": "stock_code",
            "input_type": "text",
            "required": True,
            "placeholder": {"tag": "plain_text", "content": "例如：600036"},
        }
    ]


def test_feishu_stock_reuse_card_offers_session_report_and_fresh_paths() -> None:
    card = FeishuChannel._build_stock_reuse_card(
        {
            "symbol": "600036.SH",
            "name": "招商银行",
            "analysis_action": "holding",
            "session": {
                "session_id": "session-today",
                "title": "飞书·个股·招商银行（600036.SH）",
                "created_time": "09:30",
            },
            "report": {
                "run_id": "dpr_today",
                "artifact_id": "pdf-today",
                "market_date": "2026-07-14",
                "revision": 2,
            },
        },
        reply_chat_id="ou_user",
        chat_type="p2p",
        session_key=None,
    )
    callbacks = [
        button["behaviors"][0]["value"]
        for button in _elements_with_tag(card, "button")
    ]

    assert {item["action"] for item in callbacks} == {
        "resume_stock_session",
        "send_stock_daily_report",
        "continue_stock_daily_report",
        "start_stock_research",
    }
    assert all(item["symbol"] == "600036.SH" for item in callbacks)
    assert all(item["analysis_action"] == "holding" for item in callbacks)
    assert next(
        item for item in callbacks if item["action"] == "send_stock_daily_report"
    )["artifact_id"] == "pdf-today"
    assert "不会重复生成" in _elements_with_tag(card, "markdown")[0]["content"]


def test_feishu_daily_completion_and_holding_picker_use_artifact_callbacks() -> None:
    complete = FeishuChannel._build_daily_complete_card(
        "组合报告已生成",
        run_id="dpr_1",
        revision=2,
        reply_chat_id="ou_user",
        chat_type="p2p",
        session_key=None,
    )
    complete_actions = {
        button["behaviors"][0]["value"]["action"]
        for button in _elements_with_tag(complete, "button")
    }
    assert complete_actions == {
        "send_daily_master",
        "show_daily_reports",
        "rerun_daily",
    }

    picker = FeishuChannel._build_daily_report_picker_card(
        [
            {
                "symbol": "600036.SH",
                "name": "招商银行",
                "artifact_id": "artifact_1",
                "status": "completed",
            }
        ],
        run_id="dpr_1",
        revision=2,
        reply_chat_id="ou_user",
        chat_type="p2p",
        session_key=None,
    )
    callbacks = [
        button["behaviors"][0]["value"]
        for button in _elements_with_tag(picker, "button")
    ]
    assert {item["action"] for item in callbacks} == {
        "send_daily_report",
        "retry_daily_holding",
    }
    assert all(item["run_id"] == "dpr_1" for item in callbacks)
    assert next(
        item for item in callbacks if item["action"] == "send_daily_report"
    )["artifact_id"] == "artifact_1"

    skipped = FeishuChannel._build_daily_skipped_card(
        "数据覆盖不足",
        run_id="dpr_1",
        revision=2,
        reply_chat_id="ou_user",
        chat_type="p2p",
        session_key=None,
    )
    skipped_callbacks = [
        button["behaviors"][0]["value"]
        for button in _elements_with_tag(skipped, "button")
    ]
    assert skipped_callbacks[0]["action"] == "rerun_daily"


def test_feishu_custom_stock_prompt_keeps_one_explicit_target() -> None:
    prompt, label = FeishuChannel._build_research_prompt(
        {"action": "custom_stock", "code": "600036"}
    )

    assert "600036.SH" in prompt
    assert "不要把我的其他持仓扩展成分析对象" in prompt
    assert label == "600036个股分析"


def test_feishu_new_research_message_sends_card_without_entering_agent_bus(monkeypatch) -> None:
    bus = MessageBus()
    channel = FeishuChannel(FeishuConfig(allow_from=["ou_user"]), bus)
    channel._client = object()
    sent: dict = {}

    async def fake_reaction(*_args, **_kwargs):
        return None

    async def fake_send_research_card(**kwargs):
        sent.update(kwargs)

    monkeypatch.setattr(channel, "_add_reaction", fake_reaction)
    monkeypatch.setattr(channel, "_send_research_card", fake_send_research_card)

    message = SimpleNamespace(
        message_id="om_trigger",
        chat_id="oc_chat",
        chat_type="p2p",
        message_type="text",
        content=json.dumps({"text": "新的研究"}, ensure_ascii=False),
        mentions=[],
        parent_id=None,
        root_id=None,
        thread_id=None,
    )
    sender = SimpleNamespace(
        sender_type="user",
        sender_id=SimpleNamespace(open_id="ou_user"),
    )

    asyncio.run(channel._on_message(SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))))

    assert sent["reply_chat_id"] == "ou_user"
    assert sent["chat_type"] == "p2p"
    assert sent["card"]["header"]["title"]["content"] == "分析菜单"
    assert bus.inbound_size == 0


def test_feishu_bot_menu_events_dispatch_without_user_messages(monkeypatch) -> None:
    async def scenario() -> None:
        channel = FeishuChannel(FeishuConfig(allow_from=["ou_user"]), MessageBus())
        channel._loop = asyncio.get_running_loop()
        captured: list[tuple[str, str]] = []

        async def fake_handle(sender_id: str, event_key: str) -> None:
            captured.append((sender_id, event_key))

        monkeypatch.setattr(channel, "_handle_bot_menu_action", fake_handle)
        for index, event_key in enumerate(
            ["research_hub", "new_conversation", "refresh_report"]
        ):
            channel._on_bot_menu(
                SimpleNamespace(
                    header=SimpleNamespace(event_id=f"menu-{index}"),
                    event=SimpleNamespace(
                        operator=SimpleNamespace(
                            operator_id=SimpleNamespace(open_id="ou_user")
                        ),
                        event_key=event_key,
                    ),
                )
            )
        await asyncio.sleep(0.05)

        assert captured == [
            ("ou_user", "research_hub"),
            ("ou_user", "new_conversation"),
            ("ou_user", "refresh_report"),
        ]
        assert channel.bus.inbound_size == 0

    asyncio.run(scenario())


def test_feishu_premarket_shortcut_expands_to_grounded_prompt(monkeypatch) -> None:
    async def scenario() -> None:
        bus = MessageBus()
        channel = FeishuChannel(FeishuConfig(allow_from=["ou_user"]), bus)
        channel._client = object()

        async def fake_reaction(*_args, **_kwargs):
            return None

        monkeypatch.setattr(channel, "_add_reaction", fake_reaction)
        message = SimpleNamespace(
            message_id="om_premarket",
            chat_id="oc_chat",
            chat_type="p2p",
            message_type="text",
            content=json.dumps({"text": "盘前分析"}, ensure_ascii=False),
            mentions=[],
            parent_id=None,
            root_id=None,
            thread_id=None,
        )
        sender = SimpleNamespace(
            sender_type="user",
            sender_id=SimpleNamespace(open_id="ou_user"),
        )

        await channel._on_message(
            SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))
        )
        inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=1)

        assert 'portfolio_state(action="get")' in inbound.content
        assert inbound.metadata["research_action"] == "premarket"
        assert inbound.metadata["research_label"] == "盘前分析"
        assert inbound.metadata["research_activate"] is True
        assert ":research:premarket:" in inbound.session_key

    asyncio.run(scenario())


def test_feishu_text_control_is_forwarded_without_agent_prompt(monkeypatch) -> None:
    async def scenario() -> None:
        bus = MessageBus()
        channel = FeishuChannel(FeishuConfig(allow_from=["ou_user"]), bus)
        channel._client = object()

        async def fake_reaction(*_args, **_kwargs):
            return None

        monkeypatch.setattr(channel, "_add_reaction", fake_reaction)
        message = SimpleNamespace(
            message_id="om_refresh",
            chat_id="oc_chat",
            chat_type="p2p",
            message_type="text",
            content=json.dumps({"text": "更新报告"}, ensure_ascii=False),
            mentions=[],
            parent_id=None,
            root_id=None,
            thread_id=None,
        )
        sender = SimpleNamespace(
            sender_type="user",
            sender_id=SimpleNamespace(open_id="ou_user"),
        )

        await channel._on_message(
            SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))
        )
        inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=1)

        assert inbound.content == ""
        assert inbound.metadata["research_control"] == "refresh_report"
        assert inbound.metadata["research_base_session_key"] == "feishu:ou_user"
        assert inbound.session_key == "feishu:ou_user"

    asyncio.run(scenario())


def test_feishu_research_callback_preserves_base_session(monkeypatch) -> None:
    async def scenario() -> None:
        channel = FeishuChannel(FeishuConfig(allow_from=["ou_user"]), MessageBus())
        channel._loop = asyncio.get_running_loop()
        captured: dict = {}

        async def fake_handle(payload):
            captured.update(payload)

        monkeypatch.setattr(channel, "_handle_research_card_action", fake_handle)
        value = FeishuChannel._research_callback(
            "premarket",
            FeishuChannel._research_route(
                reply_chat_id="ou_user",
                chat_type="p2p",
                session_key=None,
            ),
        )
        data = SimpleNamespace(
            header=SimpleNamespace(event_id="event-continue"),
            event=SimpleNamespace(
                operator=SimpleNamespace(open_id="ou_user"),
                action=SimpleNamespace(
                    value=value,
                    form_value={},
                ),
                context=SimpleNamespace(
                    open_chat_id="oc_chat",
                    open_message_id="om_card",
                ),
            ),
        )

        channel._on_card_action_trigger(data)
        await asyncio.sleep(0.05)

        assert captured["base_session_key"] == "feishu:ou_user"
        assert captured["action"] == "premarket"

    asyncio.run(scenario())


def test_feishu_stock_selection_checks_reuse_before_starting_agent(monkeypatch) -> None:
    async def scenario() -> None:
        channel = FeishuChannel(FeishuConfig(allow_from=["ou_user"]), MessageBus())
        captured: dict = {}

        async def fake_handle_message(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(channel, "_handle_message", fake_handle_message)
        await channel._handle_research_card_action(
            {
                "action": "custom_stock",
                "code": "600036",
                "sender_id": "ou_user",
                "reply_chat_id": "ou_user",
                "chat_type": "p2p",
                "base_session_key": "feishu:ou_user",
                "source_message_id": "om_card",
            }
        )

        assert captured["metadata"]["stock_research_action"] == "prepare"
        assert captured["metadata"]["research_symbol"] == "600036.SH"
        assert captured["session_key"].endswith(":research:symbol:600036.SH")
        assert "600036.SH" in captured["content"]

    asyncio.run(scenario())
