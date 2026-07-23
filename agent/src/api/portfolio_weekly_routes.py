"""Authenticated APIs for formal single-symbol WeeklyReportRun records."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.portfolio.weekly import ETF_TRACKING_METHOD_VERSION, WeeklyReportRunService
from src.reports.data_gaps import data_gap_registry_payload


class StartWeeklyRunsPayload(BaseModel):
    week_end: str | None = Field(None, min_length=10, max_length=10)
    symbols: list[str] | None = Field(None, max_length=50)
    refresh_policy: Literal["ensure_fresh", "force", "reuse"] = "ensure_fresh"
    report_profile: str = Field("weekly_review_v1", min_length=1, max_length=80)
    report_audience: Literal["user"] = "user"
    force_new: bool = False
    single_source_authorized: bool = False


def _public_run(record: dict[str, Any]) -> dict[str, Any]:
    value = dict(record)
    value["artifacts"] = [
        {key: item for key, item in dict(artifact).items() if key != "path"}
        for artifact in record.get("artifacts") or []
    ]
    return value


def register_portfolio_weekly_routes(
    app: FastAPI,
    require_local_or_auth: Callable[..., Any],
    *,
    get_service: Callable[[], WeeklyReportRunService],
    get_scheduler: Callable[[], Any] | None = None,
) -> None:
    dependency = [Depends(require_local_or_auth)]

    @app.get("/portfolio/weekly-runs", dependencies=dependency)
    async def list_weekly_runs(limit: int = Query(30, ge=1, le=200)):
        return {"runs": [_public_run(item) for item in get_service().list_runs(limit)]}

    @app.get("/portfolio/weekly-runs/metrics", dependencies=dependency)
    async def get_weekly_run_metrics():
        return get_service().metrics()

    @app.get("/portfolio/weekly-runs/capabilities", dependencies=dependency)
    async def get_weekly_run_capabilities():
        return {
            "schema_version": 1,
            "instrument_types": ["company_equity", "etf"],
            "report_audiences": [
                {
                    "id": "user",
                    "status": "available",
                    "default_profile": "weekly_review_v1",
                },
                {
                    "id": "monitor",
                    "status": "reserved",
                    "default_profile": None,
                },
            ],
            "data_gap_registry": data_gap_registry_payload(),
            "deterministic_methods": [
                {
                    "method_id": "etf_tracking_metrics",
                    "method_version": ETF_TRACKING_METHOD_VERSION,
                    "instrument_types": ["etf"],
                    "outputs": [
                        "index_relative_strength",
                        "market_tracking_deviation_20d",
                        "market_tracking_deviation_60d",
                    ],
                    "official_metric": False,
                }
            ],
        }

    if get_scheduler is not None:

        @app.get("/portfolio/weekly-scheduler/status", dependencies=dependency)
        async def get_weekly_scheduler_status():
            return get_scheduler().status()

    @app.post("/portfolio/weekly-runs", dependencies=dependency, status_code=202)
    async def start_weekly_runs(payload: StartWeeklyRunsPayload):
        try:
            records = await get_service().start(
                week_end=payload.week_end,
                symbols=payload.symbols,
                refresh_policy=payload.refresh_policy,
                report_profile=payload.report_profile,
                report_audience=payload.report_audience,
                force_new=payload.force_new,
                single_source_authorized=payload.single_source_authorized,
                trigger="manual",
            )
        except ValueError as exc:
            status = 503 if "disabled" in str(exc).lower() else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return {"runs": [_public_run(item) for item in records]}

    @app.get("/portfolio/weekly-runs/{run_id}", dependencies=dependency)
    async def get_weekly_run(run_id: str):
        record = get_service().get_run(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="weekly run not found")
        return _public_run(record)

    @app.post("/portfolio/weekly-runs/{run_id}/cancel", dependencies=dependency)
    async def cancel_weekly_run(run_id: str):
        try:
            return _public_run(await get_service().cancel(run_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="weekly run not found") from exc

    @app.post("/portfolio/weekly-runs/{run_id}/retry", dependencies=dependency, status_code=202)
    async def retry_weekly_run(run_id: str):
        try:
            return _public_run(await get_service().retry(run_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="weekly run not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get(
        "/portfolio/weekly-runs/{run_id}/artifacts/{artifact_id}",
        dependencies=dependency,
    )
    async def download_weekly_artifact(
        run_id: str,
        artifact_id: str,
        download: bool = Query(True),
    ):
        resolved = get_service().store.resolve_artifact(run_id, artifact_id)
        if resolved is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        artifact, path = resolved
        return FileResponse(
            Path(path),
            media_type=str(artifact.get("media_type") or "application/octet-stream"),
            filename=str(artifact.get("filename") or path.name),
            content_disposition_type="attachment" if download else "inline",
        )
