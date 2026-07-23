from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.portfolio.daily.contracts import parse_holding_brief
from src.portfolio.daily.monitoring import build_monitoring_bundle
from src.portfolio.daily.reporting import render_holding_markdown
from src.portfolio.monitoring.models import (
    PlanValidationError,
    validate_monitoring_bundle,
    validate_monitoring_candidate,
)
from src.portfolio.monitoring.report_planner import ReportDrivenMonitoringPlanner


NOW = "2026-07-18T09:00:00+08:00"
DATA_AS_OF = "2026-07-18T08:55:00+08:00"


def _condition(
    *,
    source_text: str = "价格上穿1.85",
    kind: str = "price_compare",
    interval: str = "5m",
    metric: str | None = None,
) -> tuple[dict, dict]:
    source = {
        "condition_id": "required-price",
        "source_text": source_text,
        "role": "required",
        "coverage_status": "mapped",
        "reason": "",
        "evidence_refs": ["market.closed_bars.5m"],
    }
    executable = {
        "condition_id": "entry-price",
        "source_condition_id": "required-price",
        "kind": kind,
        "operator": "gte",
        "value": 1.85 if kind not in {"volume_ratio", "cumulative_volume"} else 1.5,
        "unit": "CNY" if kind == "price_compare" else "ratio",
        "interval": interval,
        "consecutive": 1,
        "lookback_bars": 1,
        "freshness_seconds": 900,
    }
    if metric is not None:
        executable["metric"] = metric
    return source, executable


def _candidate(**overrides) -> dict:
    source, executable = _condition()
    value = {
        "label": "突破阻力后的观察",
        "intent": "breakout",
        "priority": "normal",
        "original_level": {
            "kind": "price",
            "value": 1.85,
            "unit": "CNY",
            "adjustment": "raw",
            "source_text": "关注1.85阻力位",
        },
        "calculation_basis": {
            "method": "swing_high",
            "method_label": "前高阻力",
            "formula": "最近有效前高",
            "summary": "最近平台上沿位于1.85",
            "recommended_value": 1.85,
            "references": [{"label": "最近前高", "value": 1.85, "date": "2026-07-17"}],
        },
        "source_conditions": [source],
        "trigger": {
            "kind": "price_cross_above",
            "threshold": 1.85,
            "interval": "5m",
            "confirmation_count": 2,
        },
        "approach_policy": {"distance_bps": 100, "source": "report", "check_interval": "1m"},
        "volume_confirmation": {
            "metric": "same_bucket_5m_volume_ratio",
            "comparator": "gte",
            "threshold": 1.5,
            "min_samples": 5,
            "mode": "classify_only",
            "unit": "ratio",
        },
        "entry_conditions": {"operator": "all", "conditions": [executable]},
        "confirmation_conditions": {"operator": "all", "conditions": []},
        "invalidation_conditions": {"operator": "all", "conditions": []},
        "sequence_policy": {"enabled": False, "max_wait_bars": 6, "reset_on_invalidation": True},
        "invalidation": {"kind": "price_cross_below", "level": 1.80},
        "resolution_policy": {
            "rejection_hysteresis_bps": 30,
            "max_observation_bars": 6,
            "close_action": "unresolved",
        },
        "action_template": {
            "action": "observe",
            "sizing": {"kind": "default_policy", "source": "system_default"},
            "confidence_floor": "medium",
        },
        "rationale": "突破前高后进入人工复核。",
        "interpretation": {
            "price_only": "价格已触发，但量价尚未确认。",
            "confirmed": "价格与量能共同确认，可进入人工复核。",
            "divergence": "价格触发但量能不足。",
            "invalidated": "重新跌回关键位置，原判断失效。",
            "insufficient_data": "量价证据不足，仅保留价格提醒。",
            "bullish_case": "站稳阻力且放量时，多头参与度提高。",
            "bearish_case": "突破后快速回落，上方抛压仍强。",
        },
        "mapping_status": "mapped",
        "automation_status": "action_ready",
    }
    value.update(overrides)
    return value


def _context(**overrides) -> dict:
    value = {
        "data_mode": "verified",
        "source_count": 2,
        "sources": ["tencent", "mootdx"],
        "single_source_authorized": False,
        "warnings": [],
        "refresh_attempted": True,
        "refresh_succeeded": True,
    }
    value.update(overrides)
    return value


