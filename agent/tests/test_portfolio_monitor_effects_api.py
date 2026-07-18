from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from src.channels.bus.events import DeliveryReceipt
from src.api.portfolio_monitor_routes import (
    _MONITOR_SSE_EMPTY_CURSOR,
    _monitor_event_stream,
    _valid_audio_magic,
    _valid_sticker_magic,
    register_portfolio_monitor_routes,
)


_VALID_MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00"
_VALID_PNG = b"\x89PNG\r\n\x1a\n"
_VALID_WEBP = b"RIFF\x04\x00\x00\x00WEBP"


@dataclass
class FakeStore:
    plan: dict[str, Any] = field(
        default_factory=lambda: {
            "plan": {
                "market_rules": [
                    {
                        "client_rule_id": "rule-1",
                        "kind": "price_cross_above",
                        "enabled": True,
                        "alert_cue": "ymca_v1",
                    }
                ]
            }
        }
    )
    events: list[dict[str, Any]] = field(default_factory=list)
    activated: bool = False

    def get_plan(self, profile_id: str, version: int) -> dict[str, Any] | None:
        return self.plan

    def activate(self, profile_id: str, version: int, *, max_active: int) -> dict[str, Any]:
        self.activated = True
        return {"profile_id": profile_id, "version": version, "status": "active"}

    def latest_event_cursor(self) -> str | None:
        ordered = self._ordered_events()
        return str(ordered[-1]["event_id"]) if ordered else None

    def list_events_after(self, event_id: str, limit: int = 200) -> list[dict[str, Any]]:
        ordered = self._ordered_events()
        positions = [index for index, event in enumerate(ordered) if event["event_id"] == event_id]
        if not positions:
            raise KeyError(event_id)
        return ordered[positions[0] + 1 : positions[0] + 1 + limit]

    def list_events_from_start(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._ordered_events()[:limit]

    def list_events(self, *, limit: int = 50, symbol: str | None = None) -> list[dict[str, Any]]:
        values = self._ordered_events()
        if symbol:
            values = [event for event in values if event.get("symbol") == symbol]
        return list(reversed(values))[:limit]

    def _ordered_events(self) -> list[dict[str, Any]]:
        return list(self.events)


class FakeService:
    def __init__(self, store: FakeStore):
        self.store = store

    def status(self, runtime_status: dict[str, Any]) -> dict[str, Any]:
        return {"runtime": runtime_status, "effects": {"other": {"available": True}}}


class FakeRuntime:
    def status(self) -> dict[str, Any]:
        return {"running": False, "mode": "off"}

    async def start(self, force: bool = False) -> None:
        return None

    async def stop(self) -> None:
        return None


async def _require_bearer(request: Request) -> None:
    if request.headers.get("authorization") != "Bearer secret":
        raise HTTPException(status_code=401, detail="auth required")


def _app(store: FakeStore) -> FastAPI:
    app = FastAPI()
    service = FakeService(store)
    runtime = FakeRuntime()
    register_portfolio_monitor_routes(
        app,
        _require_bearer,
        get_service=lambda: service,
        get_runtime=lambda: runtime,
        set_runtime_config=lambda enabled, mode: mode or "off",
        require_event_stream_auth=_require_bearer,
    )
    return app


def _event(event_id: str, seen_at: str) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "status": "confirmed",
        "symbol": "600036.SH",
        "first_seen_at": seen_at,
        "facts": {"alert_cue": "ymca_v1", "delivery_mode": "deliver"},
    }


class _DisconnectAfter:
    def __init__(self, connected_checks: int):
        self.remaining = connected_checks

    async def is_disconnected(self) -> bool:
        if self.remaining <= 0:
            return True
        self.remaining -= 1
        return False


async def _frames(store: FakeStore, cursor: str | None, connected_checks: int = 1) -> list[str]:
    return [
        frame
        async for frame in _monitor_event_stream(
            _DisconnectAfter(connected_checks),
            store,
            cursor,
            poll_seconds=0,
            heartbeat_seconds=999,
        )
    ]


