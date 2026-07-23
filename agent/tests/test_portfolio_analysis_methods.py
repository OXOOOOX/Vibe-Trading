from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.portfolio.analysis_methods import (
    AgentAnalysisContractError,
    analyze_price_continuity,
    build_market_analysis_snapshot,
    evaluate_level_method_rollout_gate,
    method_release_status,
    validate_agent_method_analysis,
    walk_forward_level_evaluation,
)


def test_method_release_requires_walk_forward_approval() -> None:
    approved = method_release_status()
    assert approved["eligible_for_automatic_release"] is True
    assert {item["instrument_type"] for item in approved["samples"]} == {
        "company_equity",
        "etf",
    }

    unreviewed = method_release_status("market-analysis-methods/next")
    assert unreviewed == {
        "registry_version": "market-analysis-methods/next",
        "eligible_for_automatic_release": False,
        "reason": "method_version_not_walk_forward_approved",
        "samples": [],
    }


def make_bars(count: int = 260) -> list[dict]:
    bars: list[dict] = []
    cursor = date(2025, 1, 2)
    while len(bars) < count:
        if cursor.weekday() < 5:
            ordinal = len(bars)
            wave = (ordinal % 20 - 10) * 0.015
            base = 20 + ordinal * 0.01 + wave
            bars.append(
                {
                    "date": cursor.isoformat(),
                    "open": base,
                    "high": base + 0.25,
                    "low": base - 0.2,
                    "close": base + 0.08,
                    "volume": 100_000 + (ordinal % 11) * 5_000,
                }
            )
        cursor += timedelta(days=1)
    return bars


def valid_agent_payload(snapshot: dict) -> dict:
    return {
        "regime_interpretation": "当前结构偏强，但仍需要成交与回踩共同确认。",
        "selected_methods": [
            item["method_id"]
            for item in snapshot["methods"]
            if item["status"] == "available"
        ][:3],
        "selected_level_ids": [
            item["candidate_id"] for item in snapshot["level_candidates"][:2]
        ],
        "evidence_for": ["多周期结构方向一致。"],
        "counter_evidence": ["反向触及后的确认仍不充分。"],
        "cross_horizon_conclusion": "短中周期方向一致，长期结构仍需继续复核。",
        "invalidation_conditions": ["收盘结构反向破坏且量价确认。"],
        "confidence": "medium",
        "data_gaps": [],
        "critic": {"verdict": "pass", "issues": []},
    }


def test_snapshot_is_multi_horizon_and_excludes_future_bars() -> None:
    bars = make_bars()
    cutoff = bars[-6]["date"]
    snapshot = build_market_analysis_snapshot(
        bars,
        through=cutoff,
        symbol="000651.SZ",
        instrument_type="company_equity",
    )
    reference = build_market_analysis_snapshot(
        bars[:-5],
        through=cutoff,
        symbol="000651.SZ",
        instrument_type="company_equity",
    )

    assert snapshot == reference
    assert snapshot["cutoff_policy"] == "completed_daily_bars_only"
    assert {item["trading_days"] for item in snapshot["horizons"]} == {
        5,
        20,
        60,
        120,
        250,
    }
    assert snapshot["primary_levels"]
    assert all(
        item["calculation_basis"]["method_version"]
        == snapshot["registry_version"]
        for item in snapshot["level_candidates"]
    )
    assert snapshot["level_ladder"].keys() == {"support", "resistance"}
    assert all(
        item["role"] in {"T0", "S1", "S2", "R1", "R2"}
        and item["method_families"]
        and item["noise_gate"]["noise_band_atr"] >= 0.75
        and abs(float(item["invalidation"]["value"]) - float(item["representative_value"]))
        >= float(item["noise_gate"]["noise_band"])
        for item in snapshot["level_candidates"]
    )
    assert all(
        item["automation_status"] == "watch_only"
        for item in snapshot["level_candidates"]
        if item["role"] == "T0"
    )


def test_adjusted_daily_series_cannot_become_price_actionable() -> None:
    snapshot = build_market_analysis_snapshot(
        make_bars(80),
        symbol="588870.SH",
        instrument_type="etf",
        adjustment="qfq",
    )

    assert snapshot["price_basis"]["actionability"] == "analysis_only"
    assert all(
        item["price_actionability"] == "analysis_only"
        for item in snapshot["level_candidates"]
    )
    assert {item["scope"] for item in snapshot["instrument_context_requirements"]} == {
        "tracking_index_relative_strength",
        "tracking_error",
        "fund_shares",
        "premium_discount",
        "component_contribution",
    }


def _scale_bar(bar: dict, factor: float) -> dict:
    return {
        **bar,
        **{field: float(bar[field]) * factor for field in ("open", "high", "low", "close")},
    }


def test_unverified_price_reset_uses_only_post_event_raw_history() -> None:
    bars = make_bars(80)
    reset = [_scale_bar(bar, 0.5) if index >= 70 else bar for index, bar in enumerate(bars)]

    snapshot = build_market_analysis_snapshot(
        reset,
        symbol="159516.SZ",
        instrument_type="etf",
    )

    assert snapshot["continuity"]["status"] == "blocked"
    assert snapshot["continuity"]["post_event_bar_count"] == 10
    assert snapshot["data_gap_codes"] == [
        "price_series_discontinuity_unverified",
        "adjustment_factor_unverified",
        "insufficient_post_event_history",
    ]
    assert snapshot["price_basis"]["actionability"] == "analysis_only"
    assert snapshot["level_candidates"] == []
    assert snapshot["primary_levels"] == {}


