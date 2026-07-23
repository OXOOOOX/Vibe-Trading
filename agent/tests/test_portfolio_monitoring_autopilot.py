from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from src.portfolio.monitoring.compound import CompoundConditionEvaluator
from src.portfolio.monitoring.evidence import AutonomousEvidenceCollector
from src.portfolio.monitoring.models import PlanValidationError, validate_plan
from src.portfolio.monitoring.recommendations import RecommendationResolver
from src.portfolio.monitoring.service import MonitoringService, _holding_hash
from src.portfolio.monitoring.store import MonitoringStore
from src.portfolio.state import PortfolioState


NOW = datetime(2026, 7, 16, 7, 10, tzinfo=timezone.utc)


def _condition(condition_id: str, source_id: str, kind: str, operator: str, **values):
    return {
        "condition_id": condition_id,
        "source_condition_id": source_id,
        "kind": kind,
        "operator": operator,
        "interval": values.pop("interval", "5m"),
        "consecutive": values.pop("consecutive", 1),
        "lookback_bars": values.pop("lookback_bars", 1),
        "freshness_seconds": values.pop("freshness_seconds", 900),
        **values,
    }


def _v5_plan(*, symbol: str = "159999.SZ", scenario_overrides=None):
    scenario = {
        "scenario_id": "weak-rebound",
        "client_rule_id": "report-weak-rebound",
        "label": "反弹至区间后30分钟收阴",
        "intent": "take_profit",
        "evidence_refs": ["report:scenario-2"],
        "original_level": {
            "kind": "zone",
            "lower": 1.175,
            "upper": 1.180,
            "unit": "CNY",
            "adjustment": "raw",
            "source_text": "反弹至 1.175-1.180 后回落",
        },
        "trigger": {
            "kind": "price_cross_above",
            "threshold": 1.175,
            "interval": "5m",
            "confirmation_count": 1,
        },
        "approach_policy": {"distance_bps": 100, "source": "report", "check_interval": "1m"},
        "volume_confirmation": {
            "metric": "same_bucket_5m_volume_ratio",
            "comparator": "lte",
            "threshold": 1.0,
            "min_samples": 5,
            "mode": "classify_only",
            "unit": "ratio",
        },
        "resolution_policy": {
            "rejection_hysteresis_bps": 30,
            "max_observation_bars": 6,
            "close_action": "unresolved",
        },
        "rationale": "报告原始条件完整映射",
        "source_conditions": [
            {
                "condition_id": "source-zone",
                "source_text": "反弹至 1.175-1.180",
                "role": "required",
                "coverage_status": "mapped",
                "reason": "",
                "evidence_refs": ["report:scenario-2"],
            },
            {
                "condition_id": "source-30m-bearish",
                "source_text": "触碰后 30 分 K 收阴",
                "role": "required",
                "coverage_status": "mapped",
                "reason": "",
                "evidence_refs": ["report:scenario-2"],
            },
        ],
        "entry_conditions": {
            "operator": "all",
            "conditions": [
                _condition(
                    "entry-zone", "source-zone", "price_zone", "between",
                    lower=1.175, upper=1.180,
                )
            ],
        },
        "confirmation_conditions": {
            "operator": "all",
            "conditions": [
                _condition(
                    "confirm-30m", "source-30m-bearish", "bar_direction", "equals",
                    interval="30m", direction="bearish", freshness_seconds=3600,
                )
            ],
        },
        "invalidation_conditions": {"operator": "all", "conditions": []},
        "sequence_policy": {"enabled": True, "max_wait_bars": 6, "reset_on_invalidation": True},
        "action_template": {
            "action": "reduce",
            "sizing": {"kind": "units", "value": 4000, "unit": "shares", "source": "report"},
            "confidence_floor": "medium",
        },
        "automation_status": "action_ready",
    }
    scenario.update(scenario_overrides or {})
    return {
        "schema_version": 5,
        "symbol": symbol,
        "data_mode": "verified",
        "summary": "report-driven autonomous monitor",
        "quote_tier": "normal",
        "near_trigger_tier": "active",
        "near_trigger_distance_bps": 100,
        "price_volume_policy": {
            "enabled": True,
            "interval": "5m",
            "baseline_method": "same_time_bucket_median",
            "baseline_sessions": 10,
            "min_samples": 5,
            "contraction_ratio": 0.8,
            "expansion_ratio": 1.5,
            "flat_return_bps": 10,
            "acceleration_multiplier": 1.2,
        },
        "analysis_ref": {
            "snapshot_id": "snapshot-1",
            "report_ref": "report-1",
            "report_type": "holding_analysis",
            "title": "holding report",
            "revision": 1,
            "body_sha256": "a" * 64,
            "quality_status": "ready",
            "generated_at": NOW.isoformat(),
            "data_as_of": NOW.isoformat(),
        },
        "watch_scenarios": [scenario],
        "market_rules": [
            {
                "client_rule_id": "report-weak-rebound",
                "kind": "price_cross_above",
                "severity": "warning",
                "enabled": True,
                "alert_cue": "none",
                "target_intent": "take_profit",
                "target_level": 1,
                "parameters": {
                    "threshold": 1.175,
                    "interval": "5m",
                    "adjustment": "raw",
                    "confirmation_count": 1,
                    "cooldown_minutes": 120,
                    "clear_hysteresis_bps": 30,
                },
                "valid_until": (NOW + timedelta(days=45)).isoformat(),
                "rationale": "approach sensor only",
            }
        ],
        "news_topics": [],
        "fundamental_monitor": {"enabled": False},
        "hard_valid_until": (NOW + timedelta(days=90)).isoformat(),
        "automation_policy": {
            "activation_mode": "autonomous",
            "activated_by": "autopilot",
            "evidence_fingerprint": "evidence-1",
            "trade_execution": "forbidden",
        },
    }


