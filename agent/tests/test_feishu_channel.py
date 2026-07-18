from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from src.channels.bus.events import DeliveryReceipt, OutboundMessage
from src.channels.bus.queue import MessageBus
from src.channels.feishu import (
    FeishuChannel,
    FeishuConfig,
    _direct_research_action,
    _is_new_research_request,
    _monitor_binding_code,
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


def test_feishu_strict_delivery_rejects_missing_provider_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = FeishuChannel(FeishuConfig(), MessageBus())
    channel._client = object()
    monkeypatch.setattr(channel, "_send_message_sync", lambda *_args: None)

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="rejected the text message"):
            await channel.send(
                OutboundMessage(
                    channel="feishu",
                    chat_id="ou_user",
                    content="report",
                    metadata={"_require_delivery_receipt": True},
                )
            )

    asyncio.run(scenario())


def test_feishu_monitor_alert_combines_sticker_and_uses_delivery_id_uuid(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = FeishuChannel(FeishuConfig(), MessageBus())
    channel._client = object()
    sticker = tmp_path / "ymca.webp"
    sticker.write_bytes(b"authorized-sticker")
    captured: dict[str, object] = {}

    monkeypatch.setattr(channel, "_upload_image_sync", lambda path: "img_monitor_1")

    def fake_send(receive_id_type, receive_id, msg_type, content, delivery_uuid=None):
        captured.update(
            receive_id_type=receive_id_type,
            receive_id=receive_id,
            msg_type=msg_type,
            content=content,
            delivery_uuid=delivery_uuid,
        )
        return "om-monitor-1"

    monkeypatch.setattr(channel, "_send_message_sync", fake_send)
    receipt = asyncio.run(
        channel.send(
            OutboundMessage(
                channel="feishu",
                chat_id="ou_user",
                content="## 600036.SH 突破提醒",
                metadata={
                    "portfolio_monitor_alert": True,
                    "monitor_sticker_path": str(sticker),
                    "_delivery_uuid": "delivery-123",
                    "_require_delivery_receipt": True,
                },
            )
        )
    )

    assert isinstance(receipt, DeliveryReceipt)
    assert receipt.remote_message_id == "om-monitor-1"
    assert receipt.provider_request_id == "delivery-123"
    assert captured["msg_type"] == "interactive"
    assert captured["delivery_uuid"] == "delivery-123"
    card = json.loads(str(captured["content"]))
    assert [element["tag"] for element in card["elements"]] == ["markdown", "img"]
    assert card["elements"][1]["img_key"] == "img_monitor_1"
    assert card["elements"][1]["alt"]["content"] == "YMCA 关键价位提醒"


def test_feishu_monitor_alert_image_failure_degrades_to_text_only_card(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = FeishuChannel(FeishuConfig(), MessageBus())
    channel._client = object()
    sticker = tmp_path / "ymca.gif"
    sticker.write_bytes(b"gif")
    sent: list[tuple] = []

    monkeypatch.setattr(channel, "_upload_image_sync", lambda path: None)
    monkeypatch.setattr(
        channel,
        "_send_message_sync",
        lambda *args: sent.append(args) or "om-monitor-fallback",
    )

    asyncio.run(
        channel.send(
            OutboundMessage(
                channel="feishu",
                chat_id="oc_group",
                content="monitor alert",
                metadata={
                    "portfolio_monitor_alert": True,
                    "monitor_sticker_path": str(sticker),
                    "_delivery_uuid": "delivery-fallback",
                    "_require_delivery_receipt": True,
                },
            )
        )
    )

    assert len(sent) == 1
    assert sent[0][0:3] == ("chat_id", "oc_group", "interactive")
    assert sent[0][4] == "delivery-fallback"
    assert json.loads(sent[0][3])["elements"] == [
        {"tag": "markdown", "content": "monitor alert"}
    ]


def test_feishu_monitor_alert_without_cue_sends_one_text_only_card_with_delivery_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = FeishuChannel(FeishuConfig(), MessageBus())
    channel._client = object()
    sent: list[tuple] = []

    def unexpected_upload(_path: str):
        raise AssertionError("a cue-free alert must not upload a sticker")

    monkeypatch.setattr(channel, "_upload_image_sync", unexpected_upload)
    monkeypatch.setattr(
        channel,
        "_send_message_sync",
        lambda *args: sent.append(args) or "om-monitor-plain",
    )

    asyncio.run(
        channel.send(
            OutboundMessage(
                channel="feishu",
                chat_id="ou_user",
                content="plain monitoring alert",
                metadata={
                    "portfolio_monitor_alert": True,
                    "monitor_alert_cue": "none",
                    "monitor_sticker_path": "",
                    "_delivery_uuid": "delivery-plain",
                    "_require_delivery_receipt": True,
                },
            )
        )
    )

    assert len(sent) == 1
    assert sent[0][0:3] == ("open_id", "ou_user", "interactive")
    assert sent[0][4] == "delivery-plain"
    assert json.loads(sent[0][3])["elements"] == [
        {"tag": "markdown", "content": "plain monitoring alert"}
    ]


def test_feishu_stable_delivery_uuid_is_reused_across_uncertain_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = FeishuChannel(FeishuConfig(), MessageBus())
    channel._client = object()
    captured: list[str | None] = []

    def fake_send(*args):
        captured.append(args[4])
        return "om-result"

    monkeypatch.setattr(channel, "_send_message_sync", fake_send)
    message = OutboundMessage(
        channel="feishu",
        chat_id="ou_user",
        content="final answer",
        metadata={
            "_require_delivery_receipt": True,
            "_delivery_uuid": "9ca707c4-1322-5b24-a820-e39b764268b7",
        },
    )

    asyncio.run(channel.send(message))
    asyncio.run(channel.send(message))

    assert captured[0] == captured[1]
    assert captured[0]


def test_feishu_does_not_fallback_after_uncertain_strict_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = FeishuChannel(FeishuConfig(reply_to_message=True), MessageBus())
    channel._client = object()
    creates: list[tuple] = []
    monkeypatch.setattr(channel, "_reply_message_sync", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        channel,
        "_send_message_sync",
        lambda *args: creates.append(args) or "om-created",
    )

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="outcome is uncertain"):
            await channel.send(
                OutboundMessage(
                    channel="feishu",
                    chat_id="ou_user",
                    content="answer",
                    metadata={
                        "message_id": "om-origin",
                        "_require_delivery_receipt": True,
                        "_delivery_uuid": "stable-root",
                    },
                )
            )

    asyncio.run(scenario())
    assert creates == []


def test_feishu_releases_inflight_and_withholds_reaction_until_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        bus = MessageBus()
        bus.require_inbound_acceptance = True
        channel = FeishuChannel(FeishuConfig(allow_from=["ou_user"]), bus)
        channel._client = object()
        reactions: list[str] = []

        async def fake_reaction(message_id: str, _emoji: str):
            reactions.append(message_id)
            return "reaction-1"

        monkeypatch.setattr(channel, "_add_reaction", fake_reaction)
        message = SimpleNamespace(
            message_id="om-durable",
            chat_id="oc-chat",
            chat_type="p2p",
            message_type="text",
            content=json.dumps({"text": "请分析 513120"}, ensure_ascii=False),
            mentions=[],
            parent_id=None,
            root_id=None,
            thread_id=None,
        )
        sender = SimpleNamespace(
            sender_type="user",
            sender_id=SimpleNamespace(open_id="ou_user"),
        )
        event = SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))

        first = asyncio.create_task(channel._on_message(event))
        rejected = await asyncio.wait_for(bus.consume_inbound(), timeout=1)
        assert reactions == []
        rejected.reject(RuntimeError("dispatch db unavailable"))
        with pytest.raises(RuntimeError, match="dispatch db unavailable"):
            await first
        assert "om-durable" not in channel._processed_message_ids
        assert channel._inflight_message_ids == {}
        assert reactions == []

        retry = asyncio.create_task(channel._on_message(event))
        accepted = await asyncio.wait_for(bus.consume_inbound(), timeout=1)
        assert accepted.source_event_id == "om-durable"
        accepted.accept({"job_id": "job-1"})
        await retry
        await asyncio.sleep(0)

        assert reactions == ["om-durable"]
        assert "om-durable" in channel._processed_message_ids
        assert channel._last_persisted_at is not None

    asyncio.run(scenario())