def test_effect_status_audio_endpoint_and_activation_gate(tmp_path, monkeypatch) -> None:
    store = FakeStore()
    client = TestClient(_app(store), client=("203.0.113.10", 50000))
    headers = {"Authorization": "Bearer secret"}
    audio = tmp_path / "ymca.mp3"
    sticker = tmp_path / "ymca-up.webp"
    audio.write_bytes(_VALID_MP3)
    sticker.write_bytes(_VALID_WEBP)

    monkeypatch.delenv("VIBE_TRADING_MONITOR_YMCA_AUDIO_PATH", raising=False)
    monkeypatch.delenv("VIBE_TRADING_MONITOR_YMCA_STICKER_PATH", raising=False)
    monkeypatch.delenv("VIBE_TRADING_MONITOR_YMCA_DOWN_STICKER_PATH", raising=False)
    status = client.get("/portfolio/monitoring/status", headers=headers)
    assert status.status_code == 200
    assert status.json()["effects"]["ymca_v1"] == {
        "audio_ready": False,
        "up_sticker_ready": False,
        "down_sticker_ready": False,
        "sticker_ready": False,
        "available": False,
    }
    assert status.json()["effects"]["other"] == {"available": True}
    blocked = client.post("/portfolio/monitors/profile-1/plans/1/activate", headers=headers)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["error_code"] == "monitor_effect_unavailable"
    assert store.activated is False

    monkeypatch.setenv("VIBE_TRADING_MONITOR_YMCA_AUDIO_PATH", str(audio))
    monkeypatch.setenv("VIBE_TRADING_MONITOR_YMCA_STICKER_PATH", str(sticker))
    ready = client.get("/portfolio/monitoring/status", headers=headers)
    assert ready.json()["effects"]["ymca_v1"] == {
        "audio_ready": True,
        "up_sticker_ready": True,
        "down_sticker_ready": False,
        "sticker_ready": False,
        "available": False,
    }
    assert str(audio) not in ready.text
    assert str(sticker) not in ready.text

    assert client.get("/portfolio/monitor-effects/ymca_v1/audio").status_code == 401
    served = client.get("/portfolio/monitor-effects/ymca_v1/audio", headers=headers)
    assert served.status_code == 200
    assert served.content == _VALID_MP3
    assert served.headers["content-type"].startswith("audio/mpeg")
    assert served.headers["cache-control"] == "private, no-store"

    activated = client.post("/portfolio/monitors/profile-1/plans/1/activate", headers=headers)
    assert activated.status_code == 200
    assert store.activated is True


def test_downward_cue_activation_requires_only_audio_and_down_sticker(
    tmp_path, monkeypatch
) -> None:
    store = FakeStore()
    store.plan["plan"]["market_rules"][0]["kind"] = "price_cross_below"
    client = TestClient(_app(store), client=("203.0.113.10", 50000))
    headers = {"Authorization": "Bearer secret"}
    audio = tmp_path / "ymca.mp3"
    down_sticker = tmp_path / "ymca-down.png"
    audio.write_bytes(_VALID_MP3)
    down_sticker.write_bytes(_VALID_PNG)
    monkeypatch.setenv("VIBE_TRADING_MONITOR_YMCA_AUDIO_PATH", str(audio))
    monkeypatch.delenv("VIBE_TRADING_MONITOR_YMCA_STICKER_PATH", raising=False)
    monkeypatch.setenv(
        "VIBE_TRADING_MONITOR_YMCA_DOWN_STICKER_PATH", str(down_sticker)
    )

    status = client.get("/portfolio/monitoring/status", headers=headers)
    assert status.json()["effects"]["ymca_v1"] == {
        "audio_ready": True,
        "up_sticker_ready": False,
        "down_sticker_ready": True,
        "sticker_ready": False,
        "available": False,
    }
    activated = client.post(
        "/portfolio/monitors/profile-1/plans/1/activate", headers=headers
    )
    assert activated.status_code == 200
    assert store.activated is True


def test_invalid_or_oversized_sticker_is_not_ready(tmp_path, monkeypatch) -> None:
    store = FakeStore()
    client = TestClient(_app(store))
    audio = tmp_path / "ymca.mp3"
    sticker = tmp_path / "ymca.gif"
    audio.write_bytes(_VALID_MP3)
    with sticker.open("wb") as handle:
        handle.write(b"GIF89a")
        handle.truncate(10 * 1024 * 1024 + 1)
    monkeypatch.setenv("VIBE_TRADING_MONITOR_YMCA_AUDIO_PATH", str(audio))
    monkeypatch.setenv("VIBE_TRADING_MONITOR_YMCA_STICKER_PATH", str(sticker))
    monkeypatch.delenv("VIBE_TRADING_MONITOR_YMCA_DOWN_STICKER_PATH", raising=False)

    status = client.get(
        "/portfolio/monitoring/status",
        headers={"Authorization": "Bearer secret"},
    )
    assert status.json()["effects"]["ymca_v1"] == {
        "audio_ready": True,
        "up_sticker_ready": False,
        "down_sticker_ready": False,
        "sticker_ready": False,
        "available": False,
    }


