"""P4A deterministic ETF component scanning and adaptive selection."""

from __future__ import annotations

from typing import Any, Iterable, Literal

from .contracts import (
    ETFComponentObservation,
    ETFComponentSelection,
    ETFConcentrationMetrics,
    ETFResearchSnapshot,
    ETFSelectedComponent,
)
from .etf_research import (
    ETFResearchStore,
    module_input_fingerprint,
    snapshot_is_reusable,
    stable_fingerprint,
)


P4A_SELECTOR_VERSION = "1.0"
WeightScale = Literal["auto", "fraction", "percent"]


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip().replace(",", "")
    if not raw:
        return None
    percent = raw.endswith(("%", "％"))
    if percent:
        raw = raw[:-1]
    try:
        result = float(raw)
    except ValueError:
        return None
    return result / 100.0 if percent else result


def _signal_fraction(value: Any) -> float | None:
    result = _number(value)
    if result is None:
        return None
    return result / 100.0 if abs(result) > 1.0 else result


def _weight_divisor(raw_weights: list[float], scale: WeightScale) -> float:
    if scale == "fraction":
        return 1.0
    if scale == "percent":
        return 100.0
    return 100.0 if any(value > 1.0 for value in raw_weights) or sum(raw_weights) > 1.5 else 1.0


def normalize_components(
    components: Iterable[ETFComponentObservation | dict[str, Any]],
    *,
    weight_scale: WeightScale = "auto",
) -> tuple[list[ETFComponentObservation], list[str]]:
    """Normalize weights and merge duplicate component symbols."""

    raw_rows: list[dict[str, Any]] = []
    raw_weights: list[float] = []
    warnings: list[str] = []
    for item in components:
        row = item.to_dict() if isinstance(item, ETFComponentObservation) else dict(item)
        symbol = str(row.get("symbol") or row.get("code") or "").strip().upper()
        weight = _number(row.get("weight"))
        if not symbol or weight is None or weight <= 0:
            warnings.append("component_without_valid_symbol_or_weight_skipped")
            continue
        row["symbol"] = symbol
        row["_raw_weight"] = weight
        raw_rows.append(row)
        raw_weights.append(weight)
    if not raw_rows:
        return [], list(dict.fromkeys(warnings))

    divisor = _weight_divisor(raw_weights, weight_scale)
    merged: dict[str, ETFComponentObservation] = {}
    for row in raw_rows:
        symbol = row["symbol"]
        current = merged.get(symbol)
        price_contribution = _signal_fraction(row.get("price_contribution"))
        earnings_contribution = _signal_fraction(row.get("earnings_contribution"))
        weight = float(row["_raw_weight"]) / divisor
        observation = ETFComponentObservation(
            symbol=symbol,
            name=str(row.get("name") or symbol).strip(),
            weight=weight,
            price_contribution=price_contribution,
            earnings_contribution=earnings_contribution,
            major_event=bool(row.get("major_event")),
            evidence_conflict=bool(row.get("evidence_conflict")),
            research_stale=bool(row.get("research_stale")),
            metadata=dict(row.get("metadata") or {}),
        )
        if current is None:
            merged[symbol] = observation
            continue
        warnings.append("duplicate_component_symbols_merged")
        merged[symbol] = ETFComponentObservation(
            symbol=symbol,
            name=current.name or observation.name,
            weight=current.weight + observation.weight,
            price_contribution=(
                None
                if current.price_contribution is None and observation.price_contribution is None
                else (current.price_contribution or 0.0) + (observation.price_contribution or 0.0)
            ),
            earnings_contribution=(
                None
                if current.earnings_contribution is None and observation.earnings_contribution is None
                else (current.earnings_contribution or 0.0) + (observation.earnings_contribution or 0.0)
            ),
            major_event=current.major_event or observation.major_event,
            evidence_conflict=current.evidence_conflict or observation.evidence_conflict,
            research_stale=current.research_stale or observation.research_stale,
            metadata={**current.metadata, **observation.metadata},
        )
    normalized = sorted(merged.values(), key=lambda item: (-item.weight, item.symbol))
    coverage = sum(item.weight for item in normalized)
    if coverage > 1.05:
        warnings.append("component_weights_renormalized_above_105_percent")
        normalized = [
            ETFComponentObservation(
                **{
                    **item.to_dict(),
                    "weight": item.weight / coverage,
                }
            )
            for item in normalized
        ]
    return normalized, list(dict.fromkeys(warnings))


