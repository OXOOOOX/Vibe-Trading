"""Tool for multi-source verified market-data cache."""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools import BaseTool
from src.market_data import DEFAULT_MAX_ROWS
from src.market_cache import get_market_refresh_service
from src.market_verification import normalize_adjustment


def _summary_map(
    rows: list[dict[str, Any]], *, codes: set[str], interval: str, adjustment: str
) -> dict[str, dict[str, Any]]:
    return {
        str(row["symbol"]).upper(): dict(row)
        for row in rows
        if str(row.get("symbol") or "").upper() in codes
        and row.get("interval") == interval
        and row.get("actual_adjustment") == adjustment
    }


def _cache_comparison(
    cached: dict[str, Any], refreshed: dict[str, Any], *, tolerance_pct: float
) -> dict[str, Any]:
    cached_close = cached.get("consensus_close")
    refreshed_close = refreshed.get("consensus_close")
    absolute_change = None
    change_pct = None
    if cached_close is not None and refreshed_close is not None:
        absolute_change = float(refreshed_close) - float(cached_close)
        if float(cached_close) != 0:
            change_pct = absolute_change / float(cached_close) * 100.0
    same_bar_time = cached.get("bar_time") == refreshed.get("bar_time")
    if not same_bar_time:
        comparison_status = "different_bar_time"
    elif change_pct is None:
        comparison_status = "not_comparable"
    elif abs(change_pct) <= tolerance_pct:
        comparison_status = "within_tolerance"
    else:
        comparison_status = "cache_mismatch"
    return {
        "comparison_type": "live_vs_pre_refresh_cache",
        "independent_source": False,
        "note": (
            "The cache is a prior local snapshot used for continuity/fallback, "
            "not an independent market-data provider."
        ),
        "cached_close": cached_close,
        "cached_bar_time": cached.get("bar_time"),
        "cached_verified_at": cached.get("verified_at"),
        "refreshed_close": refreshed_close,
        "refreshed_bar_time": refreshed.get("bar_time"),
        "refreshed_verified_at": refreshed.get("verified_at"),
        "same_bar_time": same_bar_time,
        "comparison_status": comparison_status,
        "tolerance_pct": tolerance_pct,
        "absolute_change": absolute_change,
        "change_pct": change_pct,
    }


class VerifiedMarketDataTool(BaseTool):
    """Fetch market data from multiple sources, check consistency, and cache it."""

    name = "verified_market_data"
    description = (
        "Fetch exact symbols from multiple market-data sources, compare latest "
        "close prices within a tolerance, and write the verified/conflict result "
        "with provenance to the local verified market-data cache. If every live "
        "source fails, return the pre-existing cached snapshot as an explicitly "
        "labelled fallback. If live data succeeds and a prior cache exists, also "
        "return a live-versus-cache continuity comparison. Use this for price-sensitive "
        "portfolio analysis, especially ETF holdings."
    )
    repeatable = True
    is_readonly = False
    parameters = {
        "type": "object",
        "properties": {
            "codes": {
                "type": "array",
                "items": {"type": "string"},
                "description": 'Exact normalized symbols, e.g. ["510300.SH", "159842.SZ"].',
            },
            "start_date": {"type": "string", "description": "Start date in YYYY-MM-DD format."},
            "end_date": {"type": "string", "description": "End date in YYYY-MM-DD format."},
            "sources": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional explicit source list. Defaults by market.",
            },
            "interval": {"type": "string", "default": "1D"},
            "adjustment": {
                "type": "string",
                "enum": ["source_default", "raw", "qfq", "hfq", "unknown"],
                "default": "source_default",
                "description": (
                    "Price adjustment basis to record in the cache: raw/unadjusted, "
                    "qfq/front-adjusted, hfq/back-adjusted, source_default, or unknown. "
                    "Current loaders may have fixed adjustment behavior; the cache records "
                    "both requested_adjustment and source_adjustments."
                ),
            },
            "tolerance_pct": {
                "type": "number",
                "default": 0.5,
                "description": "Max allowed percent spread between sources before status=conflict.",
            },
            "max_rows": {
                "type": "integer",
                "default": DEFAULT_MAX_ROWS,
                "description": "Per-source row cap before selecting the last observation.",
            },
        },
        "required": ["codes", "start_date", "end_date"],
    }

    def execute(self, **kwargs: Any) -> str:
        interval = kwargs.get("interval", "1D")
        requested_adjustment = normalize_adjustment(kwargs.get("adjustment", "source_default"))
        adjustment = (
            "raw" if interval != "1D" else "qfq"
        ) if requested_adjustment == "source_default" else requested_adjustment
        service = get_market_refresh_service()
        codes = {str(code).upper() for code in kwargs["codes"]}
        tolerance_pct = float(kwargs.get("tolerance_pct", 0.5))
        cached_before = _summary_map(
            service.store.cache_summaries(limit=1000),
            codes=codes,
            interval=interval,
            adjustment=adjustment,
        )
        run = service.refresh_sync(
            symbols=kwargs["codes"],
            profile="agent_verified",
            sources=kwargs.get("sources"),
            force=False,
            start_date=kwargs["start_date"],
            end_date=kwargs["end_date"],
            items=[(interval, adjustment)],
        )
        refreshed = _summary_map(
            service.store.cache_summaries(limit=1000),
            codes=codes,
            interval=interval,
            adjustment=adjustment,
        )
        run_items = {
            str(item.get("symbol") or "").upper(): item
            for item in run.get("items") or []
            if item.get("interval") == interval and item.get("adjustment") == adjustment
        }
        result_by_symbol: dict[str, dict[str, Any]] = {}
        modes: set[str] = set()
        for code in sorted(codes):
            item = run_items.get(code) or {}
            live_sources = list(item.get("actual_sources") or [])
            source_fetch_succeeded = bool(live_sources)
            prior = cached_before.get(code)
            current = refreshed.get(code)
            live_succeeded = source_fetch_succeeded and current is not None

            if live_succeeded:
                mode = "live_with_cache_comparison" if prior else "live_only"
                selected = dict(current or {})
            elif prior:
                mode = "cache_fallback"
                selected = dict(prior)
            else:
                mode = "unresolved"
                selected = dict(current or {})
            modes.add(mode)

            retrieval = {
                "mode": mode,
                "live_fetch_succeeded": live_succeeded,
                "source_fetch_succeeded": source_fetch_succeeded,
                "live_result_usable": live_succeeded,
                "live_sources": live_sources,
                "cache_available_before_fetch": prior is not None,
                "cache_fallback_used": mode == "cache_fallback",
                "refresh_item_status": item.get("status"),
                "refresh_message": item.get("message"),
            }
            if live_succeeded and prior and current:
                retrieval["cache_comparison"] = _cache_comparison(
                    prior,
                    current,
                    tolerance_pct=tolerance_pct,
                )
            selected["retrieval"] = retrieval
            result_by_symbol[code] = selected

        if len(modes) == 1:
            data_mode = next(iter(modes))
        else:
            data_mode = "mixed"
        return json.dumps(
            {
                "status": run["status"],
                "run_id": run["run_id"],
                "data_mode": data_mode,
                "cache_dir": str(service.store.path.parent),
                "cache_policy": {
                    "fallback_on_live_failure": True,
                    "compare_live_to_pre_refresh_cache": True,
                    "cache_is_independent_source": False,
                },
                "requested_adjustment": requested_adjustment,
                "actual_adjustment": adjustment,
                "results": result_by_symbol,
            },
            ensure_ascii=False,
            indent=2,
        )