@pytest.mark.parametrize(
    ("extension", "payload"),
    [
        (".mp3", _VALID_MP3),
        (".mp3", b"\xff\xfb\x90\x64"),
        (".wav", b"RIFF\x04\x00\x00\x00WAVE"),
        (".ogg", b"OggS\x00\x02"),
        (".oga", b"OggS\x00\x02"),
        (".opus", b"OggS\x00\x02OpusHead"),
        (".webm", b"\x1aE\xdf\xa3"),
        (".m4a", b"\x00\x00\x00\x18ftypM4A "),
        (".mp4", b"\x00\x00\x00\x18ftypisom"),
        (".aac", b"\xff\xf1\x50\x80"),
    ],
)
def test_monitor_audio_magic_accepts_supported_containers(extension: str, payload: bytes) -> None:
    assert _valid_audio_magic(extension, payload) is True
    assert _valid_audio_magic(extension, b"renamed-but-corrupt") is False


@pytest.mark.parametrize(
    ("extension", "payload"),
    [
        (".gif", b"GIF87a"),
        (".gif", b"GIF89a"),
        (".png", _VALID_PNG),
        (".webp", _VALID_WEBP),
    ],
)
def test_monitor_sticker_magic_accepts_supported_formats(extension: str, payload: bytes) -> None:
    assert _valid_sticker_magic(extension, payload) is True
    assert _valid_sticker_magic(extension, b"renamed-but-corrupt") is False


def test_renamed_corrupt_assets_do_not_pass_readiness(tmp_path, monkeypatch) -> None:
    store = FakeStore()
    client = TestClient(_app(store))
    audio = tmp_path / "ymca.mp3"
    sticker = tmp_path / "ymca.webp"
    audio.write_bytes(b"not an mp3")
    sticker.write_bytes(b"not a webp")
    monkeypatch.setenv("VIBE_TRADING_MONITOR_YMCA_AUDIO_PATH", str(audio))
    monkeypatch.setenv("VIBE_TRADING_MONITOR_YMCA_STICKER_PATH", str(sticker))
    monkeypatch.delenv("VIBE_TRADING_MONITOR_YMCA_DOWN_STICKER_PATH", raising=False)
    headers = {"Authorization": "Bearer secret"}

    status = client.get("/portfolio/monitoring/status", headers=headers)
    assert status.json()["effects"]["ymca_v1"] == {
        "audio_ready": False,
        "up_sticker_ready": False,
        "down_sticker_ready": False,
        "sticker_ready": False,
        "available": False,
    }
    assert client.get(
        "/portfolio/monitor-effects/ymca_v1/audio",
        headers=headers,
    ).status_code == 404
    assert client.post(
        "/portfolio/monitors/profile-1/plans/1/activate",
        headers=headers,
    ).status_code == 409
    assert store.activated is False


def test_monitor_delivery_passes_sticker_and_exact_delivery_uuid(tmp_path, monkeypatch) -> None:
    import api_server

    sticker = tmp_path / "ymca.png"
    sticker.write_bytes(_VALID_PNG)
    monkeypatch.setenv("VIBE_TRADING_MONITOR_YMCA_STICKER_PATH", str(sticker))
    captured: list[Any] = []

    class Manager:
        async def send_direct(self, message) -> DeliveryReceipt:
            captured.append(message)
            return DeliveryReceipt(
                provider="feishu",
                remote_message_id="om-monitor-test",
                provider_request_id="delivery-123",
                accepted_at="2026-07-15T01:05:01+00:00",
            )

    class Runtime:
        manager = Manager()

        def status(self) -> dict[str, Any]:
            return {"running": True}

    monkeypatch.setattr(api_server, "_get_channel_runtime", lambda: Runtime())
    event = {
        "event_id": "event-1",
        "symbol": "600036.SH",
        "title": "突破提醒",
        "summary": "confirmed",
        "facts": {
            "alert_cue": "ymca_v1",
            "direction": "above",
            "threshold": 42.0,
            "last_price": 42.1,
            "bar_time": "2026-07-15T01:05:00+00:00",
            "sources": ["verified"],
        },
    }
    delivery = {
        "delivery_id": "delivery-123",
        "channel": "feishu",
        "chat_id": "ou_user",
        "session_key": "feishu:ou_user",
    }

    asyncio.run(api_server._deliver_portfolio_monitor_event(event, delivery))

    assert len(captured) == 1
    message = captured[0]
    assert message.metadata["portfolio_monitor_alert"] is True
    assert message.metadata["_delivery_uuid"] == "delivery-123"
    assert message.metadata["monitor_sticker_path"] == str(sticker)
    assert message.metadata["monitor_alert_cue"] == "ymca_v1"