def test_v5_rejects_silent_condition_omission_and_hallucinated_metric():
    plan = _v5_plan()
    plan["watch_scenarios"][0]["confirmation_conditions"]["conditions"] = []
    with pytest.raises(PlanValidationError, match="mapped source conditions"):
        validate_plan(plan)

    plan = _v5_plan()
    source = plan["watch_scenarios"][0]["source_conditions"][1]
    source["coverage_status"] = "awaiting_data"
    plan["watch_scenarios"][0]["confirmation_conditions"]["conditions"] = []
    plan["watch_scenarios"][0]["automation_status"] = "action_ready"
    with pytest.raises(PlanValidationError, match="must be watch_only"):
        validate_plan(plan)

    plan = _v5_plan()
    plan["watch_scenarios"][0]["confirmation_conditions"]["conditions"][0] = _condition(
        "bad", "source-30m-bearish", "fund_flow", "positive", metric="imaginary_indicator"
    )
    with pytest.raises(PlanValidationError, match="metric is not allowed"):
        validate_plan(plan)


class _Bars:
    def __init__(self, rows):
        self.rows = rows

    def query_bars(self, *, interval, **_kwargs):
        return deepcopy(self.rows.get(interval, []))


def _bar(at: str, *, opened: float, close: float, amount: float = 1_000_000):
    return {
        "bar_time": f"2026-07-16T{at}:00+08:00",
        "session_date": "2026-07-16",
        "status": "verified",
        "sources": ["source-a", "source-b"],
        "open": opened,
        "high": max(opened, close) + 0.001,
        "low": min(opened, close) - 0.001,
        "close": close,
        "volume": 10_000,
        "amount": amount,
    }


def test_30m_confirmation_requires_six_closed_5m_bars():
    plan = validate_plan(_v5_plan())
    five = [
        _bar(at, opened=1.18 if index == 0 else 1.178, close=1.178 if index < 5 else 1.176)
        for index, at in enumerate(("09:30", "09:35", "09:40", "09:45", "09:50", "09:55"))
    ]
    evaluator = CompoundConditionEvaluator()
    before = evaluator.evaluate(
        plan=plan,
        symbol=plan["symbol"],
        market_store=_Bars({"1m": [], "5m": five[:5], "1D": []}),
        now_utc=datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc),
    )["report-weak-rebound"]
    assert before["confirmation_met"] is False
    assert before["evidence_pending"] is True

    after = evaluator.evaluate(
        plan=plan,
        symbol=plan["symbol"],
        market_store=_Bars({"1m": [], "5m": five, "1D": []}),
        now_utc=datetime(2026, 7, 16, 2, 1, tzinfo=timezone.utc),
    )["report-weak-rebound"]
    assert after["confirmation_met"] is True


def test_daily_turnover_uses_amount_and_waits_for_consecutive_closes():
    plan = _v5_plan()
    scenario = plan["watch_scenarios"][0]
    scenario["source_conditions"] = [{
        "condition_id": "source-turnover",
        "source_text": "连续 2 日成交额小于 3000 万",
        "role": "required",
        "coverage_status": "mapped",
        "reason": "",
        "evidence_refs": ["report"],
    }]
    scenario["entry_conditions"] = {"operator": "all", "conditions": []}
    scenario["confirmation_conditions"] = {
        "operator": "all",
        "conditions": [
            _condition(
                "turnover", "source-turnover", "cumulative_turnover", "lt",
                interval="1d", consecutive=2, value=30_000_000, unit="CNY",
                metric="cumulative_amount",
            )
        ],
    }
    plan = validate_plan(plan)
    daily = [
        {
            **_bar("15:00", opened=1.15, close=1.16, amount=20_000_000),
            "bar_time": f"2026-07-{day}T15:00:00+08:00",
            "session_date": f"2026-07-{day}",
            "volume": 999_000_000,
        }
        for day in (15, 16)
    ]
    result = CompoundConditionEvaluator().evaluate(
        plan=plan,
        symbol=plan["symbol"],
        market_store=_Bars({"1m": [], "5m": [], "1D": daily}),
        now_utc=datetime(2026, 7, 16, 7, 10, tzinfo=timezone.utc),
    )["report-weak-rebound"]
    assert result["confirmation_met"] is True

    daily[-1].pop("amount")
    pending = CompoundConditionEvaluator().evaluate(
        plan=plan,
        symbol=plan["symbol"],
        market_store=_Bars({"1m": [], "5m": [], "1D": daily}),
        now_utc=datetime(2026, 7, 16, 7, 10, tzinfo=timezone.utc),
    )["report-weak-rebound"]
    assert pending["confirmation_met"] is False
    assert pending["evidence_pending"] is True


