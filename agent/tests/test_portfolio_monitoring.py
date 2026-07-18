from __future__ import annotations

import asyncio
import copy
import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest

import api_server
from src.market_cache.storage import MarketCacheStore
from src.portfolio.monitoring.planner import MonitoringPlanner
from src.portfolio.monitoring.models import (
    DEFAULT_PRICE_VOLUME_POLICY,
    PlanValidationError,
    validate_plan,
)
from src.portfolio.monitoring.price_volume import PriceVolumeAnalyzer
from src.portfolio.monitoring.replay import replay_quotes
from src.portfolio.monitoring.runtime import MonitoringRuntime
from src.portfolio.monitoring.service import MonitoringService
from src.portfolio.monitoring.store import MonitoringStore
from src.portfolio.state import update_holdings


class FakeMarketStore:
    def __init__(self) -> None:
        self.current_quote = {
            "symbol": "600036.SH",
            "interval": "5m",
            "bar_time": "2026-07-14T02:00:00+00:00",
            "session_date": "2026-07-14",
            "adjustment": "raw",
            "last_price": 40.0,
            "status": "verified",
            "sources": ["tencent", "mootdx"],
            "verified_at": "2026-07-14T02:00:10+00:00",
        }

    def quote(self, symbol: str):
        return dict(self.current_quote) if symbol == "600036.SH" else None

    def query_bars(self, **kwargs):
        return [
            {
                "bar_time": f"2026-06-{index:02d}T07:00:00+00:00",
                "open": 38 + index / 10,
                "high": 39 + index / 10,
                "low": 37 + index / 10,
                "close": 38.5 + index / 10,
                "status": "verified",
            }
            for index in range(1, 21)
        ]


class FakeMarketService:
    def __init__(self) -> None:
        self.store = FakeMarketStore()
        self.refresh_calls = 0
        self.last_refresh_kwargs = None

    def refresh_sync(self, **kwargs):
        assert kwargs["read_only"] is True
        self.refresh_calls += 1
        self.last_refresh_kwargs = dict(kwargs)
        return {"status": "completed"}


def test_planner_uses_recent_verified_five_minute_bar_when_latest_quote_is_stale() -> None:
    market = FakeMarketService()
    market.store.current_quote["status"] = "stale"
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)

    def query_bars(**kwargs):
        if kwargs["interval"] == "5m":
            return [
                {
                    "bar_time": recent.isoformat(),
                    "session_date": recent.date().isoformat(),
                    "close": 40.1,
                    "status": "verified",
                    "source_count": 2,
                    "sources": ["yahoo", "nasdaq"],
                }
            ]
        if kwargs["interval"] == "1m":
            return []
        return FakeMarketStore().query_bars(**kwargs)

    market.store.query_bars = query_bars
    plan, evidence, blocked = MonitoringPlanner(market).build(
        {"symbol": "600036.SH", "name": "test", "quantity": 1, "cost_price": 38}
    )

    assert blocked == []
    assert plan is not None
    assert evidence["quote"]["interval"] == "5m"
    assert evidence["quote"]["sources"] == ["yahoo", "nasdaq"]


def test_plan_check_frequency_cannot_be_slower_than_enabled_rule_bars() -> None:
    plan, _evidence, blocked = MonitoringPlanner(FakeMarketService()).build(
        {"symbol": "600036.SH", "name": "test", "quantity": 1, "cost_price": 38}
    )
    assert blocked == []
    assert plan is not None

    one_minute_rule = copy.deepcopy(plan)
    one_minute_rule["market_rules"][0]["parameters"]["interval"] = "1m"
    one_minute_rule["quote_tier"] = "normal"
    with pytest.raises(PlanValidationError, match="cannot be slower"):
        validate_plan(one_minute_rule, expected_symbol="600036.SH")

    one_minute_rule["quote_tier"] = "active"
    assert validate_plan(one_minute_rule, expected_symbol="600036.SH")["quote_tier"] == "active"

    fifteen_minute_polling = copy.deepcopy(plan)
    fifteen_minute_polling["quote_tier"] = "low"
    with pytest.raises(PlanValidationError, match="cannot be slower"):
        validate_plan(fifteen_minute_polling, expected_symbol="600036.SH")

    invalid_data_mode = copy.deepcopy(plan)
    invalid_data_mode["data_mode"] = "unverified_any_source"
    with pytest.raises(PlanValidationError, match="data mode"):
        validate_plan(invalid_data_mode, expected_symbol="600036.SH")


def test_planner_builds_a_labeled_two_sided_price_target_ladder() -> None:
    plan, evidence, blocked = MonitoringPlanner(FakeMarketService()).build(
        {"symbol": "600036.SH", "name": "test", "quantity": 1, "cost_price": 38}
    )

    assert blocked == []
    assert plan is not None
    price_rules = [rule for rule in plan["market_rules"] if rule["kind"].startswith("price_cross")]
    assert len(price_rules) == 4
    by_id = {rule["client_rule_id"]: rule for rule in price_rules}
    assert by_id["add-position-watch-level-1"]["target_intent"] == "add_position"
    assert by_id["add-position-watch-level-1"]["target_level"] == 1
    assert by_id["stop-loss-level-2"]["target_intent"] == "stop_loss"
    assert by_id["stop-loss-level-2"]["target_level"] == 2
    assert by_id["take-profit-level-1"]["target_intent"] == "take_profit"
    assert by_id["take-profit-level-2"]["target_level"] == 2
    take_profit_basis = by_id["take-profit-level-1"]["calculation_basis"]
    assert take_profit_basis["method"] == "range_upper_with_noise_buffer"
    assert take_profit_basis["recommended_value"] == pytest.approx(41.0)
    assert "2026-06-20 高点" in take_profit_basis["summary"]
    assert "震荡区间上沿" in take_profit_basis["method_label"]
    stop_loss_basis = by_id["stop-loss-level-2"]["calculation_basis"]
    assert stop_loss_basis["recommended_value"] == pytest.approx(37.1)
    assert "2026-06-01 低点" in stop_loss_basis["summary"]
    assert by_id["add-position-watch-level-1"]["calculation_basis"]["formula"] == "(最新价 + L2 止损点) ÷ 2"
    assert "等距延展" in by_id["take-profit-level-2"]["calculation_basis"]["method_label"]
    assert (
        by_id["stop-loss-level-2"]["parameters"]["threshold"]
        < by_id["add-position-watch-level-1"]["parameters"]["threshold"]
        < 40.0
        < by_id["take-profit-level-1"]["parameters"]["threshold"]
        < by_id["take-profit-level-2"]["parameters"]["threshold"]
    )
    assert evidence["target_ladder"]["upside"][1]["level"] == 2

    invalid_intent = copy.deepcopy(plan)
    invalid_intent["market_rules"][0]["target_intent"] = "automatic_trade"
    with pytest.raises(PlanValidationError, match="target_intent"):
        validate_plan(invalid_intent, expected_symbol="600036.SH")

    invalid_level = copy.deepcopy(plan)
    invalid_level["market_rules"][0]["target_level"] = 1.5
    with pytest.raises(PlanValidationError, match="target_level"):
        validate_plan(invalid_level, expected_symbol="600036.SH")

    invalid_basis = copy.deepcopy(plan)
    invalid_basis["market_rules"][0]["calculation_basis"]["recommended_value"] = "nan"
    with pytest.raises(PlanValidationError, match="recommended_value"):
        validate_plan(invalid_basis, expected_symbol="600036.SH")


def test_v3_alert_cue_contract_and_planner_defaults() -> None:
    plan, _evidence, blocked = MonitoringPlanner(FakeMarketService()).build(
        {"symbol": "600036.SH", "name": "test", "quantity": 1, "cost_price": 38}
    )
    assert blocked == []
    assert plan is not None
    assert plan["schema_version"] == 3
    assert {rule["alert_cue"] for rule in plan["market_rules"]} == {"none"}

    above_indexes = [
        index
        for index, rule in enumerate(plan["market_rules"])
        if rule["kind"] == "price_cross_above"
    ]
    below_indexes = [
        index
        for index, rule in enumerate(plan["market_rules"])
        if rule["kind"] == "price_cross_below"
    ]
    selected = copy.deepcopy(plan)
    selected["market_rules"][above_indexes[0]]["alert_cue"] = "ymca_v1"
    selected["market_rules"][below_indexes[0]]["alert_cue"] = "ymca_v1"
    normalized = validate_plan(selected, expected_symbol="600036.SH")
    assert normalized["market_rules"][above_indexes[0]]["alert_cue"] == "ymca_v1"
    assert normalized["market_rules"][below_indexes[0]]["alert_cue"] == "ymca_v1"

    duplicate = copy.deepcopy(selected)
    duplicate["market_rules"][above_indexes[1]]["alert_cue"] = "ymca_v1"
    with pytest.raises(PlanValidationError, match="at most one price_cross_above"):
        validate_plan(duplicate, expected_symbol="600036.SH")

    duplicate_below = copy.deepcopy(selected)
    duplicate_below["market_rules"][below_indexes[1]]["alert_cue"] = "ymca_v1"
    with pytest.raises(PlanValidationError, match="at most one price_cross_below"):
        validate_plan(duplicate_below, expected_symbol="600036.SH")

    disabled = copy.deepcopy(selected)
    disabled["market_rules"][above_indexes[0]]["enabled"] = False
    with pytest.raises(PlanValidationError, match="enabled price_cross_above or"):
        validate_plan(disabled, expected_symbol="600036.SH")

    wrong_kind = copy.deepcopy(plan)
    wrong_kind["market_rules"][0]["kind"] = "volume_ratio_above"
    wrong_kind["market_rules"][0]["alert_cue"] = "ymca_v1"
    with pytest.raises(PlanValidationError, match="enabled price_cross_above or"):
        validate_plan(wrong_kind, expected_symbol="600036.SH")

    unknown = copy.deepcopy(plan)
    unknown["market_rules"][above_indexes[0]]["alert_cue"] = "air_horn"
    with pytest.raises(PlanValidationError, match="alert_cue"):
        validate_plan(unknown, expected_symbol="600036.SH")

    legacy = copy.deepcopy(selected)
    legacy["schema_version"] = 2
    with pytest.raises(PlanValidationError, match="schema_version=3"):
        validate_plan(legacy, expected_symbol="600036.SH")