def _build(candidate: dict, *, previous_bundle=None, context=None):
    return build_monitoring_bundle(
        run_id="daily-20260718",
        revision=1,
        symbol="588870.SH",
        raw_bundle={"candidates": [candidate]},
        generated_at=NOW,
        data_as_of=DATA_AS_OF,
        daily_actionable=True,
        condition_actionable=True,
        price_volume_context=context or _context(),
        previous_bundle=previous_bundle,
    )


def test_bundle_generates_deterministic_ids_claims_and_legacy_conditions() -> None:
    bundle, claims, conditions = _build(_candidate())

    candidate = bundle["candidates"][0]
    assert bundle["schema_version"] == 2
    assert bundle["level_snapshot_id"]
    assert bundle["selection_mode"] == "report_candidate_validated"
    assert bundle["price_conversion"] == {
        "analysis_basis": "raw",
        "runtime_basis": "raw",
        "events": [],
    }
    assert bundle["monitoring_status"] == "available"
    assert bundle["price_basis"] == {"adjustment": "raw", "currency": "CNY", "tick_size": 0.001}
    assert candidate["candidate_id"] == candidate["scenario_id"]
    assert candidate["scenario_family_id"].startswith("scenario_")
    assert {claim["section_id"] for claim in claims} == {
        "daily_level",
        "daily_trigger",
        "daily_confirmation",
        "daily_volume_confirmation",
        "daily_invalidation",
        "daily_action",
        "daily_calculation_basis",
        "daily_interpretation",
    }
    assert candidate["claim_ids"] == [claim["claim_id"] for claim in claims]
    assert conditions[0]["candidate_id"] == candidate["candidate_id"]
    assert bundle["activation_policy"] == "manual_confirmation_required"
    assert bundle["trade_execution"] == "forbidden"


def test_unknown_candidate_field_and_non_raw_price_are_isolated_from_report() -> None:
    unknown = _candidate(unexpected="bad")
    bundle, claims, conditions = _build(unknown)
    assert bundle["monitoring_status"] == "not_recommended"
    assert bundle["candidates"] == []
    assert claims == [] and conditions == []
    assert "unsupported fields" in bundle["validation_errors"][0]

    non_raw = _candidate(
        original_level={
            "kind": "price",
            "value": 1.85,
            "unit": "CNY",
            "adjustment": "forward",
            "source_text": "复权价1.85",
        }
    )
    bundle, _, _ = _build(non_raw)
    assert bundle["candidates"] == []
    assert any("must be raw" in item for item in bundle["validation_errors"])


def test_daily_condition_is_not_simplified_to_five_minutes() -> None:
    source, executable = _condition(source_text="连续两日收盘站稳1.85", interval="5m")
    bundle, _, _ = _build(
        _candidate(
            source_conditions=[source],
            confirmation_conditions={"operator": "all", "conditions": [executable]},
            entry_conditions={"operator": "all", "conditions": []},
        )
    )

    candidate = bundle["candidates"][0]
    assert candidate["automation_status"] == "watch_only"
    assert candidate["mapping_status"] == "partial"
    assert candidate["source_conditions"][0]["coverage_status"] == "ambiguous"
    assert candidate["confirmation_conditions"]["conditions"] == []
    assert "日线或收盘条件不得简化" in candidate["source_conditions"][0]["reason"]


def test_daily_trigger_keeps_required_daily_semantics_and_only_maps_a_price_reminder() -> None:
    bundle, _, conditions = _build(
        _candidate(
            trigger={
                "kind": "price_cross_above",
                "threshold": 1.85,
                "interval": "1d",
                "confirmation_count": 1,
            },
            approach_policy={
                "distance_bps": 100,
                "source": "system_default",
                "check_interval": "1d",
            },
        )
    )

    candidate = bundle["candidates"][0]
    preserved = [
        item
        for item in candidate["source_conditions"]
        if item["coverage_status"] == "unsupported" and "1d闭合K线" in item["source_text"]
    ]
    assert candidate["trigger"]["interval"] == "1m"
    assert candidate["trigger"]["confirmation_count"] == 1
    assert candidate["approach_policy"] == {
        "distance_bps": 100,
        "source": "atr20_default",
        "check_interval": "1m",
    }
    assert candidate["automation_status"] == "watch_only"
    assert candidate["mapping_status"] == "partial"
    assert preserved and preserved[0]["role"] == "required"
    assert "仅用于价格提醒" in bundle["validation_errors"][-1]
    assert conditions[0]["trigger"].startswith("price_cross_above 1.85（1m")