def test_closed_daily_volume_ratio_uses_previous_five_sessions_across_weekend():
    plan = _v5_plan()
    scenario = plan["watch_scenarios"][0]
    scenario["source_conditions"] = [{
        "condition_id": "source-daily-volume",
        "source_text": "当日成交量至少为此前五日均量的 1.5 倍",
        "role": "required",
        "coverage_status": "mapped",
        "reason": "deterministic closed daily volume",
        "evidence_refs": ["weekly-report"],
    }]
    scenario["entry_conditions"] = {"operator": "all", "conditions": []}
    scenario["confirmation_conditions"] = {
        "operator": "all",
        "conditions": [
            _condition(
                "daily-volume-ratio",
                "source-daily-volume",
                "rolling_volume_ratio",
                "gte",
                interval="1d",
                lookback_bars=5,
                freshness_seconds=345600,
                value=1.5,
                metric="volume",
                unit="ratio",
            )
        ],
    }
    plan = validate_plan(plan)
    daily = []
    for index, day in enumerate((10, 13, 14, 15, 16, 17)):
        daily.append({
            **_bar("15:00", opened=1.15, close=1.16),
            "bar_time": f"2026-07-{day:02d}T15:00:00+08:00",
            "session_date": f"2026-07-{day:02d}",
            "volume": 160_000 if index == 5 else 100_000,
        })
    assessment = CompoundConditionEvaluator().evaluate(
        plan=plan,
        symbol=plan["symbol"],
        market_store=_Bars({"1m": [], "5m": [], "1D": daily}),
        now_utc=datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc),
    )["report-weak-rebound"]
    assert assessment["confirmation_met"] is True
    fact = assessment["facts"]["confirmation"][0]
    assert fact["value"] == pytest.approx(1.6)
    assert fact["baseline_volume"] == pytest.approx(100_000)
    assert fact["session_date"] == "2026-07-17"


def test_v5_downward_compound_confirmation_carries_cue_contract_only_when_confirmed(
    tmp_path: Path,
):
    store = MonitoringStore(tmp_path / "monitoring.sqlite3")
    store.set_autopilot_config(
        {"enabled": True, "selected_symbols": ["159999.SZ"]}
    )
    plan = _v5_plan()
    scenario = plan["watch_scenarios"][0]
    scenario["intent"] = "stop_loss"
    scenario["trigger"]["kind"] = "price_cross_below"
    scenario["trigger"]["threshold"] = 1.175
    rule = plan["market_rules"][0]
    rule["kind"] = "price_cross_below"
    rule["target_intent"] = "stop_loss"
    rule["alert_cue"] = "ymca_v1"
    plan = validate_plan(plan)
    profile_id, version = store.save_draft(
        symbol="159999.SZ",
        market="SZ",
        instrument_type="etf",
        plan=plan,
        evidence_manifest={"data_as_of": NOW.isoformat()},
        input_snapshot_hash="holding-hash",
        delivery_target_id=None,
        model_id="test-autopilot",
        created_by="autopilot",
    )
    store.activate_autonomous(
        profile_id,
        version,
        trigger_type="report_ready",
        evidence_fingerprint="evidence-1",
    )

    def evaluate(price: float, minute: int, compound=None, *, day: int = 16):
        quote = {
            "last_price": price,
            "interval": "5m",
            "bar_time": f"2026-07-{day:02d}T02:{minute:02d}:00+00:00",
            "session_date": f"2026-07-{day:02d}",
            "status": "verified",
            "sources": ["source-a", "source-b"],
        }
        if compound is not None:
            quote["compound_assessments"] = {rule["client_rule_id"]: compound}
        return store.evaluate_quote(profile_id, quote, delivery_mode="shadow")

    approaching = evaluate(1.18, 0)
    assert [event["kind"] for event in approaching] == ["watch_episode_approaching"]
    assert approaching[0]["facts"]["alert_cue"] == "none"

    rejected = evaluate(1.20, 5)
    assert [event["kind"] for event in rejected] == ["watch_episode_result"]
    assert rejected[0]["outcome"] == "approach_withdrawn"
    assert rejected[0]["facts"]["alert_cue"] == "none"

    second_approach = evaluate(1.18, 10)
    assert [event["kind"] for event in second_approach] == [
        "watch_episode_approaching"
    ]
    confirmed = evaluate(
        1.17,
        15,
        {
            "entry_met": True,
            "confirmation_met": True,
            "invalidated": False,
            "evidence_pending": False,
        },
    )
    assert [event["kind"] for event in confirmed] == ["watch_episode_result"]
    assert confirmed[0]["outcome"] == "confirmed"
    expected_contract = {
        "client_rule_id": "report-weak-rebound",
        "rule_kind": "price_cross_below",
        "direction": "below",
        "threshold": 1.175,
        "target_intent": "stop_loss",
        "target_level": 1,
        "confirmation_count": 1,
        "alert_cue": "ymca_v1",
    }
    assert expected_contract.items() <= confirmed[0]["facts"].items()

    third_approach = evaluate(1.18, 20)
    assert third_approach[0]["facts"]["alert_cue"] == "none"
    next_day = evaluate(1.18, 0, day=17)
    unresolved = next(
        event for event in next_day if event.get("outcome") == "unresolved"
    )
    assert unresolved["facts"]["alert_cue"] == "none"


def test_watch_only_compound_scenario_never_emits_confirmed_signal(tmp_path: Path):
    store = MonitoringStore(tmp_path / "watch-only.sqlite3")
    store.set_autopilot_config(
        {"enabled": True, "selected_symbols": ["159999.SZ"]}
    )
    plan = validate_plan(_v5_plan(scenario_overrides={"automation_status": "watch_only"}))
    profile_id, version = store.save_draft(
        symbol=plan["symbol"],
        market="SZ",
        instrument_type="etf",
        plan=plan,
        evidence_manifest={"data_as_of": NOW.isoformat()},
        input_snapshot_hash="holding-hash",
        delivery_target_id=None,
        model_id="test-watch-only",
        created_by="autopilot",
    )
    store.activate_autonomous(
        profile_id,
        version,
        trigger_type="report_ready",
        evidence_fingerprint="evidence-watch-only",
    )
    compound = {
        "automation_status": "watch_only",
        "entry_met": True,
        "confirmation_met": True,
        "invalidated": False,
        "evidence_pending": False,
    }

    def observe(minute: int):
        return store.evaluate_quote(
            profile_id,
            {
                "last_price": 1.18,
                "interval": "5m",
                "bar_time": f"2026-07-16T02:{minute:02d}:00+00:00",
                "session_date": "2026-07-16",
                "status": "verified",
                "sources": ["source-a", "source-b"],
                "compound_assessments": {"report-weak-rebound": compound},
            },
            delivery_mode="shadow",
        )

    assert [event["kind"] for event in observe(0)] == ["watch_episode_approaching"]
    assert observe(5) == []
    assert all(event["outcome"] != "confirmed" for event in store.list_events(limit=20))


