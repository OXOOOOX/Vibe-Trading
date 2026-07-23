"""Authenticated P4B2 dry-plan, preflight, budget, and exact live-run routes."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from src.reports.component_research_generation import (
    ComponentResearchAuthorization,
    ComponentResearchGenerationService,
    get_component_research_generation_service,
    inspect_component_research_preflight,
    prepare_component_research_live_database,
    validate_pilot_authorization,
)
from src.reports.contracts import ComponentDigestResolution


class ComponentResearchAuthorizationRequest(BaseModel):
    authorization_text: str = Field(min_length=1, max_length=500)
    etf_symbol: str = Field(min_length=8, max_length=16)
    component_symbols: list[str] = Field(min_length=1, max_length=3)
    max_model_calls: int = Field(ge=-1, le=3)
    max_input_tokens: int = Field(ge=1, le=18000)
    max_output_tokens: int = Field(ge=-1, le=3000)
    max_auto_repairs: int = Field(0, ge=0, le=0)


class ComponentResearchPlanRequest(BaseModel):
    resolution_id: str = Field(min_length=1, max_length=120)
    component_symbols: list[str] = Field(min_length=0, max_length=3)
    dry_run: bool = True
    authorization: ComponentResearchAuthorizationRequest | None = None


class ComponentResearchExecuteRequest(BaseModel):
    confirm_execute_exact_plan: bool
    authorization: ComponentResearchAuthorizationRequest


class ComponentResearchPreflightRequest(BaseModel):
    authorization: ComponentResearchAuthorizationRequest | None = None


def _authorization_payload(
    value: ComponentResearchAuthorizationRequest | None,
) -> dict[str, Any] | None:
    return value.model_dump() if value is not None else None


def _resolution_by_id(
    service: ComponentResearchGenerationService, resolution_id: str
) -> ComponentDigestResolution:
    try:
        with service.knowledge.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='component_digest_resolutions'"
            ).fetchone()
            if not exists:
                raise LookupError("P4B1 runtime schema is not initialized")
            row = conn.execute(
                "SELECT payload_json FROM component_digest_resolutions WHERE resolution_id=?",
                (resolution_id,),
            ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise LookupError("unable to read P4B1 Resolution") from exc
    if row is None:
        raise LookupError("P4B1 Resolution not found")
    import json

    return ComponentDigestResolution.from_dict(json.loads(str(row[0])))


def register_component_research_generation_routes(
    app: FastAPI,
    read_dependency,
    write_dependency,
    *,
    get_service: Callable[
        [], ComponentResearchGenerationService
    ] = get_component_research_generation_service,
) -> None:
    read_auth = [Depends(read_dependency)]
    write_auth = [Depends(write_dependency)]

    @app.post("/report-library/component-research-generation/plans", dependencies=write_auth)
    async def create_component_research_generation_plan(
        payload: ComponentResearchPlanRequest,
    ):
        service = get_service()
        authorization = _authorization_payload(payload.authorization)
        if not payload.dry_run:
            auth = ComponentResearchAuthorization.from_value(authorization)
            valid, reasons = validate_pilot_authorization(auth)
            if not valid:
                raise HTTPException(
                    status_code=403,
                    detail="Exact P4B2 pilot authorization required: " + ",".join(reasons),
                )
            service.refresh_policy()
            if not service.policy.enabled or not service.policy.live_run_enabled:
                raise HTTPException(
                    status_code=409,
                    detail="Both P4B2 generation and live-run gates must be enabled",
                )
            if not service.store.has_schema():
                try:
                    await asyncio.to_thread(
                        prepare_component_research_live_database,
                        service.knowledge.path,
                        authorization=authorization,
                    )
                except (OSError, PermissionError, RuntimeError) as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            resolution = _resolution_by_id(service, payload.resolution_id)
            plan = await asyncio.to_thread(
                service.create_plan,
                resolution,
                requested_components=payload.component_symbols,
                dry_run=payload.dry_run,
                authorization=authorization,
                persist=service.store.has_schema(),
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return plan.to_dict()

    @app.get(
        "/report-library/component-research-generation/plans/{plan_id}",
        dependencies=read_auth,
    )
    async def get_component_research_generation_plan(plan_id: str):
        plan = get_service().get_plan(plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="Generation plan not found")
        return plan.to_dict()

    @app.post(
        "/report-library/component-research-generation/plans/{plan_id}/preflight",
        dependencies=read_auth,
    )
    async def preflight_component_research_generation_plan(
        plan_id: str, payload: ComponentResearchPreflightRequest
    ):
        service = get_service()
        plan = service.get_plan(plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="Generation plan not found")
        workspace = Path(__file__).resolve().parents[3]
        result = await asyncio.to_thread(
            inspect_component_research_preflight,
            workspace_path=workspace,
            runtime_database_path=service.knowledge.path,
            plan=plan,
            policy=service.policy,
            authorization=_authorization_payload(payload.authorization),
        )
        return result.to_dict()

    @app.post(
        "/report-library/component-research-generation/plans/{plan_id}/execute",
        dependencies=write_auth,
    )
    async def execute_component_research_generation_plan(
        plan_id: str, payload: ComponentResearchExecuteRequest
    ):
        if not payload.confirm_execute_exact_plan:
            raise HTTPException(status_code=400, detail="Exact plan confirmation is required")
        service = get_service()
        service.refresh_policy()
        try:
            results = await asyncio.to_thread(
                service.execute_plan,
                plan_id,
                authorization=payload.authorization.model_dump(),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"plan_id": plan_id, "publish_results": [item.to_dict() for item in results]}

    @app.get(
        "/report-library/component-research-generation/jobs/{job_id}",
        dependencies=read_auth,
    )
    async def get_component_research_generation_job(job_id: str):
        job = get_service().get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Generation job not found")
        return job.to_dict()

    @app.post(
        "/report-library/component-research-generation/jobs/{job_id}/cancel",
        dependencies=write_auth,
    )
    async def cancel_component_research_generation_job(job_id: str):
        try:
            job = get_service().cancel_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return job.to_dict()

    @app.get(
        "/report-library/component-research-generation/budget/today",
        dependencies=read_auth,
    )
    async def get_component_research_generation_budget():
        service = get_service()
        used = service.store.budget_usage()
        policy = service.policy
        return {
            "used": used,
            "remaining": {
                "components": max(0, policy.max_components_per_day - used["components"]),
                "model_calls": max(0, policy.max_model_calls_per_day - used["model_calls"]),
                "input_tokens": max(0, policy.max_input_tokens_per_day - used["input_tokens"]),
                "output_tokens": max(0, policy.max_output_tokens_per_day - used["output_tokens"]),
            },
            "policy": policy.to_dict(),
        }

    @app.get(
        "/report-library/component-research-generation/components/{component_symbol}/latest",
        dependencies=read_auth,
    )
    async def get_latest_component_research_publish(component_symbol: str):
        try:
            result = get_service().store.latest_publish(component_symbol)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(status_code=404, detail="Published component research not found")
        return result.to_dict()