def _concentration_metrics(
    components: list[ETFComponentObservation],
    *,
    expected_component_count: int,
) -> ETFConcentrationMetrics:
    weights = [item.weight for item in components]
    observed_count = len(weights)
    expected_count = max(expected_component_count, observed_count)
    coverage = min(1.0, sum(weights))
    remaining = max(0.0, 1.0 - coverage)
    unknown_count = max(0, expected_count - observed_count)
    observed_hhi = sum(value * value for value in weights)
    hhi_lower = observed_hhi + (
        remaining * remaining / unknown_count if unknown_count else 0.0
    )
    hhi_upper = min(1.0, observed_hhi + (remaining * remaining if unknown_count else 0.0))
    effective_lower = 1.0 / hhi_upper if hhi_upper > 0 else float(expected_count or 0)

    def top(count: int) -> float:
        return min(1.0, sum(weights[:count]))

    top1, top3, top5, top10 = top(1), top(3), top(5), top(10)
    if expected_count >= 500:
        concentration_class = "highly_diversified"
        minimum, maximum = 0, 2
    elif expected_count >= 100:
        concentration_class = "moderately_diversified"
        minimum, maximum = 2, 3
    elif top1 >= 0.08 or top10 >= 0.45 or effective_lower <= 25:
        concentration_class = "concentrated"
        minimum, maximum = 3, 5
    elif expected_count >= 30:
        concentration_class = "focused"
        minimum, maximum = 3, 5
    else:
        concentration_class = "concentrated"
        minimum, maximum = 3, 5
    return ETFConcentrationMetrics(
        concentration_class=concentration_class,  # type: ignore[arg-type]
        expected_component_count=expected_count,
        observed_component_count=observed_count,
        observed_weight_coverage=round(coverage, 8),
        top1_weight=round(top1, 8),
        top3_weight=round(top3, 8),
        top5_weight=round(top5, 8),
        top10_weight=round(top10, 8),
        hhi_lower_bound=round(hhi_lower, 10),
        hhi_upper_bound=round(hhi_upper, 10),
        effective_component_count_lower_bound=round(effective_lower, 4),
        min_penetration_count=minimum,
        max_penetration_count=maximum,
    )


def _candidate(component: ETFComponentObservation) -> ETFSelectedComponent:
    price = abs(component.price_contribution or 0.0)
    earnings = abs(component.earnings_contribution or 0.0)
    forced = (
        component.weight >= 0.08
        or price >= 0.05
        or earnings >= 0.05
        or component.major_event
        or component.evidence_conflict
        or component.research_stale
    )
    reasons: list[str] = []
    if component.weight >= 0.08:
        reasons.append("weight_at_least_8pct")
    elif component.weight >= 0.05:
        reasons.append("weight_at_least_5pct")
    else:
        reasons.append("structural_representative")
    if price >= 0.05:
        reasons.append("material_price_contribution")
    if earnings >= 0.05:
        reasons.append("material_earnings_contribution")
    if component.major_event:
        reasons.append("major_event")
    if component.evidence_conflict:
        reasons.append("evidence_conflict")
    if component.research_stale:
        reasons.append("research_stale")
    score = (
        min(component.weight / 0.10, 1.0) * 0.55
        + min(price / 0.10, 1.0) * 0.18
        + min(earnings / 0.10, 1.0) * 0.12
        + (0.08 if component.major_event else 0.0)
        + (0.05 if component.evidence_conflict else 0.0)
        + (0.02 if component.research_stale else 0.0)
    )
    return ETFSelectedComponent(
        symbol=component.symbol,
        name=component.name,
        weight=round(component.weight, 8),
        score=round(min(score, 1.0), 8),
        marginal_explanation_gain=round(max(component.weight, price, earnings), 8),
        forced=forced,
        reasons=reasons,
        price_contribution=component.price_contribution,
        earnings_contribution=component.earnings_contribution,
    )