def test_recommendation_quantity_is_bounded_and_never_executes(monkeypatch):
    monkeypatch.setattr(
        "src.portfolio.monitoring.recommendations.load_state",
        lambda: PortfolioState(
            holdings=[{
                "symbol": "159999.SZ",
                "quantity": 3000,
                "last_price": 10,
                "market_value": 30_000,
            }],
            cash=15_000,
        ),
    )
    monkeypatch.setattr(
        "src.portfolio.monitoring.recommendations.load_mandate",
        lambda: {
            "cash_policy": {"configured": True, "min_amount": 5_000},
            "assignments": {},
            "sleeves": [],
        },
    )
    resolver = RecommendationResolver()
    scenario = validate_plan(_v5_plan())["watch_scenarios"][0]
    reduce = resolver.resolve(
        symbol="159999.SZ",
        scenario=scenario,
        current_price=10,
        now_utc=NOW,
        compound={"evidence_pending": False},
    )
    assert reduce["constrained_quantity"] == 3000
    assert reduce["trade_execution"] == "forbidden"

    add_scenario = deepcopy(scenario)
    add_scenario["action_template"] = {
        "action": "add",
        "sizing": {"kind": "units", "value": 2000, "unit": "shares", "source": "report"},
        "confidence_floor": "medium",
    }
    add = resolver.resolve(
        symbol="159999.SZ",
        scenario=add_scenario,
        current_price=10,
        now_utc=NOW,
        compound={"evidence_pending": False},
    )
    assert add["constrained_quantity"] == 1000
    assert add["estimated_amount"] == 10_000


def test_recommendation_never_invents_default_position_size(monkeypatch):
    monkeypatch.setattr(
        "src.portfolio.monitoring.recommendations.load_state",
        lambda: PortfolioState(
            holdings=[{"symbol": "159999.SZ", "quantity": 3000, "last_price": 10}],
            cash=15_000,
        ),
    )
    scenario = validate_plan(_v5_plan())["watch_scenarios"][0]
    scenario["action_template"] = {
        "action": "reduce",
        "sizing": {"kind": "default_policy", "source": "requires_user_risk_preferences"},
        "confidence_floor": "high",
    }

    recommendation = RecommendationResolver().resolve(
        symbol="159999.SZ",
        scenario=scenario,
        current_price=10,
        now_utc=NOW,
        compound={"evidence_pending": False},
    )

    assert recommendation["status"] == "needs_risk_preferences"
    assert recommendation["requested_quantity"] is None
    assert recommendation["constrained_quantity"] is None
    assert recommendation["system_default_used"] is False
    assert recommendation["trade_execution"] == "forbidden"


def test_autonomous_activation_needs_no_delivery_target_and_trigger_dedupes(tmp_path: Path):
    store = MonitoringStore(tmp_path / "monitoring.sqlite3")
    config = store.set_autopilot_config({
        "enabled": True,
        "runtime_mode": "shadow",
        "selected_symbols": ["159999", "159999.SZ"],
    })
    assert config["enabled"] is True
    assert config["selected_symbols"] == ["159999.SZ"]
    assert config["automatic_trading"] == "forbidden"
    first, created = store.enqueue_autopilot_trigger(
        symbol="159999.SZ",
        trigger_type="report_ready",
        dedupe_key="same-report-hash",
    )
    second, duplicate_created = store.enqueue_autopilot_trigger(
        symbol="159999.SZ",
        trigger_type="report_ready",
        dedupe_key="same-report-hash",
    )
    assert created is True and duplicate_created is False
    assert first["trigger_id"] == second["trigger_id"]

    plan = validate_plan(_v5_plan())
    profile_id, version = store.save_draft(
        symbol="159999.SZ",
        market="SZ",
        instrument_type="etf",
        plan=plan,
        evidence_manifest={"data_as_of": NOW.isoformat()},
        input_snapshot_hash="holding-hash",
        delivery_target_id=None,
        model_id="test-autopilot",
        created_by="autopilot",
    )
    active = store.activate_autonomous(
        profile_id,
        version,
        trigger_type="report_ready",
        evidence_fingerprint="evidence-1",
    )
    assert active["status"] == "active"
    assert active["delivery_target_id"] is None
    assert active["autonomous_activation"]["activated_by"] == "autopilot"

    with store.transaction() as connection:
        connection.execute(
            """UPDATE monitor_rules SET state='testing',confirmation_progress=1,armed_epoch=7
               WHERE profile_id=? AND plan_version=?""",
            (profile_id, version),
        )
    replacement = deepcopy(plan)
    replacement["summary"] = "new evidence changed only the report summary"
    replacement_profile_id, replacement_version = store.save_draft(
        symbol="159999.SZ",
        market="SZ",
        instrument_type="etf",
        plan=replacement,
        evidence_manifest={"data_as_of": NOW.isoformat()},
        input_snapshot_hash="holding-hash",
        delivery_target_id=None,
        model_id="test-autopilot",
        created_by="autopilot",
    )
    assert replacement_profile_id == profile_id
    store.activate_autonomous(
        profile_id,
        replacement_version,
        trigger_type="material_evidence_changed",
        evidence_fingerprint="evidence-2",
    )
    with store.connect() as connection:
        inherited = connection.execute(
            """SELECT state,confirmation_progress,armed_epoch FROM monitor_rules
               WHERE profile_id=? AND plan_version=?""",
            (profile_id, replacement_version),
        ).fetchone()
        old_status = connection.execute(
            """SELECT status FROM monitor_plan_versions
               WHERE profile_id=? AND version=?""",
            (profile_id, version),
        ).fetchone()[0]
    assert dict(inherited) == {
        "state": "testing",
        "confirmation_progress": 1,
        "armed_epoch": 7,
    }
    assert old_status == "superseded"

    closed = store.close_autopilot_profile(profile_id, delivery_mode="shadow")
    assert closed["status"] == "closed"
    store.close_autopilot_profile(profile_id, delivery_mode="shadow")
    summaries = [
        event for event in store.list_events(limit=20)
        if event["kind"] == "holding_monitor_closed"
    ]
    assert len(summaries) == 1
    assert summaries[0]["outcome"] == "holding_removed"


