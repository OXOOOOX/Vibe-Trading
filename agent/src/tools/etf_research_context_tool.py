"""Prepare the deterministic ETF Snapshot, P4A selection, and P4B reuse view."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from src.agent.tools import BaseTool
from src.reports.component_research import get_component_research_service
from src.reports.etf_product_profile import (
    ETFProductProfileService,
    get_etf_product_profile_service,
)
from src.reports.etf_research import (
    ETFResearchStore,
    build_etf_snapshot,
    get_etf_research_store,
)
from src.reports.etf_universe_provider import (
    ETFUniverseUnavailableError,
    get_etf_universe_service,
)
from src.reports.instrument_profile import (
    InstrumentProfileService,
    get_instrument_profile_service,
)
from src.tools.verified_market_data_tool import VerifiedMarketDataTool


def _stable_id(prefix: str, *parts: Any) -> str:
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return f"{prefix}_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


class PrepareETFResearchTool(BaseTool):
    """Attach the existing P4 chain to the active ETF Deep Report."""

    name = "prepare_etf_research"
    description = (
        "Prepare the active ETF Deep Report from verified market data, the existing "
        "ETF Universe/P4A selection, and the existing P4B digest resolution. It never "
        "runs P4B2 generation and never creates component research implicitly. Call this "
        "before drafting ETF report sections."
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "Exact market-qualified ETF symbol, for example 588870.SH.",
            },
            "security_name": {"type": "string"},
            "as_of": {
                "type": "string",
                "description": "Optional analysis cutoff timestamp or YYYY-MM-DD.",
            },
            "force_universe_refresh": {"type": "boolean", "default": False},
        },
        "required": ["symbol", "security_name"],
    }
    is_readonly = False
    repeatable = True

    def __init__(
        self,
        default_session_id: str | None = None,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
        product_profile_service: ETFProductProfileService | None = None,
        instrument_profile_service: InstrumentProfileService | None = None,
        research_store: ETFResearchStore | None = None,
    ) -> None:
        self.default_session_id = default_session_id
        self.event_callback = event_callback
        self.product_profile_service = product_profile_service
        self.instrument_profile_service = instrument_profile_service
        self.research_store = research_store

    def execute(self, **kwargs: Any) -> str:
        symbol = str(kwargs.get("symbol") or "").strip().upper()
        security_name = str(kwargs.get("security_name") or "").strip()
        as_of = str(kwargs.get("as_of") or "").strip() or datetime.now(
            timezone.utc
        ).isoformat()
        if not symbol.endswith((".SH", ".SZ")) or len(symbol) != 9:
            return json.dumps({
                "status": "error",
                "error": "ETF research requires an exact six-digit .SH or .SZ symbol",
            }, ensure_ascii=False)
        if not security_name:
            return json.dumps({
                "status": "error",
                "error": "security_name is required",
            }, ensure_ascii=False)

        universe_service = get_etf_universe_service()
        try:
            universe = universe_service.get_or_refresh(
                symbol,
                force_refresh=bool(kwargs.get("force_universe_refresh", False)),
                as_of=as_of,
            )
        except (ETFUniverseUnavailableError, ValueError) as exc:
            provider_status = {}
            try:
                provider_status = universe_service.status(symbol)
            except Exception:
                pass
            return json.dumps({
                "status": "error",
                "error": str(exc),
                "stage": "etf_universe",
                "terminal": True,
                "attempts": list(getattr(exc, "attempts", []) or []),
                "provider_status": provider_status,
            }, ensure_ascii=False)

        end_day = date.today()
        start_day = end_day - timedelta(days=760)
        try:
            market_payload = json.loads(VerifiedMarketDataTool().execute(
                codes=[symbol],
                start_date=start_day.isoformat(),
                end_date=end_day.isoformat(),
                interval="1D",
                adjustment="raw",
            ))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return json.dumps({
                "status": "error",
                "error": f"verified ETF market data failed: {exc}",
                "stage": "market_data",
                "terminal": True,
            }, ensure_ascii=False)
        market = dict((market_payload.get("results") or {}).get(symbol) or {})
        current_price = market.get("consensus_close")
        if market.get("status") != "verified" or current_price is None:
            return json.dumps({
                "status": "error",
                "error": "ETF market data is not multi-source verified",
                "stage": "market_data",
                "market_status": market.get("status") or "unresolved",
                "terminal": True,
            }, ensure_ascii=False)

        try:
            instrument_profile = (
                self.instrument_profile_service or get_instrument_profile_service()
            ).refresh(symbol)
        except Exception:
            instrument_profile = (
                self.instrument_profile_service or get_instrument_profile_service()
            ).latest_snapshot(symbol)
        try:
            subject_profile = (
                self.product_profile_service or get_etf_product_profile_service()
            ).get_or_refresh(
                symbol,
                force_refresh=False,
                as_of=as_of,
                instrument_profile=instrument_profile,
                universe_snapshot=universe.snapshot,
            )
        except Exception as exc:
            return json.dumps({
                "status": "error",
                "error": f"ETF product profile refresh failed: {exc}",
                "stage": "etf_product_profile",
                "terminal": True,
            }, ensure_ascii=False)
        if subject_profile.get("hard_gate_status") != "passed":
            return json.dumps({
                "status": "error",
                "error": "ETF product identity or index methodology hard fields are missing",
                "stage": "etf_product_profile",
                "terminal": True,
                "missing_hard_fields": subject_profile.get("missing_hard_fields") or [],
                "source_errors": subject_profile.get("source_errors") or [],
                "subject_profile": subject_profile,
            }, ensure_ascii=False)

        snapshot = universe.snapshot
        selection = universe.selection
        component_service = get_component_research_service()
        resolution = component_service.resolve_selection(
            selection,
            as_of,
            selection_data_as_of=snapshot.data_as_of,
        )
        materialization = component_service.materialize_resolution(resolution)

        universe_urls = [
            str(value) for value in (snapshot.payload.get("source_urls") or []) if str(value)
        ]
        universe_locator = (
            universe_urls[0]
            if universe_urls
            else str((snapshot.source_ids or ["etf-universe"])[0])
        )
        universe_evidence_id = _stable_id(
            "evidence", symbol, snapshot.snapshot_id, universe_locator
        )
        market_evidence_id = _stable_id(
            "evidence", symbol, market.get("bar_time"), market.get("sources")
        )
        price_fact_id = _stable_id(
            "fact", symbol, "current_price", market.get("bar_time"), current_price
        )
        evidence = [
            {
                "evidence_id": universe_evidence_id,
                "symbol": symbol,
                "domain": "etf_universe",
                "source": universe.provider_id,
                "source_locator": universe_locator,
                "retrieved_at": snapshot.retrieved_at,
                "published_at": snapshot.data_as_of,
                "content_hash": snapshot.content_hash,
                "summary": "ETF 跟踪指数成分及权重快照",
                "status": "verified" if selection.quality == "complete" else "partial",
                "metadata": {
                    "snapshot_id": snapshot.snapshot_id,
                    "source_ids": list(snapshot.source_ids),
                    "source_type": universe.source_type,
                    "provider_id": universe.provider_id,
                },
            },
            {
                "evidence_id": market_evidence_id,
                "symbol": symbol,
                "domain": "market",
                "source": "、".join(market.get("sources") or []) or "verified_market_data",
                "source_locator": f"market-cache:{symbol}:1D:raw",
                "retrieved_at": market.get("verified_at") or as_of,
                "published_at": market.get("bar_time") or as_of,
                "content_hash": _stable_id("market", market),
                "summary": "多来源核验的 ETF 原始价格",
                "status": "verified",
                "metadata": {
                    "adjustment": "raw",
                    "interval": "1D",
                    "sources": list(market.get("sources") or []),
                    "retrieval": market.get("retrieval") or {},
                },
            },
        ]
        facts = [{
            "fact_id": price_fact_id,
            "symbol": symbol,
            "metric": "current_price",
            "value": str(current_price),
            "unit": "CNY",
            "period": str(market.get("bar_time") or as_of),
            "formula": None,
            "input_fact_ids": [],
            "evidence_ids": [market_evidence_id],
            "calculation_version": "verified-market-v1",
            "validation_status": "pass",
            "metadata": {"currency": "CNY", "adjustment": "raw"},
        }]
        product_service = self.product_profile_service or get_etf_product_profile_service()
        try:
            product_facts, product_evidence = product_service.to_report_records(
                subject_profile, base_facts=facts
            )
        except TypeError as exc:
            if "base_facts" not in str(exc):
                raise
            # One-release compatibility for injected adapters that still
            # implement the v1 method signature.
            product_facts, product_evidence = product_service.to_report_records(
                subject_profile
            )
        facts.extend(product_facts)
        evidence.extend(product_evidence)
        facts = list({str(item.get("fact_id")): item for item in facts if item.get("fact_id")}.values())
        evidence = list({
            str(item.get("evidence_id")): item
            for item in evidence if item.get("evidence_id")
        }.values())

        research_store = self.research_store or get_etf_research_store()
        market_snapshot = build_etf_snapshot(
            symbol=symbol,
            snapshot_type="market",
            data_as_of=str(market.get("bar_time") or as_of),
            payload={
                "price_verified": True,
                "current_price": current_price,
                "adjustment": "raw",
                "interval": "1D",
                "sources": list(market.get("sources") or []),
                "instrument_profile_snapshot_id": (
                    (instrument_profile or {}).get("snapshot_id")
                    if isinstance(instrument_profile, dict) else None
                ),
            },
            coverage_ratio=1.0,
            source_ids=list(market.get("sources") or []),
            fact_ids=[price_fact_id],
            evidence_ids=[market_evidence_id],
            freshness_expires_at=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
        )
        market_snapshot = research_store.save_snapshot(market_snapshot)[0]
        mapping = dict(snapshot.payload.get("mapping") or {})
        identity_snapshot_id = str(
            (subject_profile.get("snapshot_ids") or {}).get("identity") or ""
        )
        market_snapshot_id = market_snapshot.snapshot_id
        identity_fields = dict(subject_profile.get("identity") or {})
        identity_required = ("manager", "exchange", "tracked_index_code", "tracked_index_name")
        identity_coverage = sum(
            (identity_fields.get(key) or {}).get("status") == "available"
            for key in identity_required
        ) / len(identity_required)
        peer_group = dict(subject_profile.get("peer_group") or {})
        profile_quality = str(subject_profile.get("quality_status") or "failed_validation")
        selection_quality = selection.quality
        quality_status = (
            "passed"
            if selection_quality == "complete" and profile_quality == "passed"
            else "passed_with_gaps"
        )
        analysis = {
            "profile": "etf_deep_research",
            "symbol": symbol,
            "security_name": security_name,
            "data_as_of": str(market.get("bar_time") or as_of),
            "quality_status": quality_status,
            "snapshot": {
                "symbol": symbol,
                "security_name": security_name,
                "data_as_of": str(market.get("bar_time") or as_of),
                "snapshot_ids": {
                    "identity": identity_snapshot_id,
                    "universe": snapshot.snapshot_id,
                    "market": market_snapshot_id,
                    **{
                        key: value
                        for key, value in dict(subject_profile.get("snapshot_ids") or {}).items()
                        if key not in {"identity", "market", "universe"}
                    },
                },
                "coverage_ratio": snapshot.coverage_ratio,
                "price_verified": True,
                "tracking_index": (
                    (identity_fields.get("tracked_index_name") or {}).get("value")
                    or mapping.get("tracked_index_name")
                    or mapping.get("index_name")
                ),
                "tracking_index_code": (
                    (identity_fields.get("tracked_index_code") or {}).get("value")
                    or mapping.get("tracked_index_code")
                    or mapping.get("index_code")
                ),
                "current_price": current_price,
                "price_adjustment": "raw",
                "subject_profile": subject_profile,
            },
            "facts": facts,
            "evidence": evidence,
            "module_statuses": {
                "identity": {
                    "status": "passed" if identity_coverage == 1.0 else "failed_validation",
                    "coverage": identity_coverage,
                    "reason": None if identity_coverage == 1.0 else "required_identity_fields_missing",
                },
                "product_profile": {
                    "status": "passed" if profile_quality == "passed" else "warning",
                    "coverage": 1.0 - (
                        len(subject_profile.get("missing_optional_fields") or []) / 8
                    ),
                    "reason": (
                        None if profile_quality == "passed"
                        else "optional_product_fields_missing"
                    ),
                    "details": {
                        "missing_optional_fields": subject_profile.get("missing_optional_fields") or [],
                        "profile_snapshot_id": subject_profile.get("profile_snapshot_id"),
                    },
                },
                "universe": {
                    "status": "passed" if snapshot.coverage_ratio >= 0.95 else "warning",
                    "coverage": snapshot.coverage_ratio,
                },
                "market_data": {"status": "passed", "coverage": 1.0},
                "peer_flow": {
                    "status": (
                        "passed" if float(peer_group.get("unit_change_coverage_ratio") or 0.0) >= 0.80
                        else "warning"
                    ),
                    "coverage": float(peer_group.get("unit_change_coverage_ratio") or 0.0),
                    "reason": (
                        None if float(peer_group.get("unit_change_coverage_ratio") or 0.0) >= 0.80
                        else "peer_share_change_coverage_partial"
                    ),
                },
            },
            "source_statuses": {
                "universe": selection.quality,
                "market": market.get("status"),
                "product_profile": subject_profile.get("refresh_status"),
                "peer_flow": (
                    "completed" if subject_profile.get("peer_group") else "completed_with_gaps"
                ),
            },
            "research_status": {
                "selection_id": selection.selection_id,
                "resolution_id": resolution.resolution_id,
                "component_research_reuse_ratio": resolution.reuse_ratio,
            },
        }
        if self.event_callback is not None:
            self.event_callback("report.analysis_snapshot", {"analysis": analysis})
            self.event_callback(
                "report.etf_component_selection",
                {"selection": selection.to_dict()},
            )
            self.event_callback(
                "report.component_digest_resolution",
                {
                    "resolution": resolution.to_dict(),
                    "materialization": materialization,
                },
            )
        return json.dumps({
            "status": "ok",
            "symbol": symbol,
            "data_as_of": analysis["data_as_of"],
            "snapshot_ids": analysis["snapshot"]["snapshot_ids"],
            "subject_profile_snapshot_id": subject_profile.get("profile_snapshot_id"),
            "selection_id": selection.selection_id,
            "selection_quality": selection.quality,
            "selected_count": len(selection.selected),
            "explanation_coverage": selection.explanation_coverage,
            "resolution_id": resolution.resolution_id,
            "component_research": {
                "reusable": resolution.reusable_count,
                "partial_reusable": resolution.partial_reusable_count,
                "stale": resolution.stale_count,
                "missing": resolution.missing_count,
                "conflicted": resolution.conflicted_count,
            },
            "p4b2_generation_started": False,
            "model_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }, ensure_ascii=False)