def test_known_nested_condition_shape_is_flattened_before_strict_validation() -> None:
    source, _ = _condition(source_text="日线收盘站稳1.85", interval="1d")
    bundle, _, _ = _build(
        _candidate(
            source_conditions=[source],
            entry_conditions={
                "operator": "all",
                "conditions": [
                    {
                        "kind": "price_compare",
                        "source_condition_id": "required-price",
                        "parameters": {"operator": "gte", "level": 1.85, "interval": "1d"},
                        "label": "日线收盘确认",
                    }
                ],
            },
        )
    )

    condition = bundle["candidates"][0]["entry_conditions"]["conditions"][0]
    assert condition["operator"] == "gte"
    assert condition["value"] == 1.85
    assert condition["interval"] == "1d"
    assert condition["condition_id"].startswith("condition_")
    assert "parameters" not in condition and "label" not in condition


def test_watch_only_bundle_is_not_presented_as_an_executable_condition_order() -> None:
    bundle, _, conditions = _build(
        _candidate(
            trigger={
                "kind": "price_cross_above",
                "threshold": 1.85,
                "interval": "1d",
                "confirmation_count": 1,
            }
        )
    )
    markdown = render_holding_markdown(
        market_date="2026-07-18",
        holding={"symbol": "588870.SH", "name": "科创50ETF"},
        brief={
            "symbol": "588870.SH",
            "summary": "继续观察",
            "action": "observe",
            "confidence": "medium",
            "condition_order_status": "available",
            "condition_order_summary": "仅保留价格观察。",
            "condition_orders": conditions,
            "monitoring_bundle": bundle,
        },
        data_status="fresh",
    )

    assert "条件单状态：仅观察（不可执行）" in markdown
    assert "仅观察" in markdown
    assert "watch_only" not in markdown
    assert "price_cross_above" not in markdown
    assert "候选 ID" not in markdown


def test_turnover_is_not_mapped_to_volume_and_unknown_absolute_unit_keeps_price_watch() -> None:
    source, executable = _condition(
        source_text="成交额达到过去五日均值1.5倍",
        kind="volume_ratio",
        interval="5m",
        metric="volume_ratio",
    )
    candidate = _candidate(
        source_conditions=[source],
        entry_conditions={"operator": "all", "conditions": [executable]},
        volume_confirmation={
            "metric": "absolute_cumulative_volume",
            "comparator": "gte",
            "threshold": 1000,
            "min_samples": 5,
            "mode": "classify_only",
            "unit": "CNY",
        },
    )
    bundle, _, conditions = _build(candidate)

    normalized = bundle["candidates"][0]
    assert normalized["automation_status"] == "watch_only"
    assert normalized["source_conditions"][0]["coverage_status"] == "unsupported"
    assert normalized["volume_confirmation"]["metric"] == "same_bucket_5m_volume_ratio"
    assert normalized["volume_confirmation"]["unit"] == "ratio"
    assert conditions[0]["trigger"].startswith("price_cross_above 1.85")


def test_single_source_requires_explicit_authorization_for_action_ready() -> None:
    bundle, _, _ = _build(
        _candidate(),
        context=_context(
            data_mode="single_source",
            source_count=1,
            sources=["tencent"],
            warnings=["单一来源"],
        ),
    )

    assert bundle["candidates"][0]["automation_status"] == "watch_only"
    assert any("未获明确授权" in item for item in bundle["validation_errors"])


def test_cross_report_diff_keeps_family_id_stable_and_reports_raised_and_withdrawn() -> None:
    first, _, _ = _build(_candidate())
    raised, _, _ = build_monitoring_bundle(
        run_id="daily-20260719",
        revision=1,
        symbol="588870.SH",
        raw_bundle={"candidates": [_candidate(
            original_level={
                "kind": "price", "value": 1.90, "unit": "CNY", "adjustment": "raw",
                "source_text": "关注1.90阻力位",
            },
            trigger={
                "kind": "price_cross_above", "threshold": 1.90,
                "interval": "5m", "confirmation_count": 2,
            },
            calculation_basis={
                "method": "swing_high", "method_label": "前高阻力",
                "formula": "最近有效前高", "summary": "平台上沿上移至1.90",
                "recommended_value": 1.90,
                "references": [{"label": "最近前高", "value": 1.90, "date": "2026-07-18"}],
            },
        )]},
        generated_at="2026-07-19T09:00:00+08:00",
        data_as_of="2026-07-19T08:55:00+08:00",
        daily_actionable=True,
        condition_actionable=True,
        price_volume_context=_context(),
        previous_bundle=first,
    )
    current = raised["candidates"][0]
    assert current["scenario_family_id"] == first["candidates"][0]["scenario_family_id"]
    assert current["change_type"] == "raised"
    assert current["previous_candidate_id"] == first["candidates"][0]["candidate_id"]

    withdrawn, _, _ = build_monitoring_bundle(
        run_id="daily-20260720",
        revision=1,
        symbol="588870.SH",
        raw_bundle={"candidates": []},
        generated_at="2026-07-20T09:00:00+08:00",
        data_as_of="2026-07-20T08:55:00+08:00",
        daily_actionable=True,
        condition_actionable=True,
        price_volume_context=_context(),
        previous_bundle=raised,
    )
    assert withdrawn["scenario_changes"][0]["change_type"] == "withdrawn"