def test_monitor_delivery_selects_down_sticker_and_unknown_direction_is_text_only(
    tmp_path, monkeypatch
) -> None:
    import api_server

    up_sticker = tmp_path / "up.png"
    down_sticker = tmp_path / "down.png"
    up_sticker.write_bytes(_VALID_PNG)
    down_sticker.write_bytes(_VALID_PNG)
    monkeypatch.setenv("VIBE_TRADING_MONITOR_YMCA_STICKER_PATH", str(up_sticker))
    monkeypatch.setenv(
        "VIBE_TRADING_MONITOR_YMCA_DOWN_STICKER_PATH", str(down_sticker)
    )
    captured: list[Any] = []

    class Manager:
        async def send_direct(self, message) -> DeliveryReceipt:
            captured.append(message)
            return DeliveryReceipt(
                provider="feishu",
                remote_message_id="om-monitor-test",
                provider_request_id=message.metadata["_delivery_uuid"],
                accepted_at="2026-07-15T01:05:01+00:00",
            )

    class Runtime:
        manager = Manager()

        def status(self) -> dict[str, Any]:
            return {"running": True}

    monkeypatch.setattr(api_server, "_get_channel_runtime", lambda: Runtime())
    delivery = {
        "delivery_id": "delivery-down",
        "channel": "feishu",
        "chat_id": "ou_user",
        "session_key": "feishu:ou_user",
    }
    base_event = {
        "event_id": "event-down",
        "symbol": "600036.SH",
        "title": "monitor",
        "summary": "confirmed",
        "facts": {"alert_cue": "ymca_v1", "direction": "below"},
    }

    asyncio.run(api_server._deliver_portfolio_monitor_event(base_event, delivery))
    assert captured[-1].metadata["monitor_sticker_path"] == str(down_sticker)

    unknown = {
        **base_event,
        "event_id": "event-unknown",
        "facts": {"alert_cue": "ymca_v1", "direction": "sideways"},
    }
    delivery["delivery_id"] = "delivery-unknown"
    asyncio.run(api_server._deliver_portfolio_monitor_event(unknown, delivery))
    assert captured[-1].metadata["monitor_sticker_path"] == ""


def test_preopen_delivery_explains_first_check_without_promising_a_signal(monkeypatch) -> None:
    import api_server

    captured: list[Any] = []

    class Manager:
        async def send_direct(self, message) -> DeliveryReceipt:
            captured.append(message)
            return DeliveryReceipt(
                provider="feishu",
                remote_message_id="om-preopen-test",
                provider_request_id="delivery-preopen",
                accepted_at="2026-07-16T01:00:01+00:00",
            )

    class Runtime:
        manager = Manager()

        def status(self) -> dict[str, Any]:
            return {"running": True}

    monkeypatch.setattr(api_server, "_get_channel_runtime", lambda: Runtime())
    event = {
        "event_id": "event-preopen",
        "kind": "monitoring_preopen_notice",
        "symbol": "600036.SH",
        "title": "AI 持仓监控盘前提示",
        "summary": "今日将在 09:35 开始首轮行情检查；只有触发监控规则时才会发送信号提醒。",
        "facts": {
            "symbols": ["600036.SH", "588870.SH"],
            "active_profile_count": 2,
            "first_check_at": "2026-07-16T09:35:00+08:00",
        },
    }
    delivery = {
        "delivery_id": "delivery-preopen",
        "channel": "feishu",
        "chat_id": "ou_user",
        "session_key": "feishu:ou_user",
    }

    asyncio.run(api_server._deliver_portfolio_monitor_event(event, delivery))

    assert len(captured) == 1
    content = captured[0].content
    assert "600036.SH, 588870.SH" in content
    assert "首轮检查：今日 09:35" in content
    assert "不代表一定会产生交易信号" in content