def test_autopilot_selection_is_fail_closed_and_blocks_late_activation(tmp_path: Path):
    store = MonitoringStore(tmp_path / "monitoring.sqlite3")
    empty = store.set_autopilot_config({"enabled": True, "selected_symbols": []})
    assert empty["enabled"] is False
    assert empty["selected_symbols"] == []

    selected = store.set_autopilot_config({
        "enabled": True,
        "selected_symbols": ["159999", "159999.sz", "159999.SZ"],
    })
    assert selected["enabled"] is True
    assert selected["selected_symbols"] == ["159999.SZ"]

    profile_id, version = store.save_draft(
        symbol="159999.SZ",
        market="SZ",
        instrument_type="etf",
        plan=validate_plan(_v5_plan()),
        evidence_manifest={"data_as_of": NOW.isoformat()},
        input_snapshot_hash="holding-hash",
        delivery_target_id=None,
        model_id="test-autopilot",
        created_by="autopilot",
    )
    store.set_autopilot_config({"enabled": True, "selected_symbols": []})
    with pytest.raises(PlanValidationError, match="no longer selected"):
        store.activate_autonomous(
            profile_id,
            version,
            trigger_type="report_ready",
            evidence_fingerprint="evidence-race",
        )
    assert store.get_profile(profile_id)["status"] == "pending_review"


