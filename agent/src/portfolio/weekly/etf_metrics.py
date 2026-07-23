"""Deterministic ETF/index relative-performance and tracking calculations.

The calculator is intentionally independent from the weekly run store.  Daily,
weekly, deep-research, and report-library consumers can reuse the same frozen
method without invoking an Agent or inventing missing official disclosures.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import statistics
from typing import Any

from src.reports.data_gaps import gap_codes, make_gap_detail, normalize_gap_details


ETF_TRACKING_METHOD_VERSION = "etf-tracking-metrics/1.0"
_CONFLICT_STATUSES = {"conflict", "unresolved", "unresolved_conflict"}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _bar_map(values: list[dict[str, Any]], *, through: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in values:
        if not isinstance(raw, dict):
            continue
        day = str(raw.get("date") or raw.get("session_date") or raw.get("bar_time") or "")[:10]
        close = _number(raw.get("close"))
        status = str(raw.get("status") or "").lower()
        if not day or day > through or close is None or status in _CONFLICT_STATUSES:
            continue
        result[day] = {**raw, "date": day, "close": close}
    return result


def _return(start: float, end: float) -> float:
    return end / start - 1.0


def _rounded(value: float) -> float:
    return round(float(value), 10)


def _sources(rows: list[dict[str, Any]]) -> list[str]:
    return sorted({
        str(source)
        for row in rows
        for source in row.get("sources") or []
        if str(source)
    })


def tracked_index_code_from_context(context: dict[str, Any]) -> str | None:
    """Resolve the tracked index only from structured product/index facts."""

    scopes = context.get("scopes") or {}
    for scope_id in ("tracking_index", "product_profile"):
        for fact in (scopes.get(scope_id) or {}).get("facts") or []:
            if str(fact.get("metric") or "") in {"tracked_index_code", "index_code"}:
                code = str(fact.get("value") or "").strip().upper()
                if code:
                    return code
    return None


def build_etf_tracking_metrics(
    *,
    etf_symbol: str,
    tracked_index_code: str,
    etf_bars: list[dict[str, Any]],
    index_bars: list[dict[str, Any]],
    week_start: str,
    week_end: str,
    annualization_sessions: int = 250,
) -> dict[str, Any]:
    """Calculate reproducible ETF/index metrics from common completed sessions."""

    etf = _bar_map(etf_bars, through=week_end)
    index = _bar_map(index_bars, through=week_end)
    common_dates = sorted(set(etf).intersection(index))
    prior_dates = [day for day in common_dates if day < week_start]
    end_available = week_end in etf and week_end in index
    baseline = prior_dates[-1] if prior_dates else None

    relative: dict[str, Any] = {
        "availability": "missing",
        "data_as_of": None,
        "metrics": {},
        "calculation_basis": {
            "method_version": ETF_TRACKING_METHOD_VERSION,
            "formula": "end_close / prior_common_close - 1; ETF return minus index return",
            "price_basis": "raw_close",
        },
    }
    if baseline and end_available:
        etf_return = _return(etf[baseline]["close"], etf[week_end]["close"])
        index_return = _return(index[baseline]["close"], index[week_end]["close"])
        gap = etf_return - index_return
        relative = {
            **relative,
            "availability": "complete",
            "data_as_of": week_end,
            "metrics": {
                "etf_market_return_1w": _rounded(etf_return),
                "tracked_index_return_1w": _rounded(index_return),
                "fund_index_return_gap_1w": _rounded(gap),
                "index_relative_strength_1w": (
                    "outperformed" if gap > 1e-10 else "underperformed" if gap < -1e-10 else "flat"
                ),
            },
            "calculation_basis": {
                **relative["calculation_basis"],
                "baseline_date": baseline,
                "end_date": week_end,
                "common_session_count": len(common_dates),
            },
        }

    daily_gaps: list[dict[str, Any]] = []
    for previous_day, current_day in zip(common_dates, common_dates[1:]):
        etf_return = _return(etf[previous_day]["close"], etf[current_day]["close"])
        index_return = _return(index[previous_day]["close"], index[current_day]["close"])
        daily_gaps.append({
            "date": current_day,
            "etf_return": etf_return,
            "index_return": index_return,
            "gap": etf_return - index_return,
        })

    tracking_metrics: dict[str, Any] = {}
    available_windows: list[int] = []
    for window in (20, 60):
        if len(daily_gaps) < window:
            continue
        trailing = daily_gaps[-window:]
        first_date = common_dates[-window - 1]
        last_date = common_dates[-1]
        deviation = statistics.stdev(item["gap"] for item in trailing) * math.sqrt(
            annualization_sessions
        )
        cumulative_gap = (
            _return(etf[first_date]["close"], etf[last_date]["close"])
            - _return(index[first_date]["close"], index[last_date]["close"])
        )
        tracking_metrics[f"market_tracking_deviation_{window}d"] = _rounded(deviation)
        tracking_metrics[f"market_return_gap_{window}d"] = _rounded(cumulative_gap)
        available_windows.append(window)

    market_tracking = {
        "availability": "complete" if 20 in available_windows else "missing",
        "data_as_of": common_dates[-1] if common_dates else None,
        "metrics": tracking_metrics,
        "available_windows": available_windows,
        "calculation_basis": {
            "method_version": ETF_TRACKING_METHOD_VERSION,
            "formula": "stddev(ETF daily return - index daily return) * sqrt(250)",
            "price_basis": "raw_close_market_price_proxy",
            "annualization_sessions": annualization_sessions,
            "common_session_count": len(common_dates),
            "official_metric": False,
        },
    }
    used_rows = [etf[day] for day in common_dates] + [index[day] for day in common_dates]
    fingerprint_payload = {
        "method_version": ETF_TRACKING_METHOD_VERSION,
        "etf_symbol": etf_symbol,
        "tracked_index_code": tracked_index_code,
        "week_start": week_start,
        "week_end": week_end,
        "common_closes": [
            [day, etf[day]["close"], index[day]["close"]] for day in common_dates
        ],
    }
    return {
        "schema_version": 1,
        "method_version": ETF_TRACKING_METHOD_VERSION,
        "etf_symbol": str(etf_symbol).upper(),
        "tracked_index_code": str(tracked_index_code).upper(),
        "week_start": week_start,
        "week_end": week_end,
        "index_relative_strength": relative,
        "market_tracking_deviation": market_tracking,
        "source_manifest": {
            "source_kind": "verified_market_data",
            "etf_sources": _sources([etf[day] for day in common_dates]),
            "index_sources": _sources([index[day] for day in common_dates]),
            "common_session_count": len(common_dates),
            "input_fingerprint": hashlib.sha256(
                json.dumps(
                    fingerprint_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "input_row_count": len(used_rows),
        },
    }


def _calculation_facts(snapshot: dict[str, Any], scope_id: str) -> list[dict[str, Any]]:
    scope = snapshot.get(scope_id) or {}
    period = scope.get("data_as_of")
    basis = scope.get("calculation_basis") or {}
    return [
        {
            "fact_id": None,
            "metric": metric,
            "value": value,
            "unit": "classification" if isinstance(value, str) else "ratio",
            "period": period,
            "formula": basis.get("formula"),
            "validation_status": "deterministic_calculation",
            "source_kind": "deterministic_calculation",
            "input_fact_ids": [],
            "evidence_ids": [],
        }
        for metric, value in (scope.get("metrics") or {}).items()
    ]


def enrich_weekly_context_with_etf_metrics(
    context: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Merge calculated scopes without mutating the catalog-owned context."""

    result = copy.deepcopy(context)
    scopes = result.setdefault("scopes", {})
    for scope_id in ("index_relative_strength", "market_tracking_deviation"):
        calculated = snapshot.get(scope_id) or {}
        scopes[scope_id] = {
            "availability": calculated.get("availability") or "missing",
            "facts": _calculation_facts(snapshot, scope_id),
            "fact_ids": [],
            "evidence_ids": [],
            "data_as_of": calculated.get("data_as_of"),
            "calculation_basis": copy.deepcopy(calculated.get("calculation_basis") or {}),
            "source_kind": "deterministic_calculation",
        }

    official = scopes.get("official_tracking_quality") or {}
    market = scopes.get("market_tracking_deviation") or {}
    complete_count = sum(
        str(item.get("availability") or "missing") == "complete"
        for item in (official, market)
    )
    scopes["tracking_error"] = {
        "availability": "complete" if complete_count == 2 else "partial" if complete_count else "missing",
        "legacy": True,
        "scope_aliases": ["official_tracking_quality", "market_tracking_deviation"],
        "official_tracking_quality": copy.deepcopy(official),
        "market_tracking_deviation": copy.deepcopy(market),
        "data_as_of": max(
            (str(item.get("data_as_of") or "") for item in (official, market)),
            default="",
        ) or None,
    }

    preserved = [
        item for item in result.get("data_gap_details") or []
        if str((item or {}).get("scope") or "")
        not in {"index_relative_strength", "market_tracking_deviation"}
    ]
    if str(scopes["index_relative_strength"].get("availability")) != "complete":
        preserved.append(make_gap_detail(
            "etf_index_relative_strength_scope_unavailable",
            source="etf_tracking_metrics",
            instrument_type="etf",
            data_as_of=snapshot.get("week_end"),
        ))
    if str(scopes["market_tracking_deviation"].get("availability")) != "complete":
        preserved.append(make_gap_detail(
            "etf_market_tracking_deviation_scope_unavailable",
            source="etf_tracking_metrics",
            instrument_type="etf",
            missing_items=["minimum_20_common_return_sessions"],
            data_as_of=(snapshot.get("market_tracking_deviation") or {}).get("data_as_of"),
        ))
    normalized = normalize_gap_details(preserved, instrument_type="etf")
    result["data_gap_details"] = normalized
    result["data_gaps"] = gap_codes(normalized)
    result["tracking_metrics"] = copy.deepcopy(snapshot)
    result.setdefault("source_manifest", {})["tracking_metrics"] = copy.deepcopy(
        snapshot.get("source_manifest") or {}
    )
    result["context_fingerprint"] = hashlib.sha256(
        json.dumps(
            {
                "catalog_context_fingerprint": context.get("context_fingerprint"),
                "tracking_input_fingerprint": (
                    snapshot.get("source_manifest") or {}
                ).get("input_fingerprint"),
                "data_gaps": result["data_gaps"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return result