def test_feishu_sync_callback_propagates_async_failure_to_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = FeishuChannel(FeishuConfig(), MessageBus())
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    thread.start()
    channel._loop = loop

    async def fail(_data):
        raise RuntimeError("persist failed")

    monkeypatch.setattr(channel, "_on_message", fail)
    data = SimpleNamespace(
        event=SimpleNamespace(message=SimpleNamespace(message_id="om-fail"))
    )
    try:
        with pytest.raises(RuntimeError, match="persist failed"):
            channel._on_message_sync(data)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


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


def test_feishu_monitor_binding_code_parser_requires_explicit_command() -> None:
    assert _monitor_binding_code("绑定监控 ABCD-EFGH") == "ABCD-EFGH"
    assert _monitor_binding_code("监控绑定 abcd efgh") == "ABCD-EFGH"
    assert _monitor_binding_code("/bind-monitor ABCDEFGH") == "ABCD-EFGH"
    assert _monitor_binding_code("ABCD-EFGH") is None
    assert _monitor_binding_code("不要绑定监控 ABCD-EFGH") is None


@pytest.mark.parametrize("chat_type", ["p2p", "group"])
def test_feishu_monitor_binding_claims_private_or_group_before_allowlist(
    chat_type: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed: dict = {}
    notices: list[dict] = []

    def claim(**kwargs):
        claimed.update(kwargs)
        return {
            "target_id": "target-1",
            "chat_id": kwargs["chat_id"],
            "chat_type": kwargs["chat_type"],
        }

    config = FeishuConfig(group_policy="mention", allow_from=[])
    channel = FeishuChannel(
        config,
        MessageBus(),
        monitor_binding_claimer=claim,
    )
    channel._bot_open_id = "ou_bot"

    async def fake_notice(**kwargs):
        notices.append(kwargs)

    monkeypatch.setattr(channel, "_send_research_notice", fake_notice)
    mentions = [_mention("ou_bot")] if chat_type == "group" else []
    prefix = "@_user_1 " if chat_type == "group" else ""
    message = SimpleNamespace(
        message_id=f"om_bind_{chat_type}",
        chat_id="oc_monitor_group" if chat_type == "group" else "oc_private_chat",
        chat_type=chat_type,
        message_type="text",
        content=json.dumps({"text": f"{prefix}绑定监控 ABCD-EFGH"}, ensure_ascii=False),
        mentions=mentions,
        parent_id=None,
        root_id=None,
        thread_id=None,
    )
    sender = SimpleNamespace(
        sender_type="user",
        sender_id=SimpleNamespace(open_id="ou_user"),
    )

    asyncio.run(
        channel._on_message(
            SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))
        )
    )

    expected_chat_id = "oc_monitor_group" if chat_type == "group" else "ou_user"
    assert claimed == {
        "code": "ABCD-EFGH",
        "channel": "feishu",
        "chat_id": expected_chat_id,
        "chat_type": chat_type,
        "sender_id": "ou_user",
        "session_key": f"feishu:{expected_chat_id}",
    }
    assert notices[0]["reply_chat_id"] == expected_chat_id
    assert "绑定" in notices[0]["content"]
    assert channel.bus.inbound_size == 0


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
        "start_deep_stock_research",
        "start_stock_research",
    }
    assert all(item["symbol"] == "600036.SH" for item in callbacks)
    assert all(item["analysis_action"] == "holding" for item in callbacks)
    assert next(
        item for item in callbacks if item["action"] == "send_stock_daily_report"
    )["artifact_id"] == "pdf-today"
    assert "不会重复生成" in _elements_with_tag(card, "markdown")[0]["content"]