def test_v8_config_migration_adds_an_empty_fail_closed_selection(tmp_path: Path):
    path = tmp_path / "monitoring.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES('schema_version','8');
            CREATE TABLE monitor_autopilot_config (
                config_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                activation_mode TEXT NOT NULL DEFAULT 'autonomous',
                research_policy TEXT NOT NULL DEFAULT 'if_needed',
                trigger_types_json TEXT NOT NULL DEFAULT '[]',
                daily_close_enabled INTEGER NOT NULL DEFAULT 1,
                delivery_target_id TEXT,
                runtime_mode TEXT NOT NULL DEFAULT 'shadow',
                revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO monitor_autopilot_config VALUES(
                'default',1,'autonomous','if_needed','["report_ready"]',1,NULL,
                'deliver',4,'2026-07-15T00:00:00+00:00','2026-07-15T00:00:00+00:00'
            );
            """
        )

    store = MonitoringStore(path)
    config = store.get_autopilot_config()
    assert config["enabled"] is True
    assert config["selected_symbols"] == []
    assert config["runtime_mode"] == "deliver"
    assert config["trigger_types"] == ["report_ready"]
    assert MonitoringService(store=store).autopilot_tick(force=True)["status"] == (
        "no_selected_symbols"
    )
    with store.connect() as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "10"


def test_deselect_cancels_jobs_closes_only_autopilot_and_reselects(
    tmp_path: Path,
    monkeypatch,
):
    holdings = PortfolioState(holdings=[
        {"symbol": "159999.SZ", "quantity": 3000, "name": "selected ETF"},
        {"symbol": "600036.SH", "quantity": 1000, "name": "manual stock"},
    ])
    monkeypatch.setattr("src.portfolio.monitoring.service.load_state", lambda: holdings)
    store = MonitoringStore(tmp_path / "monitoring.sqlite3")
    store.set_autopilot_config({
        "enabled": True,
        "selected_symbols": ["159999.SZ", "600036.SH"],
        "trigger_types": ["holdings_changed"],
    })
    service = MonitoringService(store=store)
    real_tick = service.autopilot_tick
    monkeypatch.setattr(service, "autopilot_tick", lambda *, force=False: {"status": "skipped"})

    auto_profile_id, auto_version = store.save_draft(
        symbol="159999.SZ",
        market="SZ",
        instrument_type="etf",
        plan=validate_plan(_v5_plan()),
        evidence_manifest={"data_as_of": NOW.isoformat()},
        input_snapshot_hash=_holding_hash(holdings.holdings[0]),
        delivery_target_id=None,
        model_id="test-autopilot",
        created_by="autopilot",
    )
    store.activate_autonomous(
        auto_profile_id,
        auto_version,
        trigger_type="report_ready",
        evidence_fingerprint="evidence-1",
    )
    recommendation = store.save_recommendation({
        "profile_id": auto_profile_id,
        "plan_version": auto_version,
        "episode_id": None,
        "symbol": "159999.SZ",
        "scenario_id": "weak-rebound",
        "scenario_fingerprint": "scenario-before-deselect",
        "status": "action_ready",
        "action": "observe",
        "valid_until": (NOW + timedelta(hours=1)).isoformat(),
    })
    target = store.bind_target(channel="feishu", chat_id="ou_selection_test")
    with store.transaction() as connection:
        for suffix, status in (("pending", "pending"), ("uncertain", "delivery_uncertain")):
            event_id = f"event-before-deselect-{suffix}"
            connection.execute(
                """INSERT INTO monitor_events(
                       event_id,profile_id,symbol,plan_version,rule_id,armed_epoch,kind,status,
                       severity,title,summary,facts_json,first_seen_at,last_seen_at
                   ) VALUES(?,?,?,?,?,1,'test','confirmed','info','test','test','{}',?,?)""",
                (
                    event_id, auto_profile_id, "159999.SZ", auto_version,
                    f"test:{suffix}", NOW.isoformat(), NOW.isoformat(),
                ),
            )
            connection.execute(
                """INSERT INTO delivery_outbox(
                       delivery_id,event_id,delivery_target_id,status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    f"delivery-before-deselect-{suffix}", event_id, target["target_id"],
                    status, NOW.isoformat(), NOW.isoformat(),
                ),
            )
    manual_profile_id, _manual_version = store.save_draft(
        symbol="600036.SH",
        market="SH",
        instrument_type="company_equity",
        plan=validate_plan(_v5_plan(symbol="600036.SH")),
        evidence_manifest={"data_as_of": NOW.isoformat()},
        input_snapshot_hash="manual-hash",
        delivery_target_id=None,
        model_id="test-manual",
        created_by="monitor_planner",
    )
    trigger, _ = store.enqueue_autopilot_trigger(
        symbol="159999.SZ",
        trigger_type="holdings_changed",
        dedupe_key="in-flight",
    )
    job = store.create_planner_job(
        symbols=["159999.SZ"],
        report_refs={},
        research_policy="if_needed",
        delivery_target_id=None,
        force_fresh=True,
        activation_mode="autonomous",
        trigger_type="holdings_changed",
        autopilot_trigger_id=trigger["trigger_id"],
    )
    store.update_autopilot_trigger(
        trigger["trigger_id"], status="running", planner_job_id=job["job_id"]
    )
    public_job = store.create_planner_job(
        symbols=["159999.SZ"],
        report_refs={},
        research_policy="if_needed",
        delivery_target_id=None,
        force_fresh=True,
        activation_mode="autonomous",
        trigger_type="report_ready",
    )

    deselected = service.set_autopilot_config({
        "enabled": True,
        "selected_symbols": ["600036.SH"],
        "trigger_types": ["holdings_changed"],
    })
    assert deselected["selected_symbols"] == ["600036.SH"]
    assert store.get_profile(auto_profile_id)["status"] == "closed"
    assert store.get_profile(manual_profile_id)["status"] == "pending_review"
    assert store.get_planner_job(job["job_id"])["status"] == "cancelled"
    assert store.get_planner_job(public_job["job_id"])["status"] == "cancelled"
    cancelled_recommendation = next(
        item for item in store.list_recommendations(limit=20)
        if item["recommendation_id"] == recommendation["recommendation_id"]
    )
    assert cancelled_recommendation["status"] == "cancelled"
    with store.connect() as connection:
        delivery_statuses = {
            row["delivery_id"]: row["status"]
            for row in connection.execute(
                """SELECT delivery_id,status FROM delivery_outbox
                   WHERE delivery_id LIKE 'delivery-before-deselect-%'"""
            ).fetchall()
        }
    assert delivery_statuses == {
        "delivery-before-deselect-pending": "cancelled",
        "delivery-before-deselect-uncertain": "delivery_uncertain",
    }
    cancelled = next(
        item for item in store.list_autopilot_triggers(limit=20)
        if item["trigger_id"] == trigger["trigger_id"]
    )
    assert cancelled["status"] == "cancelled"
    lifecycle = next(
        event for event in store.list_events(limit=20)
        if event["profile_id"] == auto_profile_id
    )
    assert lifecycle["outcome"] == "selection_removed"
    assert "移出自主监控" in lifecycle["title"]
    with pytest.raises(ValueError, match="selected current holdings"):
        service.create_planner_job(
            ["159999.SZ"],
            activation_mode="autonomous",
        )

    service.set_autopilot_config({
        "enabled": True,
        "selected_symbols": ["159999.SZ"],
        "trigger_types": [],
    })
    monkeypatch.setattr(service, "_submit_planner_job", lambda _job_id: None)
    monkeypatch.setattr(
        service.report_catalog,
        "choose_candidate",
        lambda _symbol, _report_ref: (None, ["missing"]),
    )
    result = real_tick(force=True)
    assert result["covered_symbols"] == ["159999.SZ"]
    assert result["submitted_jobs"] == 1
    latest_job = max(
        (
            item for item in store.list_autopilot_triggers(limit=20)
            if item["symbol"] == "159999.SZ" and item["status"] == "running"
        ),
        key=lambda item: item["created_at"],
    )
    assert latest_job["dedupe_key"].endswith(f":selection:{result.get('config_revision', store.get_autopilot_config()['revision'])}")