class ETFComponentSelector:
    """Select at most five components after a deterministic full-universe scan."""

    def __init__(self, *, marginal_gain_floor: float = 0.05) -> None:
        if not 0.0 <= marginal_gain_floor <= 1.0:
            raise ValueError("marginal_gain_floor must be between 0 and 1")
        self.marginal_gain_floor = marginal_gain_floor

    def select(
        self,
        *,
        etf_symbol: str,
        components: Iterable[ETFComponentObservation | dict[str, Any]],
        expected_component_count: int | None = None,
        universe_complete: bool = True,
        partial_components_are_top_ranked: bool = False,
        weight_scale: WeightScale = "auto",
    ) -> ETFComponentSelection:
        normalized, warnings = normalize_components(components, weight_scale=weight_scale)
        observed_count = len(normalized)
        expected_count = max(int(expected_component_count or observed_count), observed_count)
        if not normalized:
            concentration = _concentration_metrics([], expected_component_count=expected_count)
            fingerprint = stable_fingerprint("p4ainput", {
                "etf_symbol": etf_symbol.upper(), "components": [],
                "expected_component_count": expected_count,
                "universe_complete": universe_complete,
                "partial_components_are_top_ranked": partial_components_are_top_ranked,
                "selector_version": P4A_SELECTOR_VERSION,
            })
            return ETFComponentSelection(
                selection_id=stable_fingerprint("p4aselection", fingerprint),
                etf_symbol=etf_symbol.upper(),
                input_fingerprint=fingerprint,
                quality="insufficient",
                concentration=concentration,
                selected=[],
                selected_weight_coverage=0.0,
                explanation_coverage=0.0,
                stop_reason="no_valid_components",
                warnings=[*warnings, "component_universe_missing"],
            )

        concentration = _concentration_metrics(
            normalized,
            expected_component_count=expected_count,
        )
        coverage = concentration.observed_weight_coverage
        complete = (
            universe_complete
            and observed_count >= expected_count
            and coverage >= 0.90
        )
        partial_is_usable = universe_complete or partial_components_are_top_ranked
        quality = "complete" if complete else "partial" if partial_is_usable else "insufficient"
        if not complete:
            warnings.append("partial_component_universe")
        if not partial_is_usable:
            warnings.append("partial_components_not_confirmed_top_ranked")
        if coverage < 0.50:
            warnings.append("known_component_weight_coverage_below_50pct")

        candidates = [_candidate(item) for item in normalized]
        candidates.sort(key=lambda item: (not item.forced, -item.score, -item.weight, item.symbol))
        selected: list[ETFSelectedComponent] = []
        stop_reason = "candidate_limit_reached"
        maximum = concentration.max_penetration_count
        minimum = concentration.min_penetration_count
        forced_count = sum(1 for item in candidates if item.forced)
        if forced_count > maximum:
            warnings.append("forced_candidates_exceed_hard_limit")
        for candidate in candidates:
            if len(selected) >= maximum:
                stop_reason = "hard_limit_reached"
                break
            if candidate.forced or len(selected) < minimum:
                selected.append(candidate)
                continue
            if candidate.marginal_explanation_gain >= self.marginal_gain_floor:
                selected.append(candidate)
                continue
            stop_reason = "marginal_explanation_gain_below_5pct"
            break
        else:
            stop_reason = "all_candidates_considered"
        if len(selected) < minimum:
            warnings.append("fewer_candidates_than_profile_minimum")

        input_payload = {
            "etf_symbol": etf_symbol.upper(),
            "components": [item.to_dict() for item in normalized],
            "expected_component_count": expected_count,
            "universe_complete": universe_complete,
            "partial_components_are_top_ranked": partial_components_are_top_ranked,
            "marginal_gain_floor": self.marginal_gain_floor,
            "selector_version": P4A_SELECTOR_VERSION,
        }
        fingerprint = stable_fingerprint("p4ainput", input_payload)
        selection_payload = {
            "input_fingerprint": fingerprint,
            "selected": [item.to_dict() for item in selected],
            "concentration": concentration.to_dict(),
            "quality": quality,
            "stop_reason": stop_reason,
        }
        return ETFComponentSelection(
            selection_id=stable_fingerprint("p4aselection", selection_payload),
            etf_symbol=etf_symbol.upper(),
            input_fingerprint=fingerprint,
            quality=quality,  # type: ignore[arg-type]
            concentration=concentration,
            selected=selected,
            selected_weight_coverage=round(sum(item.weight for item in selected), 8),
            explanation_coverage=round(min(1.0, sum(
                item.marginal_explanation_gain for item in selected
            )), 8),
            stop_reason=stop_reason,
            warnings=list(dict.fromkeys(warnings)),
        )


