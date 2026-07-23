"""Authenticated portfolio monitoring APIs."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from src.portfolio.monitoring.models import PlanValidationError


_YMCA_AUDIO_ENV = "VIBE_TRADING_MONITOR_YMCA_AUDIO_PATH"
_YMCA_STICKER_ENV = "VIBE_TRADING_MONITOR_YMCA_STICKER_PATH"
_YMCA_DOWN_STICKER_ENV = "VIBE_TRADING_MONITOR_YMCA_DOWN_STICKER_PATH"
_YMCA_AUDIO_EXTENSIONS = {
    ".aac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".oga",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
}
_YMCA_STICKER_EXTENSIONS = {".gif", ".png", ".webp"}
_YMCA_STICKER_MAX_BYTES = 10 * 1024 * 1024
_MONITOR_SSE_POLL_SECONDS = 0.5
_MONITOR_SSE_HEARTBEAT_SECONDS = 30.0
_MONITOR_SSE_EMPTY_CURSOR = "portfolio-monitor-empty-v1"


def _valid_audio_magic(extension: str, prefix: bytes) -> bool:
    """Recognize common browser-playable audio container signatures."""

    if extension == ".mp3":
        return prefix.startswith(b"ID3") or (
            len(prefix) >= 2
            and prefix[0] == 0xFF
            and prefix[1] & 0xE0 == 0xE0
            and prefix[1] & 0x18 != 0x08
            and prefix[1] & 0x06 != 0
        )
    if extension == ".wav":
        return len(prefix) >= 12 and prefix.startswith(b"RIFF") and prefix[8:12] == b"WAVE"
    if extension in {".oga", ".ogg", ".opus"}:
        return prefix.startswith(b"OggS")
    if extension == ".webm":
        return prefix.startswith(b"\x1aE\xdf\xa3")
    if extension in {".m4a", ".mp4"}:
        return len(prefix) >= 8 and prefix[4:8] == b"ftyp"
    if extension == ".aac":
        return (
            len(prefix) >= 2
            and prefix[0] == 0xFF
            and prefix[1] & 0xF0 == 0xF0
            and prefix[1] & 0x06 == 0
        )
    return False


def _valid_sticker_magic(extension: str, prefix: bytes) -> bool:
    """Recognize the three Feishu monitoring-sticker formats."""

    if extension == ".gif":
        return prefix.startswith((b"GIF87a", b"GIF89a"))
    if extension == ".png":
        return prefix.startswith(b"\x89PNG\r\n\x1a\n")
    if extension == ".webp":
        return len(prefix) >= 12 and prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP"
    return False


def _configured_asset_path(
    env_name: str,
    *,
    extensions: set[str],
    maximum_bytes: int | None = None,
    magic_validator: Callable[[str, bytes], bool] | None = None,
) -> Path | None:
    """Return a validated local effect asset without exposing it to API callers."""

    configured = os.getenv(env_name, "").strip()
    if not configured:
        return None
    path = Path(os.path.expandvars(configured)).expanduser()
    try:
        size = path.stat().st_size
    except OSError:
        return None
    extension = path.suffix.lower()
    if not path.is_file() or extension not in extensions or size <= 0:
        return None
    if maximum_bytes is not None and size > maximum_bytes:
        return None
    if magic_validator is not None:
        try:
            with path.open("rb") as handle:
                prefix = handle.read(32)
        except OSError:
            return None
        if not magic_validator(extension, prefix):
            return None
    return path


def get_monitor_ymca_audio_path() -> Path | None:
    return _configured_asset_path(
        _YMCA_AUDIO_ENV,
        extensions=_YMCA_AUDIO_EXTENSIONS,
        magic_validator=_valid_audio_magic,
    )


def get_monitor_ymca_sticker_path() -> Path | None:
    """Return the upward-breakout sticker (legacy env name retained)."""

    return _configured_asset_path(
        _YMCA_STICKER_ENV,
        extensions=_YMCA_STICKER_EXTENSIONS,
        maximum_bytes=_YMCA_STICKER_MAX_BYTES,
        magic_validator=_valid_sticker_magic,
    )


def get_monitor_ymca_down_sticker_path() -> Path | None:
    return _configured_asset_path(
        _YMCA_DOWN_STICKER_ENV,
        extensions=_YMCA_STICKER_EXTENSIONS,
        maximum_bytes=_YMCA_STICKER_MAX_BYTES,
        magic_validator=_valid_sticker_magic,
    )


def get_monitor_effect_status() -> dict[str, dict[str, bool]]:
    audio_ready = get_monitor_ymca_audio_path() is not None
    up_sticker_ready = get_monitor_ymca_sticker_path() is not None
    down_sticker_ready = get_monitor_ymca_down_sticker_path() is not None
    return {
        "ymca_v1": {
            "audio_ready": audio_ready,
            "up_sticker_ready": up_sticker_ready,
            "down_sticker_ready": down_sticker_ready,
            "sticker_ready": up_sticker_ready and down_sticker_ready,
            "available": audio_ready and up_sticker_ready and down_sticker_ready,
        }
    }


def _status_with_effects(service: Any, runtime_status: dict[str, Any]) -> dict[str, Any]:
    status = dict(service.status(runtime_status))
    effects = dict(status.get("effects") or {})
    effects.update(get_monitor_effect_status())
    status["effects"] = effects
    return status


def _plan_ymca_directions(plan_record: dict[str, Any] | None) -> set[Literal["above", "below"]]:
    plan = (plan_record or {}).get("plan") or {}
    directions: set[Literal["above", "below"]] = set()
    for rule in plan.get("market_rules") or []:
        if not isinstance(rule, dict) or rule.get("alert_cue") != "ymca_v1":
            continue
        if rule.get("kind") == "price_cross_above":
            directions.add("above")
        elif rule.get("kind") == "price_cross_below":
            directions.add("below")
    return directions


def _missing_ymca_assets(plan_record: dict[str, Any] | None) -> list[str]:
    directions = _plan_ymca_directions(plan_record)
    if not directions:
        return []
    status = get_monitor_effect_status()["ymca_v1"]
    missing: list[str] = []
    if not status["audio_ready"]:
        missing.append("audio")
    if "above" in directions and not status["up_sticker_ready"]:
        missing.append("up_sticker")
    if "below" in directions and not status["down_sticker_ready"]:
        missing.append("down_sticker")
    return missing


def _sse_frame(event: str, data: dict[str, Any], *, event_id: str | None = None) -> str:
    lines = []
    if event_id:
        lines.append(f"id: {event_id.replace(chr(10), '').replace(chr(13), '')}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'), default=str)}")
    return "\n".join(lines) + "\n\n"


async def _monitor_event_stream(
    request: Request,
    store: Any,
    last_event_id: str | None,
    *,
    poll_seconds: float | None = None,
    heartbeat_seconds: float | None = None,
):
    """Poll the durable event log and expose cursor-safe SSE frames."""

    poll = _MONITOR_SSE_POLL_SECONDS if poll_seconds is None else max(0.0, poll_seconds)
    heartbeat = (
        _MONITOR_SSE_HEARTBEAT_SECONDS
        if heartbeat_seconds is None
        else max(0.0, heartbeat_seconds)
    )
    cursor = last_event_id.strip() if last_event_id else None
    initial_connection = not cursor
    pending: list[dict[str, Any]] = []
    reset_payload: dict[str, Any] | None = None

    # Take the durable cursor snapshot before yielding the connected frame so
    # an event created immediately afterward is never mistaken for history.
    if cursor == _MONITOR_SSE_EMPTY_CURSOR:
        pending = await asyncio.to_thread(store.list_events_from_start, limit=200)
    elif cursor:
        try:
            pending = await asyncio.to_thread(store.list_events_after, cursor, limit=200)
        except KeyError:
            cursor = (
                await asyncio.to_thread(store.latest_event_cursor)
                or _MONITOR_SSE_EMPTY_CURSOR
            )
            reset_payload = {"reason": "cursor_not_found", "cursor": cursor}
    else:
        cursor = (
            await asyncio.to_thread(store.latest_event_cursor)
            or _MONITOR_SSE_EMPTY_CURSOR
        )

    # Establish the browser's page-lifetime cursor without replaying history.
    # A stable sentinel lets an EventSource that first connected to an empty
    # database recover events created during a short network interruption.
    yield "retry: 2000\n\n"
    if reset_payload is not None:
        yield _sse_frame("portfolio.monitor.reset", reset_payload, event_id=cursor)
    elif initial_connection:
        yield _sse_frame(
            "portfolio.monitor.cursor",
            {"cursor": cursor},
            event_id=cursor,
        )

    last_emit = time.monotonic()
    while not await request.is_disconnected():
        if not pending:
            if cursor == _MONITOR_SSE_EMPTY_CURSOR:
                pending = await asyncio.to_thread(store.list_events_from_start, limit=200)
            else:
                try:
                    pending = await asyncio.to_thread(
                        store.list_events_after,
                        cursor,
                        limit=200,
                    )
                except KeyError:
                    cursor = (
                        await asyncio.to_thread(store.latest_event_cursor)
                        or _MONITOR_SSE_EMPTY_CURSOR
                    )
                    yield _sse_frame(
                        "portfolio.monitor.reset",
                        {"reason": "cursor_not_found", "cursor": cursor},
                        event_id=cursor,
                    )
                    last_emit = time.monotonic()

        for event in pending:
            event_id = str(event.get("event_id") or "").strip()
            if not event_id:
                continue
            cursor = event_id
            yield _sse_frame(
                "portfolio.monitor.confirmed",
                {"type": "portfolio.monitor.confirmed", "event": event},
                event_id=event_id,
            )
            last_emit = time.monotonic()
        pending = []

        if time.monotonic() - last_emit >= heartbeat:
            yield f": heartbeat {datetime.now(timezone.utc).isoformat()}\n\n"
            last_emit = time.monotonic()
        if poll:
            await asyncio.sleep(poll)
        else:
            await asyncio.sleep(0)


class DraftBatchPayload(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=10)
    delivery_target_id: str | None = None
    force_fresh: bool = False
    allow_single_source: bool = False


class MonitorPlannerJobPayload(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=10)
    report_refs: dict[str, str] | None = None
    research_policy: Literal["if_needed"] = "if_needed"
    delivery_target_id: str | None = None
    force_fresh: bool = True
    activation_mode: Literal["manual", "autonomous"] = "manual"
    trigger_type: Literal[
        "report_ready", "holdings_changed", "scheduled_close", "approaching",
        "invalidated", "material_evidence_changed",
    ] | None = None


class MonitorAutopilotPayload(BaseModel):
    enabled: bool
    selected_symbols: list[str] | None = Field(default=None, max_length=100)
    change_source: Literal["user_toggle", "holding_selection"] | None = None
    trigger_types: list[
        Literal[
            "report_ready", "holdings_changed", "scheduled_close", "approaching",
            "invalidated", "material_evidence_changed",
        ]
    ] | None = None
    daily_close_enabled: bool = True
    delivery_target_id: str | None = None
    runtime_mode: Literal["shadow", "deliver"] = "shadow"


class MonitorRecommendationAcknowledgePayload(BaseModel):
    feedback_status: Literal["handled", "continue_observing", "ignored"]


class MonitorRiskPreferencePayload(BaseModel):
    holding_period: Literal["short_term", "swing", "long_term"]
    max_risk_amount: float | None = Field(default=None, ge=0)
    max_risk_pct: float | None = Field(default=None, ge=0, le=1)
    max_add_amount: float | None = Field(default=None, ge=0)
    max_position_amount: float | None = Field(default=None, ge=0)
    minimum_reward_risk: float | None = Field(default=None, ge=0, le=20)
    confirmation_intervals: list[Literal["5m", "30m", "1d"]] = Field(
        default_factory=lambda: ["5m", "1d"], min_length=1, max_length=3
    )
    max_buy_price: float | None = Field(default=None, gt=0)
    min_sell_price: float | None = Field(default=None, gt=0)
    slippage_bps: float | None = Field(default=None, ge=0, le=1000)
    draft_valid_minutes: int = Field(default=30, ge=5, le=10080)
    condition_order_permission: Literal["only_alert", "local_draft", "broker_export"] = "only_alert"
    sellable_quantity: float | None = Field(default=None, ge=0)
    intraday_added_quantity: float | None = Field(default=None, ge=0)
    default_reduce_fraction: float | None = Field(default=None, gt=0, le=1)


class MonitorDecisionChoicePayload(BaseModel):
    decision_id: str = Field(min_length=8, max_length=80)
    choice_id: str = Field(min_length=2, max_length=80)
    decision_revision: int = Field(ge=1)
    evidence_fingerprint: str = Field(min_length=32, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=128)


class MonitorConditionDraftPayload(BaseModel):
    choice_id: str = Field(min_length=2, max_length=80)
    decision_revision: int = Field(ge=1)
    evidence_fingerprint: str = Field(min_length=32, max_length=128)


class DeliveryTargetPayload(BaseModel):
    channel: Literal["feishu"] = "feishu"
    chat_id: str = Field(min_length=2, max_length=200)
    chat_type: Literal["p2p", "group"] = "p2p"
    session_key: str = Field(default="", max_length=300)


class PlanPatchPayload(BaseModel):
    plan: dict[str, Any]
    expected_revision: int = Field(ge=1)


class PausePayload(BaseModel):
    duration_hours: int | None = Field(default=None, ge=1, le=168)
    reason: str = Field(default="user_paused", max_length=200)


class ReopenPayload(BaseModel):
    delivery_target_id: str | None = None
    allow_single_source: bool = False


class ReanalyzePayload(BaseModel):
    allow_single_source: bool = False


class MaintenancePayload(BaseModel):
    force: bool = False


class RuntimeConfigPayload(BaseModel):
    enabled: bool
    mode: Literal["shadow", "deliver"] | None = None


class DeliveryReconcilePayload(BaseModel):
    status: Literal["delivered", "rejected"]
    remote_message_id: str | None = Field(default=None, max_length=300)
    provider: str = Field(default="feishu", min_length=1, max_length=40)
    note: str = Field(min_length=1, max_length=500)


def _error(status: int, code: str, detail: str, blocked: list[str] | None = None) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"error_code": code, "message": detail, "blocked_reasons": blocked or []},
    )


def register_portfolio_monitor_routes(
    app: FastAPI,
    require_local_or_auth: Callable[..., Any],
    *,
    get_service: Callable[[], Any],
    get_runtime: Callable[[], Any],
    set_runtime_config: Callable[[bool, str | None], str],
    require_event_stream_auth: Callable[..., Any] | None = None,
) -> None:
    dependency = [Depends(require_local_or_auth)]
    stream_dependency = [Depends(require_event_stream_auth or require_local_or_auth)]

    @app.get("/portfolio/monitor-delivery-targets", dependencies=dependency)
    async def list_monitor_delivery_targets():
        return {"targets": get_service().store.list_targets()}

    @app.post("/portfolio/monitor-delivery-targets/bind", dependencies=dependency)
    async def bind_monitor_delivery_target(payload: DeliveryTargetPayload):
        return get_service().store.bind_target(**payload.model_dump())

    @app.post(
        "/portfolio/monitor-delivery-targets/binding-codes",
        dependencies=dependency,
        status_code=201,
    )
    async def create_monitor_delivery_binding_code():
        return get_service().store.create_binding_code()

    @app.get(
        "/portfolio/monitor-delivery-targets/binding-codes/{binding_id}",
        dependencies=dependency,
    )
    async def get_monitor_delivery_binding_code(binding_id: str):
        result = get_service().store.get_binding_code(binding_id)
        if not result:
            raise _error(404, "binding_code_not_found", "monitoring binding code not found")
        return result

    @app.post("/portfolio/monitor-delivery-targets/{target_id}/revoke", dependencies=dependency)
    async def revoke_monitor_delivery_target(target_id: str):
        try:
            return get_service().store.revoke_target(target_id)
        except KeyError as exc:
            raise _error(404, "delivery_target_not_found", "delivery target not found") from exc

    @app.post("/portfolio/monitor-draft-batches", dependencies=dependency, status_code=202)
    async def create_monitor_draft_batch(payload: DraftBatchPayload, response: Response):
        try:
            result = await asyncio.to_thread(
                get_service().create_draft_batch,
                payload.symbols,
                payload.delivery_target_id
                or get_service().store.get_default_delivery_target_id(),
                force_fresh=payload.force_fresh,
                allow_single_source=payload.allow_single_source,
            )
        except ValueError as exc:
            raise _error(400, "invalid_monitor_request", str(exc)) from exc
        response.headers["Location"] = f"/portfolio/monitor-draft-batches/{result['batch_id']}"
        return result

    @app.get("/portfolio/monitor-draft-batches/{batch_id}", dependencies=dependency)
    async def get_monitor_draft_batch(batch_id: str):
        result = get_service().store.get_batch(batch_id)
        if not result:
            raise _error(404, "draft_batch_not_found", "draft batch not found")
        return result

    @app.post("/portfolio/monitor-draft-batches/{batch_id}/cancel", dependencies=dependency)
    async def cancel_monitor_draft_batch(batch_id: str):
        result = get_service().store.get_batch(batch_id)
        if not result:
            raise _error(404, "draft_batch_not_found", "draft batch not found")
        if result["status"] not in {"generating"}:
            raise _error(409, "draft_batch_terminal", "draft batch has already finished")
        return result

    @app.get("/portfolio/monitor-report-candidates", dependencies=dependency)
    async def list_monitor_report_candidates(symbol: str = Query(min_length=2, max_length=40)):
        try:
            return await asyncio.to_thread(get_service().list_report_candidates, symbol)
        except ValueError as exc:
            raise _error(400, "invalid_monitor_symbol", str(exc)) from exc

    @app.post("/portfolio/monitor-planner-jobs", dependencies=dependency, status_code=202)
    async def create_monitor_planner_job(payload: MonitorPlannerJobPayload, response: Response):
        try:
            result = get_service().create_planner_job(
                payload.symbols,
                report_refs=payload.report_refs,
                research_policy=payload.research_policy,
                delivery_target_id=(
                    payload.delivery_target_id
                    or get_service().store.get_default_delivery_target_id()
                ),
                force_fresh=payload.force_fresh,
                activation_mode=payload.activation_mode,
                trigger_type=payload.trigger_type,
            )
        except ValueError as exc:
            raise _error(400, "invalid_monitor_planner_request", str(exc)) from exc
        response.headers["Location"] = f"/portfolio/monitor-planner-jobs/{result['job_id']}"
        return result

    @app.get("/portfolio/monitor-planner-jobs/{job_id}", dependencies=dependency)
    async def get_monitor_planner_job(job_id: str):
        result = get_service().store.get_planner_job(job_id)
        if not result:
            raise _error(404, "monitor_planner_job_not_found", "monitor planner job not found")
        return result

    @app.get("/portfolio/monitor-planner-jobs/{job_id}/usage", dependencies=dependency)
    async def get_monitor_planner_job_usage(job_id: str):
        try:
            return get_service().get_job_usage(job_id)
        except KeyError as exc:
            raise _error(
                404,
                "monitor_planner_job_not_found",
                "monitor planner job not found",
            ) from exc

    @app.get(
        "/portfolio/monitor-planner-jobs/{job_id}/usage/events",
        dependencies=dependency,
    )
    async def list_monitor_planner_job_usage_events(
        job_id: str,
        kind: Literal["llm_call", "tool_call", "resource_call"] | None = None,
        category: str | None = Query(default=None, max_length=40),
        attempt_id: str | None = Query(default=None, max_length=120),
        cursor: str | None = None,
        limit: int = Query(50, ge=1, le=100),
    ):
        try:
            return get_service().list_job_usage_events(
                job_id,
                kind=kind,
                category=category,
                attempt_id=attempt_id,
                cursor=cursor,
                limit=limit,
            )
        except KeyError as exc:
            raise _error(
                404,
                "monitor_planner_job_not_found",
                "monitor planner job not found",
            ) from exc
        except ValueError as exc:
            raise _error(400, "invalid_usage_cursor", str(exc)) from exc

    @app.get("/portfolio/monitoring/usage", dependencies=dependency)
    async def get_portfolio_monitoring_usage(
        period: Literal["today", "7d", "30d"] = "today",
    ):
        return get_service().get_usage_summary(period)

    @app.get("/portfolio/monitoring/usage/events", dependencies=dependency)
    async def list_portfolio_monitoring_usage_events(
        period: Literal["today", "7d", "30d"] = "today",
        kind: Literal["llm_call", "tool_call", "resource_call"] | None = None,
        category: str | None = Query(default=None, max_length=40),
        cursor: str | None = None,
        limit: int = Query(50, ge=1, le=100),
    ):
        try:
            return get_service().list_usage_events(
                period,
                kind=kind,
                category=category,
                cursor=cursor,
                limit=limit,
            )
        except ValueError as exc:
            raise _error(400, "invalid_usage_cursor", str(exc)) from exc

    @app.post("/portfolio/monitor-planner-jobs/{job_id}/cancel", dependencies=dependency)
    async def cancel_monitor_planner_job(job_id: str):
        try:
            return get_service().cancel_planner_job(job_id)
        except KeyError as exc:
            raise _error(404, "monitor_planner_job_not_found", "monitor planner job not found") from exc
        except RuntimeError as exc:
            raise _error(409, "monitor_planner_job_terminal", str(exc)) from exc

    @app.post(
        "/portfolio/monitor-planner-jobs/{job_id}/items/{symbol}/retry",
        dependencies=dependency,
        status_code=202,
    )
    async def retry_monitor_planner_job_item(job_id: str, symbol: str):
        try:
            return get_service().retry_planner_job_item(job_id, symbol)
        except KeyError as exc:
            raise _error(404, "monitor_planner_item_not_found", "monitor planner item not found") from exc
        except RuntimeError as exc:
            raise _error(409, "monitor_planner_item_not_retryable", str(exc)) from exc
        except ValueError as exc:
            raise _error(422, "monitor_planner_item_not_selected", str(exc)) from exc

    @app.get("/portfolio/monitors", dependencies=dependency)
    async def list_portfolio_monitors():
        return {"profiles": get_service().list_profiles()}

    @app.get("/portfolio/monitors/{profile_id}", dependencies=dependency)
    async def get_portfolio_monitor(profile_id: str):
        result = get_service().get_profile(profile_id)
        if not result:
            raise _error(404, "monitor_not_found", "monitor profile not found")
        return result

    @app.get("/portfolio/monitors/{profile_id}/plans/{version}", dependencies=dependency)
    async def get_portfolio_monitor_plan(profile_id: str, version: int):
        result = get_service().store.get_plan(profile_id, version)
        if not result:
            raise _error(404, "monitor_plan_not_found", "monitor plan not found")
        return result

    @app.patch("/portfolio/monitors/{profile_id}/plans/{version}", dependencies=dependency)
    async def patch_portfolio_monitor_plan(
        profile_id: str,
        version: int,
        payload: PlanPatchPayload,
        if_match: str | None = Header(default=None, alias="If-Match"),
    ):
        expected = payload.expected_revision
        if if_match:
            try:
                expected = int(if_match.strip('"W/'))
            except ValueError as exc:
                raise _error(400, "invalid_if_match", "If-Match must contain a profile revision") from exc
        try:
            return get_service().store.update_draft(profile_id, version, payload.plan, expected)
        except KeyError as exc:
            raise _error(404, "monitor_plan_not_found", "monitor plan not found") from exc
        except RuntimeError as exc:
            raise _error(409, "profile_revision_conflict", str(exc)) from exc
        except (PlanValidationError, ValueError) as exc:
            raise _error(422, "monitor_plan_invalid", str(exc)) from exc

    @app.post(
        "/portfolio/monitors/{profile_id}/plans/{version}/save-and-activate",
        dependencies=dependency,
    )
    async def save_and_activate_portfolio_monitor_plan(
        profile_id: str,
        version: int,
        payload: PlanPatchPayload,
        if_match: str | None = Header(default=None, alias="If-Match"),
    ):
        expected = payload.expected_revision
        if if_match:
            try:
                expected = int(if_match.strip('"W/'))
            except ValueError as exc:
                raise _error(
                    400,
                    "invalid_if_match",
                    "If-Match must contain a profile revision",
                ) from exc
        missing_assets = _missing_ymca_assets({"plan": payload.plan})
        if missing_assets:
            raise _error(
                409,
                "monitor_effect_unavailable",
                "YMCA audio and the sticker for each selected direction must be ready before activation",
                [f"ymca_{asset}_unavailable" for asset in missing_assets],
            )
        try:
            maximum = int(os.getenv("VIBE_TRADING_MONITOR_MAX_ACTIVE_SYMBOLS", "10"))
            return get_service().store.save_and_activate(
                profile_id,
                version,
                payload.plan,
                expected,
                max_active=max(1, maximum),
            )
        except KeyError as exc:
            raise _error(404, "monitor_plan_not_found", "monitor plan not found") from exc
        except RuntimeError as exc:
            raise _error(409, "profile_revision_conflict", str(exc)) from exc
        except OverflowError as exc:
            raise _error(429, "active_monitor_limit", str(exc)) from exc
        except (PlanValidationError, ValueError) as exc:
            raise _error(422, "monitor_plan_not_activatable", str(exc)) from exc

    @app.post("/portfolio/monitors/{profile_id}/plans/{version}/activate", dependencies=dependency)
    async def activate_portfolio_monitor_plan(
        profile_id: str,
        version: int,
        if_match: str | None = Header(default=None, alias="If-Match"),
    ):
        expected_revision: int | None = None
        if if_match:
            try:
                expected_revision = int(if_match.strip('"W/'))
            except ValueError as exc:
                raise _error(
                    400,
                    "invalid_if_match",
                    "If-Match must contain a profile revision",
                ) from exc
        try:
            plan_record = get_service().store.get_plan(profile_id, version)
            if not plan_record:
                raise _error(404, "monitor_plan_not_found", "monitor plan not found")
            missing_assets = _missing_ymca_assets(plan_record)
            if missing_assets:
                raise _error(
                    409,
                    "monitor_effect_unavailable",
                    "YMCA audio and the sticker for each selected direction must be ready before activation",
                    [f"ymca_{asset}_unavailable" for asset in missing_assets],
                )
            maximum = int(os.getenv("VIBE_TRADING_MONITOR_MAX_ACTIVE_SYMBOLS", "10"))
            if expected_revision is None:
                return get_service().store.activate(
                    profile_id,
                    version,
                    max_active=max(1, maximum),
                )
            return get_service().store.activate(
                profile_id,
                version,
                max_active=max(1, maximum),
                expected_revision=expected_revision,
            )
        except KeyError as exc:
            raise _error(404, "monitor_plan_not_found", "monitor plan not found") from exc
        except RuntimeError as exc:
            raise _error(409, "profile_revision_conflict", str(exc)) from exc
        except OverflowError as exc:
            raise _error(429, "active_monitor_limit", str(exc)) from exc
        except (PlanValidationError, ValueError) as exc:
            raise _error(422, "monitor_plan_not_activatable", str(exc)) from exc

    @app.post("/portfolio/monitors/{profile_id}/reanalyze", dependencies=dependency, status_code=202)
    async def reanalyze_portfolio_monitor(
        profile_id: str,
        payload: ReanalyzePayload | None = None,
    ):
        try:
            return await asyncio.to_thread(
                get_service().reanalyze,
                profile_id,
                allow_single_source=(payload or ReanalyzePayload()).allow_single_source,
            )
        except KeyError as exc:
            raise _error(404, "monitor_not_found", "monitor profile not found") from exc
        except ValueError as exc:
            raise _error(409, "monitor_reanalysis_blocked", str(exc)) from exc

    @app.post("/portfolio/monitors/{profile_id}/reopen", dependencies=dependency, status_code=202)
    async def reopen_portfolio_monitor(profile_id: str, payload: ReopenPayload | None = None):
        value = payload or ReopenPayload()
        try:
            return await asyncio.to_thread(
                get_service().reopen,
                profile_id,
                value.delivery_target_id,
                allow_single_source=value.allow_single_source,
            )
        except KeyError as exc:
            raise _error(404, "monitor_not_found", "monitor profile not found") from exc
        except ValueError as exc:
            raise _error(409, "monitor_reopen_invalid", str(exc)) from exc

    @app.post("/portfolio/monitors/{profile_id}/pause", dependencies=dependency)
    async def pause_portfolio_monitor(profile_id: str, payload: PausePayload | None = None):
        value = payload or PausePayload()
        resume_at = (
            (datetime.now(timezone.utc) + timedelta(hours=value.duration_hours)).isoformat()
            if value.duration_hours else None
        )
        try:
            return get_service().store.transition(profile_id, "pause", resume_at=resume_at, reason=value.reason)
        except KeyError as exc:
            raise _error(404, "monitor_not_found", "monitor profile not found") from exc
        except ValueError as exc:
            raise _error(409, "monitor_transition_invalid", str(exc)) from exc

    @app.post("/portfolio/monitors/{profile_id}/resume", dependencies=dependency)
    async def resume_portfolio_monitor(profile_id: str):
        try:
            return get_service().store.transition(profile_id, "resume")
        except KeyError as exc:
            raise _error(404, "monitor_not_found", "monitor profile not found") from exc
        except ValueError as exc:
            raise _error(409, "monitor_transition_invalid", str(exc)) from exc

    @app.post("/portfolio/monitors/{profile_id}/close", dependencies=dependency)
    async def close_portfolio_monitor(profile_id: str):
        try:
            return get_service().store.transition(profile_id, "close")
        except KeyError as exc:
            raise _error(404, "monitor_not_found", "monitor profile not found") from exc

    @app.get("/portfolio/monitor-events", dependencies=dependency)
    async def list_portfolio_monitor_events(
        limit: int = Query(50, ge=1, le=200), symbol: str | None = None
    ):
        return {"events": get_service().store.list_events(limit=limit, symbol=symbol)}

    @app.get("/portfolio/monitor-events/stream", dependencies=stream_dependency)
    async def stream_portfolio_monitor_events(
        request: Request,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ):
        return StreamingResponse(
            _monitor_event_stream(request, get_service().store, last_event_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "private, no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/portfolio/monitor-effects/ymca_v1/audio", dependencies=dependency)
    async def get_portfolio_monitor_ymca_audio():
        path = get_monitor_ymca_audio_path()
        if path is None:
            raise _error(404, "monitor_effect_unavailable", "YMCA audio asset is not ready")
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(
            path,
            media_type=media_type,
            headers={"Cache-Control": "private, no-store"},
        )

    @app.get("/portfolio/monitor-events/{event_id}", dependencies=dependency)
    async def get_portfolio_monitor_event(event_id: str):
        result = get_service().store.get_event(event_id)
        if not result:
            raise _error(404, "monitor_event_not_found", "monitor event not found")
        return result

    @app.post("/portfolio/monitor-events/{event_id}/acknowledge", dependencies=dependency)
    async def acknowledge_portfolio_monitor_event(event_id: str):
        try:
            return get_service().store.acknowledge_event(event_id)
        except KeyError as exc:
            raise _error(404, "monitor_event_not_found", "monitor event not found") from exc

    @app.get("/portfolio/monitoring/status", dependencies=dependency)
    async def get_portfolio_monitoring_status():
        runtime = get_runtime()
        return _status_with_effects(get_service(), runtime.status())

    @app.get("/portfolio/monitoring/targets", dependencies=dependency)
    async def list_portfolio_monitoring_targets():
        return {"targets": get_service().list_monitoring_targets()}

    @app.get("/portfolio/monitoring/target-cards", dependencies=dependency)
    async def list_portfolio_monitoring_target_cards():
        return {"targets": get_service().list_monitoring_targets()}

    @app.get("/portfolio/monitoring/targets/{symbol}/decision", dependencies=dependency)
    async def get_portfolio_monitoring_target_decision(symbol: str):
        try:
            return get_service().get_target_decision(symbol)
        except KeyError as exc:
            raise _error(404, "monitor_target_not_found", "monitor target not found") from exc

    @app.put("/portfolio/monitoring/risk-preferences/{symbol}", dependencies=dependency)
    async def put_portfolio_monitoring_risk_preference(
        symbol: str,
        payload: MonitorRiskPreferencePayload,
    ):
        try:
            return get_service().set_risk_preference(symbol, payload.model_dump())
        except ValueError as exc:
            raise _error(422, "monitor_risk_preference_invalid", str(exc)) from exc

    @app.post(
        "/portfolio/monitoring/decisions/{decision_id}/choices/{choice_id}",
        dependencies=dependency,
    )
    async def choose_portfolio_monitoring_decision(
        decision_id: str,
        choice_id: str,
        payload: MonitorDecisionChoicePayload,
    ):
        if payload.decision_id != decision_id or payload.choice_id != choice_id:
            raise _error(422, "monitor_decision_path_mismatch", "decision or choice path does not match payload")
        try:
            return get_service().record_decision_choice(**payload.model_dump())
        except ValueError as exc:
            raise _error(409, "monitor_decision_stale", str(exc)) from exc

    @app.post(
        "/portfolio/monitoring/decisions/{decision_id}/condition-order-drafts",
        dependencies=dependency,
    )
    async def create_portfolio_monitoring_condition_order_draft(
        decision_id: str,
        payload: MonitorConditionDraftPayload,
    ):
        try:
            return get_service().create_condition_order_draft(
                decision_id=decision_id,
                **payload.model_dump(),
            )
        except ValueError as exc:
            raise _error(409, "monitor_condition_draft_not_eligible", str(exc)) from exc

    @app.post(
        "/portfolio/monitoring/condition-order-drafts/{draft_id}/validate",
        dependencies=dependency,
    )
    async def validate_portfolio_monitoring_condition_order_draft(draft_id: str):
        try:
            return get_service().validate_condition_order_draft(draft_id)
        except KeyError as exc:
            raise _error(404, "monitor_condition_draft_not_found", "condition-order draft not found") from exc
        except ValueError as exc:
            raise _error(409, "monitor_condition_draft_invalid", str(exc)) from exc

    @app.post(
        "/portfolio/monitoring/condition-order-drafts/{draft_id}/cancel",
        dependencies=dependency,
    )
    async def cancel_portfolio_monitoring_condition_order_draft(draft_id: str):
        try:
            return get_service().cancel_condition_order_draft(draft_id)
        except KeyError as exc:
            raise _error(404, "monitor_condition_draft_not_found", "condition-order draft not found") from exc

    @app.get("/portfolio/monitoring/autopilot", dependencies=dependency)
    async def get_portfolio_monitoring_autopilot():
        return get_service().get_autopilot_config()

    @app.put("/portfolio/monitoring/autopilot", dependencies=dependency)
    async def configure_portfolio_monitoring_autopilot(payload: MonitorAutopilotPayload):
        service = get_service()
        current = service.get_autopilot_config()
        requested_empty_scope = payload.selected_symbols is not None and not payload.selected_symbols
        if (
            current.get("enabled")
            and (not payload.enabled or requested_empty_scope)
            and payload.change_source is None
        ):
            raise _error(
                409,
                "monitor_autopilot_change_source_required",
                "disabling autonomous monitoring requires an explicit user action source",
            )
        try:
            config_payload = payload.model_dump(exclude={"change_source"}, exclude_none=True)
            if "delivery_target_id" in payload.model_fields_set:
                config_payload["delivery_target_id"] = payload.delivery_target_id
            return await asyncio.to_thread(
                service.set_autopilot_config,
                config_payload,
            )
        except ValueError as exc:
            raise _error(422, "monitor_autopilot_invalid", str(exc)) from exc

    @app.get("/portfolio/monitoring/autopilot/runs", dependencies=dependency)
    async def list_portfolio_monitoring_autopilot_runs(
        limit: int = Query(100, ge=1, le=500),
    ):
        return {"runs": get_service().list_autopilot_runs(limit=limit)}

    @app.get("/portfolio/monitor-recommendations", dependencies=dependency)
    async def list_portfolio_monitor_recommendations(
        symbol: str | None = None,
        status: str | None = None,
        limit: int = Query(100, ge=1, le=500),
    ):
        return {
            "recommendations": get_service().list_recommendations(
                symbol=symbol,
                status=status,
                limit=limit,
            )
        }

    @app.post(
        "/portfolio/monitor-recommendations/{recommendation_id}/acknowledge",
        dependencies=dependency,
    )
    async def acknowledge_portfolio_monitor_recommendation(
        recommendation_id: str,
        payload: MonitorRecommendationAcknowledgePayload,
    ):
        try:
            return get_service().acknowledge_recommendation(
                recommendation_id,
                payload.feedback_status,
            )
        except KeyError as exc:
            raise _error(404, "monitor_recommendation_not_found", "monitor recommendation not found") from exc

    @app.put("/admin/portfolio/monitoring/config", dependencies=dependency)
    async def configure_portfolio_monitoring_runtime(payload: RuntimeConfigPayload):
        runtime = get_runtime()
        if payload.enabled and payload.mode == "deliver":
            readiness = runtime.deliver_readiness()
            if not readiness["ready"]:
                raise _error(
                    409,
                    "monitor_deliver_not_ready",
                    "real monitoring delivery has not passed its release gates",
                    readiness["blocked_reasons"],
                )
        try:
            set_runtime_config(payload.enabled, payload.mode)
        except OSError as exc:
            raise _error(500, "monitor_config_write_failed", "failed to persist monitoring configuration") from exc
        if payload.enabled:
            await runtime.start(force=True)
        else:
            await runtime.stop()
        return _status_with_effects(get_service(), runtime.status())

    @app.post(
        "/admin/portfolio/monitoring/test-delivery",
        dependencies=dependency,
    )
    async def test_portfolio_monitoring_delivery():
        try:
            return await get_runtime().send_test_delivery()
        except ValueError as exc:
            raise _error(
                409,
                "monitor_test_delivery_not_ready",
                str(exc),
            ) from exc
        except Exception as exc:
            raise _error(
                502,
                "monitor_test_delivery_failed",
                f"monitoring test delivery failed: {type(exc).__name__}",
            ) from exc

    @app.post(
        "/admin/portfolio/monitoring/deliveries/{delivery_id}/reconcile",
        dependencies=dependency,
    )
    async def reconcile_portfolio_monitoring_delivery(
        delivery_id: str,
        payload: DeliveryReconcilePayload,
    ):
        try:
            return get_service().store.reconcile_uncertain_delivery(
                delivery_id,
                status=payload.status,
                remote_message_id=payload.remote_message_id,
                provider=payload.provider,
                note=payload.note,
            )
        except ValueError as exc:
            raise _error(
                409,
                "monitor_delivery_reconcile_conflict",
                str(exc),
            ) from exc

    @app.post("/admin/portfolio/monitoring/start", dependencies=dependency)
    async def start_portfolio_monitoring_runtime():
        runtime = get_runtime()
        await runtime.start(force=True)
        return _status_with_effects(get_service(), runtime.status())

    @app.post("/admin/portfolio/monitoring/stop", dependencies=dependency)
    async def stop_portfolio_monitoring_runtime():
        runtime = get_runtime()
        await runtime.stop()
        return _status_with_effects(get_service(), runtime.status())

    @app.post("/admin/portfolio/monitoring/maintenance", dependencies=dependency)
    async def maintain_portfolio_monitoring(payload: MaintenancePayload | None = None):
        value = payload or MaintenancePayload()
        return await asyncio.to_thread(
            get_service().store.run_maintenance,
            force=value.force,
        )
