"""P4A deterministic ETF component selection tests."""

from __future__ import annotations

import pytest

from src.reports import (
    ETFComponentSelector,
    ETFResearchStore,
    build_etf_snapshot,
    execute_p4a_selection,
)
from src.reports.service import DeepReportService


def _equal_components(count: int) -> list[dict]:
    weight_pct = 100.0 / count
    return [
        {"symbol": f"C{index:04d}", "name": f"成分{index}", "weight": weight_pct}
        for index in range(count)
    ]


def test_p4a_structural_profiles_choose_adaptive_counts() -> None:
    selector = ETFComponentSelector()

    csi1000 = selector.select(
        etf_symbol="560010.SH",
        components=_equal_components(1000),
        expected_component_count=1000,
        universe_complete=True,
    )
    assert csi1000.quality == "complete"
    assert csi1000.concentration.concentration_class == "highly_diversified"
    assert csi1000.concentration.min_penetration_count == 0
    assert csi1000.selected == []
    assert csi1000.stop_reason == "marginal_explanation_gain_below_5pct"

    csi300 = selector.select(
        etf_symbol="510300.SH",
        components=_equal_components(300),
        expected_component_count=300,
        universe_complete=True,
    )
    assert csi300.concentration.concentration_class == "moderately_diversified"
    assert len(csi300.selected) == 2
    assert csi300.stop_reason == "marginal_explanation_gain_below_5pct"

    star50 = selector.select(
        etf_symbol="588870.SH",
        components=_equal_components(50),
        expected_component_count=50,
        universe_complete=True,
    )
    assert star50.concentration.concentration_class == "focused"
    assert len(star50.selected) == 3

    concentrated = selector.select(
        etf_symbol="516010.SH",
        components=[
            {"symbol": f"G{index:02d}", "name": f"游戏{index}", "weight": weight}
            for index, weight in enumerate([15, 12, 10, 8, 7, 4, 4, 4, 4, 4, 3, 3, 3, 3, 3, 2, 2, 2, 2, 2])
        ],
        expected_component_count=20,
        universe_complete=True,
    )
    assert concentrated.concentration.concentration_class == "concentrated"
    assert len(concentrated.selected) == 5
    assert all(item.weight >= 0.07 for item in concentrated.selected)


@pytest.mark.parametrize(
    (
        "symbol",
        "expected_count",
        "weights",
        "expected_class",
        "selected_count",
        "selected_weight_coverage",
    ),
    [
        ("588870.SH", 50, [9.31, 8.78, 8.45, 6.44, 5.98, 4.11, 3.65, 3.02, 2.62, 2.43], "concentrated", 5, 0.3896),
        ("510300.SH", 300, [4.27, 3.65, 2.67, 2.44, 2.12, 1.87, 1.66, 1.55, 1.40, 1.30], "moderately_diversified", 2, 0.0792),
        ("560010.SH", 1000, [0.68, 0.53, 0.48, 0.43, 0.42, 0.40, 0.38, 0.36, 0.33, 0.31], "highly_diversified", 0, 0.0),
        ("513120.SH", 38, [10.32, 9.33, 9.21, 8.32, 7.15, 6.81, 5.72, 5.15, 4.54, 3.77], "concentrated", 5, 0.4433),
        ("516010.SH", 27, [12.75, 11.44, 10.44, 8.24, 7.79, 6.57, 5.24, 4.46, 4.04, 3.52], "concentrated", 5, 0.5066),
    ],
)
def test_p4a_current_holding_etf_q1_top10_values_are_reasonable(
    symbol: str,
    expected_count: int,
    weights: list[float],
    expected_class: str,
    selected_count: int,
    selected_weight_coverage: float,
) -> None:
    selection = ETFComponentSelector().select(
        etf_symbol=symbol,
        components=[
            {"symbol": f"{symbol}-H{index:02d}", "weight": weight}
            for index, weight in enumerate(weights, start=1)
        ],
        expected_component_count=expected_count,
        universe_complete=False,
        partial_components_are_top_ranked=True,
    )

    assert selection.quality == "partial"
    assert selection.concentration.concentration_class == expected_class
    assert len(selection.selected) == selected_count
    assert selection.selected_weight_coverage == pytest.approx(selected_weight_coverage)


def test_p4a_partial_top10_is_not_misrepresented_as_full_universe() -> None:
    selection = ETFComponentSelector().select(
        etf_symbol="510300.SH",
        components=[
            {"symbol": f"S{index}", "name": f"样本{index}", "weight": weight}
            for index, weight in enumerate([3.8, 3.37, 2.85, 2.61, 2.22, 1.8, 1.7, 1.6, 1.5, 1.4])
        ],
        expected_component_count=300,
        universe_complete=False,
        partial_components_are_top_ranked=True,
    )
    assert selection.quality == "partial"
    assert selection.concentration.expected_component_count == 300
    assert selection.concentration.observed_component_count == 10
    assert selection.concentration.observed_weight_coverage == 0.2285
    assert "partial_component_universe" in selection.warnings
    assert "known_component_weight_coverage_below_50pct" in selection.warnings
    assert len(selection.selected) == 2

    unsafe_partial = ETFComponentSelector().select(
        etf_symbol="510300.SH",
        components=[{"symbol": "600000.SH", "name": "随机样本", "weight": 1.0}],
        expected_component_count=300,
        universe_complete=False,
    )
    assert unsafe_partial.quality == "insufficient"
    assert "partial_components_not_confirmed_top_ranked" in unsafe_partial.warnings