def _selection_from_dict(payload: dict[str, Any]) -> ETFComponentSelection:
    concentration = ETFConcentrationMetrics(**dict(payload["concentration"]))
    selected = [ETFSelectedComponent(**dict(item)) for item in payload.get("selected") or []]
    return ETFComponentSelection(
        selection_id=str(payload["selection_id"]),
        etf_symbol=str(payload["etf_symbol"]),
        input_fingerprint=str(payload["input_fingerprint"]),
        quality=payload["quality"],
        concentration=concentration,
        selected=selected,
        selected_weight_coverage=float(payload.get("selected_weight_coverage") or 0.0),
        explanation_coverage=float(payload.get("explanation_coverage") or 0.0),
        stop_reason=str(payload.get("stop_reason") or ""),
        warnings=[str(item) for item in payload.get("warnings") or []],
        created_at=str(payload.get("created_at") or ""),
    )


def selection_from_dict(payload: dict[str, Any]) -> ETFComponentSelection:
    """Deserialize the public P4A contract for deterministic API workflows."""

    return _selection_from_dict(payload)


def execute_p4a_selection(
    *,
    store: ETFResearchStore,
    universe_snapshot: ETFResearchSnapshot,
    selector: ETFComponentSelector | None = None,
    event_symbols: Iterable[str] = (),
) -> tuple[ETFComponentSelection, bool]:
    """Run P4A through the P3 module cache; no model or token budget is used."""

    if universe_snapshot.snapshot_type != "universe":
        raise ValueError("P4A requires an ETF universe snapshot")
    if not snapshot_is_reusable(universe_snapshot):
        raise ValueError("ETF universe snapshot is failed or stale")
    payload = dict(universe_snapshot.payload)
    forced_event_symbols = {
        str(item or "").strip().upper()
        for item in event_symbols
        if str(item or "").strip()
    }
    components = []
    for item in payload.get("components") or []:
        row = item.to_dict() if isinstance(item, ETFComponentObservation) else dict(item)
        if str(row.get("symbol") or row.get("code") or "").strip().upper() in forced_event_symbols:
            row["major_event"] = True
        components.append(row)
    expected_count = payload.get("expected_component_count")
    universe_complete = bool(payload.get("universe_complete", False))
    partial_top_ranked = bool(payload.get("partial_components_are_top_ranked", False))
    weight_scale = str(payload.get("weight_scale") or "auto")
    if weight_scale not in {"auto", "fraction", "percent"}:
        raise ValueError("invalid ETF universe weight_scale")
    active_selector = selector or ETFComponentSelector()
    preview = active_selector.select(
        etf_symbol=universe_snapshot.symbol,
        components=components,
        expected_component_count=int(expected_count) if expected_count is not None else None,
        universe_complete=universe_complete,
        partial_components_are_top_ranked=partial_top_ranked,
        weight_scale=weight_scale,  # type: ignore[arg-type]
    )
    fingerprint = module_input_fingerprint(
        module_id="holding_penetration",
        snapshot_ids=[universe_snapshot.snapshot_id],
        dependency_ids=[
            preview.input_fingerprint,
            f"p4a:{P4A_SELECTOR_VERSION}",
            *[f"event:{item}" for item in sorted(forced_event_symbols)],
        ],
        model_id="deterministic",
    )

    def runner() -> dict[str, Any]:
        return {
            "status": (
                "passed" if preview.quality == "complete"
                else "warning" if preview.quality == "partial"
                else "insufficient_evidence"
            ),
            "selection": preview.to_dict(),
            "selector_version": P4A_SELECTOR_VERSION,
            "model_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }

    cached, cache_hit = store.execute_module(
        symbol=universe_snapshot.symbol,
        module_id="holding_penetration",
        input_fingerprint=fingerprint,
        runner=runner,
        model_id="deterministic",
    )
    selection_payload = dict(cached.result.get("selection") or {})
    return _selection_from_dict(selection_payload), cache_hit
