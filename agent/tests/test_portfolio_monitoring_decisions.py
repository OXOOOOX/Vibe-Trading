from __future__ import annotations

from pathlib import Path

from src.portfolio.monitoring.decisions import DecisionEngine, validate_risk_preference
from src.portfolio.monitoring.store import MonitoringStore


def _snapshot() -> dict:
    return {
        "level_snapshot_id": "levels-1",
        "level_ladder": {
            "support": [
                {
                    "candidate_id": "support-s1",
                    "role": "S1",
                    "lower": 39.5,
                    "upper": 40.0,
                    "score": 82,
                    "invalidation": {"value": 38.5},
                },
                {
                    "candidate_id": "support-s2",
                    "role": "S2",
                    "lower": 36.0,
                    "upper": 36.5,
                    "score": 78,
                    "invalidation": {"value": 34.8},
                },
            ],
            "resistance": [
                {
                    "candidate_id": "resistance-r1",
                    "role": "R1",
                    "lower": 45.0,
                    "upper": 45.5,
                    "score": 80,
                    "invalidation": {"value": 46.2},
                }
            ],
        },
    }


def _preference() -> dict:
    return {
        **validate_risk_preference(
            {
                "holding_period": "swing",
                "max_risk_amount": 3000,
                "max_add_amount": 50_000,
                "max_position_amount": 100_000,
                "minimum_reward_risk": 2,
                "condition_order_permission": "local_draft",
                "sellable_quantity": 1000,
                "default_reduce_fraction": 0.3,
            }
        ),
        "revision": 1,
    }


def test_decision_brief_separates_support_touch_from_confirmed_action() -> None:
    engine = DecisionEngine()
    profile = {
        "last_quote": {"payload": {"last_price": 39.8}},
        "watch_episodes": [
            {
                "episode_id": "episode-1",
                "client_rule_id": "support-zone-test",
                "state": "testing",
                "updated_at": "2026-07-23T02:00:00+00:00",
                "facts": {"scenario": {"client_rule_id": "support-zone-test"}},
            }
        ],
    }
    decision = engine.build(
        symbol="000651.SZ",
        name="格力电器",
        profile_status="active",
        blockers=[],
        continuity={"status": "continuous"},
        volume_gate={"status": "pending_evidence"},
        snapshot=_snapshot(),
        market_evidence={},
        profile=profile,
        holding={"quantity": 1000},
        risk_preference=None,
        latest_draft=None,
    )

    brief = decision["decision_brief"]
    assert brief["market_state"] == "testing_support"
    assert brief["recommended_choice_id"] == "wait_confirmation"
    assert len(brief["why_now"]) <= 3
    assert {item["role"] for item in decision["level_ladder"]["support"]} >= {"S1", "S2"}
    assert decision["risk_assessment"]["risk_probability"] is None


def test_local_draft_uses_user_risk_budget_and_never_submits_order() -> None:
    engine = DecisionEngine()
    preference = _preference()
    profile = {
        "last_quote": {"payload": {"last_price": 40.0}},
        "watch_episodes": [
            {
                "episode_id": "episode-confirmed",
                "client_rule_id": "support-zone-test",
                "state": "confirmed",
                "facts": {"scenario": {"client_rule_id": "support-zone-test"}},
            }
        ],
    }
    decision = engine.build(
        symbol="000651.SZ",
        name="格力电器",
        profile_status="active",
        blockers=[],
        continuity={"status": "continuous"},
        volume_gate={"status": "ready"},
        snapshot=_snapshot(),
        market_evidence={},
        profile=profile,
        holding={"quantity": 1000},
        risk_preference=preference,
        latest_draft=None,
    )

    draft = engine.create_draft(
        decision=decision,
        choice_id="generate_add_draft",
        risk_preference=preference,
        holding={"quantity": 1000},
        cash=100_000,
    )

    assert draft["status"] == "draft"
    assert draft["quantity"] == 1200
    assert draft["quantity_formula"]["risk_quantity_cap"] == 2000
    assert draft["order_submission"] == "forbidden"
    assert draft["trade_execution"] == "forbidden"

    missing = engine.create_draft(
        decision=decision,
        choice_id="generate_add_draft",
        risk_preference=None,
        holding={"quantity": 1000},
        cash=100_000,
    )
    assert missing["status"] == "needs_risk_preferences"
    assert missing["quantity"] is None


def test_schema_v10_persists_preferences_choices_and_stales_old_drafts(tmp_path: Path) -> None:
    store = MonitoringStore(tmp_path / "monitoring.sqlite3")
    preference = store.save_risk_preference("000651.SZ", _preference())
    assert preference["revision"] == 1

    choice = store.record_decision_choice(
        {
            "decision_id": "decision-12345678",
            "symbol": "000651.SZ",
            "choice_id": "continue_monitoring",
            "decision_revision": 1,
            "evidence_fingerprint": "a" * 64,
            "idempotency_key": "idempotency-12345678",
            "status": "recorded",
        }
    )
    duplicate = store.record_decision_choice(
        {
            **choice,
            "choice_record_id": "different",
        }
    )
    assert duplicate["choice_record_id"] == choice["choice_record_id"]

    draft = store.save_condition_order_draft(
        {
            "decision_id": "decision-12345678",
            "symbol": "000651.SZ",
            "side": "buy",
            "status": "draft",
            "evidence_fingerprint": "a" * 64,
            "valid_until": "2026-07-24T00:00:00+00:00",
            "quantity": 100,
            "trade_execution": "forbidden",
            "order_submission": "forbidden",
        }
    )
    assert store.get_condition_order_draft(draft["draft_id"])["status"] == "draft"

    store.save_risk_preference("000651.SZ", _preference())
    assert store.get_condition_order_draft(draft["draft_id"])["status"] == "stale"
    with store.connect() as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "10"