def test_deselect_during_refresh_stops_before_evidence_collection(tmp_path: Path, monkeypatch):
    holdings = PortfolioState(holdings=[{
        "symbol": "159999.SZ",
        "quantity": 3000,
        "name": "selected ETF",
    }])
    monkeypatch.setattr("src.portfolio.monitoring.service.load_state", lambda: holdings)
    store = MonitoringStore(tmp_path / "monitoring.sqlite3")
    store.set_autopilot_config({"enabled": True, "selected_symbols": ["159999.SZ"]})
    service = MonitoringService(store=store)
    candidate = {
        "report_ref": "report-race",
        "report_type": "holding_analysis",
        "symbol": "159999.SZ",
        "title": "race report",
        "source_id": "test",
        "source_message_id": None,
        "artifact_id": "artifact-race",
        "revision": 1,
        "body": "# Monitoring report\n\nNo rules are needed for this cancellation test.",
        "quality_status": "ready",
        "generated_at": NOW.isoformat(),
        "data_as_of": NOW.isoformat(),
        "metadata": {},
    }
    monkeypatch.setattr(
        service.report_catalog,
        "choose_candidate",
        lambda _symbol, _report_ref: (candidate, []),
    )
    evidence_calls = []
    monkeypatch.setattr(
        service.evidence_collector,
        "collect",
        lambda **_kwargs: evidence_calls.append(True),
    )

    def refresh_and_deselect(**_kwargs):
        store.set_autopilot_config({"enabled": True, "selected_symbols": []})
        return {"status": "completed"}

    monkeypatch.setattr(service.planner.market_service, "refresh_sync", refresh_and_deselect)
    job = store.create_planner_job(
        symbols=["159999.SZ"],
        report_refs={"159999.SZ": "report-race"},
        research_policy="if_needed",
        delivery_target_id=None,
        force_fresh=True,
        activation_mode="autonomous",
        trigger_type="report_ready",
    )
    service._run_planner_job(job["job_id"])

    finished = store.get_planner_job(job["job_id"])
    assert finished["status"] == "cancelled"
    assert finished["items"][0]["status"] == "cancelled"
    assert evidence_calls == []
    assert store.get_profile_by_symbol("159999.SZ") is None


def test_restart_cancels_recovered_autonomous_job_outside_selection(tmp_path: Path, monkeypatch):
    holdings = PortfolioState(holdings=[{"symbol": "159999.SZ", "quantity": 3000}])
    monkeypatch.setattr("src.portfolio.monitoring.service.load_state", lambda: holdings)
    store = MonitoringStore(tmp_path / "monitoring.sqlite3")
    store.set_autopilot_config({"enabled": True, "selected_symbols": ["159999.SZ"]})
    job = store.create_planner_job(
        symbols=["159999.SZ"],
        report_refs={},
        research_policy="if_needed",
        delivery_target_id=None,
        force_fresh=True,
        activation_mode="autonomous",
        trigger_type="report_ready",
    )
    store.set_autopilot_config({"enabled": True, "selected_symbols": []})

    MonitoringService(store=store)

    recovered = store.get_planner_job(job["job_id"])
    assert recovered["status"] == "cancelled"
    assert recovered["cancel_requested"] is True


def test_evidence_probe_fingerprint_ignores_collection_clock():
    class Tool:
        def __init__(self, payload):
            self.payload = payload

        def execute(self, **_kwargs):
            return json.dumps(self.payload)

    times = iter((NOW, NOW + timedelta(minutes=30)))
    collector = AutonomousEvidenceCollector(
        news_tool=Tool({"ok": True, "source": "news", "items": [], "requested_at": "first-clock"}),
        fund_flow_tool=Tool({"ok": True, "source": "fund", "data": {"159999.SZ": {"rows": []}}}),
        sector_tool=Tool({"ok": True, "source": "sector", "data": {"boards": []}}),
        now_factory=lambda: next(times),
    )
    arguments = {
        "symbol": "159999.SZ",
        "holding": {"symbol": "159999.SZ", "quantity": 3000},
        "report_snapshot": {
            "snapshot_id": "snapshot-1",
            "report_ref": "report-1",
            "body_sha256": "report-hash",
            "data_as_of": "2026-07-16T06:00:00+00:00",
        },
        "market_evidence": {
            "symbol": "159999.SZ",
            "quote": {"last_price": 1.16, "bar_time": "2026-07-16T07:05:00+00:00"},
            "bar_hashes": {"5m": "bars-1"},
            "data_as_of": "2026-07-16T07:05:00+00:00",
            "generated_at": "changes-on-every-probe",
        },
    }
    first = collector.collect(**arguments)
    arguments["market_evidence"]["generated_at"] = "different-clock-only"
    collector.news_tool.payload["requested_at"] = "second-clock"
    second = collector.collect(**arguments)
    assert first["collected_at"] != second["collected_at"]
    assert first["evidence_fingerprint"] == second["evidence_fingerprint"]


def test_autopilot_tick_coalesces_initial_holding_report_and_close_triggers(
    tmp_path: Path,
    monkeypatch,
):
    holdings = PortfolioState(holdings=[{
        "symbol": "159999.SZ",
        "quantity": 3000,
        "name": "selected ETF",
    }])
    monkeypatch.setattr("src.portfolio.monitoring.service.load_state", lambda: holdings)
    store = MonitoringStore(tmp_path / "monitoring.sqlite3")
    store.set_autopilot_config({
        "enabled": True,
        "selected_symbols": ["159999.SZ"],
        "trigger_types": [
            "holdings_changed", "report_ready", "scheduled_close",
            "material_evidence_changed",
        ],
        "daily_close_enabled": True,
    })
    service = MonitoringService(store=store)
    candidate = {
        "report_ref": "report-current",
        "body": "# current report\n\n1.20 breakout",
    }
    monkeypatch.setattr(
        service.report_catalog,
        "choose_candidate",
        lambda _symbol, _report_ref: (candidate, []),
    )
    monkeypatch.setattr(service, "_submit_planner_job", lambda _job_id: None)

    result = service.autopilot_tick(force=True)
    triggers = store.list_autopilot_triggers(limit=20)

    assert result["created_triggers"] == 1
    assert result["submitted_jobs"] == 1
    assert [item["trigger_type"] for item in triggers] == ["holdings_changed"]
    assert triggers[0]["payload"]["report_ref"] == "report-current"
    assert triggers[0]["payload"]["report_hash"]


