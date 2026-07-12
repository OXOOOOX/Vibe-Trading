from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from src.channels.bus.queue import MessageBus
from src.channels.feishu import FeishuChannel, FeishuConfig


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
