from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import sqlite3
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

import api_server
from src.portfolio.monitoring.models import PlanValidationError, validate_plan
from src.portfolio.monitoring.store import MonitoringStore


def _plan(*, now: datetime | None = None) -> dict:
    current = now or datetime.now(timezone.utc)
    valid_until = (current + timedelta(days=45)).isoformat()
    return {
        "schema_version": 1,
        "symbol": "600036.SH",
        "data_mode": "verified",
        "quote_tier": "normal",
        "near_trigger_tier": "active",
        "near_trigger_distance_bps": 100,
        "market_rules": [
            {
                "client_rule_id": "take-profit-l1",
                "kind": "price_cross_above",
                "severity": "warning",
                "enabled": True,
                "target_intent": "take_profit",
                "target_level": 1,
                "parameters": {
                    "threshold": 41.0,
                    "interval": "5m",
                    "adjustment": "raw",
                    "confirmation_count": 2,
                    "cooldown_minutes": 60,
                    "clear_hysteresis_bps": 30,
                },
                "valid_until": valid_until,
            },
            {
                "client_rule_id": "take-profit-l2",
                "kind": "price_cross_above",
                "severity": "warning",
                "enabled": True,
                "target_intent": "take_profit",
                "target_level": 2,
                "parameters": {
                    "threshold": 43.0,
                    "interval": "5m",
                    "adjustment": "raw",
                    "confirmation_count": 2,
                    "cooldown_minutes": 60,
                    "clear_hysteresis_bps": 30,
                },
                "valid_until": valid_until,
            },
        ],
        "news_topics": [],
        "fundamental_monitor": {"enabled": False},
        "hard_valid_until": (current + timedelta(days=90)).isoformat(),
    }


def _draft(store: MonitoringStore, plan: dict | None = None) -> tuple[str, int]:
    target = store.bind_target(channel="feishu", chat_id="ou_plan_safety")
    return store.save_draft(
        symbol="600036.SH",
        market="SH",
        instrument_type="company_equity",
        plan=plan or _plan(),
        evidence_manifest={"data_as_of": datetime.now(timezone.utc).isoformat()},
        input_snapshot_hash="holding-v1",
        delivery_target_id=target["target_id"],
        model_id="evidence-policy-v3",
    )