def test_monitor_planning_auto_refreshes_once_before_reporting_single_source(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    service.planner.market_service.store.current_quote.update(
        status="single_source",
        sources=["yahoo"],
    )

    item = service.create_draft_batch(["600036.SH"])["items"][0]

    assert service.planner.market_service.refresh_calls == 1
    assert item["status"] == "blocked"
    assert item["blocked_reasons"] == ["quote_not_actionable:single_source"]


def test_explicit_single_source_consent_creates_an_auditable_draft(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    service.planner.market_service.store.current_quote.update(
        status="single_source",
        sources=["yahoo"],
    )

    item = service.create_draft_batch(
        ["600036.SH"],
        allow_single_source=True,
    )["items"][0]

    assert service.planner.market_service.refresh_calls == 1
    assert item["status"] == "ready"
    profile = service.store.get_profile(item["profile_id"])
    assert profile is not None
    draft = profile["plans"][0]
    assert draft["plan"]["data_mode"] == "single_source"
    assert draft["evidence_manifest"]["data_mode"] == "single_source"
    assert draft["evidence_manifest"]["single_source_consent"]["granted"] is True


class FakeRuntime:
    def __init__(self) -> None:
        self.running = False
        self.leader = False

    async def start(self, *, force=False):
        self.running = os.getenv("VIBE_TRADING_MONITORING_ENABLED", "0") == "1"
        self.leader = self.running

    async def stop(self):
        self.running = False
        self.leader = False

    def status(self):
        enabled = os.getenv("VIBE_TRADING_MONITORING_ENABLED", "0") == "1"
        mode = os.getenv("VIBE_TRADING_MONITORING_MODE", "shadow") if enabled else "off"
        return {
            "enabled": enabled,
            "running": self.running,
            "leader": self.leader,
            "mode": mode,
            "last_tick": None,
        }


def _service(tmp_path: Path, monkeypatch) -> MonitoringService:
    monkeypatch.setenv("VIBE_TRADING_PORTFOLIO_STATE_PATH", str(tmp_path / "portfolio.json"))
    update_holdings(
        holdings=[
            {
                "name": "招商银行",
                "code": "600036",
                "symbol": "600036.SH",
                "quantity": 1000,
                "cost_price": 38,
            }
        ]
    )
    market = FakeMarketService()
    return MonitoringService(
        store=MonitoringStore(tmp_path / "monitoring.sqlite3"),
        planner=MonitoringPlanner(market),
    )


def test_autopilot_event_persists_holding_name(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    service.store.set_autopilot_config({
        "enabled": True,
        "runtime_mode": "shadow",
        "selected_symbols": ["600036.SH"],
    })
    monkeypatch.setattr(service, "autopilot_tick", lambda *, force=False: {"status": "skipped"})

    trigger = service.enqueue_autopilot_event(
        trigger_type="approaching",
        symbol="600036.SH",
        fingerprint="approaching-600036",
    )

    assert trigger is not None
    assert trigger["payload"]["holding_name"] == "招商银行"


def test_monitoring_episode_dedup_and_outbox_are_durable(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    target = service.store.bind_target(channel="feishu", chat_id="ou_test")
    batch = service.create_draft_batch(["600036.SH"], target["target_id"])
    assert batch["status"] == "completed"
    item = batch["items"][0]
    assert item["status"] == "ready"

    profile = service.store.activate(item["profile_id"], item["plan_version"], max_active=10)
    plan = profile["plans"][0]["plan"]
    threshold = next(
        rule["parameters"]["threshold"]
        for rule in plan["market_rules"]
        if rule["kind"] == "price_cross_above"
    )
    quote = {
        "last_price": threshold - 1,
        "interval": "5m",
        "bar_time": "2026-07-14T02:05:00+00:00",
        "status": "verified",
        "sources": ["tencent", "mootdx"],
    }
    assert service.store.evaluate_quote(profile["profile_id"], quote) == []
    quote.update(last_price=threshold + 1, bar_time="2026-07-14T02:10:00+00:00")
    assert service.store.evaluate_quote(profile["profile_id"], quote) == []
    quote["bar_time"] = "2026-07-14T02:15:00+00:00"
    events = service.store.evaluate_quote(profile["profile_id"], quote)
    assert len(events) == 1
    assert len(service.store.pending_deliveries()) == 1
    assert service.store.evaluate_quote(profile["profile_id"], quote) == []

    quote.update(last_price=threshold * 0.98, bar_time="2026-07-14T02:20:00+00:00")
    service.store.evaluate_quote(profile["profile_id"], quote)
    quote.update(last_price=threshold + 1, bar_time="2026-07-14T02:25:00+00:00")
    assert service.store.evaluate_quote(profile["profile_id"], quote) == []
    quote["bar_time"] = "2026-07-14T02:30:00+00:00"
    assert len(service.store.evaluate_quote(profile["profile_id"], quote)) == 1
    assert len(service.store.list_events()) == 2
    latest_profile = service.store.list_profiles()[0]
    assert latest_profile["last_quote"]["price"] == pytest.approx(threshold + 1)
    assert latest_profile["last_quote"]["data_as_of"] == "2026-07-14T02:30:00+00:00"
    assert latest_profile["last_quote"]["interval"] == "5m"
    assert latest_profile["last_quote"]["sources"] == ["tencent", "mootdx"]
    assert latest_profile["last_quote"]["previous_price"] == pytest.approx(threshold + 1)
    assert latest_profile["last_quote"]["price_change_pct"] == pytest.approx(0)
    assert latest_profile["last_quote"]["trend"] == "flat"

    claimed = service.store.pending_deliveries()[0]
    assert service.store.claim_delivery(claimed["delivery_id"]) is True
    asyncio.run(MonitoringRuntime(store=service.store, market_service=FakeMarketService()).stop())
    delivery = service.store.get_event(claimed["event_id"])["deliveries"][0]
    assert delivery["status"] == "delivery_uncertain"
    assert delivery["error"] == "runtime stopped during delivery"


def test_monitoring_api_lifecycle(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    monkeypatch.setenv("VIBE_TRADING_MONITOR_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("VIBE_TRADING_MONITORING_ENABLED", "0")
    monkeypatch.setenv("VIBE_TRADING_MONITORING_MODE", "shadow")
    monkeypatch.setenv("VIBE_TRADING_MONITOR_MAINTENANCE_ENABLED", "0")
    monkeypatch.setattr(api_server, "ENV_PATH", tmp_path / "agent.env")
    monkeypatch.setattr(api_server, "_portfolio_monitoring_service", service)
    runtime = FakeRuntime()
    monkeypatch.setattr(api_server, "_portfolio_monitoring_runtime", runtime)
    client = TestClient(api_server.app, client=("127.0.0.1", 50000))

    enabled = client.put(
        "/admin/portfolio/monitoring/config",
        json={"enabled": True, "mode": "shadow"},
    )
    assert enabled.status_code == 200
    assert enabled.json()["enabled_by_config"] is True
    assert enabled.json()["effective_mode"] == "shadow"
    assert enabled.json()["runtime"]["running"] is True
    assert "VIBE_TRADING_MONITORING_ENABLED=1" in api_server.ENV_PATH.read_text(
        encoding="utf-8"
    )
    disabled = client.put(
        "/admin/portfolio/monitoring/config",
        json={"enabled": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled_by_config"] is False
    assert disabled.json()["runtime"]["running"] is False

    target = client.post(
        "/portfolio/monitor-delivery-targets/bind",
        json={"channel": "feishu", "chat_id": "ou_test", "chat_type": "p2p"},
    )
    assert target.status_code == 200
    created = client.post(
        "/portfolio/monitor-draft-batches",
        json={
            "symbols": ["600036.SH"],
            "delivery_target_id": target.json()["target_id"],
            "force_fresh": True,
        },
    )
    assert created.status_code == 202
    assert service.planner.market_service.refresh_calls == 1
    assert service.planner.market_service.last_refresh_kwargs["force"] is True
    assert service.planner.market_service.last_refresh_kwargs["items"] == [
        ("1m", "raw"),
        ("5m", "raw"),
        ("1D", "raw"),
    ]
    assert created.headers["location"].endswith(created.json()["batch_id"])
    item = created.json()["items"][0]

    fetched = client.get(f"/portfolio/monitors/{item['profile_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "pending_review"
    activated = client.post(
        f"/portfolio/monitors/{item['profile_id']}/plans/{item['plan_version']}/activate"
    )
    assert activated.status_code == 200
    assert activated.json()["status"] == "active"
    listed_profile = next(
        profile
        for profile in client.get("/portfolio/monitors").json()["profiles"]
        if profile["profile_id"] == item["profile_id"]
    )
    assert listed_profile["display_plan"]["version"] == item["plan_version"]
    assert listed_profile["display_plan"]["status"] == "active"
    service.planner.market_service.store.current_quote.update(
        status="single_source",
        sources=["yahoo"],
    )
    consented = client.post(
        f"/portfolio/monitors/{item['profile_id']}/reanalyze",
        json={"allow_single_source": True},
    )
    assert consented.status_code == 202
    consented_item = consented.json()["items"][0]
    assert consented_item["status"] == "ready"
    consented_plan = client.get(
        f"/portfolio/monitors/{item['profile_id']}/plans/{consented_item['plan_version']}"
    ).json()
    assert consented_plan["plan"]["data_mode"] == "single_source"
    service.planner.market_service.store.current_quote.update(
        status="verified",
        sources=["tencent", "mootdx"],
    )
    assert client.post(f"/portfolio/monitors/{item['profile_id']}/pause", json={}).json()["status"] == "paused"
    assert client.post(f"/portfolio/monitors/{item['profile_id']}/resume").json()["status"] == "active"
    assert client.get("/portfolio/monitoring/status").status_code == 200
    maintenance = client.post(
        "/admin/portfolio/monitoring/maintenance",
        json={"force": True},
    )
    assert maintenance.status_code == 200
    assert maintenance.json()["status"] == "completed"
    assert client.post(f"/portfolio/monitors/{item['profile_id']}/close").json()["status"] == "closed"
    reopened = client.post(
        f"/portfolio/monitors/{item['profile_id']}/reopen",
        json={"delivery_target_id": target.json()["target_id"]},
    )
    assert reopened.status_code == 202
    assert reopened.json()["items"][0]["status"] == "ready"
    reopened_profile = client.get(f"/portfolio/monitors/{item['profile_id']}").json()
    assert reopened_profile["status"] == "pending_review"
    assert reopened_profile["active_plan_version"] is None
    assert reopened_profile["closed_at"] is None
    assert client.post(
        f"/portfolio/monitors/{item['profile_id']}/reopen",
        json={"delivery_target_id": target.json()["target_id"]},
    ).status_code == 409


def test_fresh_monitor_planning_does_not_block_read_only_api_routes(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    monkeypatch.setattr(api_server, "_portfolio_monitoring_service", service)
    monkeypatch.setattr(api_server, "_portfolio_monitoring_runtime", FakeRuntime())
    writer = TestClient(api_server.app, client=("127.0.0.1", 50001))
    reader = TestClient(api_server.app, client=("127.0.0.1", 50002))
    target = service.store.bind_target(channel="feishu", chat_id="ou_nonblocking")
    refresh_started = threading.Event()
    refresh_release = threading.Event()

    def slow_refresh(**kwargs):
        refresh_started.set()
        assert refresh_release.wait(timeout=3)
        return {"status": "completed"}

    service.planner.market_service.refresh_sync = slow_refresh
    with ThreadPoolExecutor(max_workers=2) as pool:
        planning = pool.submit(
            writer.post,
            "/portfolio/monitor-draft-batches",
            json={
                "symbols": ["600036.SH"],
                "delivery_target_id": target["target_id"],
                "force_fresh": True,
            },
        )
        assert refresh_started.wait(timeout=1)
        reading = pool.submit(reader.get, "/portfolio/monitors")
        try:
            response = reading.result(timeout=0.75)
        except FutureTimeoutError:
            response = None
        finally:
            refresh_release.set()

        assert planning.result(timeout=3).status_code == 202
        assert response is not None
        assert response.status_code == 200


def test_feishu_binding_code_is_one_time_hashed_and_pollable(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    attempt = service.store.create_binding_code()
    assert attempt["status"] == "pending"
    assert attempt["command"] == f"绑定监控 {attempt['code']}"

    with service.store.connect() as connection:
        row = connection.execute(
            "SELECT * FROM monitor_delivery_binding_codes WHERE binding_id=?",
            (attempt["binding_id"],),
        ).fetchone()
    assert row is not None
    assert attempt["code"].replace("-", "") not in str(dict(row))

    target = service.store.claim_binding_code(
        code=attempt["code"],
        channel="feishu",
        chat_id="oc_monitor_group",
        chat_type="group",
        sender_id="ou_owner",
        session_key="feishu:oc_monitor_group",
    )
    assert target["chat_id"] == "oc_monitor_group"
    assert target["chat_type"] == "group"
    polled = service.store.get_binding_code(attempt["binding_id"])
    assert polled is not None
    assert polled["status"] == "claimed"
    assert polled["target"] == target

    try:
        service.store.claim_binding_code(
            code=attempt["code"],
            channel="feishu",
            chat_id="ou_second",
            chat_type="p2p",
            sender_id="ou_second",
        )
    except ValueError as exc:
        assert "invalid or expired" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("one-time binding code was accepted twice")

    expired = service.store.create_binding_code()
    with service.store.connect() as connection:
        connection.execute(
            """UPDATE monitor_delivery_binding_codes
               SET expires_at='2020-01-01T00:00:00+00:00' WHERE binding_id=?""",
            (expired["binding_id"],),
        )
    try:
        service.store.claim_binding_code(
            code=expired["code"],
            channel="feishu",
            chat_id="ou_expired",
            chat_type="p2p",
            sender_id="ou_expired",
        )
    except ValueError as exc:
        assert "invalid or expired" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("expired binding code was accepted")


def test_monitoring_binding_code_api_claim_flow(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    monkeypatch.setattr(api_server, "_portfolio_monitoring_service", service)
    monkeypatch.setattr(api_server, "_portfolio_monitoring_runtime", FakeRuntime())
    client = TestClient(api_server.app, client=("127.0.0.1", 50000))

    created = client.post("/portfolio/monitor-delivery-targets/binding-codes")
    assert created.status_code == 201
    attempt = created.json()
    pending = client.get(
        f"/portfolio/monitor-delivery-targets/binding-codes/{attempt['binding_id']}"
    )
    assert pending.status_code == 200
    assert pending.json()["status"] == "pending"
    assert "code" not in pending.json()

    service.store.claim_binding_code(
        code=attempt["code"],
        channel="feishu",
        chat_id="ou_bound",
        chat_type="p2p",
        sender_id="ou_bound",
        session_key="feishu:ou_bound",
    )
    claimed = client.get(
        f"/portfolio/monitor-delivery-targets/binding-codes/{attempt['binding_id']}"
    )
    assert claimed.json()["status"] == "claimed"
    assert claimed.json()["target"]["chat_id"] == "ou_bound"


def test_runtime_is_default_off(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("VIBE_TRADING_MONITORING_ENABLED", raising=False)
    monkeypatch.delenv("VIBE_TRADING_MONITORING_MODE", raising=False)
    runtime = MonitoringRuntime(
        store=MonitoringStore(tmp_path / "runtime.sqlite3"),
        market_service=FakeMarketService(),
    )
    assert runtime.enabled() is False
    assert runtime.status()["running"] is False


def test_reanalysis_preserves_active_plan_and_holding_removal_stays_paused(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    target = service.store.bind_target(channel="feishu", chat_id="ou_test")
    first = service.create_draft_batch(["600036.SH"], target["target_id"])["items"][0]
    active = service.store.activate(first["profile_id"], first["plan_version"], max_active=10)
    assert active["active_plan_version"] == 1

    second = service.reanalyze(active["profile_id"])["items"][0]
    assert second["plan_version"] == 2
    profile = service.store.get_profile(active["profile_id"])
    assert profile is not None
    assert profile["status"] == "active"
    assert profile["active_plan_version"] == 1
    assert profile["plans"][0]["status"] == "pending_review"
    assert service.planner.market_service.refresh_calls == 1
    assert service.planner.market_service.last_refresh_kwargs["force"] is True
    assert service.planner.market_service.last_refresh_kwargs["read_only"] is True

    service.store.maintain_profiles({})
    removed = service.store.get_profile(active["profile_id"])
    assert removed is not None
    assert removed["status"] == "paused"
    assert removed["pause_reason"] == "holding_removed"
    service.store.maintain_profiles({"600036.SH": "new-holding-hash"})
    assert service.store.get_profile(active["profile_id"])["status"] == "paused"


def test_closed_monitor_reopen_rechecks_data_gate_before_creating_a_draft(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    target = service.store.bind_target(channel="feishu", chat_id="ou_test")
    market = service.planner.market_service
    market.store.current_quote.update(status="single_source", sources=["yahoo"])

    blocked = service.create_draft_batch(["600036.SH"], target["target_id"])["items"][0]
    assert blocked["status"] == "blocked"
    assert blocked["blocked_reasons"] == ["quote_not_actionable:single_source"]
    profile_id = blocked["profile_id"]
    service.store.transition(profile_id, "close")

    still_blocked = service.reopen(profile_id, target["target_id"])["items"][0]
    assert still_blocked["status"] == "blocked"
    profile = service.store.get_profile(profile_id)
    assert profile is not None
    assert profile["status"] == "drafting"
    assert profile["closed_at"] is None
    assert profile["blocked_reasons"] == ["quote_not_actionable:single_source"]
    assert profile["plans"] == []

    service.store.transition(profile_id, "close")
    market.store.current_quote.update(
        status="verified",
        sources=["nasdaq", "yahoo"],
        verified_at=datetime.now(timezone.utc).isoformat(),
    )
    ready = service.reopen(profile_id, target["target_id"])["items"][0]
    assert ready["status"] == "ready"
    assert ready["profile_id"] == profile_id
    reopened = service.store.get_profile(profile_id)
    assert reopened is not None
    assert reopened["status"] == "pending_review"
    assert reopened["active_plan_version"] is None
    assert reopened["blocked_reasons"] == []
    assert reopened["delivery_target_id"] == target["target_id"]
    assert reopened["plans"][0]["status"] == "pending_review"


def test_activation_requires_an_active_delivery_target(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    item = service.create_draft_batch(["600036.SH"])["items"][0]
    try:
        service.store.activate(item["profile_id"], item["plan_version"], max_active=10)
    except ValueError as exc:
        assert "Feishu delivery target" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("activation unexpectedly succeeded without a delivery target")


def _activate(service: MonitoringService) -> dict:
    target = service.store.bind_target(channel="feishu", chat_id="ou_test")
    item = service.create_draft_batch(["600036.SH"], target["target_id"])["items"][0]
    return service.store.activate(item["profile_id"], item["plan_version"], max_active=10)


def _activate_single_cross_above(
    service: MonitoringService,
    *,
    alert_cue: str = "none",
) -> tuple[dict, dict]:
    target = service.store.bind_target(channel="feishu", chat_id="ou_single_cross")
    item = service.create_draft_batch(["600036.SH"], target["target_id"])["items"][0]
    profile = service.store.get_profile(item["profile_id"])
    assert profile is not None
    plan = copy.deepcopy(profile["plans"][0]["plan"])
    selected = next(
        rule for rule in plan["market_rules"] if rule["kind"] == "price_cross_above"
    )
    for rule in plan["market_rules"]:
        rule["enabled"] = rule["client_rule_id"] == selected["client_rule_id"]
        rule["alert_cue"] = alert_cue if rule["enabled"] else "none"
    service.store.update_draft(
        item["profile_id"],
        item["plan_version"],
        plan,
        expected_revision=profile["profile_revision"],
    )
    activated = service.store.activate(
        item["profile_id"], item["plan_version"], max_active=10
    )
    return activated, selected


def _closed_quote(price: float, bar_time: str, *, source_suffix: str = "") -> dict:
    return {
        "last_price": price,
        "interval": "5m",
        "bar_time": bar_time,
        "status": "verified",
        "sources": ["tencent", f"mootdx{source_suffix}"],
    }


def test_strict_crossing_suppresses_true_baseline_and_rearms_after_hysteresis(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    profile, selected = _activate_single_cross_above(service, alert_cue="ymca_v1")
    profile_id = profile["profile_id"]
    threshold = float(selected["parameters"]["threshold"])
    hysteresis = float(selected["parameters"]["clear_hysteresis_bps"]) / 10000
    above = threshold + 1
    deep_clear = threshold * (1 - hysteresis) - 0.01
    shallow_clear = threshold * (1 - hysteresis / 2)

    assert service.store.latest_event_cursor() is None
    assert service.store.list_events_from_start() == []
    for bar_time in (
        "2026-07-14T02:05:00+00:00",
        "2026-07-14T02:10:00+00:00",
        "2026-07-14T02:15:00+00:00",
    ):
        assert service.store.evaluate_quote(
            profile_id, _closed_quote(above, bar_time)
        ) == []
    with service.store.connect() as connection:
        state = connection.execute(
            """SELECT state,confirmation_progress FROM monitor_rules
               WHERE profile_id=? AND client_rule_id=?""",
            (profile_id, selected["client_rule_id"]),
        ).fetchone()
    assert tuple(state) == ("suppressed", 0)

    assert service.store.evaluate_quote(
        profile_id, _closed_quote(deep_clear, "2026-07-14T02:20:00+00:00")
    ) == []
    assert service.store.evaluate_quote(
        profile_id, _closed_quote(above, "2026-07-14T02:25:00+00:00")
    ) == []
    first = service.store.evaluate_quote(
        profile_id, _closed_quote(above, "2026-07-14T02:30:00+00:00")
    )
    assert len(first) == 1
    facts = first[0]["facts"]
    assert facts["client_rule_id"] == selected["client_rule_id"]
    assert facts["direction"] == "above"
    assert facts["threshold"] == pytest.approx(threshold)
    assert facts["target_intent"] == selected["target_intent"]
    assert facts["target_level"] == selected["target_level"]
    assert facts["confirmation_count"] == 2
    assert facts["alert_cue"] == "ymca_v1"

    # Merely dropping below the threshold is insufficient after an event; the
    # configured hysteresis boundary must clear before another crossing arms.
    assert service.store.evaluate_quote(
        profile_id, _closed_quote(shallow_clear, "2026-07-14T02:35:00+00:00")
    ) == []
    assert service.store.evaluate_quote(
        profile_id, _closed_quote(above, "2026-07-14T02:40:00+00:00")
    ) == []
    assert service.store.evaluate_quote(
        profile_id, _closed_quote(deep_clear, "2026-07-14T02:45:00+00:00")
    ) == []
    assert service.store.evaluate_quote(
        profile_id, _closed_quote(above, "2026-07-14T02:50:00+00:00")
    ) == []
    second = service.store.evaluate_quote(
        profile_id, _closed_quote(above, "2026-07-14T02:55:00+00:00")
    )
    assert len(second) == 1

    # Cursor order follows durable insertion order even if stored wall-clock
    # timestamps move backwards because of clock skew or a restored fixture.
    with service.store.connect() as connection:
        connection.execute(
            "UPDATE monitor_events SET first_seen_at='2030-01-01T00:00:00+00:00' WHERE event_id=?",
            (first[0]["event_id"],),
        )
        connection.execute(
            "UPDATE monitor_events SET first_seen_at='2020-01-01T00:00:00+00:00' WHERE event_id=?",
            (second[0]["event_id"],),
        )
    assert service.store.latest_event_cursor() == second[0]["event_id"]
    assert [event["event_id"] for event in service.store.list_events_from_start()] == [
        first[0]["event_id"],
        second[0]["event_id"],
    ]
    assert [event["event_id"] for event in service.store.list_events_after(first[0]["event_id"])] == [
        second[0]["event_id"]
    ]
    assert service.store.list_events_after(second[0]["event_id"]) == []
    with pytest.raises(KeyError, match="unknown-cursor"):
        service.store.list_events_after("unknown-cursor")


def test_duplicate_bar_does_not_confirm_and_data_gap_cancels_crossing_candidate(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    profile, selected = _activate_single_cross_above(service)
    profile_id = profile["profile_id"]
    threshold = float(selected["parameters"]["threshold"])

    assert service.store.evaluate_quote(
        profile_id,
        _closed_quote(threshold - 1, "2026-07-14T02:05:00+00:00"),
    ) == []
    assert service.store.evaluate_quote(
        profile_id,
        _closed_quote(threshold + 1, "2026-07-14T02:10:00+00:00"),
    ) == []
    assert service.store.evaluate_quote(
        profile_id,
        _closed_quote(
            threshold + 1,
            "2026-07-14T02:10:00+00:00",
            source_suffix="-duplicate",
        ),
    ) == []
    with service.store.connect() as connection:
        candidate = connection.execute(
            """SELECT state,confirmation_progress FROM monitor_rules
               WHERE profile_id=? AND client_rule_id=?""",
            (profile_id, selected["client_rule_id"]),
        ).fetchone()
    assert tuple(candidate) == ("candidate", 1)

    assert service.store.evaluate_quote(
        profile_id,
        _closed_quote(threshold + 1, "2026-07-14T02:20:00+00:00"),
    ) == []
    with service.store.connect() as connection:
        cancelled = connection.execute(
            """SELECT state,confirmation_progress FROM monitor_rules
               WHERE profile_id=? AND client_rule_id=?""",
            (profile_id, selected["client_rule_id"]),
        ).fetchone()
    assert tuple(cancelled) == ("suppressed", 0)
    assert service.store.list_events() == []


def test_crossing_start_requires_an_exactly_adjacent_closed_bar(
    tmp_path,
    monkeypatch,
) -> None:
    continuous = MonitoringStore._bars_are_continuous
    assert continuous(
        "2026-07-14T02:05:00+00:00",
        "2026-07-14T02:10:00+00:00",
        "5m",
    ) is True
    assert continuous(
        "2026-07-14T02:05:00+00:00",
        "2026-07-14T02:10:01+00:00",
        "5m",
    ) is True
    assert continuous(
        "2026-07-14T02:05:00+00:00",
        "2026-07-14T02:09:00+00:00",
        "5m",
    ) is False
    assert continuous(
        "2026-07-14T02:05:00+00:00",
        "2026-07-14T02:12:00+00:00",
        "5m",
    ) is False
    assert continuous(
        "2026-07-14T02:05:00+00:00",
        "2026-07-14T02:10:01.000001+00:00",
        "5m",
    ) is False
    assert continuous(
        "2026-07-14T02:05:00+00:00",
        "2026-07-14T02:06:00+00:00",
        "1m",
    ) is True

    service = _service(tmp_path, monkeypatch)
    profile, selected = _activate_single_cross_above(service)
    profile_id = profile["profile_id"]
    threshold = float(selected["parameters"]["threshold"])
    assert service.store.evaluate_quote(
        profile_id,
        _closed_quote(threshold - 1, "2026-07-14T02:05:00+00:00"),
    ) == []
    assert service.store.evaluate_quote(
        profile_id,
        _closed_quote(threshold + 1, "2026-07-14T02:15:00+00:00"),
    ) == []
    with service.store.connect() as connection:
        state = connection.execute(
            """SELECT state,confirmation_progress FROM monitor_rules
               WHERE profile_id=? AND client_rule_id=?""",
            (profile_id, selected["client_rule_id"]),
        ).fetchone()
    assert tuple(state) == ("suppressed", 0)
    assert service.store.list_events() == []


def test_pause_preserves_triggered_epoch_until_deep_clear(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    profile, selected = _activate_single_cross_above(service)
    profile_id = profile["profile_id"]
    threshold = float(selected["parameters"]["threshold"])
    hysteresis = float(selected["parameters"]["clear_hysteresis_bps"]) / 10000
    above = threshold + 1
    shallow_clear = threshold * (1 - hysteresis / 2)
    deep_clear = threshold * (1 - hysteresis) - 0.01

    service.store.evaluate_quote(
        profile_id, _closed_quote(threshold - 1, "2026-07-14T02:05:00+00:00")
    )
    service.store.evaluate_quote(
        profile_id, _closed_quote(above, "2026-07-14T02:10:00+00:00")
    )
    first = service.store.evaluate_quote(
        profile_id, _closed_quote(above, "2026-07-14T02:15:00+00:00")
    )
    assert len(first) == 1
    assert first[0]["armed_epoch"] == 1

    service.store.transition(profile_id, "pause", reason="test")
    service.store.transition(profile_id, "resume")
    assert service.store.evaluate_quote(
        profile_id, _closed_quote(above, "2026-07-14T02:20:00+00:00")
    ) == []
    assert service.store.evaluate_quote(
        profile_id, _closed_quote(shallow_clear, "2026-07-14T02:25:00+00:00")
    ) == []
    with service.store.connect() as connection:
        before_clear = connection.execute(
            """SELECT state,armed_epoch FROM monitor_rules
               WHERE profile_id=? AND client_rule_id=?""",
            (profile_id, selected["client_rule_id"]),
        ).fetchone()
    assert tuple(before_clear) == ("cooldown", 1)

    service.store.evaluate_quote(
        profile_id, _closed_quote(deep_clear, "2026-07-14T02:30:00+00:00")
    )
    service.store.evaluate_quote(
        profile_id, _closed_quote(above, "2026-07-14T02:35:00+00:00")
    )
    second = service.store.evaluate_quote(
        profile_id, _closed_quote(above, "2026-07-14T02:40:00+00:00")
    )
    assert len(second) == 1
    assert second[0]["armed_epoch"] == 2
    assert [event["armed_epoch"] for event in service.store.list_events_from_start()] == [1, 2]


def test_timed_resume_cancels_candidate_and_does_not_backfill_paused_crossing(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    profile, selected = _activate_single_cross_above(service)
    profile_id = profile["profile_id"]
    threshold = float(selected["parameters"]["threshold"])
    service.store.evaluate_quote(
        profile_id, _closed_quote(threshold - 1, "2026-07-14T02:05:00+00:00")
    )
    service.store.evaluate_quote(
        profile_id, _closed_quote(threshold + 1, "2026-07-14T02:10:00+00:00")
    )

    service.store.transition(
        profile_id,
        "pause",
        resume_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        reason="timed-test",
    )
    with service.store.connect() as connection:
        paused_rule = connection.execute(
            """SELECT state,confirmation_progress,last_condition_value,last_bar_time
               FROM monitor_rules WHERE profile_id=? AND client_rule_id=?""",
            (profile_id, selected["client_rule_id"]),
        ).fetchone()
    assert tuple(paused_rule) == ("armed", 0, None, None)

    paused_profile = service.store.get_profile(profile_id)
    assert paused_profile is not None
    service.store.maintain_profiles(
        {"600036.SH": str(paused_profile["input_snapshot_hash"])}
    )
    assert service.store.get_profile(profile_id)["status"] == "active"
    assert service.store.evaluate_quote(
        profile_id, _closed_quote(threshold + 1, "2026-07-14T02:15:00+00:00")
    ) == []
    with service.store.connect() as connection:
        resumed_rule = connection.execute(
            """SELECT state,confirmation_progress,armed_epoch FROM monitor_rules
               WHERE profile_id=? AND client_rule_id=?""",
            (profile_id, selected["client_rule_id"]),
        ).fetchone()
    assert tuple(resumed_rule) == ("suppressed", 0, 1)
    assert service.store.list_events() == []


def test_data_blocked_recovery_cannot_reuse_an_emitted_epoch(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    profile, selected = _activate_single_cross_above(service)
    profile_id = profile["profile_id"]
    threshold = float(selected["parameters"]["threshold"])
    hysteresis = float(selected["parameters"]["clear_hysteresis_bps"]) / 10000
    above = threshold + 1
    shallow_clear = threshold * (1 - hysteresis / 2)
    deep_clear = threshold * (1 - hysteresis) - 0.01

    service.store.evaluate_quote(
        profile_id, _closed_quote(threshold - 1, "2026-07-14T02:05:00+00:00")
    )
    service.store.evaluate_quote(
        profile_id, _closed_quote(above, "2026-07-14T02:10:00+00:00")
    )
    assert len(service.store.evaluate_quote(
        profile_id, _closed_quote(above, "2026-07-14T02:15:00+00:00")
    )) == 1

    unavailable = _closed_quote(above, "2026-07-14T02:20:00+00:00")
    unavailable["last_price"] = None
    assert service.store.evaluate_quote(profile_id, unavailable) == []
    assert service.store.evaluate_quote(
        profile_id, _closed_quote(shallow_clear, "2026-07-14T02:25:00+00:00")
    ) == []
    with service.store.connect() as connection:
        recovered = connection.execute(
            """SELECT state,armed_epoch FROM monitor_rules
               WHERE profile_id=? AND client_rule_id=?""",
            (profile_id, selected["client_rule_id"]),
        ).fetchone()
    assert tuple(recovered) == ("suppressed", 1)
    assert service.store.evaluate_quote(
        profile_id, _closed_quote(above, "2026-07-14T02:30:00+00:00")
    ) == []

    service.store.evaluate_quote(
        profile_id, _closed_quote(deep_clear, "2026-07-14T02:35:00+00:00")
    )
    service.store.evaluate_quote(
        profile_id, _closed_quote(above, "2026-07-14T02:40:00+00:00")
    )
    second = service.store.evaluate_quote(
        profile_id, _closed_quote(above, "2026-07-14T02:45:00+00:00")
    )
    assert len(second) == 1
    assert second[0]["armed_epoch"] == 2
    assert [event["armed_epoch"] for event in service.store.list_events_from_start()] == [1, 2]


def test_event_rule_and_outbox_confirmation_is_atomic_on_outbox_failure(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    profile, selected = _activate_single_cross_above(service)
    profile_id = profile["profile_id"]
    threshold = float(selected["parameters"]["threshold"])
    service.store.evaluate_quote(
        profile_id, _closed_quote(threshold - 1, "2026-07-14T02:05:00+00:00")
    )
    service.store.evaluate_quote(
        profile_id, _closed_quote(threshold + 1, "2026-07-14T02:10:00+00:00")
    )
    with service.store.connect() as connection:
        before = connection.execute(
            """SELECT state,confirmation_progress,last_bar_time,armed_epoch
               FROM monitor_rules WHERE profile_id=? AND client_rule_id=?""",
            (profile_id, selected["client_rule_id"]),
        ).fetchone()
        connection.execute(
            """CREATE TRIGGER fail_monitor_outbox_insert
               BEFORE INSERT ON delivery_outbox
               BEGIN
                   SELECT RAISE(ABORT, 'forced outbox failure');
               END"""
        )
    assert tuple(before) == (
        "candidate",
        1,
        "2026-07-14T02:10:00+00:00",
        1,
    )

    with pytest.raises(sqlite3.DatabaseError, match="forced outbox failure"):
        service.store.evaluate_quote(
            profile_id,
            _closed_quote(threshold + 1, "2026-07-14T02:15:00+00:00"),
        )
    with service.store.connect() as connection:
        after = connection.execute(
            """SELECT state,confirmation_progress,last_bar_time,armed_epoch
               FROM monitor_rules WHERE profile_id=? AND client_rule_id=?""",
            (profile_id, selected["client_rule_id"]),
        ).fetchone()
        event_count = connection.execute("SELECT COUNT(*) FROM monitor_events").fetchone()[0]
        outbox_count = connection.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0]
        failed_observation_count = connection.execute(
            """SELECT COUNT(*) FROM monitor_observations
               WHERE profile_id=? AND data_as_of='2026-07-14T02:15:00+00:00'""",
            (profile_id,),
        ).fetchone()[0]
    assert tuple(after) == tuple(before)
    assert event_count == 0
    assert outbox_count == 0
    assert failed_observation_count == 0

    with service.store.connect() as connection:
        connection.execute("DROP TRIGGER fail_monitor_outbox_insert")
    retried = service.store.evaluate_quote(
        profile_id,
        _closed_quote(threshold + 1, "2026-07-14T02:15:00+00:00"),
    )
    assert len(retried) == 1
    assert len(retried[0]["deliveries"]) == 1


def test_monitoring_mode_fails_safe_and_force_cannot_bypass_kill_switch(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("VIBE_TRADING_MONITORING_ENABLED", raising=False)
    runtime = MonitoringRuntime(
        store=MonitoringStore(tmp_path / "runtime.sqlite3"),
        market_service=FakeMarketService(),
    )
    asyncio.run(runtime.start(force=True))
    assert runtime.status()["mode"] == "off"
    assert runtime.status()["running"] is False

    monkeypatch.setenv("VIBE_TRADING_MONITORING_ENABLED", "1")
    monkeypatch.setenv("VIBE_TRADING_MONITORING_MODE", "unexpected")
    assert runtime.status()["mode"] == "shadow"
    assert runtime.status()["mode_valid"] is False
    assert runtime.status()["mode_reason"] == "invalid_mode_fell_back_to_shadow"


def test_shadow_replay_persists_would_deliver_without_outbound_call(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    profile = _activate(service)
    plan = service.store.get_plan(profile["profile_id"], int(profile["active_plan_version"]))
    threshold = next(
        rule["parameters"]["threshold"]
        for rule in plan["plan"]["market_rules"]
        if rule["kind"] == "price_cross_above"
    )
    result = replay_quotes(
        service.store,
        profile["profile_id"],
        [
            {
                "last_price": threshold - 1,
                "interval": "5m",
                "bar_time": "2026-07-14T02:05:00+00:00",
                "status": "verified",
                "sources": ["tencent", "mootdx"],
            },
            {
                "last_price": threshold + 1,
                "interval": "5m",
                "bar_time": "2026-07-14T02:10:00+00:00",
                "status": "verified",
                "sources": ["tencent", "mootdx"],
            },
            {
                "last_price": threshold + 1,
                "interval": "5m",
                "bar_time": "2026-07-14T02:15:00+00:00",
                "status": "verified",
                "sources": ["tencent", "mootdx"],
            },
        ],
        delivery_mode="shadow",
        duplicate_indexes={2},
        reopen_before_indexes={2},
    )
    assert result["events_created"] == 1
    assert result["pending_deliveries"] == 0
    assert result["shadow_suppressed_deliveries"] == 1
    assert result["runtime_counters"]["duplicate_observation_count"]["value"] == 1
    event = service.store.get_event(result["event_ids"][0])
    assert event is not None
    assert event["deliveries"][0]["status"] == "shadow_suppressed"
    assert event["deliveries"][0]["would_deliver"] is True
    assert event["deliveries"][0]["suppression_reason"] == "shadow_mode"

    calls = 0

    async def deliver(_event, _delivery):
        nonlocal calls
        calls += 1
        return "message-id"

    runtime = MonitoringRuntime(
        store=service.store,
        market_service=FakeMarketService(),
        delivery_callback=deliver,
    )
    assert asyncio.run(runtime._deliver_pending("shadow")) == 0
    assert calls == 0


def test_global_kill_switch_suppresses_already_pending_delivery(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    profile = _activate(service)
    plan = service.store.get_plan(profile["profile_id"], int(profile["active_plan_version"]))
    threshold = next(
        rule["parameters"]["threshold"]
        for rule in plan["plan"]["market_rules"]
        if rule["kind"] == "price_cross_above"
    )
    replay_quotes(
        service.store,
        profile["profile_id"],
        [
            {
                "last_price": threshold - 1,
                "interval": "5m",
                "bar_time": "2026-07-14T02:05:00+00:00",
                "status": "verified",
                "sources": ["tencent", "mootdx"],
            },
            {
                "last_price": threshold + 1,
                "interval": "5m",
                "bar_time": "2026-07-14T02:10:00+00:00",
                "status": "verified",
                "sources": ["tencent", "mootdx"],
            },
            {
                "last_price": threshold + 1,
                "interval": "5m",
                "bar_time": "2026-07-14T02:15:00+00:00",
                "status": "verified",
                "sources": ["tencent", "mootdx"],
            },
        ],
        delivery_mode="deliver",
    )
    assert len(service.store.pending_deliveries()) == 1
    calls = 0

    async def deliver(_event, _delivery):
        nonlocal calls
        calls += 1
        return "message-id"

    monkeypatch.setenv("VIBE_TRADING_MONITORING_ENABLED", "0")
    runtime = MonitoringRuntime(
        store=service.store,
        market_service=FakeMarketService(),
        delivery_callback=deliver,
    )
    assert asyncio.run(runtime._deliver_pending("deliver")) == 1
    assert calls == 0
    event = service.store.list_events()[0]
    delivery = service.store.get_event(event["event_id"])["deliveries"][0]
    assert delivery["status"] == "shadow_suppressed"
    assert delivery["suppression_reason"] == "global_kill_switch"


@pytest.mark.parametrize("price_volume_mode", ["off", "shadow"])
def test_price_volume_kill_switch_suppresses_only_legacy_price_volume_outbox(
    tmp_path,
    monkeypatch,
    price_volume_mode,
) -> None:
    service = _service(tmp_path, monkeypatch)
    profile = _activate(service)
    plan = service.store.get_plan(profile["profile_id"], int(profile["active_plan_version"]))
    assert plan is not None
    add_threshold = next(
        float(rule["parameters"]["threshold"])
        for rule in plan["plan"]["market_rules"]
        if rule["target_intent"] == "add_position"
    )
    take_profit_threshold = next(
        float(rule["parameters"]["threshold"])
        for rule in plan["plan"]["market_rules"]
        if rule["target_intent"] == "take_profit" and rule["target_level"] == 1
    )
    price_volume_events = service.store.evaluate_quote(
        profile["profile_id"],
        {
            "last_price": add_threshold * 1.005,
            "interval": "5m",
            "bar_time": "2026-07-14T02:05:00+00:00",
            "price_volume_bar_time": "2026-07-14T02:05:00+00:00",
            "status": "verified",
            "sources": ["tencent", "mootdx"],
            "price_volume": _ready_price_volume(accelerated=True),
        },
        delivery_mode="deliver",
        price_volume_mode="deliver",
    )
    assert {event["kind"] for event in price_volume_events} == {
        "price_volume_accelerated_decline",
        "target_proximity",
    }
    for index in range(2):
        service.store.evaluate_quote(
            profile["profile_id"],
            {
                "last_price": take_profit_threshold + 0.1 + index * 0.1,
                "interval": "5m",
                "bar_time": f"2026-07-14T02:{10 + index * 5:02d}:00+00:00",
                "status": "verified",
                "sources": ["tencent", "mootdx"],
            },
            delivery_mode="deliver",
            price_volume_mode="off",
        )
    pending = service.store.pending_deliveries()
    assert len(pending) == 3

    delivered_kinds: list[str] = []

    async def deliver(event, _delivery):
        delivered_kinds.append(str(event["kind"]))
        return "message-id"

    monkeypatch.setenv("VIBE_TRADING_MONITORING_ENABLED", "1")
    monkeypatch.setenv("VIBE_TRADING_MONITORING_MODE", "deliver")
    monkeypatch.setenv("VIBE_TRADING_MONITOR_SOAK_APPROVED", "1")
    monkeypatch.setenv(
        "VIBE_TRADING_MONITOR_DELIVER_ALLOWLIST",
        str(profile["profile_id"]),
    )
    monkeypatch.setenv(
        "VIBE_TRADING_MONITOR_DELIVER_TEST_TARGET_ID",
        str(profile["delivery_target_id"]),
    )
    monkeypatch.setenv(
        "VIBE_TRADING_MONITOR_PRICE_VOLUME_MODE",
        price_volume_mode,
    )
    runtime = MonitoringRuntime(
        store=service.store,
        market_service=FakeMarketService(),
        delivery_callback=deliver,
    )
    assert asyncio.run(runtime._deliver_pending("deliver")) == 2
    assert delivered_kinds == ["market_rule_trigger"]
    for event in price_volume_events:
        delivery = service.store.get_event(event["event_id"])["deliveries"][0]
        assert delivery["status"] == "shadow_suppressed"
        assert delivery["suppression_reason"] == f"price_volume_mode_{price_volume_mode}"
    price_event = next(
        event for event in service.store.list_events(limit=20)
        if event["kind"] == "market_rule_trigger"
    )
    assert service.store.get_event(price_event["event_id"])["deliveries"][0]["status"] == "delivered"


class _Calendar:
    def __init__(self, trading_day: bool, mode: str) -> None:
        self.trading_day = trading_day
        self.mode = mode

    def is_trading_day(self, _value) -> bool:
        return self.trading_day


class _RuntimeMarketStore:
    @staticmethod
    def query_bars(**kwargs):
        if kwargs["interval"] == "1D":
            return [{
                "bar_time": "2026-07-13T07:00:00+00:00",
                "session_date": "2026-07-13",
                "close": 39.0,
                "status": "verified",
                "sources": ["tencent", "mootdx"],
            }]
        return [{
            "bar_time": "2026-07-14T01:55:00+00:00",
            "session_date": "2026-07-14",
            "open": 39.8,
            "high": 40.2,
            "low": 39.7,
            "close": 40.0,
            "volume": 1000,
            "status": "verified",
            "sources": ["tencent", "mootdx"],
            "verified_at": "2026-07-14T02:00:00+00:00",
        }]


class _RuntimeMarketService(FakeMarketService):
    def __init__(self) -> None:
        super().__init__()
        self.store = _RuntimeMarketStore()


class _SingleSourceRuntimeMarketStore(_RuntimeMarketStore):
    @staticmethod
    def query_bars(**kwargs):
        return [
            {**row, "status": "single_source", "sources": ["tencent"]}
            for row in _RuntimeMarketStore.query_bars(**kwargs)
        ]


class _SingleSourceRuntimeMarketService(FakeMarketService):
    def __init__(self) -> None:
        super().__init__()
        self.store = _SingleSourceRuntimeMarketStore()


def test_runtime_creates_one_durable_0900_preopen_notice_per_target(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    profile = _activate(service)
    monkeypatch.setenv("VIBE_TRADING_MONITORING_ENABLED", "1")
    monkeypatch.setenv("VIBE_TRADING_MONITORING_MODE", "shadow")
    monkeypatch.setenv("VIBE_TRADING_MONITOR_MAINTENANCE_ENABLED", "0")
    current_time = [
        datetime(2026, 7, 14, 8, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
    ]
    runtime = MonitoringRuntime(
        store=service.store,
        market_service=_RuntimeMarketService(),
        calendar=_Calendar(True, "cached_exchange_calendar"),
        now_factory=lambda: current_time[0],
    )

    before_nine = asyncio.run(runtime.run_once())
    assert before_nine["events_created"] == 0

    current_time[0] = current_time[0].replace(hour=9, minute=0)
    at_nine = asyncio.run(runtime.run_once())
    assert at_nine["decision"] == "calendar_closed"
    assert at_nine["events_created"] == 1
    notice = next(
        event for event in service.store.list_events(limit=10)
        if event["kind"] == "monitoring_preopen_notice"
    )
    assert notice["facts"]["symbols"] == [profile["symbol"]]
    assert notice["facts"]["active_profile_count"] == 1
    assert notice["facts"]["first_check_at"].endswith("T09:35:00+08:00")
    delivery = service.store.get_event(notice["event_id"])["deliveries"][0]
    assert delivery["status"] == "shadow_suppressed"
    assert delivery["would_deliver"] is True

    current_time[0] = current_time[0].replace(minute=5)
    repeated = asyncio.run(runtime.run_once())
    assert repeated["events_created"] == 0
    assert sum(
        event["kind"] == "monitoring_preopen_notice"
        for event in service.store.list_events(limit=10)
    ) == 1
    assert MonitoringRuntime._session_name(
        current_time[0].replace(hour=9, minute=34)
    ) == "preopen"
    assert MonitoringRuntime._session_name(
        current_time[0].replace(hour=9, minute=35)
    ) == "morning"
    asyncio.run(runtime.stop())


def test_runtime_uses_fail_closed_calendar_and_preserves_portfolio_state(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    _activate(service)
    monkeypatch.setenv("VIBE_TRADING_MONITORING_ENABLED", "1")
    monkeypatch.setenv("VIBE_TRADING_MONITORING_MODE", "shadow")
    monkeypatch.setenv("VIBE_TRADING_MONITOR_MAINTENANCE_ENABLED", "0")
    def now() -> datetime:
        return datetime(2026, 7, 14, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    closed_market = _RuntimeMarketService()
    closed_runtime = MonitoringRuntime(
        store=service.store,
        market_service=closed_market,
        calendar=_Calendar(False, "calendar_unavailable"),
        now_factory=now,
    )
    closed = asyncio.run(closed_runtime.run_once())
    assert closed["decision"] == "calendar_closed"
    assert closed["calendar"]["open"] is False
    assert closed["schedule_lag_ms"] is None
    assert closed["closed_session_due_profiles"] == 1
    assert closed["closed_session_backlog_lag_ms"] is not None
    assert service.store.profile_tick_outcomes(closed["tick_id"])[0]["reason_code"] == "calendar_closed"
    assert closed_market.refresh_calls == 0
    asyncio.run(closed_runtime.stop())

    state_path = Path(str(tmp_path / "portfolio.json"))
    before = state_path.read_bytes()
    open_market = _RuntimeMarketService()
    open_runtime = MonitoringRuntime(
        store=service.store,
        market_service=open_market,
        calendar=_Calendar(True, "cached_exchange_calendar"),
        now_factory=now,
    )
    evaluated = asyncio.run(open_runtime.run_once())
    assert evaluated["decision"] == "evaluated"
    assert evaluated["evaluated_profiles"] == 1
    assert evaluated["all_due_profiles"] == 1
    assert evaluated["outcome_profiles"] == 1
    assert evaluated["supported_blocked_profiles"] == 0
    assert evaluated["outcome_invariant_ok"] is True
    assert open_market.refresh_calls == 1
    last_quote = service.store.list_profiles()[0]["last_quote"]
    assert last_quote["session_open"] == pytest.approx(39.8)
    assert last_quote["session_high"] == pytest.approx(40.2)
    assert last_quote["session_low"] == pytest.approx(39.7)
    assert state_path.read_bytes() == before
    health = service.store.runtime_health()
    assert health["tick_count"] == 2
    assert health["bar_lag_ms"]["max"] == 300000.0


def test_runtime_evaluates_single_source_only_after_plan_consent(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    service.planner.market_service.store.current_quote.update(
        status="single_source",
        sources=["tencent"],
    )
    target = service.store.bind_target(channel="feishu", chat_id="ou_test")
    item = service.create_draft_batch(
        ["600036.SH"],
        target["target_id"],
        allow_single_source=True,
    )["items"][0]
    service.store.activate(item["profile_id"], item["plan_version"], max_active=10)
    monkeypatch.setenv("VIBE_TRADING_MONITORING_ENABLED", "1")
    monkeypatch.setenv("VIBE_TRADING_MONITORING_MODE", "shadow")
    monkeypatch.setenv("VIBE_TRADING_MONITOR_MAINTENANCE_ENABLED", "0")
    runtime = MonitoringRuntime(
        store=service.store,
        market_service=_SingleSourceRuntimeMarketService(),
        calendar=_Calendar(True, "cached_exchange_calendar"),
        now_factory=lambda: datetime(
            2026, 7, 14, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")
        ),
    )

    result = asyncio.run(runtime.run_once())

    assert result["evaluated_profiles"] == 1
    profile = service.store.get_profile(item["profile_id"])
    assert profile is not None
    assert profile["last_quote"]["status"] == "single_source"
    assert profile["last_quote"]["sources"] == ["tencent"]
    asyncio.run(runtime.stop())


def test_runtime_health_ignores_pre_contract_ticks_when_counting_outcome_failures(
    tmp_path,
) -> None:
    store = MonitoringStore(tmp_path / "monitoring.sqlite3")
    legacy = store.record_runtime_tick(
        {
            "tick_id": "legacy-outcome-metrics",
            "owner_id": "legacy",
            "mode": "shadow",
            "decision": "calendar_closed",
            "due_profiles": 2,
            "all_due_profiles": 0,
            "evaluated_profiles": 0,
            "supported_blocked_profiles": 0,
            "outcome_profiles": 0,
            "schedule_lag_ms": 999_999,
        }
    )
    with store.connect() as connection:
        connection.execute(
            "UPDATE monitor_runtime_ticks SET outcome_contract_version=0 WHERE tick_id=?",
            (legacy["tick_id"],),
        )

    legacy_health = store.runtime_health()
    assert legacy_health["outcome_invariant_failure_count"] == 0
    assert legacy_health["schedule_lag_ms"]["p95"] is None

    store.record_runtime_tick(
        {
            "tick_id": "contract-outcome-metrics",
            "owner_id": "current",
            "mode": "shadow",
            "decision": "failed",
            "due_profiles": 1,
            "all_due_profiles": 1,
            "evaluated_profiles": 0,
            "supported_blocked_profiles": 0,
            "outcome_profiles": 0,
            "schedule_lag_ms": 100,
        }
    )
    current_health = store.runtime_health()
    assert current_health["outcome_invariant_failure_count"] == 1
    assert current_health["schedule_lag_ms"]["p95"] == 100.0


def test_profile_reads_backfill_missing_session_metrics_from_verified_cache(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    profile = _activate(service)
    service.store.evaluate_quote(
        profile["profile_id"],
        {
            "last_price": 40.0,
            "interval": "5m",
            "bar_time": "2026-07-14T02:05:00+00:00",
            "session_date": "2026-07-14",
            "status": "verified",
            "sources": ["tencent", "mootdx"],
        },
    )

    def session_bars(**kwargs):
        assert kwargs["symbol"] == "600036.SH"
        assert kwargs["interval"] == "5m"
        assert kwargs["adjustment"] == "raw"
        assert kwargs["view"] == "consensus"
        return [
            {
                "bar_time": "2026-07-14T01:35:00+00:00",
                "session_date": "2026-07-14",
                "open": 39.5,
                "high": 39.8,
                "low": 39.4,
                "close": 39.7,
                "status": "verified",
            },
            {
                "bar_time": "2026-07-14T01:40:00+00:00",
                "session_date": "2026-07-14",
                "open": 39.7,
                "high": 40.1,
                "low": 39.6,
                "close": 39.9,
                "status": "verified",
            },
        ]

    service.planner.market_service.store.query_bars = session_bars
    stored = service.store.get_profile(profile["profile_id"])
    assert stored is not None
    assert stored["last_quote"]["session_open"] is None
    assert stored["last_quote"]["session_high"] is None
    assert stored["last_quote"]["session_low"] is None

    listed = service.list_profiles()[0]
    detailed = service.get_profile(profile["profile_id"])
    assert listed["last_quote"]["session_open"] == pytest.approx(39.5)
    assert listed["last_quote"]["session_high"] == pytest.approx(40.1)
    assert listed["last_quote"]["session_low"] == pytest.approx(39.4)
    assert detailed is not None
    assert detailed["last_quote"]["session_open"] == pytest.approx(39.5)
    assert detailed["last_quote"]["session_high"] == pytest.approx(40.1)
    assert detailed["last_quote"]["session_low"] == pytest.approx(39.4)
    assert service.store.get_profile(profile["profile_id"])["last_quote"]["session_open"] is None


def test_active_check_frequency_schedules_the_next_attempt_one_minute_later(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    target = service.store.bind_target(channel="feishu", chat_id="ou_test")
    item = service.create_draft_batch(["600036.SH"], target["target_id"])["items"][0]
    profile = service.store.get_profile(item["profile_id"])
    assert profile is not None
    plan = copy.deepcopy(profile["plans"][0]["plan"])
    plan["quote_tier"] = "active"
    service.store.update_draft(
        item["profile_id"],
        item["plan_version"],
        plan,
        expected_revision=profile["profile_revision"],
    )
    service.store.activate(item["profile_id"], item["plan_version"], max_active=10)
    monkeypatch.setenv("VIBE_TRADING_MONITORING_ENABLED", "1")
    monkeypatch.setenv("VIBE_TRADING_MONITORING_MODE", "shadow")
    monkeypatch.setenv("VIBE_TRADING_MONITOR_MAINTENANCE_ENABLED", "0")

    runtime = MonitoringRuntime(
        store=service.store,
        market_service=_RuntimeMarketService(),
        calendar=_Calendar(True, "cached_exchange_calendar"),
        now_factory=lambda: datetime(2026, 7, 14, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    result = asyncio.run(runtime.run_once())
    assert result["evaluated_profiles"] == 1
    scheduled = service.store.get_profile(item["profile_id"])
    assert scheduled is not None
    last_check = datetime.fromisoformat(scheduled["last_quote_check_at"])
    next_check = datetime.fromisoformat(scheduled["next_quote_run_at"])
    assert (next_check - last_check).total_seconds() == 60
    asyncio.run(runtime.stop())


def test_runtime_does_not_apply_mainland_schedule_to_unsupported_markets(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    profile = _activate(service)
    with service.store.connect() as connection:
        connection.execute(
            "UPDATE monitor_profiles SET symbol='AAPL.US', market='US' WHERE profile_id=?",
            (profile["profile_id"],),
        )
    update_holdings(
        holdings=[{
            "name": "Apple Inc.",
            "code": "AAPL",
            "symbol": "AAPL.US",
            "quantity": 10,
            "cost_price": 200,
        }]
    )
    monkeypatch.setenv("VIBE_TRADING_MONITORING_ENABLED", "1")
    monkeypatch.setenv("VIBE_TRADING_MONITORING_MODE", "shadow")
    monkeypatch.setenv("VIBE_TRADING_MONITOR_MAINTENANCE_ENABLED", "0")
    market = _RuntimeMarketService()
    runtime = MonitoringRuntime(
        store=service.store,
        market_service=market,
        calendar=_Calendar(True, "cached_exchange_calendar"),
        now_factory=lambda: datetime(
            2026, 7, 14, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")
        ),
    )

    result = asyncio.run(runtime.run_once())

    assert result["decision"] == "unsupported_market_schedule"
    assert result["due_profiles"] == 0
    assert result["all_due_profiles"] == 1
    assert result["unsupported_market_profiles"] == 1
    assert result["blocked_profiles"] == 1
    assert result["supported_blocked_profiles"] == 0
    assert result["outcome_profiles"] == 1
    assert result["outcome_invariant_ok"] is True
    assert result["evaluated_profiles"] == 0
    assert market.refresh_calls == 0
    asyncio.run(runtime.stop())


def test_monitoring_schema_migrates_shadow_delivery_columns(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES('schema_version','1');
            CREATE TABLE delivery_outbox(
                delivery_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                delivery_target_id TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                claimed_at TEXT,
                delivered_at TEXT,
                remote_message_id TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(event_id,delivery_target_id)
            );
            """
        )

    store = MonitoringStore(path)
    with store.connect() as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(delivery_outbox)").fetchall()
        }
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
    assert {"delivery_mode", "would_deliver", "suppressed_at", "suppression_reason"} <= columns
    assert version == "9"


def test_v5_rule_migration_backfills_targets_and_disables_legacy_cues(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    item = service.create_draft_batch(["600036.SH"])["items"][0]
    profile = service.store.get_profile(item["profile_id"])
    assert profile is not None
    expected_rule = next(
        rule
        for rule in profile["plans"][0]["plan"]["market_rules"]
        if rule["kind"] == "price_cross_above"
    )
    legacy_rule = next(
        rule
        for rule in profile["plans"][0]["plan"]["market_rules"]
        if rule["kind"] == "price_cross_below"
    )
    legacy_plan = copy.deepcopy(profile["plans"][0]["plan"])
    for rule in legacy_plan["market_rules"]:
        if rule["client_rule_id"] == legacy_rule["client_rule_id"]:
            rule.pop("target_intent", None)
            rule.pop("target_level", None)

    with service.store.connect() as connection:
        connection.execute(
            """UPDATE monitor_plan_versions SET plan_json=?
               WHERE profile_id=? AND version=?""",
            (
                json.dumps(legacy_plan, ensure_ascii=False),
                item["profile_id"],
                item["plan_version"],
            ),
        )
        connection.execute("ALTER TABLE monitor_rules DROP COLUMN target_intent")
        connection.execute("ALTER TABLE monitor_rules DROP COLUMN target_level")
        connection.execute("ALTER TABLE monitor_rules DROP COLUMN alert_cue")
        connection.execute(
            "UPDATE schema_meta SET value='4' WHERE key='schema_version'"
        )

    migrated = MonitoringStore(service.store.path)
    with migrated.connect() as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(monitor_rules)").fetchall()
        }
        row = connection.execute(
            """SELECT target_intent,target_level,alert_cue FROM monitor_rules
               WHERE profile_id=? AND plan_version=? AND client_rule_id=?""",
            (
                item["profile_id"],
                item["plan_version"],
                expected_rule["client_rule_id"],
            ),
        ).fetchone()
        legacy_row = connection.execute(
            """SELECT target_intent,target_level,alert_cue FROM monitor_rules
               WHERE profile_id=? AND plan_version=? AND client_rule_id=?""",
            (
                item["profile_id"],
                item["plan_version"],
                legacy_rule["client_rule_id"],
            ),
        ).fetchone()
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
    assert {"target_intent", "target_level", "alert_cue"} <= columns
    assert dict(row) == {
        "target_intent": expected_rule["target_intent"],
        "target_level": expected_rule["target_level"],
        "alert_cue": "none",
    }
    assert dict(legacy_row) == {
        "target_intent": "watch",
        "target_level": 1,
        "alert_cue": "none",
    }
    assert version == "9"


def test_monitoring_maintenance_backs_up_and_prunes_unreferenced_data(
    tmp_path,
    monkeypatch,
) -> None:
    store = MonitoringStore(tmp_path / "monitoring.sqlite3")
    monkeypatch.setenv("VIBE_TRADING_MONITOR_BACKUP_DIR", str(tmp_path / "backups"))
    with store.connect() as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """INSERT INTO monitor_observations(
               observation_id,profile_id,domain,source_key,observed_at,data_as_of,status,
               payload_json,payload_hash
               ) VALUES('old-observation','missing-profile','quote','test',
               '2020-01-01T00:00:00+00:00',NULL,'verified','{}','hash')"""
        )
    result = store.run_maintenance(force=True)
    assert result["status"] == "completed"
    assert Path(result["details"]["backup_path"]).exists()
    assert result["details"]["observations_pruned"] == 1
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM monitor_observations WHERE observation_id='old-observation'"
        ).fetchone()[0] == 0


def _price_volume_bar(
    day: str,
    hhmm: str,
    *,
    close: float,
    volume: float,
    status: str = "verified",
    sources: tuple[str, ...] = ("tencent", "mootdx"),
    unit: str = "share",
    quality_flags: list[str] | None = None,
    low: float | None = None,
    high: float | None = None,
) -> dict:
    return {
        "bar_time": f"{day}T{hhmm}:00+00:00",
        "session_date": day,
        "open": close,
        "high": high if high is not None else close + 0.2,
        "low": low if low is not None else close - 0.8,
        "close": close,
        "volume": volume,
        "status": status,
        "sources": list(sources),
        "quality_flags": list(quality_flags or []),
        "observations": [
            {
                "actual_source": source,
                "volume_unit": unit,
                "included_in_consensus": True,
            }
            for source in sources
        ],
    }


class _PriceVolumeRowsStore:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.limits: list[int] = []

    def query_bars(self, **kwargs):
        self.limits.append(int(kwargs["limit"]))
        return sorted((dict(row) for row in self.rows), key=lambda row: row["bar_time"])


def _price_volume_rows(
    closes: list[float],
    latest_volume: float,
    *,
    latest_low: float | None = None,
    latest_high: float | None = None,
) -> list[dict]:
    rows = [
        _price_volume_bar(
            f"2026-06-{day:02d}",
            "01:50",
            close=100,
            volume=100,
        )
        for day in range(1, 11)
    ]
    for index, (hhmm, close) in enumerate(
        zip(("01:35", "01:40", "01:45", "01:50"), closes)
    ):
        rows.append(
            _price_volume_bar(
                "2026-07-14",
                hhmm,
                close=close,
                volume=latest_volume if index == 3 else 100 + index * 10,
                low=latest_low if index == 3 else None,
                high=latest_high if index == 3 else None,
            )
        )
    return rows


@pytest.mark.parametrize(
    ("closes", "latest_volume", "expected_regime", "expected_volume_state"),
    [
        ([100, 100.2, 100.4, 100.6], 200, "bullish_expansion", "expanded"),
        ([100, 100.2, 100.4, 100.6], 50, "bullish_contraction", "contracted"),
        ([100, 99.8, 99.6, 99.4], 200, "bearish_expansion", "expanded"),
        ([100, 99.8, 99.6, 99.4], 50, "bearish_contraction", "contracted"),
        ([100, 100.02, 100.01, 100.03], 200, "high_volume_stall", "expanded"),
        ([100, 100.02, 100.01, 100.03], 100, "neutral", "normal"),
    ],
)
def test_price_volume_analyzer_classifies_same_bucket_regimes(
    closes,
    latest_volume,
    expected_regime,
    expected_volume_state,
) -> None:
    store = _PriceVolumeRowsStore(_price_volume_rows(closes, latest_volume))
    analyzer = PriceVolumeAnalyzer()
    result, evidence_bar_time = analyzer.analyze(
        market_store=store,
        symbol="600036.SH",
        now_utc=datetime(2026, 7, 14, 1, 55, tzinfo=timezone.utc),
        policy=dict(DEFAULT_PRICE_VOLUME_POLICY),
    )

    assert result["status"] == "ready"
    assert result["regime"] == expected_regime
    assert result["volume_state"] == expected_volume_state
    assert result["volume_ratio"] == pytest.approx(latest_volume / 100)
    assert result["baseline_samples"] == 10
    assert evidence_bar_time == "2026-07-14T01:50:00+00:00"
    assert store.limits == [64, 2000]

    repeated, _ = analyzer.analyze(
        market_store=store,
        symbol="600036.SH",
        now_utc=datetime(2026, 7, 14, 1, 56, tzinfo=timezone.utc),
        policy=dict(DEFAULT_PRICE_VOLUME_POLICY),
    )
    assert repeated["volume_ratio"] == result["volume_ratio"]
    assert store.limits == [64, 2000, 64]


def test_price_volume_baseline_uses_median_and_fails_closed_on_incompatible_history() -> None:
    outlier_rows = _price_volume_rows([100, 100.2, 100.4, 100.6], 200)
    outlier_rows[0]["volume"] = 10000
    outlier, _ = PriceVolumeAnalyzer().analyze(
        market_store=_PriceVolumeRowsStore(outlier_rows),
        symbol="600036.SH",
        now_utc=datetime(2026, 7, 14, 1, 55, tzinfo=timezone.utc),
        policy=dict(DEFAULT_PRICE_VOLUME_POLICY),
    )
    assert outlier["status"] == "ready"
    assert outlier["volume_ratio"] == pytest.approx(2.0)

    source_mismatch = _price_volume_rows([100, 100.2, 100.4, 100.6], 200)
    for row in source_mismatch[:6]:
        row["sources"] = ["tencent"]
        row["observations"] = [row["observations"][0]]
    mismatched, _ = PriceVolumeAnalyzer().analyze(
        market_store=_PriceVolumeRowsStore(source_mismatch),
        symbol="600036.SH",
        now_utc=datetime(2026, 7, 14, 1, 55, tzinfo=timezone.utc),
        policy=dict(DEFAULT_PRICE_VOLUME_POLICY),
    )
    assert mismatched["status"] == "insufficient_data"
    assert mismatched["baseline_samples"] == 4
    assert mismatched["reason_codes"] == [
        "source_signature_mismatch",
        "insufficient_same_time_baseline",
    ]

    conflict = _price_volume_rows([100, 100.2, 100.4, 100.6], 200)
    for row in conflict[-4:]:
        row["quality_flags"] = ["volume_conflict"]
    conflicted, _ = PriceVolumeAnalyzer().analyze(
        market_store=_PriceVolumeRowsStore(conflict),
        symbol="600036.SH",
        now_utc=datetime(2026, 7, 14, 1, 55, tzinfo=timezone.utc),
        policy=dict(DEFAULT_PRICE_VOLUME_POLICY),
    )
    assert conflicted["status"] == "insufficient_data"
    assert conflicted["reason_codes"] == [
        "volume_conflict",
        "no_actionable_closed_bar",
    ]


def test_volume_ratio_only_uses_rule_history_and_does_not_require_four_bars() -> None:
    rows = _price_volume_rows([100, 100.1, 100.2, 100.3], 180)
    rows = [*rows[:10], rows[-1]]
    store = _PriceVolumeRowsStore(rows)

    result, _ = PriceVolumeAnalyzer().analyze(
        market_store=store,
        symbol="600036.SH",
        now_utc=datetime(2026, 7, 14, 1, 52, tzinfo=timezone.utc),
        policy={**DEFAULT_PRICE_VOLUME_POLICY, "interval": "1m"},
        interval="1m",
        require_pattern=False,
    )

    assert result["status"] == "ready"
    assert result["volume_ratio"] == pytest.approx(1.8)
    assert result["reason_codes"] == ["volume_ratio_only", "volume_expanded"]
    assert store.limits == [64, 2000]


def test_price_volume_baseline_prefers_targeted_same_bucket_cache_query() -> None:
    class TargetedStore(_PriceVolumeRowsStore):
        def __init__(self, rows: list[dict]) -> None:
            super().__init__(rows)
            self.bucket_calls: list[dict] = []

        def query_same_time_bucket_bars(self, **kwargs):
            self.bucket_calls.append(dict(kwargs))
            return sorted(
                (dict(row) for row in self.rows),
                key=lambda row: row["bar_time"],
            )

    store = TargetedStore(_price_volume_rows([100, 100.1, 100.2, 100.3], 180))
    result, _ = PriceVolumeAnalyzer().analyze(
        market_store=store,
        symbol="600036.SH",
        now_utc=datetime(2026, 7, 14, 1, 55, tzinfo=timezone.utc),
        policy=dict(DEFAULT_PRICE_VOLUME_POLICY),
    )

    assert result["status"] == "ready"
    assert store.limits == [64]
    assert len(store.bucket_calls) == 1
    assert store.bucket_calls[0]["local_time_bucket"] == "09:50"
    assert store.bucket_calls[0]["limit"] == 2000


def test_market_cache_queries_one_shanghai_time_bucket(tmp_path) -> None:
    store = MarketCacheStore(tmp_path / "market-cache.sqlite3")
    with store.connect() as connection:
        for index, bar_time in enumerate(
            (
                "2026-07-13T01:30:00+00:00",
                "2026-07-13T01:31:00+00:00",
                "2026-07-14T09:30:00+08:00",
            )
        ):
            connection.execute(
                """INSERT INTO consensus_bars(
                   symbol,interval,bar_time,session_date,adjustment,open,high,low,
                   close,volume,status,source_count,sources_json,observations_json,
                   quality_flags,verified_at,batch_id
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "600036.SH",
                    "1m",
                    bar_time,
                    "2026-07-13" if index < 2 else "2026-07-14",
                    "raw",
                    40,
                    40.1,
                    39.9,
                    40,
                    100 + index,
                    "verified",
                    2,
                    '["tencent","mootdx"]',
                    "[]",
                    "[]",
                    bar_time,
                    "test-batch",
                ),
            )
        connection.commit()

    rows = store.query_same_time_bucket_bars(
        symbol="600036.SH",
        interval="1m",
        adjustment="raw",
        local_time_bucket="09:30",
    )

    assert [row["bar_time"] for row in rows] == [
        "2026-07-13T01:30:00+00:00",
        "2026-07-14T09:30:00+08:00",
    ]
    assert rows[0]["sources"] == ["tencent", "mootdx"]


def test_price_volume_analyzer_detects_accelerated_decline() -> None:
    rows = _price_volume_rows(
        [100, 99.8, 99.4, 98.6],
        200,
        latest_low=98.5,
        latest_high=99.5,
    )
    result, _ = PriceVolumeAnalyzer().analyze(
        market_store=_PriceVolumeRowsStore(rows),
        symbol="600036.SH",
        now_utc=datetime(2026, 7, 14, 1, 55, tzinfo=timezone.utc),
        policy=dict(DEFAULT_PRICE_VOLUME_POLICY),
    )

    assert result["accelerated_decline"] is True
    assert result["close_location"] == pytest.approx(0.1)
    assert "accelerated_decline" in result["reason_codes"]

    only_two_consecutive_declines, _ = PriceVolumeAnalyzer().analyze(
        market_store=_PriceVolumeRowsStore(
            _price_volume_rows(
                [99.5, 100.0, 99.7, 99.1],
                200,
                latest_low=99.0,
                latest_high=100.0,
            )
        ),
        symbol="600036.SH",
        now_utc=datetime(2026, 7, 14, 1, 55, tzinfo=timezone.utc),
        policy=dict(DEFAULT_PRICE_VOLUME_POLICY),
    )
    assert only_two_consecutive_declines["accelerated_decline"] is False


def test_shrinking_add_confirmation_requires_stabilization_before_rebound() -> None:
    stable, _ = PriceVolumeAnalyzer().analyze(
        market_store=_PriceVolumeRowsStore(
            _price_volume_rows([100, 99.95, 99.96, 100.1], 50)
        ),
        symbol="600036.SH",
        now_utc=datetime(2026, 7, 14, 1, 55, tzinfo=timezone.utc),
        policy=dict(DEFAULT_PRICE_VOLUME_POLICY),
    )
    assert "add_shrinking_reversal" in stable["reason_codes"]

    falling_knife_bounce, _ = PriceVolumeAnalyzer().analyze(
        market_store=_PriceVolumeRowsStore(
            _price_volume_rows([100, 99, 98, 98.2], 50)
        ),
        symbol="600036.SH",
        now_utc=datetime(2026, 7, 14, 1, 55, tzinfo=timezone.utc),
        policy=dict(DEFAULT_PRICE_VOLUME_POLICY),
    )
    assert "add_shrinking_reversal" not in falling_knife_bounce["reason_codes"]


def test_price_volume_analyzer_skips_latest_low_quality_bar_and_rejects_unknown_units() -> None:
    rows = _price_volume_rows([100, 100.1, 100.2, 100.3], 200)
    rows.append(
        _price_volume_bar(
            "2026-07-14",
            "01:55",
            close=100.4,
            volume=300,
            status="source_lag",
            sources=("tencent",),
        )
    )
    analyzer = PriceVolumeAnalyzer()
    result, evidence_bar_time = analyzer.analyze(
        market_store=_PriceVolumeRowsStore(rows),
        symbol="600036.SH",
        now_utc=datetime(2026, 7, 14, 2, 1, tzinfo=timezone.utc),
        policy=dict(DEFAULT_PRICE_VOLUME_POLICY),
    )
    assert result["status"] == "ready"
    assert evidence_bar_time == "2026-07-14T01:50:00+00:00"

    stale, stale_bar_time = PriceVolumeAnalyzer().analyze(
        market_store=_PriceVolumeRowsStore(rows),
        symbol="600036.SH",
        now_utc=datetime(2026, 7, 14, 3, 0, tzinfo=timezone.utc),
        policy=dict(DEFAULT_PRICE_VOLUME_POLICY),
    )
    assert stale["status"] == "insufficient_data"
    assert stale["reason_codes"] == ["stale_price_volume_bar"]
    assert stale_bar_time == "2026-07-14T01:50:00+00:00"

    for row in rows:
        if row["session_date"] == "2026-07-14" and row["status"] == "verified":
            row["observations"][0]["volume_unit"] = "unknown"
    invalid, _ = PriceVolumeAnalyzer().analyze(
        market_store=_PriceVolumeRowsStore(rows),
        symbol="600036.SH",
        now_utc=datetime(2026, 7, 14, 2, 1, tzinfo=timezone.utc),
        policy=dict(DEFAULT_PRICE_VOLUME_POLICY),
    )
    assert invalid["status"] == "insufficient_data"
    assert "no_actionable_closed_bar" in invalid["reason_codes"]


def test_price_volume_analyzer_does_not_form_patterns_across_lunch_or_gaps() -> None:
    rows = [
        _price_volume_bar(
            f"2026-06-{day:02d}",
            "05:10",
            close=100,
            volume=100,
        )
        for day in range(1, 11)
    ]
    for hhmm, close in (("03:25", 100), ("03:30", 99.9), ("05:05", 99.7), ("05:10", 99.4)):
        rows.append(
            _price_volume_bar(
                "2026-07-14",
                hhmm,
                close=close,
                volume=200 if hhmm == "05:10" else 100,
            )
        )
    result, _ = PriceVolumeAnalyzer().analyze(
        market_store=_PriceVolumeRowsStore(rows),
        symbol="600036.SH",
        now_utc=datetime(2026, 7, 14, 5, 15, tzinfo=timezone.utc),
        policy=dict(DEFAULT_PRICE_VOLUME_POLICY),
    )
    assert result["status"] == "insufficient_data"
    assert result["reason_codes"] == ["insufficient_recent_bars"]

    one_minute_rows = [
        _price_volume_bar(
            f"2026-06-{day:02d}",
            "01:03",
            close=100,
            volume=100,
        )
        for day in range(1, 11)
    ]
    for hhmm, close in (("00:59", 100), ("01:00", 99.9), ("01:02", 99.8), ("01:03", 99.7)):
        one_minute_rows.append(
            _price_volume_bar(
                "2026-07-14",
                hhmm,
                close=close,
                volume=200 if hhmm == "01:03" else 100,
            )
        )
    one_minute_policy = {**DEFAULT_PRICE_VOLUME_POLICY, "interval": "1m"}
    one_minute, _ = PriceVolumeAnalyzer().analyze(
        market_store=_PriceVolumeRowsStore(one_minute_rows),
        symbol="600036.SH",
        now_utc=datetime(2026, 7, 14, 1, 4, tzinfo=timezone.utc),
        policy=one_minute_policy,
        interval="1m",
    )
    assert one_minute["status"] == "insufficient_data"
    assert one_minute["reason_codes"] == ["insufficient_recent_bars"]


def test_v2_plus_policy_validation_and_v1_price_only_compatibility() -> None:
    plan, _evidence, blocked = MonitoringPlanner(FakeMarketService()).build(
        {"symbol": "600036.SH", "quantity": 1, "cost_price": 38}
    )
    assert blocked == []
    assert plan is not None
    assert plan["schema_version"] >= 2
    assert plan["price_volume_policy"] == DEFAULT_PRICE_VOLUME_POLICY

    invalid = copy.deepcopy(plan)
    invalid["price_volume_policy"]["expansion_ratio"] = 0.9
    with pytest.raises(PlanValidationError, match="expansion_ratio"):
        validate_plan(invalid, expected_symbol="600036.SH")
    invalid_samples = copy.deepcopy(plan)
    invalid_samples["price_volume_policy"]["min_samples"] = 4
    with pytest.raises(PlanValidationError, match="between 5"):
        validate_plan(invalid_samples, expected_symbol="600036.SH")

    v1 = copy.deepcopy(plan)
    v1["schema_version"] = 1
    normalized = validate_plan(v1, expected_symbol="600036.SH")
    assert normalized["schema_version"] == 1
    assert "price_volume_policy" not in normalized


def _ready_price_volume(*, accelerated: bool, reasons: list[str] | None = None) -> dict:
    return {
        "status": "ready",
        "regime": "bearish_expansion" if accelerated else "neutral",
        "volume_state": "expanded" if accelerated else "normal",
        "volume_ratio": 2.0 if accelerated else 1.0,
        "baseline_samples": 10,
        "three_bar_return_bps": -180.0 if accelerated else 2.0,
        "latest_return_bps": -90.0 if accelerated else 1.0,
        "close_location": 0.1 if accelerated else 0.7,
        "accelerated_decline": accelerated,
        "reason_codes": reasons or (["accelerated_decline"] if accelerated else ["price_stabilized"]),
    }


def test_upper_wick_alone_does_not_confirm_take_profit() -> None:
    decision, _message, reasons = MonitoringStore._base_target_decision(
        {"target_intent": "take_profit"},
        {
            **_ready_price_volume(
                accelerated=False,
                reasons=["take_profit_upper_wick"],
            ),
        },
    )
    assert decision == "no_confirmation"
    assert "take_profit_upper_wick" in reasons


def test_replay_supports_price_volume_shadow_without_outbox(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    profile = _activate(service)
    plan = service.store.get_plan(profile["profile_id"], int(profile["active_plan_version"]))
    add_rule = next(
        rule for rule in plan["plan"]["market_rules"]
        if rule["target_intent"] == "add_position"
    )
    bar_time = "2026-07-14T02:05:00+00:00"
    result = replay_quotes(
        service.store,
        profile["profile_id"],
        [{
            "last_price": float(add_rule["parameters"]["threshold"]) * 1.005,
            "interval": "5m",
            "bar_time": bar_time,
            "price_volume_bar_time": bar_time,
            "status": "verified",
            "sources": ["tencent", "mootdx"],
            "price_volume": _ready_price_volume(accelerated=True),
        }],
        delivery_mode="deliver",
        price_volume_mode="shadow",
        duplicate_indexes={0},
    )

    assert result["price_volume_mode"] == "shadow"
    assert result["events_created"] == 2
    assert result["pending_deliveries"] == 0


def test_price_volume_target_episode_is_deduped_and_requires_two_bar_clearance(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    profile = _activate(service)
    plan = service.store.get_plan(profile["profile_id"], int(profile["active_plan_version"]))
    assert plan is not None
    add_rule = next(
        rule for rule in plan["plan"]["market_rules"]
        if rule["target_intent"] == "add_position"
    )
    threshold = float(add_rule["parameters"]["threshold"])

    def quote(bar_time: str, price_volume: dict) -> dict:
        return {
            "last_price": threshold * 1.005,
            "interval": "5m",
            "bar_time": bar_time,
            "price_volume_bar_time": bar_time,
            "status": "verified",
            "sources": ["tencent", "mootdx"],
            "price_volume": price_volume,
        }

    first = service.store.evaluate_quote(
        profile["profile_id"],
        quote("2026-07-14T02:05:00+00:00", _ready_price_volume(accelerated=True)),
        delivery_mode="deliver",
        price_volume_mode="shadow",
    )
    assert {event["kind"] for event in first} == {
        "price_volume_accelerated_decline",
        "target_proximity",
    }
    target_event = next(event for event in first if event["kind"] == "target_proximity")
    assert target_event["facts"]["target_assessment"]["decision"] == "opposes_add"
    assert target_event["facts"]["target_assessment"]["message"] == "放量加速下跌，不宜补仓"
    accelerated_event = next(
        event for event in first
        if event["kind"] == "price_volume_accelerated_decline"
    )
    assert accelerated_event["facts"]["last_price"] == pytest.approx(
        threshold * 1.005
    )
    assert accelerated_event["facts"]["bar_time"] == "2026-07-14T02:05:00+00:00"
    assert accelerated_event["facts"]["sources"] == ["tencent", "mootdx"]
    assert accelerated_event["facts"]["quality_status"] == "verified"
    assert all(event["deliveries"] == [] for event in first)

    boundary = _ready_price_volume(accelerated=False, reasons=["neutral"])
    boundary["volume_ratio"] = 1.2
    second = service.store.evaluate_quote(
        profile["profile_id"],
        quote("2026-07-14T02:10:00+00:00", boundary),
        delivery_mode="deliver",
        price_volume_mode="shadow",
    )
    assert second == []
    third = service.store.evaluate_quote(
        profile["profile_id"],
        quote("2026-07-14T02:15:00+00:00", boundary),
        delivery_mode="deliver",
        price_volume_mode="shadow",
    )
    assert third == []
    boundary_state = next(
        state for state in service.store.list_signal_states(profile["profile_id"])
        if state["signal_type"] == "target_assessment"
        and state["client_rule_id"] == add_rule["client_rule_id"]
    )
    assert boundary_state["release_progress"] == 0
    assert boundary_state["payload"]["decision"] == "opposes_add"

    below_boundary = copy.deepcopy(boundary)
    below_boundary["volume_ratio"] = 1.19
    service.store.evaluate_quote(
        profile["profile_id"],
        quote("2026-07-14T02:20:00+00:00", below_boundary),
        delivery_mode="deliver",
        price_volume_mode="shadow",
    )
    fifth = service.store.evaluate_quote(
        profile["profile_id"],
        quote("2026-07-14T02:25:00+00:00", below_boundary),
        delivery_mode="deliver",
        price_volume_mode="shadow",
    )
    changed = next(event for event in fifth if event["kind"] == "target_assessment_changed")
    assert changed["facts"]["target_assessment"]["decision"] == "no_confirmation"
    states = service.store.list_signal_states(profile["profile_id"])
    add_state = next(
        state for state in states
        if state["signal_type"] == "target_assessment"
        and state["client_rule_id"] == add_rule["client_rule_id"]
    )
    assert add_state["release_progress"] == 2
    assert service.store.get_profile(profile["profile_id"])["last_quote"]["price_volume"]["status"] == "ready"


def test_accelerated_decline_episode_requires_two_ready_bars_before_rearm(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    profile = _activate(service)

    def quote(bar_time: str, accelerated: bool) -> dict:
        return {
            "last_price": 40,
            "interval": "5m",
            "bar_time": bar_time,
            "price_volume_bar_time": bar_time,
            "status": "verified",
            "sources": ["tencent", "mootdx"],
            "price_volume": _ready_price_volume(accelerated=accelerated),
        }

    first = service.store.evaluate_quote(
        profile["profile_id"],
        quote("2026-07-14T02:05:00+00:00", True),
        price_volume_mode="shadow",
    )
    assert sum(
        event["kind"] == "price_volume_accelerated_decline" for event in first
    ) == 1

    service.store.evaluate_quote(
        profile["profile_id"],
        quote("2026-07-14T02:10:00+00:00", False),
        price_volume_mode="shadow",
    )
    resumed = service.store.evaluate_quote(
        profile["profile_id"],
        quote("2026-07-14T02:15:00+00:00", True),
        price_volume_mode="shadow",
    )
    assert all(
        event["kind"] != "price_volume_accelerated_decline" for event in resumed
    )

    service.store.evaluate_quote(
        profile["profile_id"],
        quote("2026-07-14T02:20:00+00:00", False),
        price_volume_mode="shadow",
    )
    service.store.evaluate_quote(
        profile["profile_id"],
        quote("2026-07-14T02:25:00+00:00", False),
        price_volume_mode="shadow",
    )
    rearmed = service.store.evaluate_quote(
        profile["profile_id"],
        quote("2026-07-14T02:30:00+00:00", True),
        price_volume_mode="shadow",
    )
    assert sum(
        event["kind"] == "price_volume_accelerated_decline" for event in rearmed
    ) == 1


def test_accelerated_event_uses_price_volume_evidence_time_for_mixed_intervals(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    profile = _activate(service)
    events = service.store.evaluate_quote(
        profile["profile_id"],
        {
            "last_price": 40,
            "interval": "1m",
            "bar_time": "2026-07-14T02:06:00+00:00",
            "price_volume_bar_time": "2026-07-14T02:05:00+00:00",
            "status": "verified",
            "sources": ["tencent", "mootdx"],
            "price_volume": _ready_price_volume(accelerated=True),
        },
        price_volume_mode="shadow",
    )

    accelerated = next(
        event for event in events
        if event["kind"] == "price_volume_accelerated_decline"
    )
    assert accelerated["facts"]["bar_time"] == "2026-07-14T02:05:00+00:00"
    assert (
        accelerated["facts"]["price_volume_bar_time"]
        == "2026-07-14T02:05:00+00:00"
    )
    assert accelerated["facts"]["quote_bar_time"] == "2026-07-14T02:06:00+00:00"


def test_metrics_report_deduped_price_volume_insufficient_and_conflict_rates(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    profile = _activate(service)

    def evaluate(
        *,
        bar_time: str,
        evidence_bar_time: str,
        price_volume: dict,
    ) -> None:
        service.store.evaluate_quote(
            profile["profile_id"],
            {
                "last_price": 40,
                "interval": "5m",
                "bar_time": bar_time,
                "price_volume_bar_time": evidence_bar_time,
                "status": "verified",
                "sources": ["tencent", "mootdx"],
                "price_volume": price_volume,
            },
            price_volume_mode="shadow",
        )

    evaluate(
        bar_time="2026-07-14T02:05:00+00:00",
        evidence_bar_time="2026-07-14T02:05:00+00:00",
        price_volume=_ready_price_volume(accelerated=False),
    )
    # A scheduler observation of the same closed 5m evidence must not inflate
    # the rollout denominator.
    evaluate(
        bar_time="2026-07-14T02:06:00+00:00",
        evidence_bar_time="2026-07-14T02:05:00+00:00",
        price_volume=_ready_price_volume(accelerated=False),
    )
    evaluate(
        bar_time="2026-07-14T02:10:00+00:00",
        evidence_bar_time="2026-07-14T02:10:00+00:00",
        price_volume={
            "status": "insufficient_data",
            "reason_codes": ["volume_conflict", "no_actionable_closed_bar"],
        },
    )
    evaluate(
        bar_time="2026-07-14T02:15:00+00:00",
        evidence_bar_time="2026-07-14T02:15:00+00:00",
        price_volume={
            "status": "disabled",
            "reason_codes": ["price_volume_mode_off"],
        },
    )

    quality = service.store.metrics()["price_volume_quality"]
    assert quality["window_hours"] == 24
    assert quality["observation_count"] == 3
    assert quality["evidence_count"] == 2
    assert quality["disabled_count"] == 1
    assert quality["status_counts"] == {
        "ready": 1,
        "insufficient_data": 1,
        "disabled": 1,
    }
    assert quality["reason_counts"]["volume_conflict"] == 1
    assert quality["insufficient_rate"] == pytest.approx(0.5)
    assert quality["conflict_rate"] == pytest.approx(0.5)


def test_runtime_health_reports_windowed_duplicate_event_rate(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc).isoformat()
    service.store.record_runtime_tick(
        {
            "tick_id": "duplicate-rate-tick",
            "owner_id": "test-owner",
            "mode": "shadow",
            "decision": "evaluated",
            "started_at": now,
            "finished_at": now,
            "duration_ms": 20,
            "events_created": 8,
            "duplicate_events": 2,
        }
    )

    health = service.store.runtime_health()
    assert health["events_created"] == 8
    assert health["duplicate_event_count"] == 2
    assert health["event_attempt_count"] == 10
    assert health["duplicate_event_rate"] == pytest.approx(0.2)


def test_standby_runtime_does_not_claim_another_leaders_duplicate_counter(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    monkeypatch.setenv("VIBE_TRADING_MONITORING_ENABLED", "1")
    monkeypatch.setenv("VIBE_TRADING_MONITORING_MODE", "shadow")
    with service.store.transaction() as connection:
        service.store._increment_counter(
            connection,
            "duplicate_event_count",
            amount=5,
        )
    assert service.store.acquire_lease(
        "portfolio_monitoring_runtime",
        "other-leader",
        ttl_seconds=90,
    )
    runtime = MonitoringRuntime(
        store=service.store,
        market_service=FakeMarketService(),
        calendar=_Calendar(True, "cached_exchange_calendar"),
        now_factory=lambda: datetime(
            2026,
            7,
            14,
            10,
            0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
    )

    result = asyncio.run(runtime.run_once())

    assert result["decision"] == "standby_not_leader"
    assert result["duplicate_events"] == 0
    assert service.store.runtime_health()["duplicate_event_count"] == 0


def test_price_volume_deliver_mode_creates_outbox_but_off_mode_creates_no_signal_events(
    tmp_path,
    monkeypatch,
) -> None:
    off_service = _service(tmp_path / "off", monkeypatch)
    off_profile = _activate(off_service)
    off_service.store.evaluate_quote(
        off_profile["profile_id"],
        {
            "last_price": 40,
            "interval": "5m",
            "bar_time": "2026-07-14T02:05:00+00:00",
            "status": "verified",
            "sources": ["tencent", "mootdx"],
            "price_volume": _ready_price_volume(accelerated=True),
        },
        price_volume_mode="off",
    )
    assert off_service.store.list_signal_states(off_profile["profile_id"]) == []
    assert off_service.store.list_events() == []

    deliver_service = _service(tmp_path / "deliver", monkeypatch)
    deliver_profile = _activate(deliver_service)
    deliver_plan = deliver_service.store.get_plan(
        deliver_profile["profile_id"], int(deliver_profile["active_plan_version"])
    )
    add_threshold = next(
        float(rule["parameters"]["threshold"])
        for rule in deliver_plan["plan"]["market_rules"]
        if rule["target_intent"] == "add_position"
    )
    events = deliver_service.store.evaluate_quote(
        deliver_profile["profile_id"],
        {
            "last_price": add_threshold * 1.005,
            "interval": "5m",
            "bar_time": "2026-07-14T02:05:00+00:00",
            "price_volume_bar_time": "2026-07-14T02:05:00+00:00",
            "status": "verified",
            "sources": ["tencent", "mootdx"],
            "price_volume": _ready_price_volume(accelerated=True),
        },
        delivery_mode="deliver",
        price_volume_mode="deliver",
    )
    assert len(events) == 2
    assert len(deliver_service.store.pending_deliveries()) == 2


def test_insufficient_price_volume_never_blocks_a_price_trigger(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    profile = _activate(service)
    plan = service.store.get_plan(profile["profile_id"], int(profile["active_plan_version"]))
    take_profit = next(
        rule for rule in plan["plan"]["market_rules"]
        if rule["target_intent"] == "take_profit" and rule["target_level"] == 1
    )
    threshold = float(take_profit["parameters"]["threshold"])
    insufficient = {
        "status": "insufficient_data",
        "regime": None,
        "volume_state": None,
        "volume_ratio": None,
        "baseline_samples": 2,
        "three_bar_return_bps": None,
        "latest_return_bps": None,
        "close_location": None,
        "accelerated_decline": False,
        "reason_codes": ["insufficient_same_time_baseline"],
    }
    for index, price in enumerate((threshold - 1, threshold + 0.1, threshold + 0.2)):
        bar_time = f"2026-07-14T02:{5 + index * 5:02d}:00+00:00"
        service.store.evaluate_quote(
            profile["profile_id"],
            {
                "last_price": price,
                "interval": "5m",
                "bar_time": bar_time,
                "price_volume_bar_time": bar_time,
                "status": "verified",
                "sources": ["tencent", "mootdx"],
                "price_volume": insufficient,
            },
            delivery_mode="shadow",
            price_volume_mode="shadow",
        )
    market_event = next(
        event for event in service.store.list_events(limit=20)
        if event["kind"] == "market_rule_trigger"
        and event["facts"]["parameters"].get("threshold") == threshold
    )
    assert market_event["facts"]["target_assessment"]["decision"] == "insufficient_data"
    assert market_event["facts"]["price_volume"]["status"] == "insufficient_data"


def test_strong_bullish_volume_does_not_suppress_take_profit_price_event(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    profile = _activate(service)
    plan = service.store.get_plan(profile["profile_id"], int(profile["active_plan_version"]))
    take_profit = next(
        rule for rule in plan["plan"]["market_rules"]
        if rule["target_intent"] == "take_profit" and rule["target_level"] == 1
    )
    threshold = float(take_profit["parameters"]["threshold"])
    bullish = {
        **_ready_price_volume(accelerated=False),
        "regime": "bullish_expansion",
        "volume_state": "expanded",
        "volume_ratio": 2.0,
        "close_location": 0.9,
        "reason_codes": ["bullish_expansion", "strong_bullish_momentum"],
    }
    for index, price in enumerate((threshold - 1, threshold + 0.1, threshold + 0.2)):
        bar_time = f"2026-07-14T03:{5 + index * 5:02d}:00+00:00"
        service.store.evaluate_quote(
            profile["profile_id"],
            {
                "last_price": price,
                "interval": "5m",
                "bar_time": bar_time,
                "price_volume_bar_time": bar_time,
                "status": "verified",
                "sources": ["tencent", "mootdx"],
                "price_volume": bullish,
            },
            delivery_mode="shadow",
            price_volume_mode="shadow",
        )
    market_event = next(
        event for event in service.store.list_events(limit=20)
        if event["kind"] == "market_rule_trigger"
        and event["facts"]["parameters"].get("threshold") == threshold
    )
    assessment = market_event["facts"]["target_assessment"]
    assert assessment["decision"] == "no_confirmation"
    assert "尚未出现动能衰竭证据" in assessment["message"]


def test_target_leave_and_reenter_starts_a_new_episode(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    profile = _activate(service)
    plan = service.store.get_plan(profile["profile_id"], int(profile["active_plan_version"]))
    add_rule = next(
        rule for rule in plan["plan"]["market_rules"]
        if rule["target_intent"] == "add_position"
    )
    threshold = float(add_rule["parameters"]["threshold"])
    for index, price in enumerate((threshold * 1.005, threshold * 1.05, threshold * 1.005)):
        bar_time = f"2026-07-14T04:{5 + index * 5:02d}:00+00:00"
        service.store.evaluate_quote(
            profile["profile_id"],
            {
                "last_price": price,
                "interval": "5m",
                "bar_time": bar_time,
                "price_volume_bar_time": bar_time,
                "status": "verified",
                "sources": ["tencent", "mootdx"],
                "price_volume": _ready_price_volume(accelerated=False),
            },
            delivery_mode="shadow",
            price_volume_mode="shadow",
        )
    proximity_events = [
        event for event in service.store.list_events(limit=30)
        if event["kind"] == "target_proximity"
        and event["facts"]["target_assessment"]["client_rule_id"] == add_rule["client_rule_id"]
    ]
    assert len(proximity_events) == 2
    state = next(
        value for value in service.store.list_signal_states(profile["profile_id"])
        if value["signal_type"] == "target_assessment"
        and value["client_rule_id"] == add_rule["client_rule_id"]
    )
    assert state["episode"] == 2


def test_mixed_intervals_cannot_reopen_another_rules_target_episode(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    target = service.store.bind_target(channel="feishu", chat_id="ou_mixed_intervals")
    item = service.create_draft_batch(["600036.SH"], target["target_id"])["items"][0]
    profile = service.store.get_profile(item["profile_id"])
    plan = copy.deepcopy(profile["plans"][0]["plan"])
    add_rule = next(
        rule for rule in plan["market_rules"] if rule["target_intent"] == "add_position"
    )
    add_rule["parameters"]["interval"] = "1m"
    plan["quote_tier"] = "active"
    service.store.update_draft(
        item["profile_id"],
        item["plan_version"],
        plan,
        expected_revision=profile["profile_revision"],
    )
    active = service.store.activate(item["profile_id"], item["plan_version"], max_active=10)
    threshold = float(add_rule["parameters"]["threshold"])

    def evaluate(interval: str, bar_time: str, last_price: float) -> None:
        service.store.evaluate_quote(
            active["profile_id"],
            {
                "last_price": last_price,
                "interval": interval,
                "bar_time": bar_time,
                "price_volume_bar_time": bar_time,
                "status": "verified",
                "sources": ["tencent", "mootdx"],
                "price_volume": _ready_price_volume(accelerated=False),
            },
            delivery_mode="shadow",
            price_volume_mode="shadow",
        )

    evaluate("1m", "2026-07-14T04:05:00+00:00", threshold * 1.005)
    evaluate("5m", "2026-07-14T04:05:00+00:00", threshold * 1.05)
    evaluate("1m", "2026-07-14T04:06:00+00:00", threshold * 1.005)

    proximity = [
        event for event in service.store.list_events(limit=30)
        if event["kind"] == "target_proximity"
        and event["facts"]["target_assessment"]["client_rule_id"]
        == add_rule["client_rule_id"]
    ]
    assert len(proximity) == 1
    state = next(
        value for value in service.store.list_signal_states(active["profile_id"])
        if value["signal_type"] == "target_assessment"
        and value["client_rule_id"] == add_rule["client_rule_id"]
    )
    assert state["episode"] == 1
    assert state["state"] == "approaching"


def test_near_target_uses_near_trigger_tier() -> None:
    plan, _evidence, _blocked = MonitoringPlanner(FakeMarketService()).build(
        {"symbol": "600036.SH", "quantity": 1, "cost_price": 38}
    )
    assert plan is not None
    plan["quote_tier"] = "normal"
    plan["near_trigger_tier"] = "active"
    plan["near_trigger_distance_bps"] = 100
    plan["market_rules"] = [copy.deepcopy(plan["market_rules"][0])]
    plan["market_rules"][0]["parameters"]["threshold"] = 40.2

    assert MonitoringRuntime._effective_quote_tier(plan, {"last_price": 40.0}) == "active"
    assert MonitoringRuntime._effective_quote_tier(plan, {"last_price": 38.0}) == "normal"


class _StagedVolumeRuleMarketStore:
    def __init__(self) -> None:
        self.stage = 0

    def query_bars(self, **kwargs):
        if kwargs["interval"] == "1D":
            return [{
                "bar_time": "2026-07-13T07:00:00+00:00",
                "session_date": "2026-07-13",
                "close": 39,
                "status": "verified",
                "sources": ["tencent", "mootdx"],
            }]
        hhmm = ("01:35", "01:40", "01:45", "01:50", "01:55")[self.stage]
        return [{
            "bar_time": f"2026-07-14T{hhmm}:00+00:00",
            "session_date": "2026-07-14",
            "open": 39.9,
            "high": 40.1,
            "low": 39.8,
            "close": 40,
            "volume": 1000,
            "status": "verified",
            "sources": ["tencent", "mootdx"],
            "verified_at": f"2026-07-14T{hhmm}:10+00:00",
        }]


class _StagedVolumeRuleMarketService(FakeMarketService):
    def __init__(self) -> None:
        super().__init__()
        self.store = _StagedVolumeRuleMarketStore()


class _SequenceVolumeAnalyzer:
    def __init__(self) -> None:
        self.values = iter((2.0, 2.0, 2.0, 0.5, 2.0))
        self.calls = 0

    def analyze(self, **_kwargs):
        self.calls += 1
        ratio = next(self.values)
        return ({
            "status": "insufficient_data" if self.calls == 1 else "ready",
            "regime": None if self.calls == 1 else "neutral",
            "volume_state": None if self.calls == 1 else "expanded" if ratio >= 1.5 else "contracted",
            "volume_ratio": ratio,
            "baseline_samples": 10,
            "three_bar_return_bps": None,
            "latest_return_bps": None,
            "close_location": None,
            "accelerated_decline": False,
            "reason_codes": ["insufficient_same_time_baseline"] if self.calls == 1 else ["test_ratio"],
        }, None)


def test_explicit_volume_ratio_rule_triggers_clears_and_rearms_when_price_volume_mode_is_off(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    target = service.store.bind_target(channel="feishu", chat_id="ou_volume_rule")
    item = service.create_draft_batch(["600036.SH"], target["target_id"])["items"][0]
    profile = service.store.get_profile(item["profile_id"])
    assert profile is not None
    plan = copy.deepcopy(profile["plans"][0]["plan"])
    plan["market_rules"].append({
        "client_rule_id": "volume-expansion",
        "kind": "volume_ratio_above",
        "severity": "warning",
        "enabled": True,
        "parameters": {
            "ratio": 1.5,
            "clear_ratio": 1.0,
            "interval": "5m",
            "adjustment": "raw",
            "confirmation_count": 1,
            "cooldown_minutes": 60,
            "clear_hysteresis_bps": 0,
            "baseline_sessions": 10,
            "min_samples": 5,
        },
        "valid_until": plan["hard_valid_until"],
        "rationale": "test",
    })
    service.store.update_draft(
        item["profile_id"],
        item["plan_version"],
        plan,
        expected_revision=profile["profile_revision"],
    )
    activated = service.store.activate(item["profile_id"], item["plan_version"], max_active=10)
    monkeypatch.setenv("VIBE_TRADING_MONITORING_ENABLED", "1")
    monkeypatch.setenv("VIBE_TRADING_MONITORING_MODE", "shadow")
    monkeypatch.setenv("VIBE_TRADING_MONITOR_PRICE_VOLUME_MODE", "off")
    monkeypatch.setenv("VIBE_TRADING_MONITOR_MAINTENANCE_ENABLED", "0")
    market = _StagedVolumeRuleMarketService()
    analyzer = _SequenceVolumeAnalyzer()
    current_time = [datetime(2026, 7, 14, 9, 50, tzinfo=ZoneInfo("Asia/Shanghai"))]
    runtime = MonitoringRuntime(
        store=service.store,
        market_service=market,
        calendar=_Calendar(True, "cached_exchange_calendar"),
        now_factory=lambda: current_time[0],
        price_volume_analyzer=analyzer,
    )

    first_rule_state = None
    for stage in range(5):
        market.store.stage = stage
        current_time[0] = datetime(
            2026, 7, 14, 9, 50, tzinfo=ZoneInfo("Asia/Shanghai")
        ) + timedelta(minutes=stage * 5)
        with service.store.connect() as connection:
            connection.execute(
                "UPDATE monitor_profiles SET next_quote_run_at=NULL WHERE profile_id=?",
                (activated["profile_id"],),
            )
        asyncio.run(runtime.run_once())
        if stage == 0:
            with service.store.connect() as connection:
                first_rule_state = connection.execute(
                    """SELECT state FROM monitor_rules
                       WHERE profile_id=? AND client_rule_id='volume-expansion'""",
                    (activated["profile_id"],),
                ).fetchone()[0]

    volume_events = [
        event for event in service.store.list_events(limit=20)
        if event["facts"].get("rule_kind") == "volume_ratio_above"
    ]
    assert len(volume_events) == 2
    assert first_rule_state == "data_blocked"
    assert analyzer.calls == 5
    assert service.store.list_signal_states(activated["profile_id"]) == []
    last_quote = service.store.get_profile(activated["profile_id"])["last_quote"]
    assert last_quote["price_volume"]["status"] == "disabled"
    assert last_quote["volume_ratio"] == pytest.approx(2.0)


def test_feishu_price_volume_copy_is_gated_and_keeps_action_boundaries(
    monkeypatch,
) -> None:
    add_facts = {
        "price_volume": _ready_price_volume(accelerated=True),
        "target_assessment": {
            "target_intent": "add_position",
            "target_level": 1,
            "phase": "approaching",
            "distance_bps": 42,
            "decision": "opposes_add",
        },
    }
    monkeypatch.setenv("VIBE_TRADING_MONITOR_PRICE_VOLUME_MODE", "shadow")
    assert api_server._format_monitor_price_volume_lines(add_facts) == []

    monkeypatch.setenv("VIBE_TRADING_MONITOR_PRICE_VOLUME_MODE", "deliver")
    add_copy = "\n".join(api_server._format_monitor_price_volume_lines(add_facts))
    assert "放量加速下跌，不宜补仓" in add_copy

    take_profit_facts = {
        "price_volume": {
            **_ready_price_volume(accelerated=False),
            "regime": "bullish_expansion",
            "volume_state": "expanded",
            "volume_ratio": 1.8,
        },
        "target_assessment": {
            "target_intent": "take_profit",
            "target_level": 1,
            "phase": "reached",
            "distance_bps": 0,
            "decision": "no_confirmation",
        },
    }
    take_profit_copy = "\n".join(
        api_server._format_monitor_price_volume_lines(take_profit_facts)
    )
    assert "动能仍强，尚未出现衰竭证据" in take_profit_copy
    assert "不否定或延迟原价格提醒" in take_profit_copy
    assert "禁止止盈" not in take_profit_copy