def test_bundle_and_candidate_contracts_reject_unknown_fields_and_invalid_enum() -> None:
    bundle, _, _ = _build(_candidate())
    invalid_bundle = {**bundle, "unknown": True}
    with pytest.raises(PlanValidationError, match="unsupported fields"):
        validate_monitoring_bundle(invalid_bundle, expected_symbol="588870.SH")

    candidate = dict(bundle["candidates"][0])
    candidate["intent"] = "automatic_buy"
    with pytest.raises(PlanValidationError, match="intent"):
        validate_monitoring_candidate(
            candidate,
            expected_symbol="588870.SH",
            generated_at=NOW,
            data_as_of=DATA_AS_OF,
            valid_until="2026-07-25T09:00:00+08:00",
        )


class _NoModelClient:
    model_id = "forbidden"

    def complete(self, messages):
        raise AssertionError("structured bundles must not call the model")


class _MarketStore:
    def query_bars(self, *, interval, **kwargs):
        return [
            {
                "status": "verified",
                "bar_time": "2026-07-18T08:55:00+08:00",
                "open": 1.82,
                "high": 1.86,
                "low": 1.81,
                "close": 1.84,
                "volume": 1000,
                "sources": ["tencent", "mootdx"],
            }
        ]


class _MarketService:
    store = _MarketStore()


class _MarketPlanner:
    market_service = _MarketService()

    def _actionable_quote(self, symbol):
        return {
            "status": "verified",
            "adjustment": "raw",
            "sources": ["tencent", "mootdx"],
            "last_price": 1.84,
            "bar_time": "2026-07-18T08:55:00+08:00",
        }

    def build(self, _holding):
        return None, {
            "level_snapshot_id": "level-snapshot-test",
            "daily_tail_hash": "tail",
            "volume_signature": "volume",
            "adjustment_factor_revision": "factor",
            "method_registry_version": "market-analysis-methods/1.1",
            "level_snapshot": {
                "level_candidates": [{
                    "candidate_id": "algorithm-resistance-1",
                    "level_type": "resistance",
                    "lower": 1.84,
                    "upper": 1.86,
                    "representative_value": 1.85,
                    "invalidation": {"value": 1.87},
                    "score": 82,
                    "confidence": "high",
                    "automation_status": "action_ready",
                    "method_ids": ["multi_horizon_extremes", "confirmed_swing_points"],
                }],
            },
        }, []


def test_planner_uses_structured_bundle_without_markdown_extraction_or_activation() -> None:
    bundle, _, _ = _build(_candidate())
    bundle = json.loads(json.dumps(bundle, ensure_ascii=False))
    bundle["valid_until"] = "2099-07-19T09:00:00+08:00"
    bundle["review_due_at"] = "2099-07-19T09:00:00+08:00"
    bundle["source_valid_until"] = "2099-07-19T09:00:00+08:00"
    planner = ReportDrivenMonitoringPlanner(
        market_planner=_MarketPlanner(), client=_NoModelClient()
    )
    plan, manifest, research = planner.build_from_monitoring_bundle(
        holding={"symbol": "588870.SH"},
        report_snapshot={
            "snapshot_id": "snapshot-1",
            "report_ref": "daily:run:json",
            "report_type": "holding_analysis",
            "title": "588870日报",
            "revision": 1,
            "body_sha256": "a" * 64,
            "quality_status": "ready",
            "generated_at": NOW,
            "data_as_of": DATA_AS_OF,
            "monitoring_bundle": bundle,
        },
    )

    assert research is None
    assert manifest["planner_mode"] == "structured_monitoring_bundle"
    assert manifest["legacy_extraction"] is False
    assert plan["watch_scenarios"][0]["candidate_id"] == bundle["candidates"][0]["candidate_id"]
    assert plan["market_rules"][0]["enabled"] is True
    validation = manifest["algorithm_candidate_validation"]
    assert validation["matches"][0]["algorithm_candidate_id"] == "algorithm-resistance-1"
    assert validation["disagreements"] == []
    hard_valid_until = datetime.fromisoformat(plan["hard_valid_until"])
    current = datetime.now(timezone.utc)
    assert current + timedelta(days=30) < hard_valid_until < current + timedelta(days=365)
    assert plan["market_rules"][0]["valid_until"] == plan["hard_valid_until"]
    assert plan["source_valid_until"] == "2099-07-19T01:00:00+00:00"
    assert plan["automation_policy"]["activation_mode"] == "manual_confirmation_required"
    assert plan["automation_policy"]["trade_execution"] == "forbidden"


