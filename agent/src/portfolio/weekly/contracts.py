"""Strict structured contract for a formal single-symbol weekly review."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from src.portfolio.monitoring.models import validate_monitoring_bundle
from src.reports.data_gaps import gap_codes, normalize_gap_details


WEEKLY_TREND_STAGES = {"上升", "下降", "震荡", "筑底", "突破后回踩", "待确认"}
WEEKLY_TREND_DIRECTIONS = {"向上", "向下", "横盘", "待确认"}
WEEKLY_TREND_STRENGTHS = {"强", "中", "弱", "待确认"}
WEEKLY_OUTCOMES = {
    "confirmed",
    "invalidated",
    "approached",
    "not_triggered",
    "unresolved",
    "insufficient_data",
    "expired",
}
WEEKLY_CHANGE_TYPES = {
    "new",
    "unchanged",
    "raised",
    "lowered",
    "modified",
    "withdrawn",
    "expired",
}
WEEKLY_LEVEL_TYPES = {
    "support",
    "resistance",
    "breakout",
    "breakdown",
    "reclaim",
    "invalidation",
    "watch_zone",
}


class WeeklyContractError(ValueError):
    """Raised when a weekly report escapes its deterministic contract."""


def _aware(value: Any, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise WeeklyContractError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WeeklyContractError(f"{field} must include a timezone")
    return parsed.isoformat()


def _day(value: Any, field: str) -> str:
    try:
        return date.fromisoformat(str(value or "")[:10]).isoformat()
    except ValueError as exc:
        raise WeeklyContractError(f"{field} must be YYYY-MM-DD") from exc


def _strings(value: Any, *, maximum: int = 20) -> list[str]:
    if not isinstance(value, list):
        raise WeeklyContractError("expected a list of strings")
    return [str(item).strip()[:2000] for item in value[:maximum] if str(item).strip()]


def validate_weekly_review(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized weekly report; JSON remains the sole machine source."""

    if not isinstance(payload, dict):
        raise WeeklyContractError("weekly review must be an object")
    result = dict(payload)
    schema_version = int(result.get("schema_version") or 0)
    if schema_version not in {1, 2}:
        raise WeeklyContractError("schema_version must be 1 or 2")
    symbol = str(result.get("symbol") or "").strip().upper()
    if not symbol:
        raise WeeklyContractError("symbol is required")
    instrument_type = str(result.get("instrument_type") or "")
    if instrument_type not in {"etf", "company_equity"}:
        raise WeeklyContractError("instrument_type is not allowed")
    report_audience = str(result.get("report_audience") or "user")
    if report_audience != "user":
        raise WeeklyContractError(
            "report_audience must be user until a separate monitor profile is available"
        )
    week_start = _day(result.get("week_start"), "week_start")
    week_end = _day(result.get("week_end"), "week_end")
    if week_start > week_end:
        raise WeeklyContractError("week_start cannot follow week_end")

    weekly_view = result.get("weekly_view")
    if not isinstance(weekly_view, dict):
        raise WeeklyContractError("weekly_view must be an object")
    if str(weekly_view.get("trend_stage") or "") not in WEEKLY_TREND_STAGES:
        raise WeeklyContractError("weekly_view.trend_stage is not allowed")
    if str(weekly_view.get("trend_direction") or "") not in WEEKLY_TREND_DIRECTIONS:
        raise WeeklyContractError("weekly_view.trend_direction is not allowed")
    if str(weekly_view.get("trend_strength") or "") not in WEEKLY_TREND_STRENGTHS:
        raise WeeklyContractError("weekly_view.trend_strength is not allowed")
    try:
        weekly_view["week_return_pct"] = float(weekly_view.get("week_return_pct"))
    except (TypeError, ValueError) as exc:
        raise WeeklyContractError("weekly_view.week_return_pct must be numeric") from exc

    levels = result.get("key_levels") or []
    if not isinstance(levels, list) or len(levels) > 16:
        raise WeeklyContractError("key_levels must be a list")
    for index, level in enumerate(levels):
        if not isinstance(level, dict):
            raise WeeklyContractError(f"key_levels[{index}] must be an object")
        if str(level.get("level_type") or "") not in WEEKLY_LEVEL_TYPES:
            raise WeeklyContractError(f"key_levels[{index}].level_type is not allowed")
        if str(level.get("adjustment") or "") != "raw":
            raise WeeklyContractError(f"key_levels[{index}] must use raw prices")
        if not isinstance(level.get("calculation_basis"), dict):
            raise WeeklyContractError(f"key_levels[{index}] needs calculation_basis")
        if not level.get("claim_ids"):
            raise WeeklyContractError(f"key_levels[{index}] needs Claim links")

    validations = result.get("previous_week_validation") or []
    if not isinstance(validations, list):
        raise WeeklyContractError("previous_week_validation must be a list")
    for index, item in enumerate(validations):
        if not isinstance(item, dict) or str(item.get("outcome") or "") not in WEEKLY_OUTCOMES:
            raise WeeklyContractError(f"previous_week_validation[{index}] is invalid")

    changes = result.get("scenario_changes") or []
    if not isinstance(changes, list):
        raise WeeklyContractError("scenario_changes must be a list")
    for index, item in enumerate(changes):
        if not isinstance(item, dict) or str(item.get("change_type") or "") not in WEEKLY_CHANGE_TYPES:
            raise WeeklyContractError(f"scenario_changes[{index}] is invalid")

    quality = str(result.get("quality_status") or "")
    coverage = str(result.get("coverage_status") or "")
    if quality not in {"passed", "passed_with_gaps", "failed_validation"}:
        raise WeeklyContractError("quality_status is not allowed")
    if coverage not in {"complete", "partial", "insufficient"}:
        raise WeeklyContractError("coverage_status is not allowed")
    if str(result.get("trade_execution") or "") != "forbidden":
        raise WeeklyContractError("trade_execution must be forbidden")
    raw_gap_details = result.get("data_gap_details")
    normalized_gap_details: list[dict[str, Any]] | None = None
    if raw_gap_details is not None:
        if not isinstance(raw_gap_details, list):
            raise WeeklyContractError("data_gap_details must be a list")
        try:
            normalized_gap_details = normalize_gap_details(
                raw_gap_details,
                instrument_type=instrument_type,
            )
        except ValueError as exc:
            raise WeeklyContractError(str(exc)) from exc
        supplied_codes = _strings(result.get("data_gaps") or [])
        derived_codes = gap_codes(normalized_gap_details)
        if supplied_codes != derived_codes:
            raise WeeklyContractError(
                "data_gaps must be derived from data_gap_details in stable order"
            )
    if schema_version == 2:
        weekly_context = result.get("weekly_context")
        if not isinstance(weekly_context, dict):
            raise WeeklyContractError("weekly_context is required for schema_version 2")
        if int(weekly_context.get("schema_version") or 0) != 2:
            raise WeeklyContractError("weekly_context.schema_version must be 2")
        method_snapshot = result.get("analysis_method_snapshot")
        if not isinstance(method_snapshot, dict):
            raise WeeklyContractError("analysis_method_snapshot is required")
        if str(method_snapshot.get("cutoff_policy") or "") != "completed_daily_bars_only":
            raise WeeklyContractError("analysis methods must use completed daily bars only")
        if str((method_snapshot.get("price_basis") or {}).get("adjustment") or "") != "raw":
            raise WeeklyContractError("weekly analysis methods must use raw prices")
        agent_analysis = result.get("agent_analysis")
        if not isinstance(agent_analysis, dict):
            raise WeeklyContractError("agent_analysis is required")
        if str(agent_analysis.get("status") or "") not in {"completed", "not_run"}:
            raise WeeklyContractError("agent_analysis.status is invalid")
        if str(agent_analysis.get("trade_execution") or "") != "forbidden":
            raise WeeklyContractError("agent analysis cannot execute trades")
        available_levels = {
            str(item.get("candidate_id"))
            for item in method_snapshot.get("level_candidates") or []
            if isinstance(item, dict) and item.get("candidate_id")
        }
        if not set(agent_analysis.get("selected_level_ids") or []) <= available_levels:
            raise WeeklyContractError("agent analysis selected an unknown method level")

    bundle = validate_monitoring_bundle(
        result.get("monitoring_bundle"),
        expected_symbol=symbol,
        expected_horizon="weekly",
    )
    result.update(
        symbol=symbol,
        instrument_type=instrument_type,
        report_audience=report_audience,
        week_start=week_start,
        week_end=week_end,
        generated_at=_aware(result.get("generated_at"), "generated_at"),
        data_as_of=_aware(result.get("data_as_of"), "data_as_of"),
        valid_from=_aware(result.get("valid_from"), "valid_from"),
        valid_until=_aware(result.get("valid_until"), "valid_until"),
        review_due_at=_aware(result.get("review_due_at"), "review_due_at"),
        source_valid_until=_aware(
            result.get("source_valid_until"), "source_valid_until"
        ),
        reasons=_strings(result.get("reasons") or []),
        risks=_strings(result.get("risks") or []),
        data_gaps=(
            gap_codes(normalized_gap_details)
            if normalized_gap_details is not None
            else _strings(result.get("data_gaps") or [])
        ),
        data_gap_details=normalized_gap_details or [],
        analysis_notes=_strings(result.get("analysis_notes") or []),
        safety_notes=_strings(result.get("safety_notes") or []),
        monitoring_bundle=bundle,
        trade_execution="forbidden",
    )
    return result