def test_monitor_sse_requires_auth_and_initial_connection_skips_history() -> None:
    store = FakeStore(events=[_event("e1", "2026-07-15T01:00:00+00:00")])
    client = TestClient(_app(store), client=("203.0.113.10", 50000))
    assert client.get("/portfolio/monitor-events/stream").status_code == 401

    frames = asyncio.run(_frames(store, None))
    assert frames[0] == "retry: 2000\n\n"
    assert len(frames) == 2
    assert "event: portfolio.monitor.cursor" in frames[1]
    assert "id: e1" in frames[1]
    assert '"cursor":"e1"' in frames[1]
    assert "portfolio.monitor.confirmed" not in frames[1]


def test_monitor_sse_nonempty_initial_cursor_recovers_short_disconnect() -> None:
    store = FakeStore(events=[_event("e1", "2026-07-15T01:00:00+00:00")])
    initial = asyncio.run(_frames(store, None, connected_checks=0))
    assert "event: portfolio.monitor.cursor" in initial[1]
    assert "id: e1" in initial[1]

    store.events.append(_event("e2", "2026-07-15T01:05:00+00:00"))
    replay = asyncio.run(_frames(store, "e1"))
    assert "event: portfolio.monitor.confirmed" in replay[1]
    assert "id: e2" in replay[1]


def test_monitor_sse_empty_initial_cursor_recovers_short_disconnect() -> None:
    store = FakeStore()
    initial = asyncio.run(_frames(store, None, connected_checks=0))
    assert "event: portfolio.monitor.cursor" in initial[1]
    assert f"id: {_MONITOR_SSE_EMPTY_CURSOR}" in initial[1]
    assert f'"cursor":"{_MONITOR_SSE_EMPTY_CURSOR}"' in initial[1]

    store.events.append(_event("e1", "2026-07-15T01:00:00+00:00"))
    replay = asyncio.run(_frames(store, _MONITOR_SSE_EMPTY_CURSOR))
    assert "event: portfolio.monitor.confirmed" in replay[1]
    assert "id: e1" in replay[1]


def test_monitor_sse_replays_after_cursor_and_resets_unknown_cursor() -> None:
    store = FakeStore(
        events=[
            _event("e1", "2026-07-15T01:00:00+00:00"),
            _event("e2", "2026-07-15T01:05:00+00:00"),
        ]
    )

    replay = asyncio.run(_frames(store, "e1"))
    assert replay[0] == "retry: 2000\n\n"
    assert "event: portfolio.monitor.confirmed" in replay[1]
    assert "id: e2" in replay[1]
    assert '"type":"portfolio.monitor.confirmed"' in replay[1]
    assert '"event_id":"e2"' in replay[1]

    reset = asyncio.run(_frames(store, "missing", connected_checks=0))
    assert reset[0] == "retry: 2000\n\n"
    assert "event: portfolio.monitor.reset" in reset[1]
    assert "id: e2" in reset[1]
    assert '"reason":"cursor_not_found"' in reset[1]
    assert '"cursor":"e2"' in reset[1]

    empty_reset = asyncio.run(_frames(FakeStore(), "missing", connected_checks=0))
    assert "event: portfolio.monitor.reset" in empty_reset[1]
    assert f"id: {_MONITOR_SSE_EMPTY_CURSOR}" in empty_reset[1]
    assert f'"cursor":"{_MONITOR_SSE_EMPTY_CURSOR}"' in empty_reset[1]


def test_monitor_sse_emits_heartbeat_without_events() -> None:
    async def run() -> list[str]:
        return [
            frame
            async for frame in _monitor_event_stream(
                _DisconnectAfter(1),
                FakeStore(),
                None,
                poll_seconds=0,
                heartbeat_seconds=0,
            )
        ]

    frames = asyncio.run(run())
    assert frames[0] == "retry: 2000\n\n"
    assert "event: portfolio.monitor.cursor" in frames[1]
    assert frames[2].startswith(": heartbeat ")