def test_report_level_outside_algorithm_catalog_stays_non_executable() -> None:
    bundle, _, _ = _build(_candidate())
    bundle = json.loads(json.dumps(bundle, ensure_ascii=False))
    bundle["valid_until"] = "2099-07-19T09:00:00+08:00"
    bundle["review_due_at"] = "2099-07-19T09:00:00+08:00"
    bundle["source_valid_until"] = "2099-07-19T09:00:00+08:00"

    class DisagreeingPlanner(_MarketPlanner):
        def build(self, _holding):
            _plan, evidence, blocked = super().build(_holding)
            evidence["level_snapshot"]["level_candidates"][0].update(
                lower=2.40, upper=2.45, representative_value=2.42,
                invalidation={"value": 2.38},
            )
            return _plan, evidence, blocked

    plan, manifest, _ = ReportDrivenMonitoringPlanner(
        market_planner=DisagreeingPlanner(), client=_NoModelClient()
    ).build_from_monitoring_bundle(
        holding={"symbol": "588870.SH"},
        report_snapshot={
            "snapshot_id": "snapshot-disagreement",
            "report_ref": "daily:run:json",
            "report_type": "holding_analysis",
            "title": "588870日报",
            "revision": 1,
            "body_sha256": "c" * 64,
            "quality_status": "ready",
            "generated_at": NOW,
            "data_as_of": DATA_AS_OF,
            "monitoring_bundle": bundle,
        },
    )

    assert plan["watch_scenarios"][0]["automation_status"] == "watch_only"
    assert plan["market_rules"][0]["enabled"] is False
    assert manifest["algorithm_candidate_validation"]["matches"] == []
    assert manifest["algorithm_candidate_validation"]["disagreements"][0]["reason"] == (
        "outside_algorithm_candidate_tolerance"
    )


def test_planner_keeps_watch_only_report_candidate_non_executable() -> None:
    bundle, _, _ = _build(
        _candidate(automation_status="watch_only", mapping_status="partial")
    )
    bundle = json.loads(json.dumps(bundle, ensure_ascii=False))
    bundle["valid_until"] = "2099-07-19T09:00:00+08:00"
    bundle["review_due_at"] = "2099-07-19T09:00:00+08:00"
    bundle["source_valid_until"] = "2099-07-19T09:00:00+08:00"
    plan, _, _ = ReportDrivenMonitoringPlanner(
        market_planner=_MarketPlanner(), client=_NoModelClient()
    ).build_from_monitoring_bundle(
        holding={"symbol": "588870.SH"},
        report_snapshot={
            "snapshot_id": "snapshot-watch-only",
            "report_ref": "weekly:run:json",
            "report_type": "weekly_review",
            "title": "588870周报",
            "revision": 1,
            "body_sha256": "b" * 64,
            "quality_status": "ready",
            "generated_at": NOW,
            "data_as_of": DATA_AS_OF,
            "monitoring_bundle": {
                **bundle,
                "horizon": "weekly",
                "source": "structured_weekly_report",
            },
        },
    )

    assert plan["watch_scenarios"][0]["automation_status"] == "watch_only"
    assert plan["market_rules"][0]["enabled"] is False


def test_feature_off_keeps_legacy_schema_v2_behavior() -> None:
    brief = parse_holding_brief(
        json.dumps(
            {
                "summary": "继续观察",
                "action": "observe",
                "confidence": "medium",
                "reasons": ["趋势未确认"],
                "condition_orders": [{"trigger": "1.85", "response": "观察"}],
            },
            ensure_ascii=False,
        ),
        symbol="588870.SH",
        structured_monitoring=False,
    )

    assert brief["schema_version"] == 2
    assert "monitoring_bundle_input" not in brief
    assert brief["condition_orders"][0]["trigger"] == "1.85"