def test_save_and_activate_api_is_atomic_and_revision_guarded(tmp_path, monkeypatch) -> None:
    store = MonitoringStore(tmp_path / "monitoring.sqlite3")
    profile_id, version = _draft(store)
    profile = store.get_profile(profile_id)
    assert profile is not None
    edited = copy.deepcopy(profile["plans"][0]["plan"])
    edited["market_rules"][0]["parameters"]["threshold"] = 42.0

    monkeypatch.setattr(
        api_server,
        "_portfolio_monitoring_service",
        SimpleNamespace(store=store),
    )
    client = TestClient(api_server.app, client=("127.0.0.1", 50000))
    response = client.post(
        f"/portfolio/monitors/{profile_id}/plans/{version}/save-and-activate",
        headers={"If-Match": f'"{profile["profile_revision"]}"'},
        json={"plan": edited, "expected_revision": profile["profile_revision"]},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "active"
    assert response.json()["profile_revision"] == profile["profile_revision"] + 1
    saved = store.get_plan(profile_id, version)
    assert saved is not None
    assert saved["status"] == "active"
    assert saved["plan"]["market_rules"][0]["parameters"]["threshold"] == 42.0


def test_save_and_activate_conflict_rolls_back_the_plan_edit(tmp_path, monkeypatch) -> None:
    store = MonitoringStore(tmp_path / "monitoring.sqlite3")
    profile_id, version = _draft(store)
    profile = store.get_profile(profile_id)
    assert profile is not None
    original = copy.deepcopy(profile["plans"][0]["plan"])
    edited = copy.deepcopy(original)
    edited["market_rules"][0]["parameters"]["threshold"] = 42.0

    monkeypatch.setattr(
        api_server,
        "_portfolio_monitoring_service",
        SimpleNamespace(store=store),
    )
    client = TestClient(api_server.app, client=("127.0.0.1", 50001))
    response = client.post(
        f"/portfolio/monitors/{profile_id}/plans/{version}/save-and-activate",
        headers={"If-Match": f'"{profile["profile_revision"] + 1}"'},
        json={"plan": edited, "expected_revision": profile["profile_revision"] + 1},
    )

    assert response.status_code == 409
    persisted = store.get_plan(profile_id, version)
    assert persisted is not None
    assert persisted["status"] == "pending_review"
    assert persisted["plan"] == original


def test_legacy_activate_honors_optional_if_match_and_keeps_no_header_compatible(
    tmp_path,
    monkeypatch,
) -> None:
    store = MonitoringStore(tmp_path / "monitoring.sqlite3")
    profile_id, version = _draft(store)
    profile = store.get_profile(profile_id)
    assert profile is not None
    monkeypatch.setattr(
        api_server,
        "_portfolio_monitoring_service",
        SimpleNamespace(store=store),
    )
    client = TestClient(api_server.app, client=("127.0.0.1", 50002))
    endpoint = f"/portfolio/monitors/{profile_id}/plans/{version}/activate"

    conflict = client.post(
        endpoint,
        headers={"If-Match": str(profile["profile_revision"] + 1)},
    )

    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error_code"] == "profile_revision_conflict"
    assert store.get_plan(profile_id, version)["status"] == "pending_review"

    compatible = client.post(endpoint)

    assert compatible.status_code == 200
    assert compatible.json()["status"] == "active"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda plan, now: plan.update(
                hard_valid_until=(now + timedelta(days=90)).replace(tzinfo=None).isoformat()
            ),
            "timezone offset",
        ),
        (
            lambda plan, now: plan.update(
                hard_valid_until=(now + timedelta(days=29)).isoformat()
            ),
            "between 30 and 365 days",
        ),
        (
            lambda plan, now: plan.update(
                hard_valid_until=(now + timedelta(days=366)).isoformat()
            ),
            "between 30 and 365 days",
        ),
        (
            lambda plan, now: plan["market_rules"][0].update(valid_until=None),
            "valid_until is required",
        ),
        (
            lambda plan, now: plan["market_rules"][0].update(
                valid_until=(now + timedelta(days=45)).replace(tzinfo=None).isoformat()
            ),
            "timezone offset",
        ),
        (
            lambda plan, now: plan["market_rules"][0].update(
                valid_until=(now + timedelta(days=100)).isoformat()
            ),
            "cannot exceed hard_valid_until",
        ),
    ],
)
def test_activation_rejects_invalid_validity_windows(tmp_path, mutate, message) -> None:
    now = datetime.now(timezone.utc)
    plan = _plan(now=now)
    mutate(plan, now)
    store = MonitoringStore(tmp_path / "monitoring.sqlite3")
    profile_id, version = _draft(store, plan)

    with pytest.raises(PlanValidationError, match=message):
        store.activate(profile_id, version, max_active=10)

    assert store.get_plan(profile_id, version)["status"] == "pending_review"


def test_target_ladder_requires_l2_to_be_farther_than_l1() -> None:
    plan = _plan()
    plan["market_rules"][1]["parameters"]["threshold"] = 40.0

    with pytest.raises(PlanValidationError, match="L2 must be strictly higher than L1"):
        validate_plan(plan, expected_symbol="600036.SH")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda plan: plan["market_rules"][0].update(enabled="false"),
            "enabled must be a boolean",
        ),
        (
            lambda plan: plan["market_rules"][0]["parameters"].update(threshold=True),
            "threshold must be a number",
        ),
        (
            lambda plan: plan["market_rules"][0]["parameters"].update(
                confirmation_count=1.9
            ),
            "confirmation_count must be an integer",
        ),
        (
            lambda plan: plan["market_rules"][0].update(parameters=[]),
            "parameters must be an object",
        ),
        (
            lambda plan: plan["market_rules"][0].update(target_intent="stop_loss"),
            "stop_loss conflicts with an upward trigger",
        ),
        (
            lambda plan: plan["market_rules"][0].update(
                kind="price_cross_below", target_intent="take_profit"
            ),
            "take_profit conflicts with a downward trigger",
        ),
        (
            lambda plan: plan.update(fundamental_monitor={"enabled": "false"}),
            "fundamental_monitor.enabled must be a boolean",
        ),
    ],
)
def test_plan_inputs_fail_closed_instead_of_being_coerced(mutate, message) -> None:
    plan = _plan()
    mutate(plan)

    with pytest.raises(PlanValidationError, match=message):
        validate_plan(plan, expected_symbol="600036.SH")