def test_autopilot_run_exposes_planner_gate_details(tmp_path: Path):
    store = MonitoringStore(tmp_path / "monitoring.sqlite3")
    store.set_autopilot_config({"enabled": True, "selected_symbols": ["159999.SZ"]})
    trigger, _created = store.enqueue_autopilot_trigger(
        symbol="159999.SZ",
        trigger_type="holdings_changed",
        dedupe_key="holding-1",
    )
    job = store.create_planner_job(
        symbols=["159999.SZ"],
        report_refs={},
        research_policy="if_needed",
        delivery_target_id=None,
        force_fresh=True,
        activation_mode="autonomous",
        trigger_type="holdings_changed",
        autopilot_trigger_id=trigger["trigger_id"],
    )
    error = "watch_scenarios[0] mapped source conditions must have an executable condition"
    store.update_planner_item(
        job["job_id"],
        "159999.SZ",
        status="blocked",
        blocked_reasons=["planner_validation_failed"],
        validation_errors=[error],
        error=error,
    )
    store.update_autopilot_trigger(
        trigger["trigger_id"],
        status="blocked",
        planner_job_id=job["job_id"],
        error="planner_job_blocked",
    )

    run = MonitoringService(store=store).list_autopilot_runs(limit=20)[0]

    assert run["blocked_reasons"] == ["planner_validation_failed"]
    assert run["validation_errors"] == [error]
    assert run["detail_error"] == error


def test_monitoring_target_cards_merge_scope_and_blocked_builds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    holdings = PortfolioState(holdings=[
        {"symbol": "000651.SZ", "name": "格力电器", "quantity": 100},
        {"symbol": "159516.SZ", "name": "半导体设备ETF", "quantity": 1000},
    ])
    monkeypatch.setattr("src.portfolio.monitoring.service.load_state", lambda: holdings)
    store = MonitoringStore(tmp_path / "monitoring.sqlite3")
    store.set_autopilot_config({
        "enabled": True,
        "selected_symbols": ["000651.SZ", "159516.SZ"],
    })
    trigger, _ = store.enqueue_autopilot_trigger(
        symbol="159516.SZ",
        trigger_type="holdings_changed",
        dedupe_key="etf-discontinuity",
    )
    job = store.create_planner_job(
        symbols=["159516.SZ"],
        report_refs={},
        research_policy="if_needed",
        delivery_target_id=None,
        force_fresh=True,
        activation_mode="autonomous",
        trigger_type="holdings_changed",
        autopilot_trigger_id=trigger["trigger_id"],
    )
    blockers = [
        "price_series_discontinuity_unverified",
        "adjustment_factor_unverified",
        "insufficient_post_event_history",
    ]
    store.update_planner_item(
        job["job_id"],
        "159516.SZ",
        status="blocked",
        blocked_reasons=blockers,
        progress={
            "stage": "blocked",
            "continuity": {
                "status": "blocked",
                "post_event_bar_count": 9,
                "blocked_reasons": blockers,
            },
            "volume_gate": {"status": "ready"},
        },
    )
    store.update_autopilot_trigger(
        trigger["trigger_id"],
        status="blocked",
        planner_job_id=job["job_id"],
    )

    legacy_payload = _v5_plan(symbol="159516.SZ")
    legacy_payload["market_rules"][0]["calculation_basis"] = {
        "method": "symmetric_target_extension",
        "method_label": "legacy symmetric target",
        "formula": "legacy",
        "summary": "legacy mechanical target",
        "recommended_value": 1.175,
        "references": [],
    }
    legacy_plan = validate_plan(legacy_payload)
    profile_id, version = store.save_draft(
        symbol="159516.SZ",
        market="SZ",
        instrument_type="etf",
        plan=legacy_plan,
        evidence_manifest={"data_as_of": NOW.isoformat()},
        input_snapshot_hash="holding-hash",
        delivery_target_id=None,
        model_id="legacy-autopilot",
        created_by="autopilot",
    )
    store.activate_autonomous(
        profile_id,
        version,
        trigger_type="holdings_changed",
        evidence_fingerprint="legacy-evidence",
    )
    service = MonitoringService(store=store)
    assert service._quarantine_unsafe_autopilot_profile("159516.SZ", blockers) == (
        "level_method_migration"
    )
    assert store.get_profile(profile_id)["status"] == "closed"
    with store.connect() as connection:
        archived = connection.execute(
            """SELECT status FROM monitor_plan_versions
               WHERE profile_id=? AND version=?""",
            (profile_id, version),
        ).fetchone()[0]
        enabled_rules = connection.execute(
            """SELECT COUNT(*) FROM monitor_rules
               WHERE profile_id=? AND plan_version=? AND enabled=1""",
            (profile_id, version),
        ).fetchone()[0]
    assert archived == "superseded"
    assert enabled_rules == 0

    cards = service.list_monitoring_targets()
    by_symbol = {card["symbol"]: card for card in cards}

    assert set(by_symbol) == {"000651.SZ", "159516.SZ"}
    assert by_symbol["000651.SZ"]["name"] == "格力电器"
    assert by_symbol["000651.SZ"]["profile_status"] == "building"
    assert by_symbol["000651.SZ"]["build_state"]["progress_percent"] == 5
    assert by_symbol["159516.SZ"]["profile_status"] == "blocked"
    assert [item["code"] for item in by_symbol["159516.SZ"]["blockers"]] == blockers
    assert by_symbol["159516.SZ"]["continuity"]["post_event_bar_count"] == 9
