"""Authenticated APIs for portfolio mandates and DailyPortfolioRun."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.portfolio.daily import DailyPortfolioRunService
from src.portfolio.mandate import (
    MandateValidationError,
    ensure_assignments,
    save_mandate,
    suggest_classifications,
    update_assignment,
)
from src.portfolio.state import load_state, normalize_symbol


class MandatePayload(BaseModel):
    mandate: dict[str, Any]


class AssignmentPayload(BaseModel):
    sleeve_id: str = Field(min_length=1, max_length=80)
    user_locked: bool = True


class StartDailyRunPayload(BaseModel):
    market_date: str | None = None
    refresh_policy: Literal["ensure_fresh", "force", "reuse"] = "ensure_fresh"
    report_profile: Literal["master_with_holding_appendices"] = "master_with_holding_appendices"
    force_new: bool = False


class RetryDailyRunPayload(BaseModel):
    symbol: str | None = None


def _public_run(record: dict[str, Any]) -> dict[str, Any]:
    """Remove server-local artifact paths from API responses."""

    value = dict(record)
    value["artifacts"] = [
        {key: item for key, item in dict(artifact).items() if key != "path"}
        for artifact in record.get("artifacts") or []
    ]
    return value


def register_portfolio_daily_routes(
    app: FastAPI,
    require_local_or_auth: Callable[..., Any],
    *,
    get_service: Callable[[], DailyPortfolioRunService],
    get_scheduler: Callable[[], Any] | None = None,
) -> None:
    dependency = [Depends(require_local_or_auth)]

    @app.get("/portfolio/mandate", dependencies=dependency)
    async def get_portfolio_mandate():
        return ensure_assignments(load_state().holdings)

    @app.put("/portfolio/mandate", dependencies=dependency)
    async def put_portfolio_mandate(payload: MandatePayload):
        try:
            return save_mandate(payload.mandate)
        except (MandateValidationError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/portfolio/mandate/assignments/{symbol}", dependencies=dependency)
    @app.patch("/portfolio/mandate/assignments/{symbol}", dependencies=dependency)
    async def put_portfolio_assignment(symbol: str, payload: AssignmentPayload):
        try:
            return update_assignment(
                symbol, payload.sleeve_id, user_locked=payload.user_locked
            )
        except (MandateValidationError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/portfolio/mandate/suggest-classifications", dependencies=dependency)
    async def post_portfolio_classification_suggestions():
        return suggest_classifications(load_state().holdings)

    @app.get("/portfolio/daily-runs", dependencies=dependency)
    async def list_daily_runs(limit: int = Query(30, ge=1, le=200)):
        return {"runs": [_public_run(item) for item in get_service().list_runs(limit)]}

    if get_scheduler is not None:

        @app.get("/portfolio/daily-scheduler/status", dependencies=dependency)
        async def get_portfolio_daily_scheduler_status():
            return get_scheduler().status()

    @app.get("/portfolio/daily-runs/latest", dependencies=dependency)
    async def get_latest_daily_run():
        runs = get_service().list_runs(1)
        if not runs:
            raise HTTPException(status_code=404, detail="daily run not found")
        return _public_run(runs[0])

    @app.post("/portfolio/daily-runs", dependencies=dependency, status_code=202)
    async def start_daily_run(payload: StartDailyRunPayload):
        try:
            return _public_run(await get_service().start(
                market_date=payload.market_date,
                refresh_policy=payload.refresh_policy,
                report_profile=payload.report_profile,
                trigger="manual",
                force_new=payload.force_new,
            ))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/portfolio/daily-runs/{run_id}", dependencies=dependency)
    async def get_daily_run(run_id: str):
        record = get_service().get_run(run_id)
        if not record:
            raise HTTPException(status_code=404, detail="daily run not found")
        return _public_run(record)

    @app.post("/portfolio/daily-runs/{run_id}/cancel", dependencies=dependency)
    async def cancel_daily_run(run_id: str):
        try:
            return _public_run(await get_service().cancel(run_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="daily run not found") from exc

    @app.post("/portfolio/daily-runs/{run_id}/retry", dependencies=dependency, status_code=202)
    async def retry_daily_run(
        run_id: str, payload: RetryDailyRunPayload | None = None
    ):
        try:
            return _public_run(await get_service().retry(
                run_id, symbol=payload.symbol if payload else None
            ))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="daily run not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/portfolio/daily-runs/{run_id}/artifacts/{artifact_id}", dependencies=dependency)
    async def download_daily_run_artifact(
        run_id: str,
        artifact_id: str,
        download: bool = Query(True),
    ):
        resolved = get_service().store.resolve_artifact(run_id, artifact_id)
        if not resolved:
            raise HTTPException(status_code=404, detail="artifact not found")
        artifact, path = resolved
        return FileResponse(
            Path(path),
            media_type=str(artifact.get("media_type") or "application/octet-stream"),
            filename=str(artifact.get("filename") or path.name),
            content_disposition_type="attachment" if download else "inline",
        )

    @app.get(
        "/portfolio/daily-runs/{run_id}/reports/master",
        dependencies=dependency,
    )
    async def download_master_report(run_id: str):
        record = get_service().get_run(run_id)
        artifact = next(
            (
                item
                for item in (record or {}).get("artifacts") or []
                if item.get("kind") == "master_pdf" and not item.get("expired")
                and not item.get("superseded")
            ),
            None,
        )
        if not artifact:
            raise HTTPException(status_code=404, detail="master report not found")
        resolved = get_service().store.resolve_artifact(
            run_id, str(artifact["artifact_id"])
        )
        if not resolved:
            raise HTTPException(status_code=404, detail="master report not found")
        _, path = resolved
        return FileResponse(
            Path(path), media_type="application/pdf", filename=str(artifact["filename"])
        )

    @app.get(
        "/portfolio/daily-runs/{run_id}/reports/holdings/{symbol}",
        dependencies=dependency,
    )
    async def download_holding_report(run_id: str, symbol: str):
        record = get_service().get_run(run_id)
        normalized = normalize_symbol(symbol).upper()
        artifact = next(
            (
                item
                for item in (record or {}).get("artifacts") or []
                if item.get("kind") == "holding_daily_pdf"
                and str(item.get("symbol") or "").upper() == normalized
                and not item.get("expired")
                and not item.get("superseded")
            ),
            None,
        )
        if not artifact:
            raise HTTPException(status_code=404, detail="holding report not found")
        resolved = get_service().store.resolve_artifact(
            run_id, str(artifact["artifact_id"])
        )
        if not resolved:
            raise HTTPException(status_code=404, detail="holding report not found")
        _, path = resolved
        return FileResponse(
            Path(path), media_type="application/pdf", filename=str(artifact["filename"])
        )
