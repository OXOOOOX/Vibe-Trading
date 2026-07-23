from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.channels.bus.events import DeliveryReceipt
from src.portfolio.monitoring.runtime import MonitoringRuntime
from src.portfolio.monitoring.store import MonitoringStore, StaleLeaderError
from tests.test_portfolio_monitoring import (
    FakeMarketService,
    _Calendar,
    _RuntimeMarketService,
    _activate,
    _activate_single_cross_above,
    _closed_quote,
    _service,
)


def test_expired_weekly_source_blocks_runtime_evaluation_without_disabling_plan(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    profile = _activate(service)
    active_version = int(profile["active_plan_version"])
    stored = service.store.get_plan(profile["profile_id"], active_version)
    plan = stored["plan"]
    plan.update(
        source_horizon="weekly",
        source_report_id="weekly-expired",
        source_valid_until="2026-07-17T07:30:00+00:00",
        review_due_at="2026-07-17T07:30:00+00:00",
    )
    with service.store.connect() as connection:
        connection.execute(
            "UPDATE monitor_plan_versions SET plan_json=? WHERE profile_id=? AND version=?",
            (json.dumps(plan, ensure_ascii=False), profile["profile_id"], active_version),
        )
    monkeypatch.setenv("VIBE_TRADING_MONITORING_ENABLED", "1")
    monkeypatch.setenv("VIBE_TRADING_MONITORING_MODE", "shadow")
    monkeypatch.setenv("VIBE_TRADING_MONITOR_MAINTENANCE_ENABLED", "0")
    runtime = MonitoringRuntime(
        store=service.store,
        market_service=_RuntimeMarketService(),
        calendar=_Calendar(True, "cached_exchange_calendar"),
        now_factory=lambda: datetime(2026, 7, 18, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    result = asyncio.run(runtime.run_once())
    after = service.store.get_profile(profile["profile_id"])

    assert result["source_review_due_profiles"] == 1
    assert result["evaluated_profiles"] == 0
    assert after["status"] == "active"
    assert after["active_plan_version"] == active_version
    assert "source_report_review_due" in after["blocked_reasons"]
    assert service.store.pending_deliveries() == []
    asyncio.run(runtime.stop())


def _create_pending_delivery(service):
    profile, rule = _activate_single_cross_above(service)
    threshold = float(rule["parameters"]["threshold"])
    for price, bar_time in (
        (threshold - 1, "2026-07-14T02:05:00+00:00"),
        (threshold + 1, "2026-07-14T02:10:00+00:00"),
        (threshold + 1, "2026-07-14T02:15:00+00:00"),
    ):
        service.store.evaluate_quote(
            profile["profile_id"],
            _closed_quote(price, bar_time),
            delivery_mode="deliver",
        )
    assert len(service.store.pending_deliveries()) == 1
    return profile


def _enable_canary(monkeypatch, profile) -> None:
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


def test_delivery_readiness_private_test_and_receipt_are_fail_closed(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    profile = _create_pending_delivery(service)
    delivered_events: list[str] = []

    async def deliver(event, delivery):
        delivered_events.append(str(event["kind"]))
        return DeliveryReceipt(
            provider="feishu",
            remote_message_id=f"om-{delivery['delivery_id']}",
            provider_request_id=str(delivery["delivery_id"]),
            accepted_at="2026-07-15T01:00:00+00:00",
        )

    monkeypatch.setenv("VIBE_TRADING_MONITORING_ENABLED", "1")
    monkeypatch.setenv("VIBE_TRADING_MONITORING_MODE", "deliver")
    runtime = MonitoringRuntime(
        store=service.store,
        market_service=FakeMarketService(),
        delivery_callback=deliver,
    )
    assert runtime.status()["mode"] == "shadow"
    assert "shadow_soak_not_approved" in runtime.deliver_readiness()["blocked_reasons"]

    _enable_canary(monkeypatch, profile)
    assert runtime.deliver_readiness()["ready"] is True
    test_receipt = asyncio.run(runtime.send_test_delivery())
    assert test_receipt["remote_message_id"].startswith("om-monitor-test-")
    assert delivered_events == ["delivery_test"]

    # A normal stop in deliver mode retains an unsent durable outbox.
    asyncio.run(runtime.stop())
    assert len(service.store.pending_deliveries()) == 1
    asyncio.run(runtime._deliver_pending("deliver"))
    event = service.store.list_events()[0]
    delivery = service.store.get_event(event["event_id"])["deliveries"][0]
    assert delivery["status"] == "delivered"
    assert delivery["provider"] == "feishu"
    assert delivery["remote_message_id"].startswith("om-")
    assert delivery["provider_request_id"] == delivery["delivery_id"]
    assert delivery["accepted_at"] == "2026-07-15T01:00:00+00:00"


def test_fencing_token_prevents_an_expired_leader_from_scheduling(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    profile = _activate(service)
    lease_key = "portfolio_monitoring_runtime"
    token_one = service.store.acquire_fenced_lease(lease_key, "owner-one")
    assert token_one is not None
    assert service.store.claim_profiles(
        lease_key=lease_key,
        owner_id="owner-one",
        fencing_token=token_one,
        tick_id="tick-one",
        profile_ids=[profile["profile_id"]],
    ) == {profile["profile_id"]}
    with service.store.connect() as connection:
        connection.execute(
            "UPDATE runtime_leases SET expires_at='2000-01-01T00:00:00+00:00' WHERE lease_key=?",
            (lease_key,),
        )
        connection.execute(
            "UPDATE monitor_profile_claims SET expires_at='2000-01-01T00:00:00+00:00' WHERE profile_id=?",
            (profile["profile_id"],),
        )
    token_two = service.store.acquire_fenced_lease(lease_key, "owner-two")
    assert token_two is not None and token_two > token_one
    assert service.store.claim_profiles(
        lease_key=lease_key,
        owner_id="owner-two",
        fencing_token=token_two,
        tick_id="tick-two",
        profile_ids=[profile["profile_id"]],
    ) == {profile["profile_id"]}

    with pytest.raises(StaleLeaderError, match="lease"):
        service.store.schedule_next(
            profile["profile_id"],
            seconds=60,
            success=True,
            lease_guard={
                "lease_key": lease_key,
                "owner_id": "owner-one",
                "fencing_token": token_one,
                "tick_id": "tick-one",
            },
        )


def test_profile_tick_outcome_is_insert_only_and_counts_duplicates(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    profile = _activate(service)
    assert service.store.record_profile_tick_outcome(
        tick_id="tick-terminal",
        profile_id=profile["profile_id"],
        status="evaluated",
        reason_code="quote_evaluated",
    ) is True
    assert service.store.record_profile_tick_outcome(
        tick_id="tick-terminal",
        profile_id=profile["profile_id"],
        status="blocked",
        reason_code="must_not_overwrite",
    ) is False
    outcome = service.store.profile_tick_outcomes("tick-terminal")[0]
    assert outcome["status"] == "evaluated"
    assert outcome["reason_code"] == "quote_evaluated"
    assert service.store.counter_value("duplicate_profile_tick_outcome_count") == 1


def test_daily_delivery_limit_uses_shanghai_day_and_counts_uncertain(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    profile = _create_pending_delivery(service)
    first = service.store.pending_deliveries()[0]
    assert service.store.claim_delivery(first["delivery_id"])

    local_now = datetime.now(ZoneInfo("Asia/Shanghai"))
    day_start_utc = local_now.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    ).astimezone(timezone.utc)
    with service.store.connect() as connection:
        connection.execute(
            """UPDATE delivery_outbox
               SET status='delivery_uncertain',claimed_at=? WHERE delivery_id=?""",
            ((day_start_utc + timedelta(minutes=1)).isoformat(), first["delivery_id"]),
        )

    # A separate health episode produces another pending alert for the same profile.
    service.store.record_data_health(
        profile["profile_id"],
        healthy=False,
        reason_code="fresh_verified_quote_unavailable",
        delivery_mode="deliver",
    )
    service.store.record_data_health(
        profile["profile_id"],
        healthy=False,
        reason_code="fresh_verified_quote_unavailable",
        delivery_mode="deliver",
    )
    second = service.store.pending_deliveries()[0]
    assert service.store.claim_delivery(second["delivery_id"], daily_limit=1) is False
    with service.store.connect() as connection:
        connection.execute(
            "UPDATE delivery_outbox SET claimed_at=? WHERE delivery_id=?",
            ((day_start_utc - timedelta(minutes=1)).isoformat(), first["delivery_id"]),
        )
    assert service.store.claim_delivery(second["delivery_id"], daily_limit=1) is True


def test_stale_delivering_is_recovered_only_after_timeout(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service(tmp_path, monkeypatch)
    _create_pending_delivery(service)
    pending = service.store.pending_deliveries()[0]
    assert service.store.claim_delivery(pending["delivery_id"])
    assert service.store.recover_stale_deliveries(timeout_seconds=180) == 0
    with service.store.connect() as connection:
        connection.execute(
            "UPDATE delivery_outbox SET claimed_at='2000-01-01T00:00:00+00:00' WHERE delivery_id=?",
            (pending["delivery_id"],),
        )
    assert service.store.recover_stale_deliveries(timeout_seconds=180) == 1
    delivery = service.store.get_event(pending["event_id"])["deliveries"][0]
    assert delivery["status"] == "delivery_uncertain"
    assert delivery["receipt_status"] == "delivery_uncertain"
    reconciled = service.store.reconcile_uncertain_delivery(
        pending["delivery_id"],
        status="delivered",
        remote_message_id="om-manually-confirmed",
        note="verified in Feishu message history",
    )
    assert reconciled["status"] == "delivered"
    assert reconciled["remote_message_id"] == "om-manually-confirmed"
    with pytest.raises(ValueError, match="not awaiting"):
        service.store.reconcile_uncertain_delivery(
            pending["delivery_id"],
            status="rejected",
            note="must not reconcile twice",
        )


def test_data_health_opens_once_and_recovers_once(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    profile = _activate(service)
    kwargs = {
        "reason_code": "fresh_verified_quote_unavailable",
        "delivery_mode": "shadow",
    }
    assert service.store.record_data_health(profile["profile_id"], healthy=False, **kwargs) == []
    opened = service.store.record_data_health(profile["profile_id"], healthy=False, **kwargs)
    assert [event["kind"] for event in opened] == ["data_source_unavailable"]
    assert service.store.record_data_health(profile["profile_id"], healthy=False, **kwargs) == []
    recovered = service.store.record_data_health(
        profile["profile_id"],
        healthy=True,
        reason_code="fresh_quote_available",
        delivery_mode="shadow",
    )
    assert [event["kind"] for event in recovered] == ["data_source_recovered"]
    assert service.store.record_data_health(
        profile["profile_id"],
        healthy=True,
        reason_code="fresh_quote_available",
        delivery_mode="shadow",
    ) == []


def test_failed_schema_migration_restores_online_backup(tmp_path) -> None:
    path = tmp_path / "monitoring.sqlite3"
    store = MonitoringStore(path)
    with store.connect() as connection:
        connection.execute("UPDATE schema_meta SET value='5' WHERE key='schema_version'")

    class FailingMigrationStore(MonitoringStore):
        def initialize(self) -> None:
            super().initialize()
            raise RuntimeError("simulated migration failure")

    with pytest.raises(RuntimeError, match="simulated migration failure"):
        FailingMigrationStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "5"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    backups = list((tmp_path / "migration_backups").glob("*.sqlite3"))
    assert len(backups) == 1


def test_future_schema_fails_closed_without_modifying_database(tmp_path) -> None:
    path = tmp_path / "monitoring.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO schema_meta(key,value) VALUES('schema_version','11');
            CREATE TABLE future_schema_marker (value TEXT NOT NULL);
            INSERT INTO future_schema_marker(value) VALUES('keep-me');
            """
        )
    before = path.read_bytes()

    with pytest.raises(RuntimeError, match="newer than supported version 10"):
        MonitoringStore(path)

    assert path.read_bytes() == before
    assert not (tmp_path / "migration_backups").exists()
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "11"
        assert connection.execute(
            "SELECT value FROM future_schema_marker"
        ).fetchone()[0] == "keep-me"