def test_p4a_force_selects_material_event_even_for_highly_diversified_etf() -> None:
    components = _equal_components(1000)
    components[777]["major_event"] = True
    components[777]["research_stale"] = True
    selection = ETFComponentSelector().select(
        etf_symbol="560010.SH",
        components=components,
        expected_component_count=1000,
        universe_complete=True,
    )
    assert [item.symbol for item in selection.selected] == ["C0777"]
    assert selection.selected[0].forced is True
    assert "major_event" in selection.selected[0].reasons
    assert "research_stale" in selection.selected[0].reasons


def test_p4a_selection_id_is_stable_and_input_changes_invalidate_it() -> None:
    selector = ETFComponentSelector()
    inputs = _equal_components(50)
    first = selector.select(
        etf_symbol="588870.SH",
        components=inputs,
        expected_component_count=50,
    )
    repeated = selector.select(
        etf_symbol="588870.SH",
        components=inputs,
        expected_component_count=50,
    )
    changed_inputs = [dict(item) for item in inputs]
    changed_inputs[0]["major_event"] = True
    changed = selector.select(
        etf_symbol="588870.SH",
        components=changed_inputs,
        expected_component_count=50,
    )
    assert first.selection_id == repeated.selection_id
    assert first.input_fingerprint == repeated.input_fingerprint
    assert changed.selection_id != first.selection_id


def test_p4a_uses_p3_cache_without_model_calls(tmp_path) -> None:
    store = ETFResearchStore(tmp_path / "research_cache.sqlite3")
    snapshot = build_etf_snapshot(
        symbol="588870.SH",
        snapshot_type="universe",
        data_as_of="2026-07-18T00:00:00+00:00",
        payload={
            "components": _equal_components(50),
            "expected_component_count": 50,
            "universe_complete": True,
            "weight_scale": "auto",
        },
        coverage_ratio=1.0,
        source_ids=["official_index_components"],
        evidence_ids=["evidence_universe"],
    )
    store.save_snapshot(snapshot)

    first, first_hit = execute_p4a_selection(store=store, universe_snapshot=snapshot)
    repeated, repeated_hit = execute_p4a_selection(store=store, universe_snapshot=snapshot)

    assert first_hit is False
    assert repeated_hit is True
    assert repeated.selection_id == first.selection_id
    metrics = store.baseline_metrics("588870.SH")
    assert metrics["module_runs"] == 1
    assert metrics["deterministic_runs"] == 1
    assert metrics["model_runs"] == 0
    assert metrics["input_tokens"] == 0
    assert metrics["output_tokens"] == 0


def test_p4a_rejects_failed_universe_snapshot(tmp_path) -> None:
    store = ETFResearchStore(tmp_path / "research_cache.sqlite3")
    snapshot = build_etf_snapshot(
        symbol="588870.SH",
        snapshot_type="universe",
        data_as_of="2026-07-18T00:00:00+00:00",
        payload={"components": _equal_components(10)},
        coverage_ratio=0.2,
        source_ids=["weak_source"],
    )
    assert snapshot.quality_status == "failed_validation"
    try:
        execute_p4a_selection(store=store, universe_snapshot=snapshot)
    except ValueError as exc:
        assert "failed or stale" in str(exc)
    else:
        raise AssertionError("failed universe snapshot should stop P4A")


def test_p4a_selection_attaches_to_etf_report_without_generating_prose(tmp_path) -> None:
    store = ETFResearchStore(tmp_path / "research_cache.sqlite3")
    reports = DeepReportService(tmp_path / "reports")
    record = reports.begin(
        session_id="session-etf",
        attempt_id="attempt-etf",
        request_content="研究 588870.SH",
        profile="etf_deep_research",
    )
    record = reports.attach_etf_analysis(record.report_id, {
        "profile": "etf_deep_research",
        "symbol": "588870.SH",
        "security_name": "科创50ETF",
        "data_as_of": "2026-07-18T00:00:00+00:00",
        "snapshot": {
            "symbol": "588870.SH",
            "data_as_of": "2026-07-18T00:00:00+00:00",
            "snapshot_ids": {
                "identity": "etfsnap_aaaaaaaaaaaaaaaaaaaaaaaa",
                "universe": "etfsnap_bbbbbbbbbbbbbbbbbbbbbbbb",
                "market": "etfsnap_cccccccccccccccccccccccc",
            },
            "coverage_ratio": 1.0,
            "price_verified": True,
        },
    })
    universe = build_etf_snapshot(
        symbol="588870.SH",
        snapshot_type="universe",
        data_as_of="2026-07-18T00:00:00+00:00",
        payload={
            "components": _equal_components(50),
            "expected_component_count": 50,
            "universe_complete": True,
        },
        coverage_ratio=1.0,
        source_ids=["official_index_components"],
        evidence_ids=["evidence_universe"],
    )
    selection, _cache_hit = execute_p4a_selection(store=store, universe_snapshot=universe)

    attached = reports.attach_etf_component_selection(record.report_id, selection.to_dict())

    module = attached.analysis_modules["holding_penetration"]
    assert module.status == "passed"
    assert module.details["selected_count"] == 3
    assert module.details["model_calls"] == 0
    assert not (tmp_path / "reports" / record.report_id / "report.md").exists()
