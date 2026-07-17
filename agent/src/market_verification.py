"""Verified market-data cache built from multiple loader sources."""

from __future__ import annotations

import json
import os
import statistics
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.market_data import fetch_market_data


FetchMarketData = Callable[..., dict[str, Any]]


# Canonical price-basis contract shared by every market. Provider terminology
# is mapped into these definitions; a series is never relabelled merely to make
# a request succeed.
PRICE_BASIS_DEFINITIONS: dict[str, dict[str, str | bool]] = {
    "raw": {
        "label": "unadjusted event-time price",
        "corporate_actions": "none",
        "anchor": "event_time",
        "cross_source_comparable": True,
        "definition": (
            "OHLC as traded on each historical date, with no split, dividend, "
            "rights, or other corporate-action adjustment."
        ),
    },
    "qfq": {
        "label": "latest-anchored total-return adjusted price",
        "corporate_actions": "splits_and_distributions",
        "anchor": "latest_available_bar",
        "cross_source_comparable": True,
        "definition": (
            "OHLC scaled for share-count changes and cash distributions, anchored "
            "so the latest adjusted close equals the latest unadjusted close."
        ),
    },
    "hfq": {
        "label": "earliest-anchored corporate-action adjusted price",
        "corporate_actions": "splits_and_distributions",
        "anchor": "listing_or_full_history_start",
        "cross_source_comparable": True,
        "definition": (
            "OHLC scaled for share-count changes and cash distributions while "
            "preserving the earliest full-history price basis."
        ),
    },
    "split_adjusted": {
        "label": "latest-share-unit split-adjusted price",
        "corporate_actions": "splits_only",
        "anchor": "latest_share_unit",
        "cross_source_comparable": True,
        "definition": (
            "Historical OHLC restated for splits or consolidations but not for "
            "cash dividends; this must not be mixed with raw or qfq."
        ),
    },
    "source_default": {
        "label": "unclassified provider price basis",
        "corporate_actions": "unknown",
        "anchor": "unknown",
        "cross_source_comparable": False,
        "definition": (
            "The adapter has not proved the provider's adjustment semantics; "
            "the series is excluded from cross-source price consensus."
        ),
    },
    "unknown": {
        "label": "unknown price basis",
        "corporate_actions": "unknown",
        "anchor": "unknown",
        "cross_source_comparable": False,
        "definition": "No reliable price-basis metadata is available.",
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def verified_cache_dir() -> Path:
    override = os.getenv("VIBE_TRADING_VERIFIED_MARKET_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".vibe-trading" / "cache" / "verified_market_data"


def _safe_name(symbol: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in symbol)


def normalize_adjustment(value: str | None) -> str:
    text = str(value or "source_default").strip().lower()
    aliases = {
        "none": "raw",
        "no_adjust": "raw",
        "unadjusted": "raw",
        "front": "qfq",
        "forward": "qfq",
        "forward_adjusted": "qfq",
        "back": "hfq",
        "backward": "hfq",
        "back_adjusted": "hfq",
        "default": "source_default",
        "auto": "source_default",
    }
    text = aliases.get(text, text)
    if text not in {
        "raw",
        "qfq",
        "hfq",
        "split_adjusted",
        "source_default",
        "unknown",
    }:
        return "source_default"
    return text


def source_adjustment_policy(source: str, symbol: str) -> dict[str, Any]:
    """Return the adjustment semantics implied by current loader code."""
    src = source.lower()
    upper = symbol.upper()
    is_cn = upper.endswith((".SH", ".SZ", ".BJ"))
    is_cn_etf = is_cn and upper[:2] in {"15", "16", "50", "51", "52", "56", "58"}

    if src in {"tencent", "eastmoney", "baostock"}:
        return {
            "adjustment": "qfq",
            "confidence": "loader_code",
            "note": "Current loader implementation requests forward-adjusted China daily bars.",
        }
    if src == "akshare" and is_cn_etf:
        return {
            "adjustment": "source_default",
            "confidence": "loader_code",
            "note": "AKShare ETF path uses fund_etf_hist_sina; adjustment semantics are source-default.",
        }
    if src == "akshare" and is_cn:
        return {
            "adjustment": "qfq",
            "confidence": "loader_code",
            "note": "Current AKShare A-share path requests adjust='qfq'.",
        }
    if src == "akshare" and upper.endswith((".US", ".HK")):
        return {
            "adjustment": "qfq",
            "confidence": "loader_code",
            "note": "Current AKShare overseas-equity path requests adjust='qfq'.",
        }
    if src in {"yahoo", "yfinance"} and upper.endswith((".US", ".HK")):
        return {
            "adjustment": "split_adjusted",
            "confidence": "loader_contract",
            "note": (
                "Yahoo quote OHLC is restated for splits but excludes the cash-"
                "distribution adjustment carried by adjusted_close."
            ),
        }
    if src == "sina" and upper.endswith(".US"):
        return {
            "adjustment": "raw",
            "confidence": "loader_contract",
            "note": "Sina US daily K-line OHLC preserves event-time prices.",
        }
    return {
        "adjustment": "source_default",
        "confidence": "unknown",
        "note": "Adjustment semantics are not declared by this loader.",
    }


def _records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        payload = payload["data"]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def _last_observation(symbol: str, source: str, payload: Any) -> dict[str, Any] | None:
    rows = _records(payload)
    if not rows:
        return None
    row = rows[-1]
    close = None
    for key in ("close", "Close", "last", "price"):
        if key in row and row[key] is not None:
            close = float(row[key])
            break
    if close is None:
        return None
    obs_date = row.get("trade_date") or row.get("date") or row.get("datetime") or row.get("timestamp")
    return {"symbol": symbol, "source": source, "date": obs_date, "close": close, "row": row}


def default_sources(symbol: str) -> list[str]:
    text = symbol.upper()
    if text.endswith((".SH", ".SZ", ".BJ")):
        return ["tencent", "eastmoney", "sina"]
    if text.endswith(".US"):
        return ["yahoo", "stooq"]
    if text.endswith(".HK"):
        return ["yahoo"]
    if text.endswith("-USDT") or text.endswith("/USDT"):
        return ["okx", "ccxt"]
    return ["auto"]


def verify_market_data(
    *,
    codes: list[str],
    start_date: str,
    end_date: str,
    sources: list[str] | None = None,
    interval: str = "1D",
    adjustment: str = "source_default",
    tolerance_pct: float = 0.5,
    max_rows: int = 10,
    fetcher: FetchMarketData = fetch_market_data,
    cache_dir: Path | None = None,
) -> dict[str, Any]:
    cache_dir = cache_dir or verified_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    requested_adjustment = normalize_adjustment(adjustment)

    for code in codes:
        observations: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        source_list = sources or default_sources(code)
        source_adjustments = {
            source: source_adjustment_policy(source, code)
            for source in source_list
        }
        for source in source_list:
            try:
                payload = fetcher(
                    codes=[code],
                    start_date=start_date,
                    end_date=end_date,
                    source=source,
                    interval=interval,
                    max_rows=max_rows,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append({"source": source, "error": str(exc)})
                continue
            symbol_payload = payload.get(code)
            if symbol_payload is None and isinstance(payload, dict):
                for key, value in payload.items():
                    if str(key).upper() == code.upper():
                        symbol_payload = value
                        break
            obs = _last_observation(code, source, symbol_payload)
            if obs:
                policy = source_adjustments[source]
                obs["adjustment"] = policy["adjustment"]
                obs["adjustment_confidence"] = policy["confidence"]
                observations.append(obs)

        comparable_bases = {
            name
            for name, definition in PRICE_BASIS_DEFINITIONS.items()
            if definition["cross_source_comparable"]
        }
        actual_adjustment: str | None = None
        compatible: list[dict[str, Any]] = []
        if requested_adjustment in comparable_bases:
            actual_adjustment = requested_adjustment
            compatible = [
                obs
                for obs in observations
                if obs.get("adjustment") == requested_adjustment
            ]
        else:
            groups: dict[str, list[dict[str, Any]]] = {}
            for obs in observations:
                basis = str(obs.get("adjustment") or "unknown")
                if basis in comparable_bases:
                    groups.setdefault(basis, []).append(obs)
            ranked = sorted(groups.items(), key=lambda item: len(item[1]), reverse=True)
            if ranked and (
                len(ranked) == 1 or len(ranked[0][1]) > len(ranked[1][1])
            ):
                actual_adjustment, compatible = ranked[0]

        for obs in observations:
            obs["included_in_consensus"] = obs in compatible
            obs["exclude_reason"] = (
                None if obs in compatible else "price_basis_mismatch_or_unclassified"
            )

        closes = [obs["close"] for obs in compatible]
        status = "unresolved_basis" if observations and not compatible else "unresolved"
        spread_pct = None
        consensus_close = None
        if len(closes) == 1:
            status = "single_source"
            consensus_close = closes[0]
            spread_pct = 0.0
        elif len(closes) > 1:
            median = statistics.median(closes)
            consensus_close = median
            spread_pct = ((max(closes) - min(closes)) / median * 100.0) if median else None
            status = "verified" if spread_pct is not None and spread_pct <= tolerance_pct else "conflict"

        item = {
            "symbol": code,
            "status": status,
            "consensus_close": consensus_close,
            "spread_pct": spread_pct,
            "tolerance_pct": tolerance_pct,
            "requested_adjustment": requested_adjustment,
            "actual_adjustment": actual_adjustment,
            "source_adjustments": source_adjustments,
            "adjustment_warning": (
                "Observations with a different or unclassified price basis were "
                "excluded from consensus."
                if len(compatible) != len(observations)
                else None
            ),
            "start_date": start_date,
            "end_date": end_date,
            "interval": interval,
            "sources": source_list,
            "observations": observations,
            "errors": errors,
            "verified_at": _now(),
        }
        path = cache_dir / f"{_safe_name(code)}__adj-{requested_adjustment}.json"
        _write_json(path, item)
        item["cache_path"] = str(path)
        results[code] = item

    return {"status": "ok", "cache_dir": str(cache_dir), "results": results}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(text)
        tmp_name = handle.name
    Path(tmp_name).replace(path)


def verify_market_data_json(**kwargs: Any) -> str:
    return json.dumps(verify_market_data(**kwargs), ensure_ascii=False, indent=2)