def test_feishu_stock_reuse_card_reuses_persisted_deep_report_by_id() -> None:
    card = FeishuChannel._build_stock_reuse_card(
        {
            "symbol": "301308.SZ",
            "name": "江波龙",
            "analysis_action": "holding",
            "deep_report": {
                "report_id": "report_0123456789abcdef",
                "session_id": "session-deep",
                "revision": 3,
                "quality_status": "passed_with_gaps",
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

    deep_actions = {
        item["action"]: item
        for item in callbacks
        if item["action"] in {"send_deep_stock_report", "continue_deep_stock_report"}
    }
    assert set(deep_actions) == {"send_deep_stock_report", "continue_deep_stock_report"}
    assert all(
        item["deep_report_id"] == "report_0123456789abcdef"
        for item in deep_actions.values()
    )
    summary = _elements_with_tag(card, "markdown")[0]["content"]
    assert "已完成，部分结论保留" in summary
    assert "passed_with_gaps" not in summary
    assert "revision" not in summary


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

    research = FeishuChannel._build_research_completion_card(
        {
            "title": "科创50ETF持仓分析",
            "symbol": "588870.SH",
            "market_as_of": "2026-07-16 09:47",
            "trend_summary": "重新站回关键支撑，但资金仍在分化。",
            "condition_status": "available",
            "condition_summary": "仅保留突破确认和回踩确认两个观察情景。",
            "conditions": [
                {
                    "trigger": "放量站稳关键位",
                    "confirmation": "连续确认",
                    "invalidation": "跌回关键位下方",
                    "response": "人工复核后再决定",
                }
            ],
            "data_scopes": [
                {"scope": "daily", "status": "verified"},
                {"scope": "intraday", "status": "verified"},
                {"scope": "news", "status": "partial", "reason": "部分来源延迟"},
            ],
        },
        reply_chat_id="ou_user",
        chat_type="p2p",
        session_key="feishu:ou_user",
        route_key="feishu:ou_user:research:symbol:588870.SH",
    )
    research_actions = {
        button["behaviors"][0]["value"]["action"]
        for button in _elements_with_tag(research, "button")
    }
    assert research_actions == {
        "send_current_report",
        "continue_condition_order",
        "refresh_research_report",
        "regenerate_research_report",
    }
    research_text = _elements_with_tag(research, "markdown")[0]["content"]
    assert "日线已校核｜盘中已校核" in research_text
    assert "当前走势" in research_text
    assert "条件单 · 可设置" in research_text
    assert "新闻：部分来源延迟" in research_text

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

    failed = FeishuChannel._build_daily_failed_card(
        "09:12 自动组合晨会生成失败。",
        run_id="dpr_1",
        revision=2,
        reply_chat_id="ou_user",
        chat_type="p2p",
        session_key=None,
    )
    assert failed["header"]["template"] == "red"
    failed_callbacks = [
        button["behaviors"][0]["value"]
        for button in _elements_with_tag(failed, "button")
    ]
    assert failed_callbacks[0]["action"] == "rerun_daily"

    failed_without_run = FeishuChannel._build_daily_failed_card(
        "09:12 自动组合晨会生成失败。",
        run_id="",
        revision=1,
        reply_chat_id="ou_user",
        chat_type="p2p",
        session_key=None,
    )
    no_run_callbacks = [
        button["behaviors"][0]["value"]
        for button in _elements_with_tag(failed_without_run, "button")
    ]
    assert no_run_callbacks[0]["action"] == "rerun_daily"
    assert no_run_callbacks[0]["run_id"] == ""


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

        async def fake_handle(sender_id: str, event_key: str, _event_id: str = "") -> None:
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