def test_new_reanalysis_draft_supersedes_older_pending_review(tmp_path) -> None:
    store = MonitoringStore(tmp_path / "monitoring.sqlite3")
    profile_id, first_version = _draft(store)
    second_profile_id, second_version = store.save_draft(
        symbol="600036.SH",
        market="SH",
        instrument_type="company_equity",
        plan=_plan(),
        evidence_manifest={"data_as_of": datetime.now(timezone.utc).isoformat()},
        input_snapshot_hash="holding-v1",
        delivery_target_id=None,
        model_id="evidence-policy-v3",
    )

    assert second_profile_id == profile_id
    assert second_version == first_version + 1
    plans = store.get_profile(profile_id)["plans"]
    assert [plan["status"] for plan in plans].count("pending_review") == 1
    assert store.get_plan(profile_id, first_version)["status"] == "superseded"
    assert store.get_plan(profile_id, first_version)["superseded_at"] is not None


def test_initialize_repairs_legacy_multiple_pending_reviews(tmp_path) -> None:
    path = tmp_path / "monitoring.sqlite3"
    store = MonitoringStore(path)
    profile_id, first_version = _draft(store)
    _, second_version = store.save_draft(
        symbol="600036.SH",
        market="SH",
        instrument_type="company_equity",
        plan=_plan(),
        evidence_manifest={"data_as_of": datetime.now(timezone.utc).isoformat()},
        input_snapshot_hash="holding-v1",
        delivery_target_id=None,
        model_id="evidence-policy-v3",
    )
    connection = store.connect()
    try:
        connection.execute("DROP INDEX idx_monitor_plan_single_pending")
        connection.execute(
            """UPDATE monitor_plan_versions
               SET status='pending_review', superseded_at=NULL
               WHERE profile_id=? AND version=?""",
            (profile_id, first_version),
        )
        connection.commit()
    finally:
        connection.close()

    repaired = MonitoringStore(path)
    plans = repaired.get_profile(profile_id)["plans"]
    assert [plan["status"] for plan in plans].count("pending_review") == 1
    assert repaired.get_plan(profile_id, second_version)["status"] == "pending_review"
    assert repaired.get_plan(profile_id, first_version)["status"] == "superseded"
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        with repaired.transaction() as connection:
            connection.execute(
                """UPDATE monitor_plan_versions SET status='pending_review'
                   WHERE profile_id=? AND version=?""",
                (profile_id, first_version),
            )


def test_expiry_creates_audit_events_and_one_reviewable_renewal(tmp_path) -> None:
    store = MonitoringStore(tmp_path / "monitoring.sqlite3")
    profile_id, version = _draft(store)
    store.activate(profile_id, version, max_active=10)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    connection = store.connect()
    try:
        connection.execute(
            """UPDATE monitor_plan_versions SET hard_valid_until=?
               WHERE profile_id=? AND version=?""",
            (past, profile_id, version),
        )
        connection.execute(
            """UPDATE monitor_rules SET valid_until=?
               WHERE profile_id=? AND plan_version=?""",
            (past, profile_id, version),
        )
        connection.commit()
    finally:
        connection.close()

    store.maintain_profiles({"600036.SH": "holding-v1"})

    profile = store.get_profile(profile_id)
    assert profile is not None
    assert profile["status"] == "expired"
    assert profile["plans"][0]["status"] == "pending_review"
    assert profile["plans"][0]["created_by"] == "monitor_renewal"
    assert profile["plans"][0]["evidence_manifest"]["renewal"] == {
        "source_plan_version": version,
        "created_at": profile["plans"][0]["created_at"],
        "reason": "plan_or_rule_expired",
        "requires_human_review": True,
    }
    events = store.list_events(limit=20)
    assert {event["kind"] for event in events} == {"plan_expired", "rule_expired"}
    assert all(event["facts"]["lifecycle"] is True for event in events)
    assert store.pending_deliveries() == []


def test_single_rule_expiry_is_idempotent_while_other_rules_keep_running(tmp_path) -> None:
    store = MonitoringStore(tmp_path / "monitoring.sqlite3")
    profile_id, version = _draft(store)
    store.activate(profile_id, version, max_active=10)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    connection = store.connect()
    try:
        connection.execute(
            """UPDATE monitor_rules SET valid_until=?
               WHERE profile_id=? AND plan_version=? AND client_rule_id='take-profit-l1'""",
            (past, profile_id, version),
        )
        connection.commit()
    finally:
        connection.close()

    store.maintain_profiles({"600036.SH": "holding-v1"})
    store.maintain_profiles({"600036.SH": "holding-v1"})

    profile = store.get_profile(profile_id)
    assert profile is not None
    assert profile["status"] == "active"
    assert [plan["status"] for plan in profile["plans"]].count("pending_review") == 1
    events = [event for event in store.list_events(limit=20) if event["kind"] == "rule_expired"]
    assert len(events) == 1
    assert events[0]["facts"]["client_rule_id"] == "take-profit-l1"
