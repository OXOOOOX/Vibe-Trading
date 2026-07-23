"""REST routes for the unified report library."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field

from src.reports.component_research import (
    ComponentResearchDigestService,
    ComponentResearchDigestStore,
)
from src.reports.catalog import (
    ReportLibraryService,
    get_report_library_service,
    report_library_enabled,
)
from src.reports.etf_penetration import selection_from_dict
from src.reports.etf_product_profile import ETFProductProfileService
from src.reports.instrument_historical_percentile import (
    InstrumentHistoricalPercentileService,
)
from src.reports.instrument_profile import InstrumentProfileService
from src.reports.source_bundle import build_knowledge_source_bundle


class ReportComparisonItem(BaseModel):
    report_id: str = Field(min_length=1, max_length=120)
    horizon: str = Field(min_length=1, max_length=32)


class ReportComparisonRequest(BaseModel):
    items: list[ReportComparisonItem] = Field(min_length=2, max_length=4)
    include_ai_summary: bool = False


class ComponentResolutionRequest(BaseModel):
    selection: dict[str, Any]
    analysis_as_of: str = Field(min_length=10, max_length=80)
    selection_data_as_of: str | None = Field(None, min_length=10, max_length=80)


class AnnualReportBackfillRequest(BaseModel):
    years: list[int] = Field(min_length=1, max_length=12)
    force: bool = False


def _artifact_url(artifact: dict[str, Any]) -> str | None:
    locator = str(artifact.get("source_locator") or "")
    if locator.startswith("deep-report:"):
        _, report_id, artifact_id = locator.split(":", 2)
        return f"/reports/{report_id}/artifacts/{artifact_id}"
    if locator.startswith("daily-run:"):
        _, run_id, artifact_id = locator.split(":", 2)
        return f"/portfolio/daily-runs/{run_id}/artifacts/{artifact_id}"
    if locator.startswith("weekly-run:"):
        _, run_id, artifact_id = locator.split(":", 2)
        return f"/portfolio/weekly-runs/{run_id}/artifacts/{artifact_id}"
    return None


def _public_report(
    report: dict[str, Any],
    *,
    daily_service: Any | None = None,
    weekly_service: Any | None = None,
    deep_report_service: Any | None = None,
) -> dict[str, Any]:
    value = dict(report)
    value["artifacts"] = [
        {**dict(item), "url": _artifact_url(dict(item))}
        for item in report.get("artifacts") or []
    ]
    value["monitoring_bundle"] = None
    value["weekly_review"] = None
    locator = str(
        (report.get("knowledge_link") or {}).get("monitoring_bundle_source_locator")
        if isinstance(report.get("knowledge_link"), dict)
        else ""
    )
    if daily_service is not None and locator.startswith("daily-run:"):
        try:
            _, run_id, artifact_id = locator.split(":", 2)
            resolved = daily_service.store.resolve_artifact(run_id, artifact_id)
            if resolved is not None:
                _, path = resolved
                payload = json.loads(path.read_text(encoding="utf-8"))
                bundle = payload.get("monitoring_bundle") if isinstance(payload, dict) else None
                if isinstance(bundle, dict):
                    value["monitoring_bundle"] = bundle
        except (OSError, ValueError, json.JSONDecodeError, AttributeError):
            # The immutable catalog entry remains usable when an old/expired
            # artifact cannot be expanded.
            pass
    if weekly_service is not None and locator.startswith("weekly-run:"):
        try:
            _, run_id, artifact_id = locator.split(":", 2)
            resolved = weekly_service.store.resolve_artifact(run_id, artifact_id)
            if resolved is not None:
                _, path = resolved
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    bundle = payload.get("monitoring_bundle")
                    if isinstance(bundle, dict):
                        value["monitoring_bundle"] = bundle
                    value["weekly_review"] = {
                        key: payload.get(key)
                        for key in (
                            "week_start",
                            "week_end",
                            "generated_at",
                            "data_as_of",
                            "valid_from",
                            "valid_until",
                            "review_due_at",
                            "source_valid_until",
                            "weekly_view",
                            "previous_week_validation",
                            "key_levels",
                            "scenario_changes",
                            "data_gaps",
                            "quality_status",
                            "coverage_status",
                        )
                    }
        except (OSError, ValueError, json.JSONDecodeError, AttributeError):
            pass
    if deep_report_service is not None and locator.startswith("deep-report:"):
        try:
            _, report_id, artifact_id = locator.split(":", 2)
            path = deep_report_service.artifact_path(report_id, artifact_id)
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                value["monitoring_bundle"] = payload
        except (OSError, ValueError, KeyError, json.JSONDecodeError, AttributeError):
            pass
    return value


def _etf_universe_profile(snapshot: Any | None) -> dict[str, Any] | None:
    """Project the latest immutable universe snapshot into the subject dossier."""

    if snapshot is None:
        return None
    raw = snapshot.to_dict()
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    components = [
        {
            "symbol": str(item.get("symbol") or ""),
            "name": str(item.get("name") or item.get("symbol") or ""),
            "weight": float(item.get("weight") or 0.0),
            "metadata": dict(item.get("metadata") or {}),
        }
        for item in payload.get("components") or []
        if isinstance(item, dict) and item.get("symbol")
    ]
    source_type = str(payload.get("source_type") or "")
    weight_semantics = (
        "disclosed_fund_holding_weight"
        if source_type == "quarterly_fund_holdings"
        else "tracked_index_weight"
    )
    return {
        "snapshot_id": raw.get("snapshot_id"),
        "etf_symbol": payload.get("etf_symbol") or raw.get("symbol"),
        "etf_name": payload.get("etf_name"),
        "tracked_index_code": payload.get("tracked_index_code"),
        "tracked_index_name": payload.get("tracked_index_name"),
        "data_as_of": raw.get("data_as_of"),
        "retrieved_at": raw.get("retrieved_at"),
        "freshness_expires_at": raw.get("freshness_expires_at"),
        "quality_status": raw.get("quality_status"),
        "quality": payload.get("quality"),
        "provider_id": payload.get("provider_id"),
        "source_type": source_type,
        "source_ids": list(raw.get("source_ids") or payload.get("source_ids") or []),
        "source_urls": list(payload.get("source_urls") or []),
        "weight_scale": payload.get("weight_scale") or "fraction",
        "weight_semantics": weight_semantics,
        "expected_component_count": int(payload.get("expected_component_count") or 0),
        "observed_component_count": int(payload.get("observed_component_count") or 0),
        "observed_weight_coverage": float(payload.get("observed_weight_coverage") or 0.0),
        "required_field_coverage": float(payload.get("required_field_coverage") or 0.0),
        "universe_complete": bool(payload.get("universe_complete")),
        "partial_components_are_top_ranked": bool(
            payload.get("partial_components_are_top_ranked")
        ),
        "warnings": list(payload.get("warnings") or []),
        "components": components,
    }


def register_report_library_routes(
    app: FastAPI,
    dependency,
    *,
    get_service: Callable[[], ReportLibraryService] = get_report_library_service,
    get_deep_report_service: Callable[[], Any] | None = None,
    get_daily_service: Callable[[], Any] | None = None,
    get_weekly_service: Callable[[], Any] | None = None,
    get_monitoring_service: Callable[[], Any] | None = None,
    get_component_service: Callable[[], ComponentResearchDigestService] | None = None,
    get_etf_universe_service: Callable[[], Any] | None = None,
    get_instrument_profile_service: Callable[[], InstrumentProfileService] | None = None,
    get_etf_product_profile_service: Callable[[], ETFProductProfileService] | None = None,
    get_instrument_historical_percentile_service: (
        Callable[[], InstrumentHistoricalPercentileService] | None
    ) = None,
    # Backward-compatible injection name for callers created with the ETF-only module.
    get_etf_valuation_percentile_service: Callable[[], Any] | None = None,
    get_research_cache_store: Callable[[], Any] | None = None,
    get_data_service: Callable[[], Any] | None = None,
    get_official_service: Callable[[], Any] | None = None,
    get_financial_extraction_service: Callable[[], Any] | None = None,
    get_annual_backfill_job_service: Callable[[], Any] | None = None,
) -> None:
    auth = [Depends(dependency)]
    resolved_component_service: ComponentResearchDigestService | None = None
    resolved_instrument_profile_service: InstrumentProfileService | None = None
    resolved_etf_product_profile_service: ETFProductProfileService | None = None
    resolved_historical_percentile_service: InstrumentHistoricalPercentileService | None = None
    resolved_annual_backfill_job_service: Any | None = None
    annual_backfill_tasks: dict[str, asyncio.Task[Any]] = {}

    def require_service() -> ReportLibraryService:
        if not report_library_enabled():
            raise HTTPException(status_code=503, detail="Report library is disabled")
        return get_service()

    def component_service() -> ComponentResearchDigestService:
        nonlocal resolved_component_service
        if get_component_service is not None:
            return get_component_service()
        if resolved_component_service is None:
            knowledge = require_service().knowledge
            resolved_component_service = ComponentResearchDigestService(
                knowledge_store=knowledge,
                store=ComponentResearchDigestStore(knowledge_store=knowledge),
            )
        return resolved_component_service

    def etf_universe_service() -> Any:
        if get_etf_universe_service is not None:
            return get_etf_universe_service()
        from src.reports.etf_universe_provider import get_etf_universe_service as get_default

        return get_default()

    def instrument_profile_service() -> InstrumentProfileService:
        nonlocal resolved_instrument_profile_service
        if get_instrument_profile_service is not None:
            return get_instrument_profile_service()
        if resolved_instrument_profile_service is None:
            resolved_instrument_profile_service = InstrumentProfileService(
                require_service().knowledge
            )
        return resolved_instrument_profile_service

    def etf_product_profile_service() -> ETFProductProfileService:
        nonlocal resolved_etf_product_profile_service
        if get_etf_product_profile_service is not None:
            return get_etf_product_profile_service()
        if resolved_etf_product_profile_service is None:
            from src.reports.etf_research import ETFResearchStore
            from src.research.source_ingestion import SourceIngestionService

            knowledge = require_service().knowledge
            resolved_etf_product_profile_service = ETFProductProfileService(
                store=ETFResearchStore(research_store=knowledge),
                ingestion=SourceIngestionService(knowledge),
            )
        return resolved_etf_product_profile_service

    def historical_percentile_service() -> Any:
        nonlocal resolved_historical_percentile_service
        if get_instrument_historical_percentile_service is not None:
            return get_instrument_historical_percentile_service()
        if get_etf_valuation_percentile_service is not None:
            return get_etf_valuation_percentile_service()
        if resolved_historical_percentile_service is None:
            resolved_historical_percentile_service = InstrumentHistoricalPercentileService(
                require_service().knowledge
            )
        return resolved_historical_percentile_service

    def refresh_historical_percentile(
        symbol: str,
        *,
        instrument_type: str,
        instrument_name: str = "",
        currency: str = "",
        tracked_index_code: str = "",
        tracked_index_name: str = "",
    ) -> dict[str, Any] | None:
        service = historical_percentile_service()
        if getattr(service, "supports_all_instruments", False):
            return service.refresh(
                symbol,
                instrument_type=instrument_type,
                instrument_name=instrument_name,
                currency=currency,
                tracked_index_code=tracked_index_code,
                tracked_index_name=tracked_index_name,
            )
        if instrument_type == "etf":
            return service.refresh(
                symbol,
                tracked_index_code=tracked_index_code,
                tracked_index_name=tracked_index_name,
            )
        return service.latest_snapshot(symbol)

    def research_cache_store() -> Any:
        if get_research_cache_store is not None:
            return get_research_cache_store()
        from src.data_layer.store import ResearchCacheStore

        return ResearchCacheStore()

    def official_service() -> Any:
        if get_official_service is not None:
            return get_official_service()
        from src.research import get_official_filing_service

        return get_official_filing_service()

    def annual_backfill_job_service() -> Any:
        nonlocal resolved_annual_backfill_job_service
        if get_annual_backfill_job_service is not None:
            return get_annual_backfill_job_service()
        if resolved_annual_backfill_job_service is None:
            from src.research.annual_report_jobs import (
                get_annual_report_backfill_job_service,
            )

            resolved_annual_backfill_job_service = get_annual_report_backfill_job_service()
        return resolved_annual_backfill_job_service

    async def run_annual_backfill_job(job_id: str) -> None:
        try:
            await asyncio.to_thread(
                annual_backfill_job_service().run_job,
                job_id,
                official_service(),
            )
        finally:
            annual_backfill_tasks.pop(job_id, None)

    def schedule_annual_backfill_job(job_id: str) -> None:
        existing = annual_backfill_tasks.get(job_id)
        if existing is not None and not existing.done():
            return
        annual_backfill_tasks[job_id] = asyncio.create_task(
            run_annual_backfill_job(job_id),
            name=f"annual-report-backfill:{job_id}",
        )

    def refresh_subject_sources(symbol: str, *, force: bool) -> dict[str, Any]:
        """Refresh official filings and linkable broker-report metadata."""

        result = dict(official_service().refresh(symbol, force=force) or {})
        if get_data_service is None or not symbol.upper().endswith((".SH", ".SZ", ".BJ")):
            return result
        try:
            context = get_data_service().get_context(
                symbols=[symbol],
                purpose="holding",
                include=["reports"],
                force_live=True,
            )
            report_item = (
                (((context.get("research") or {}).get("reports") or {}).get("items") or {})
                .get(symbol, {})
            )
            documents = list(report_item.get("documents") or [])
            result["broker_research"] = {
                "status": report_item.get("mode") or "unavailable",
                "linked": sum(bool(item.get("url")) for item in documents),
                "documents": len(documents),
            }
        except Exception as exc:  # noqa: BLE001 - official refresh remains independently useful
            result["broker_research"] = {
                "status": "unavailable",
                "linked": 0,
                "documents": 0,
                "error": str(exc),
            }
        return result

    def financial_extraction_service() -> Any:
        if get_financial_extraction_service is not None:
            return get_financial_extraction_service()
        from src.research import get_official_financial_extraction_service

        return get_official_financial_extraction_service()

    @app.get("/report-library/reports", dependencies=auth)
    async def list_report_library(
        query: str = Query("", max_length=200),
        subject_type: str | None = Query(None, max_length=32),
        report_kind: str | None = Query(None, max_length=40),
        horizon: str | None = Query(None, max_length=32),
        status: str | None = Query(None, max_length=32),
        quality: str | None = Query(None, max_length=40),
        start_at: str | None = Query(None, max_length=64),
        end_at: str | None = Query(None, max_length=64),
        limit: int = Query(50, ge=1, le=100),
        cursor: str | None = Query(None, max_length=500),
    ):
        try:
            result = require_service().list_reports(
                query=query,
                subject_type=subject_type,
                report_kind=report_kind,
                horizon=horizon,
                status=status,
                quality=quality,
                start_at=start_at,
                end_at=end_at,
                limit=limit,
                cursor=cursor,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            **result,
            "reports": [
                _public_report(
                    item,
                    daily_service=(get_daily_service() if get_daily_service else None),
                    weekly_service=(get_weekly_service() if get_weekly_service else None),
                    deep_report_service=(
                        get_deep_report_service() if get_deep_report_service else None
                    ),
                )
                for item in result["reports"]
            ],
        }

    @app.get("/report-library/status", dependencies=auth)
    async def get_report_library_status():
        return require_service().status()

    @app.get("/report-library/subjects", dependencies=auth)
    async def list_report_subjects(
        query: str = Query("", max_length=200),
        report_kind: str | None = Query(None, max_length=40),
        quality: str | None = Query(None, max_length=40),
        start_at: str | None = Query(None, max_length=64),
        end_at: str | None = Query(None, max_length=64),
        limit: int = Query(30, ge=1, le=100),
        cursor: str | None = Query(None, max_length=500),
    ):
        try:
            result = require_service().list_subjects(
                query=query,
                report_kind=report_kind,
                quality=quality,
                start_at=start_at,
                end_at=end_at,
                limit=limit,
                cursor=cursor,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        public_subjects = []
        for subject in result["subjects"]:
            item = dict(subject)
            latest = item.get("latest_report")
            if latest:
                item["latest_report"] = _public_report(
                    latest,
                    daily_service=(get_daily_service() if get_daily_service else None),
                    weekly_service=(get_weekly_service() if get_weekly_service else None),
                    deep_report_service=(
                        get_deep_report_service() if get_deep_report_service else None
                    ),
                )
            public_subjects.append(item)
        return {**result, "subjects": public_subjects}

    @app.get("/report-library/subjects/{subject_key}", dependencies=auth)
    async def get_report_subject(
        subject_key: str,
        limit: int = Query(100, ge=1, le=200),
        include_timeline: bool = Query(True),
        include_source_documents: bool = Query(True),
        history_mode: str = Query("current_families", pattern="^(current_families|full)$"),
    ):
        result = require_service().subject(
            subject_key,
            limit=limit,
            include_timeline=include_timeline,
            history_mode=history_mode,
        )
        if not result.get("report_count"):
            raise HTTPException(status_code=404, detail="Report subject not found")
        try:
            component_research = component_service().component_research_profile(subject_key)
        except ValueError:
            component_research = None
        try:
            etf_universe = _etf_universe_profile(
                etf_universe_service().latest_snapshot(subject_key)
            )
        except (AttributeError, OSError, ValueError, sqlite3.Error):
            etf_universe = None
        try:
            instrument_profile = instrument_profile_service().latest_snapshot(subject_key)
        except (AttributeError, OSError, ValueError, sqlite3.Error, json.JSONDecodeError):
            instrument_profile = None
        try:
            etf_product = etf_product_profile_service().latest_profile(subject_key)
        except (AttributeError, OSError, ValueError, sqlite3.Error, json.JSONDecodeError):
            etf_product = None
        try:
            historical_percentile = (
                historical_percentile_service().latest_snapshot(subject_key)
            )
        except (AttributeError, OSError, ValueError, sqlite3.Error, json.JSONDecodeError):
            historical_percentile = None
        source_bundle = None
        if result.get("subject_type") == "symbol":
            try:
                source_bundle = build_knowledge_source_bundle(
                    str(result.get("symbol") or subject_key),
                    require_service().knowledge,
                    legacy_store=research_cache_store(),
                )
                if source_bundle is not None and not include_source_documents:
                    source_bundle = {
                        **source_bundle,
                        "domains": [
                            {**domain, "documents": []}
                            for domain in source_bundle.get("domains") or []
                        ],
                    }
            except (AttributeError, OSError, ValueError, sqlite3.Error):
                source_bundle = None
        snapshot_instrument_type = str(
            (instrument_profile or {}).get("instrument_type")
            or (historical_percentile or {}).get("instrument_type")
            or ""
        )
        is_etf = bool(
            snapshot_instrument_type == "etf"
            or etf_product
            or etf_universe
            or component_research and component_research.get("selection_id")
        )
        profile_key = (
            "etf" if is_etf
            else "index" if snapshot_instrument_type == "index"
            else "equity"
        )
        profile = {
            "component_research": component_research,
            "instrument": instrument_profile,
            "historical_percentile": historical_percentile,
        }
        if profile_key == "etf":
            profile["universe"] = etf_universe
            profile["product"] = etf_product
            profile["valuation_percentile"] = historical_percentile
        return {
            **result,
            "component_research": component_research,
            "etf_universe": etf_universe,
            "instrument_profile": instrument_profile,
            "etf_product": etf_product,
            "historical_percentile": historical_percentile,
            "etf_valuation_percentile": historical_percentile if is_etf else None,
            "source_bundle": source_bundle,
            "profile": {profile_key: profile},
            "timeline": [
                _public_report(
                    item,
                    daily_service=(get_daily_service() if get_daily_service else None),
                    weekly_service=(get_weekly_service() if get_weekly_service else None),
                    deep_report_service=(
                        get_deep_report_service() if get_deep_report_service else None
                    ),
                )
                for item in result["timeline"]
            ],
        }

    @app.get("/report-library/subjects/{subject_key}/reports", dependencies=auth)
    async def get_report_subject_reports(
        subject_key: str,
        limit: int = Query(10, ge=1, le=100),
        cursor: str | None = Query(None, max_length=500),
    ):
        result = require_service().list_subject_reports(
            subject_key,
            limit=limit,
            cursor=cursor,
        )
        if not result.get("total_count"):
            raise HTTPException(status_code=404, detail="Report subject not found")
        return {
            **result,
            "reports": [
                _public_report(
                    item,
                    daily_service=(get_daily_service() if get_daily_service else None),
                    weekly_service=(get_weekly_service() if get_weekly_service else None),
                    deep_report_service=(
                        get_deep_report_service() if get_deep_report_service else None
                    ),
                )
                for item in result["reports"]
            ],
        }

    @app.get("/report-library/subjects/{subject_key}/sources", dependencies=auth)
    async def get_report_subject_sources(
        subject_key: str,
        source_kind: str = Query("", max_length=40),
        verification_status: str = Query("", max_length=40),
        used_by_report: bool | None = Query(None),
        publisher: str = Query("", max_length=120),
        published_since: str | None = Query(None, max_length=64),
        limit: int = Query(50, ge=1, le=100),
        cursor: str | None = Query(None, max_length=120),
    ):
        subject = require_service().subject(subject_key, limit=1)
        if not subject.get("timeline") or subject.get("subject_type") != "symbol":
            raise HTTPException(status_code=404, detail="Report subject not found")
        return require_service().knowledge.list_subject_sources(
            str(subject.get("symbol") or subject_key),
            source_kind=source_kind,
            verification_status=verification_status,
            used_by_report=used_by_report,
            publisher=publisher,
            published_since=published_since,
            limit=limit,
            cursor=cursor,
        )

    @app.get(
        "/report-library/subjects/{subject_key}/research-notes",
        dependencies=auth,
    )
    async def get_report_subject_research_notes(
        subject_key: str,
        status: str = Query("", max_length=32),
        limit: int = Query(10, ge=1, le=200),
        cursor: str | None = Query(None, max_length=160),
    ):
        subject = require_service().subject(subject_key, limit=1)
        if not subject.get("timeline") or subject.get("subject_type") != "symbol":
            raise HTTPException(status_code=404, detail="Report subject not found")
        try:
            return require_service().knowledge.list_research_notes(
                str(subject.get("symbol") or subject_key),
                status=status,
                limit=limit,
                cursor=cursor,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get(
        "/report-library/subjects/{subject_key}/financial-snapshots",
        dependencies=auth,
    )
    async def get_report_subject_financial_snapshots(
        subject_key: str,
        validated_only: bool = Query(False),
        limit: int = Query(50, ge=1, le=200),
    ):
        subject = require_service().subject(subject_key, limit=1)
        if not subject.get("timeline") or subject.get("subject_type") != "symbol":
            raise HTTPException(status_code=404, detail="Report subject not found")
        return require_service().knowledge.list_financial_snapshots(
            str(subject.get("symbol") or subject_key),
            validated_only=validated_only,
            limit=limit,
        )

    @app.post(
        "/report-library/subjects/{subject_key}/financial-snapshots/rebuild",
        dependencies=auth,
    )
    async def rebuild_report_subject_financial_snapshots(
        subject_key: str,
        force: bool = Query(False),
    ):
        subject = require_service().subject(subject_key, limit=1)
        if not subject.get("timeline") or subject.get("subject_type") != "symbol":
            raise HTTPException(status_code=404, detail="Report subject not found")
        symbol = str(subject.get("symbol") or subject_key)
        try:
            return await asyncio.to_thread(
                financial_extraction_service().extract_subject,
                symbol,
                force=force,
                repair_only=True,
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post(
        "/report-library/subjects/{subject_key}/sources/refresh",
        dependencies=auth,
    )
    async def refresh_report_subject_sources(
        subject_key: str,
        force: bool = Query(True),
    ):
        subject = require_service().subject(subject_key, limit=1)
        if not subject.get("timeline") or subject.get("subject_type") != "symbol":
            raise HTTPException(status_code=404, detail="Report subject not found")
        symbol = str(subject.get("symbol") or subject_key)
        try:
            return await asyncio.to_thread(refresh_subject_sources, symbol, force=force)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Source refresh failed: {exc}",
            ) from exc

    @app.get(
        "/report-library/subjects/{subject_key}/annual-reports/coverage",
        dependencies=auth,
    )
    async def get_report_subject_annual_report_coverage(
        subject_key: str,
        start_year: int = Query(..., ge=1990, le=2100),
        end_year: int = Query(..., ge=1990, le=2100),
    ):
        subject = require_service().subject(subject_key, limit=1)
        if not subject.get("timeline") or subject.get("subject_type") != "symbol":
            raise HTTPException(status_code=404, detail="Report subject not found")
        if start_year > end_year:
            raise HTTPException(status_code=400, detail="start_year must not exceed end_year")
        years = list(range(end_year, start_year - 1, -1))
        return await asyncio.to_thread(
            official_service().annual_report_coverage,
            str(subject.get("symbol") or subject_key),
            years=years,
        )

    @app.post(
        "/report-library/subjects/{subject_key}/annual-reports/backfill-jobs",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=auth,
    )
    async def start_report_subject_annual_report_backfill_job(
        subject_key: str,
        payload: AnnualReportBackfillRequest,
    ):
        subject = require_service().subject(subject_key, limit=1)
        if not subject.get("timeline") or subject.get("subject_type") != "symbol":
            raise HTTPException(status_code=404, detail="Report subject not found")
        symbol = str(subject.get("symbol") or subject_key)
        try:
            job, deduplicated = annual_backfill_job_service().create_job(
                symbol=symbol,
                years=payload.years,
                force=payload.force,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not deduplicated:
            schedule_annual_backfill_job(str(job["job_id"]))
        return {
            "status": "accepted",
            "job_id": job["job_id"],
            "deduplicated": deduplicated,
            "job": job,
        }

    @app.get(
        "/report-library/subjects/{subject_key}/annual-reports/backfill-jobs/latest",
        dependencies=auth,
    )
    async def get_latest_report_subject_annual_report_backfill_job(subject_key: str):
        subject = require_service().subject(subject_key, limit=1)
        if not subject.get("timeline") or subject.get("subject_type") != "symbol":
            raise HTTPException(status_code=404, detail="Report subject not found")
        symbol = str(subject.get("symbol") or subject_key).upper()
        return {"job": annual_backfill_job_service().store.latest(symbol)}

    @app.get(
        "/report-library/subjects/{subject_key}/annual-reports/backfill-jobs/{job_id}",
        dependencies=auth,
    )
    async def get_report_subject_annual_report_backfill_job(
        subject_key: str,
        job_id: str,
    ):
        subject = require_service().subject(subject_key, limit=1)
        if not subject.get("timeline") or subject.get("subject_type") != "symbol":
            raise HTTPException(status_code=404, detail="Report subject not found")
        job = annual_backfill_job_service().store.get(job_id)
        if job is None or str(job.get("symbol") or "").upper() != str(
            subject.get("symbol") or subject_key
        ).upper():
            raise HTTPException(status_code=404, detail="Annual-report backfill job not found")
        return job

    @app.post(
        "/report-library/subjects/{subject_key}/annual-reports/backfill",
        dependencies=auth,
    )
    async def backfill_report_subject_annual_reports(
        subject_key: str,
        payload: AnnualReportBackfillRequest,
    ):
        subject = require_service().subject(subject_key, limit=1)
        if not subject.get("timeline") or subject.get("subject_type") != "symbol":
            raise HTTPException(status_code=404, detail="Report subject not found")
        try:
            return await asyncio.to_thread(
                official_service().backfill_annual_reports,
                str(subject.get("symbol") or subject_key),
                years=payload.years,
                force=payload.force,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Historical annual report backfill failed: {exc}",
            ) from exc

    @app.post(
        "/report-library/subjects/{subject_key}/instrument-profile/refresh",
        dependencies=auth,
    )
    async def refresh_report_subject_instrument_profile(subject_key: str):
        subject = require_service().subject(subject_key, limit=1)
        if not subject.get("timeline") or subject.get("subject_type") != "symbol":
            raise HTTPException(status_code=404, detail="Report subject not found")
        try:
            return await asyncio.to_thread(
                instrument_profile_service().refresh,
                str(subject.get("symbol") or subject_key),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Instrument profile refresh failed: {exc}",
            ) from exc

    @app.post(
        "/report-library/subjects/{subject_key}/historical-percentile/refresh",
        dependencies=auth,
    )
    async def refresh_report_subject_historical_percentile(subject_key: str):
        subject = require_service().subject(subject_key, limit=1)
        if not subject.get("timeline") or subject.get("subject_type") != "symbol":
            raise HTTPException(status_code=404, detail="Report subject not found")
        symbol = str(subject.get("symbol") or subject_key).upper()
        profile = instrument_profile_service().latest_snapshot(symbol) or {}
        identity = dict(profile.get("identity") or {})
        kind = str(profile.get("instrument_type") or "")
        if not kind:
            from src.reports.instrument_profile import instrument_type as classify

            kind = classify(symbol)
        try:
            result = await asyncio.to_thread(
                refresh_historical_percentile,
                symbol,
                instrument_type=kind,
                instrument_name=str(identity.get("name") or subject.get("security_name") or ""),
                currency=str(identity.get("currency") or ""),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Historical percentile refresh failed: {exc}",
            ) from exc
        if result is None:
            raise HTTPException(
                status_code=501,
                detail="Historical percentile provider does not support this instrument",
            )
        return result

    @app.get(
        "/report-library/subjects/{subject_key}/etf-profile/source-rules",
        dependencies=auth,
    )
    async def get_etf_profile_source_rules(subject_key: str):
        subject = require_service().subject(subject_key, limit=1)
        if not subject.get("timeline") or subject.get("subject_type") != "symbol":
            raise HTTPException(status_code=404, detail="Report subject not found")
        symbol = str(subject.get("symbol") or subject_key).upper()
        profile = etf_product_profile_service().latest_profile(symbol) or {}
        identity = dict(profile.get("identity") or {})
        manager = str((identity.get("manager") or {}).get("value") or "")
        index_code = str(
            (identity.get("tracked_index_code") or {}).get("value") or ""
        )
        return etf_product_profile_service().source_plan(
            symbol,
            manager=manager,
            index_code=index_code,
            as_of=str(profile.get("data_as_of") or "") or None,
        )

    @app.post(
        "/report-library/subjects/{subject_key}/etf-profile/refresh",
        dependencies=auth,
    )
    async def refresh_report_subject_etf_profile(subject_key: str):
        subject = require_service().subject(subject_key, limit=1)
        if not subject.get("timeline") or subject.get("subject_type") != "symbol":
            raise HTTPException(status_code=404, detail="Report subject not found")
        symbol = str(subject.get("symbol") or subject_key).upper()
        errors: list[dict[str, str]] = []

        async def refresh_instrument():
            try:
                return await asyncio.to_thread(instrument_profile_service().refresh, symbol)
            except Exception as exc:
                errors.append({"source": "instrument_profile", "error": str(exc)[:240]})
                return instrument_profile_service().latest_snapshot(symbol)

        async def refresh_universe():
            try:
                return await asyncio.to_thread(
                    etf_universe_service().get_or_refresh,
                    symbol,
                    force_refresh=True,
                )
            except Exception as exc:
                errors.append({"source": "etf_universe", "error": str(exc)[:240]})
                return None

        instrument_profile, universe_result = await asyncio.gather(
            refresh_instrument(), refresh_universe()
        )
        universe_snapshot = getattr(universe_result, "snapshot", None)
        if universe_snapshot is None:
            try:
                universe_snapshot = etf_universe_service().latest_snapshot(symbol)
            except Exception:
                universe_snapshot = None
        try:
            product = await asyncio.to_thread(
                etf_product_profile_service().get_or_refresh,
                symbol,
                force_refresh=True,
                instrument_profile=instrument_profile,
                universe_snapshot=universe_snapshot,
            )
        except Exception as exc:
            errors.append({"source": "etf_product", "error": str(exc)[:240]})
            product = etf_product_profile_service().latest_profile(symbol)
        if product is None:
            raise HTTPException(
                status_code=502,
                detail={"message": "ETF profile refresh failed", "sources": errors},
            )
        identity = dict(product.get("identity") or {})
        tracked_code = str(
            (identity.get("tracked_index_code") or {}).get("value") or ""
        )
        tracked_name = str(
            (identity.get("tracked_index_name") or {}).get("value") or ""
        )
        if universe_snapshot is not None:
            universe_payload = getattr(universe_snapshot, "payload", {}) or {}
            tracked_code = tracked_code or str(
                universe_payload.get("tracked_index_code") or ""
            )
            tracked_name = tracked_name or str(
                universe_payload.get("tracked_index_name") or ""
            )
        try:
            valuation_percentile = await asyncio.to_thread(
                refresh_historical_percentile,
                symbol,
                instrument_type="etf",
                instrument_name=str(
                    ((instrument_profile or {}).get("identity") or {}).get("name") or ""
                ),
                currency=str(
                    ((instrument_profile or {}).get("identity") or {}).get("currency") or "CNY"
                ),
                tracked_index_code=tracked_code,
                tracked_index_name=tracked_name,
            )
        except Exception as exc:
            errors.append({
                "source": "valuation_percentile",
                "error": str(exc)[:240],
            })
            valuation_percentile = (
                historical_percentile_service().latest_snapshot(symbol)
            )
        combined_errors = [*errors, *list(product.get("refresh_errors") or [])]
        return {
            "status": "completed_with_gaps" if combined_errors else "completed",
            "symbol": symbol,
            "profile": product,
            "valuation_percentile": valuation_percentile,
            "sources": {
                "product": product.get("refresh_status"),
                "instrument_profile": "completed" if instrument_profile else "failed",
                "universe": (
                    "completed" if universe_snapshot is not None else "failed"
                ),
                "peer_flow": (
                    "completed" if product.get("peer_group") else "completed_with_gaps"
                ),
                "valuation_percentile": (
                    str(valuation_percentile.get("status"))
                    if valuation_percentile else "failed"
                ),
            },
            "errors": combined_errors,
        }

    @app.get(
        "/report-library/component-research/digests/{component_symbol}",
        dependencies=auth,
    )
    async def get_component_research_digest(
        component_symbol: str,
        analysis_as_of: str | None = Query(None, max_length=80),
    ):
        try:
            digest = component_service().current_digest(
                component_symbol,
                analysis_as_of=analysis_as_of,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if digest is None:
            raise HTTPException(status_code=404, detail="Component research digest not found")
        return digest.to_dict()

    @app.get(
        "/report-library/component-research/resolutions/{selection_id}",
        dependencies=auth,
    )
    async def get_component_digest_resolution(selection_id: str):
        resolution = component_service().resolution_for_selection(selection_id)
        if resolution is None:
            raise HTTPException(status_code=404, detail="Component digest resolution not found")
        return resolution.to_dict()

    @app.post(
        "/report-library/component-research/resolutions/resolve",
        dependencies=auth,
    )
    async def resolve_component_digest_selection(payload: ComponentResolutionRequest):
        try:
            selection = selection_from_dict(payload.selection)
            resolution = await asyncio.to_thread(
                component_service().resolve_selection,
                selection,
                payload.analysis_as_of,
                selection_data_as_of=payload.selection_data_as_of,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return resolution.to_dict()

    @app.get(
        "/report-library/component-research/metrics",
        dependencies=auth,
    )
    async def get_component_research_metrics():
        return component_service().store.metrics()

    @app.get("/report-library/reports/{report_id}", dependencies=auth)
    async def get_report_library_item(report_id: str):
        result = require_service().get_report(report_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Report not found")
        public = _public_report(
            result,
            daily_service=(get_daily_service() if get_daily_service else None),
            weekly_service=(get_weekly_service() if get_weekly_service else None),
            deep_report_service=(
                get_deep_report_service() if get_deep_report_service else None
            ),
        )
        public["sources"] = require_service().knowledge.list_report_sources(
            report_id,
            limit=200,
        )["sources"]
        return public

    @app.get("/report-library/reports/{report_id}/sources", dependencies=auth)
    async def get_report_library_sources(
        report_id: str,
        limit: int = Query(100, ge=1, le=200),
    ):
        if require_service().get_report(report_id) is None:
            raise HTTPException(status_code=404, detail="Report not found")
        return require_service().knowledge.list_report_sources(report_id, limit=limit)

    @app.get("/report-library/references/{reference_code}", dependencies=auth)
    async def resolve_report_reference(reference_code: str):
        result = require_service().get_report_by_reference_code(reference_code)
        if result is None:
            raise HTTPException(status_code=404, detail="Internal report reference not found")
        return _public_report(
            result,
            daily_service=(get_daily_service() if get_daily_service else None),
            weekly_service=(get_weekly_service() if get_weekly_service else None),
            deep_report_service=(
                get_deep_report_service() if get_deep_report_service else None
            ),
        )

    @app.post("/report-library/comparisons", dependencies=auth)
    async def compare_report_library(payload: ReportComparisonRequest):
        try:
            return await asyncio.to_thread(
                require_service().compare,
                [item.model_dump() for item in payload.items],
                include_ai_summary=payload.include_ai_summary,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/report-library/reconcile", dependencies=auth)
    async def reconcile_report_library():
        service = require_service()
        return await asyncio.to_thread(
            service.reconcile,
            deep_report_service=(get_deep_report_service() if get_deep_report_service else None),
            daily_service=(get_daily_service() if get_daily_service else None),
            weekly_service=(get_weekly_service() if get_weekly_service else None),
            monitoring_service=(get_monitoring_service() if get_monitoring_service else None),
        )