def test_official_factor_converts_price_and_volume_to_current_raw_equivalent() -> None:
    bars = make_bars(120)
    reset_index = 60
    reset = [
        _scale_bar(bar, 0.5) if index >= reset_index else bar
        for index, bar in enumerate(bars)
    ]
    effective_date = reset[reset_index]["date"]

    continuity = analyze_price_continuity(
        reset,
        factor_rows=[{
            "effective_date": effective_date,
            "factor": 0.5,
            "source": "official_exchange_notice",
            "confidence": "official",
        }],
    )
    snapshot = build_market_analysis_snapshot(
        reset,
        symbol="159516.SZ",
        instrument_type="etf",
        factor_rows=[{
            "effective_date": effective_date,
            "factor": 0.5,
            "source": "official_exchange_notice",
            "confidence": "official",
        }],
    )

    assert continuity["status"] == "adjusted_verified"
    assert continuity["analysis_basis"] == "current_raw_equivalent"
    assert continuity["bars"][0]["close"] == pytest.approx(bars[0]["close"] * 0.5)
    assert continuity["bars"][0]["volume"] == pytest.approx(bars[0]["volume"] * 2)
    assert snapshot["price_basis"]["actionability"] == "price_actionable"
    assert snapshot["regime"]["atr14_pct"] < 10
    assert snapshot["data_gap_codes"] == []


def test_agent_can_only_select_registered_methods_and_levels() -> None:
    snapshot = build_market_analysis_snapshot(make_bars(), symbol="000651.SZ")
    payload = valid_agent_payload(snapshot)

    result = validate_agent_method_analysis(payload, snapshot=snapshot)

    assert result["status"] == "completed"
    assert result["trade_execution"] == "forbidden"
    assert set(result["selected_level_ids"]) <= {
        item["candidate_id"] for item in snapshot["level_candidates"]
    }

    payload["selected_level_ids"] = ["level_invented"]
    with pytest.raises(AgentAnalysisContractError, match="unknown numeric level"):
        validate_agent_method_analysis(payload, snapshot=snapshot)


def test_agent_narrative_cannot_introduce_unregistered_numbers() -> None:
    snapshot = build_market_analysis_snapshot(make_bars(), symbol="000651.SZ")
    payload = valid_agent_payload(snapshot)
    payload["cross_horizon_conclusion"] = "支撑位是二十一点五元。"

    # Chinese words are prose and remain allowed; a literal price is rejected.
    payload["cross_horizon_conclusion"] = "支撑位是 21.5 元。"
    with pytest.raises(AgentAnalysisContractError, match="numeric literal"):
        validate_agent_method_analysis(payload, snapshot=snapshot)


def test_agent_can_only_reference_frozen_data_gap_codes() -> None:
    snapshot = build_market_analysis_snapshot(make_bars(), symbol="000651.SZ")
    payload = valid_agent_payload(snapshot)
    payload["data_gaps"] = ["reusable_report_claims_unavailable"]

    result = validate_agent_method_analysis(
        payload,
        snapshot=snapshot,
        allowed_data_gap_codes={"reusable_report_claims_unavailable"},
    )
    assert result["data_gaps"] == ["reusable_report_claims_unavailable"]

    payload["data_gaps"] = ["仅供人工复核，不执行交易。"]
    with pytest.raises(AgentAnalysisContractError, match="not present in frozen inputs"):
        validate_agent_method_analysis(
            payload,
            snapshot=snapshot,
            allowed_data_gap_codes={"reusable_report_claims_unavailable"},
        )


def test_walk_forward_compares_method_with_fixed_window_without_future_inputs() -> None:
    result = walk_forward_level_evaluation(
        make_bars(),
        symbol="000651.SZ",
        minimum_history=120,
        forward_bars=5,
        step=10,
    )

    assert result["evaluation_points"] > 0
    assert result["evaluation_policy"]["no_future_snapshot_inputs"] is True
    assert set(result["methods"]) == {
        "method_v1",
        "rolling_20_day_baseline",
    }
    assert all(
        values["eligible_levels"] == result["evaluation_points"] * 2
        for values in result["methods"].values()
    )


def test_rollout_gate_rejects_method_version_outside_regression_budgets() -> None:
    decision = evaluate_level_method_rollout_gate({
        "registry_version": "candidate",
        "evaluation_points": 20,
        "methods": {
            "method_v1": {
                "touch_rate": 0.59,
                "resolved_precision": 0.77,
                "first_invalidation_rate": 0.24,
            },
            "rolling_20_day_baseline": {
                "touch_rate": 0.60,
                "resolved_precision": 0.80,
                "first_invalidation_rate": 0.20,
            },
        },
    })

    assert decision["eligible_for_automatic_release"] is False
    assert decision["checks"] == {
        "has_evaluation_points": True,
        "touch_rate_not_lower": False,
        "resolved_precision_within_2pp": False,
        "first_invalidation_within_3pp": False,
    }
