"""Formal, idempotent weekly report production for one symbol per run."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import re
import uuid
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.data_layer.prewarm import ChinaMarketCalendar
from src.data_layer.service import get_unified_data_service
from src.portfolio.analysis_methods import (
    build_agent_method_prompt,
    build_market_analysis_snapshot,
    unavailable_agent_analysis,
    validate_agent_method_analysis,
)
from src.portfolio.daily.store import TERMINAL_STATUSES
from src.portfolio.instruments import (
    infer_portfolio_instrument_type,
    portfolio_tick_size,
)
from src.portfolio.monitoring.models import DEFAULT_PRICE_VOLUME_POLICY
from src.portfolio.state import load_state, normalize_symbol
from src.reports.data_gaps import (
    DATA_GAP_LABELS,
    gap_codes,
    make_gap_detail,
    normalize_gap_details,
    quality_affecting_gaps,
)

from .contracts import validate_weekly_review
from .context import WeeklyContextAssembler
from .etf_metrics import (
    build_etf_tracking_metrics,
    enrich_weekly_context_with_etf_metrics,
    tracked_index_code_from_context,
)
from .reporting import render_weekly_markdown
from .store import WeeklyRunStore
from .verification import (
    compare_weekly_scenarios,
    deterministic_weekly_view,
    next_week_review_due,
    normalize_daily_bars,
    resolve_completed_trading_week,
    split_week_bars,
    validate_previous_week_scenarios,
    weekly_market_statistics,
)


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_TRUE_VALUES = {"1", "true", "yes", "on"}


def weekly_report_enabled() -> bool:
    return os.getenv("VIBE_TRADING_WEEKLY_REPORT_ENABLED", "0").strip().lower() in _TRUE_VALUES


def weekly_agent_analysis_enabled() -> bool:
    """Enable one bounded synthesis pass after deterministic data gates pass."""

    return (
        os.getenv("VIBE_TRADING_WEEKLY_AGENT_ANALYSIS_ENABLED", "1")
        .strip()
        .lower()
        in _TRUE_VALUES
    )


def _now_local() -> str:
    return datetime.now(_SHANGHAI).isoformat()


def _stable_id(prefix: str, *parts: Any, length: int = 24) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:length]}"


def _instrument_type(symbol: str) -> str:
    return infer_portfolio_instrument_type(symbol)


def _tick_size(symbol: str) -> float:
    return portfolio_tick_size(symbol)


def _safe_part(value: Any, fallback: str) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "").strip())
    text = re.sub(r"\s+", "", text).strip("._")
    return text[:80] or fallback


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _claim(
    report_id: str,
    section: str,
    ordinal: int,
    text: Any,
    *,
    claim_type: str = "inference",
) -> dict[str, Any] | None:
    content = str(text or "").strip()
    if not content:
        return None
    return {
        "claim_id": _stable_id("claim", report_id, section, ordinal, content),
        "section_id": section,
        "claim_type": claim_type,
        "text": content[:4000],
        "fact_ids": [],
        "evidence_ids": [],
    }


class WeeklyReportRunService:
    """Create immutable JSON/Markdown/PDF weekly reports and catalog entries."""

    def __init__(
        self,
        *,
        store: WeeklyRunStore | None = None,
        session_service: Any | None = None,
        data_service: Any | None = None,
        calendar: Any | None = None,
        pdf_renderer: Callable[[str, str], bytes] | None = None,
        state_loader: Callable[[], Any] = load_state,
        now_provider: Callable[[], datetime] | None = None,
        enabled_override: bool | None = None,
        agent_analysis_enabled_override: bool | None = None,
        recover_incomplete: bool = True,
    ) -> None:
        self.store = store or WeeklyRunStore()
        self.session_service = session_service
        self.data_service = data_service or get_unified_data_service()
        self.calendar = calendar or ChinaMarketCalendar()
        self.pdf_renderer = pdf_renderer
        self.state_loader = state_loader
        self.now_provider = now_provider or (lambda: datetime.now(_SHANGHAI))
        self.enabled_override = enabled_override
        self.agent_analysis_enabled_override = agent_analysis_enabled_override
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel: dict[str, asyncio.Event] = {}
        if recover_incomplete:
            self.store.mark_incomplete_interrupted()

    def enabled(self) -> bool:
        return self.enabled_override if self.enabled_override is not None else weekly_report_enabled()

    def agent_analysis_enabled(self) -> bool:
        configured = (
            self.agent_analysis_enabled_override
            if self.agent_analysis_enabled_override is not None
            else weekly_agent_analysis_enabled()
        )
        return bool(configured and self.session_service is not None)

    def list_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        return self.store.list(limit)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self.store.get(run_id)

    def metrics(self) -> dict[str, Any]:
        records = self.store.list(limit=10_000)
        by_status = Counter(str(item.get("status") or "unknown") for item in records)
        by_symbol: dict[str, Counter[str]] = {}
        gate_failures: Counter[str] = Counter()
        action_ready = watch_only = validation_total = validation_covered = changes = 0
        stale = 0
        now = self.now_provider().astimezone(timezone.utc)
        for item in records:
            by_symbol.setdefault(str(item.get("symbol") or ""), Counter())[str(item.get("status") or "unknown")] += 1
            gate = item.get("analysis_gate") or {}
            if gate.get("decision") != "proceed":
                for reason in gate.get("missing_scopes") or ["unknown"]:
                    gate_failures[str(reason)] += 1
            action_ready += int(item.get("action_ready_count") or 0)
            watch_only += int(item.get("watch_only_count") or 0)
            validation_total += int(item.get("previous_candidate_count") or 0)
            validation_covered += int(item.get("previous_validation_count") or 0)
            changes += int(item.get("scenario_change_count") or 0)
            due = str(item.get("review_due_at") or "")
            try:
                parsed_due = datetime.fromisoformat(due.replace("Z", "+00:00"))
                if parsed_due.tzinfo is None:
                    parsed_due = parsed_due.replace(tzinfo=_SHANGHAI)
                if parsed_due.astimezone(timezone.utc) <= now and item.get("status") in {"completed", "completed_with_warnings"}:
                    stale += 1
            except ValueError:
                pass
        return {
            "total_runs": len(records),
            "by_status": dict(by_status),
            "by_symbol": {key: dict(value) for key, value in by_symbol.items() if key},
            "data_gate_failures": dict(gate_failures),
            "model_calls": sum(int(item.get("model_calls") or 0) for item in records),
            "model_cost": sum(float(item.get("model_cost") or 0) for item in records),
            "artifact_failures": sum(1 for item in records if item.get("failure_stage") == "rendering_artifacts"),
            "catalog_failures": sum(1 for item in records if item.get("catalog_status") == "failed"),
            "duplicate_schedules_suppressed": sum(int(item.get("deduplicated_requests") or 0) for item in records),
            "previous_week_validation_coverage": (
                round(validation_covered / validation_total, 6) if validation_total else None
            ),
            "scenario_change_count": changes,
            "action_ready_count": action_ready,
            "watch_only_count": watch_only,
            "review_due_without_update_count": stale,
        }

    def _portfolio(self) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        raw = self.state_loader()
        portfolio = raw.to_dict() if hasattr(raw, "to_dict") else dict(raw)
        holdings = {
            normalize_symbol(str(item.get("symbol") or item.get("code") or "")).upper(): dict(item)
            for item in portfolio.get("holdings") or []
            if str(item.get("symbol") or item.get("code") or "").strip()
        }
        return portfolio, holdings

    async def start(
        self,
        *,
        week_end: str | None = None,
        symbols: list[str] | None = None,
        refresh_policy: str = "ensure_fresh",
        report_profile: str = "weekly_review_v2",
        report_audience: str = "user",
        force_new: bool = False,
        single_source_authorized: bool = False,
        trigger: str = "manual",
    ) -> list[dict[str, Any]]:
        if not self.enabled():
            raise ValueError("weekly report feature is disabled")
        if refresh_policy not in {"ensure_fresh", "force", "reuse"}:
            raise ValueError("refresh_policy must be ensure_fresh, force, or reuse")
        if report_audience != "user":
            raise ValueError(
                "monitor-facing weekly reports are reserved for a separate profile and are not available yet"
            )
        resolved_start, resolved_end, sessions = resolve_completed_trading_week(
            self.calendar,
            requested_week_end=week_end,
            now=self.now_provider(),
        )
        portfolio, holdings = self._portfolio()
        requested = symbols or list(holdings)
        normalized: list[str] = []
        for raw in requested:
            symbol = normalize_symbol(str(raw)).upper()
            if symbol and symbol not in normalized:
                normalized.append(symbol)
        if not normalized:
            raise ValueError("no symbols were provided and the portfolio has no holdings")
        if len(normalized) > 50:
            raise ValueError("at most 50 weekly symbols are allowed per request")
        records: list[dict[str, Any]] = []
        for symbol in normalized:
            holding = holdings.get(symbol, {"symbol": symbol, "code": symbol.split(".", 1)[0], "name": symbol})
            records.append(
                self._start_one(
                    week_start=resolved_start,
                    week_end=resolved_end,
                    trading_sessions=sessions,
                    symbol=symbol,
                    holding=holding,
                    portfolio=portfolio,
                    refresh_policy=refresh_policy,
                    report_profile=report_profile,
                    report_audience=report_audience,
                    force_new=force_new,
                    single_source_authorized=single_source_authorized,
                    trigger=trigger,
                )
            )
        return records

    def _start_one(
        self,
        *,
        week_start: str,
        week_end: str,
        trading_sessions: list[str],
        symbol: str,
        holding: dict[str, Any],
        portfolio: dict[str, Any],
        refresh_policy: str,
        report_profile: str,
        report_audience: str,
        force_new: bool,
        single_source_authorized: bool,
        trigger: str,
    ) -> dict[str, Any]:
        run_key = _stable_id(
            "weekly_key", week_end, symbol, report_audience, report_profile
        )
        if not force_new and (existing := self.store.find_reusable(run_key)) is not None:
            existing["deduplicated_requests"] = int(existing.get("deduplicated_requests") or 0) + 1
            saved = self.store.save(existing)
            return {**saved, "deduplicated": True}
        revision = self.store.next_revision_for_key(run_key)
        run_id = f"wrr_{week_end.replace('-', '')}_{symbol.replace('.', '_')}_r{revision}_{uuid.uuid4().hex[:8]}"
        report_id = _stable_id("weekly", run_key, revision)
        record = self.store.create(
            {
                "schema_version": 2,
                "run_id": run_id,
                "report_id": report_id,
                "run_key": run_key,
                "idempotency_key": _stable_id("weekly_revision", run_key, revision),
                "revision": revision,
                "artifact_revision": revision,
                "week_start": week_start,
                "week_end": week_end,
                "trading_sessions": trading_sessions,
                "symbol": symbol,
                "security_name": str(holding.get("name") or symbol),
                "instrument_type": _instrument_type(symbol),
                "report_profile": report_profile,
                "report_audience": report_audience,
                "refresh_policy": refresh_policy,
                "single_source_authorized": bool(single_source_authorized),
                "trigger": trigger,
                "status": "queued",
                "stage": "queued",
                "progress": {"completed": 0, "total": 1, "percent": 0},
                "catalog_status": "pending",
                "model_calls": 0,
                "model_cost": 0,
                "trade_execution": "forbidden",
                "delivery_status": "not_requested",
                "monitor_activation_status": "not_requested",
                "warnings": [],
                "error": None,
            }
        )
        cancel_event = asyncio.Event()
        self._cancel[run_id] = cancel_event
        self._tasks[run_id] = asyncio.create_task(
            self._execute(
                run_id,
                portfolio=portfolio,
                holding=holding,
                cancel_event=cancel_event,
            ),
            name=f"weekly-report-{symbol}-{week_end}",
        )
        self._tasks[run_id].add_done_callback(lambda _: self._tasks.pop(run_id, None))
        return record

    async def wait(self, run_id: str) -> dict[str, Any]:
        if task := self._tasks.get(run_id):
            await task
        record = self.store.get(run_id)
        if record is None:
            raise KeyError(run_id)
        return record

    async def cancel(self, run_id: str) -> dict[str, Any]:
        record = self.store.get(run_id)
        if record is None:
            raise KeyError(run_id)
        if record.get("status") in TERMINAL_STATUSES:
            return record
        self._cancel.setdefault(run_id, asyncio.Event()).set()
        record.update(status="cancelling", stage="cancelling")
        return self.store.save(record)

    async def retry(self, run_id: str) -> dict[str, Any]:
        previous = self.store.get(run_id)
        if previous is None:
            raise KeyError(run_id)
        if previous.get("status") not in TERMINAL_STATUSES:
            raise ValueError("only terminal weekly runs can be retried")
        records = await self.start(
            week_end=str(previous.get("week_end") or ""),
            symbols=[str(previous.get("symbol") or "")],
            refresh_policy=str(previous.get("refresh_policy") or "ensure_fresh"),
            report_profile=str(previous.get("report_profile") or "weekly_review_v2"),
            report_audience=str(previous.get("report_audience") or "user"),
            force_new=True,
            single_source_authorized=bool(previous.get("single_source_authorized")),
            trigger="retry",
        )
        record = records[0]
        record["parent_run_id"] = run_id
        return self.store.save(record)

    async def _execute(
        self,
        run_id: str,
        *,
        portfolio: dict[str, Any],
        holding: dict[str, Any],
        cancel_event: asyncio.Event,
    ) -> None:
        record = self.store.get(run_id) or {}
        try:
            record.update(status="running", stage="freezing_inputs", started_at=_now_local())
            record = self.store.save(record)
            self.store.write_json(run_id, "inputs/portfolio_snapshot.json", portfolio)
            self.store.write_json(run_id, "inputs/holding_snapshot.json", holding)
            if cancel_event.is_set():
                raise asyncio.CancelledError

            record.update(stage="preparing_data")
            record = self.store.save(record)
            prepared = await asyncio.to_thread(self._prepare_data, record, holding)
            self.store.write_json(run_id, "inputs/data_manifest.json", prepared["manifest"])
            try:
                from src.research import get_source_ingestion_service, knowledge_enabled

                if knowledge_enabled():
                    get_source_ingestion_service().ingest_data_manifest(
                        prepared["manifest"],
                        origin_type="weekly_run",
                        origin_id=run_id,
                    )
            except Exception:
                # The frozen manifest is authoritative and can be replayed later.
                pass
            self.store.write_json(run_id, "inputs/daily_bars.json", prepared["bars"])
            gate = prepared["analysis_gate"]
            record.update(
                analysis_gate=gate,
                data_status=gate["market_data_status"],
                data_batch_id=prepared["manifest"].get("refresh_run_id"),
            )
            record = self.store.save(record)
            if cancel_event.is_set():
                raise asyncio.CancelledError
            if gate["decision"] != "proceed":
                limited = {
                    "schema_version": 1,
                    "run_id": run_id,
                    "report_id": record.get("report_id"),
                    "symbol": record.get("symbol"),
                    "week_start": record.get("week_start"),
                    "week_end": record.get("week_end"),
                    "quality_status": "failed_validation",
                    "coverage_status": "insufficient",
                    "analysis_gate": gate,
                    "data_gaps": gate.get("missing_scopes") or [],
                    "model_calls": 0,
                    "trade_execution": "forbidden",
                    "generated_at": _now_local(),
                }
                self.store.write_json(run_id, "outputs/data_limited.json", limited)
                record.update(
                    status="completed_with_warnings",
                    stage="skipped_data_unavailable",
                    quality_status="failed_validation",
                    coverage_status="insufficient",
                    warnings=[
                        "日线核心数据未通过确定性门控，已在任何模型调用和正式 Artifact 前停止。"
                    ],
                    artifacts=[],
                    catalog_status="not_registered_diagnostic",
                    progress={"completed": 1, "total": 1, "percent": 100},
                    completed_at=_now_local(),
                )
                self.store.save(record)
                return

            record.update(stage="building_structured_review")
            record = self.store.save(record)
            previous_record = self.store.previous_success(
                symbol=str(record["symbol"]),
                before_week_end=str(record["week_end"]),
                excluding_run_id=run_id,
            )
            previous_brief = (
                self.store.read_json(str(previous_record["run_id"]), "outputs/weekly_review.json")
                if previous_record
                else None
            )
            brief = self._build_brief(
                record=record,
                holding=holding,
                prepared=prepared,
                previous_record=previous_record,
                previous_brief=previous_brief if isinstance(previous_brief, dict) else None,
            )
            self.store.write_json(
                run_id,
                "inputs/analysis_method_snapshot.json",
                brief["analysis_method_snapshot"],
            )
            agent_analysis = unavailable_agent_analysis(
                "周报 Agent 分析未启用；保留确定性方法结果。"
            )
            model_calls = 0
            analysis_session_id: str | None = None
            if self.agent_analysis_enabled():
                record.update(stage="agent_method_analysis")
                record = self.store.save(record)
                model_calls = 1
                try:
                    agent_analysis, analysis_session_id = await self._run_agent_analysis(
                        record=record,
                        brief=brief,
                    )
                except Exception as exc:  # keep deterministic report available
                    agent_analysis = unavailable_agent_analysis(
                        f"Agent 分析失败，已保留确定性结果：{type(exc).__name__}: {exc}"
                    )
                    agent_gap = make_gap_detail(
                        "agent_method_analysis_unavailable",
                        source="agent_method_analysis",
                        instrument_type=str(brief["instrument_type"]),
                        availability="partial",
                        data_as_of=str(brief["week_end"]),
                    )
                    brief["data_gap_details"] = normalize_gap_details(
                        [*(brief.get("data_gap_details") or []), agent_gap],
                        instrument_type=str(brief["instrument_type"]),
                    )
                    brief["data_gaps"] = gap_codes(brief["data_gap_details"])
                    brief["quality_status"] = "passed_with_gaps"
                    brief["coverage_status"] = "partial"
                    brief["confidence"] = "low"
            brief["agent_analysis"] = agent_analysis
            brief["side_effects"]["model_calls"] = model_calls
            if agent_analysis.get("status") == "completed":
                analysis_claim = _claim(
                    str(brief["report_id"]),
                    "agent_method_synthesis",
                    0,
                    agent_analysis.get("cross_horizon_conclusion"),
                )
                if analysis_claim is not None:
                    analysis_claim["related_claim_ids"] = list(
                        dict.fromkeys(
                            claim_id
                            for level in brief.get("key_levels") or []
                            for claim_id in level.get("claim_ids") or []
                        )
                    )
                    brief["monitoring_claims"].append(analysis_claim)
                    agent_analysis["claim_id"] = analysis_claim["claim_id"]
                critic = agent_analysis.get("critic") or {}
                if critic.get("verdict") != "pass":
                    brief["confidence"] = "low"
                    brief["analysis_notes"] = list(dict.fromkeys([
                        *(brief.get("analysis_notes") or []),
                        *(critic.get("issues") or ["分析智能体反证审查未通过。"]),
                    ]))
            record.update(
                model_calls=model_calls,
                analysis_session_id=analysis_session_id,
                agent_analysis_status=agent_analysis.get("status"),
            )
            record = self.store.save(record)
            brief = validate_weekly_review(brief)
            # The catalog may resolve a verified product name that is more
            # authoritative than the portfolio alias frozen at queue time.
            # Keep the run card/API metadata aligned with its artifacts.
            record["security_name"] = str(
                brief.get("security_name") or record.get("security_name") or record["symbol"]
            )
            self.store.write_json(run_id, "outputs/weekly_review.json", brief)
            if cancel_event.is_set():
                raise asyncio.CancelledError

            record.update(stage="rendering_artifacts")
            record = self.store.save(record)
            artifacts = await self._render_artifacts(record, brief)
            completed_at = _now_local()
            bundle = brief["monitoring_bundle"]
            record.update(
                status=(
                    "completed_with_warnings"
                    if brief["quality_status"] == "passed_with_gaps" or brief.get("data_gaps")
                    else "completed"
                ),
                stage="completed",
                security_name=brief.get("security_name") or record.get("security_name"),
                quality_status=brief["quality_status"],
                coverage_status=brief["coverage_status"],
                artifacts=artifacts,
                warnings=list(brief.get("data_gaps") or []),
                progress={"completed": 1, "total": 1, "percent": 100},
                action_ready_count=sum(
                    1 for item in bundle.get("candidates") or [] if item.get("automation_status") == "action_ready"
                ),
                watch_only_count=sum(
                    1 for item in bundle.get("candidates") or [] if item.get("automation_status") == "watch_only"
                ),
                previous_candidate_count=len((previous_brief or {}).get("monitoring_bundle", {}).get("candidates") or []),
                previous_validation_count=len(brief.get("previous_week_validation") or []),
                scenario_change_count=len(brief.get("scenario_changes") or []),
                valid_from=brief.get("valid_from"),
                valid_until=brief.get("valid_until"),
                review_due_at=brief.get("review_due_at"),
                source_valid_until=brief.get("source_valid_until"),
                completed_at=completed_at,
                catalog_status="pending",
            )
            record = self.store.save(record)
            try:
                from src.reports.catalog import (
                    get_report_library_service,
                    report_library_enabled,
                )

                if report_library_enabled():
                    registered = await asyncio.to_thread(
                        get_report_library_service().register_weekly_run,
                        record,
                        brief,
                    )
                    record.update(
                        catalog_status="registered",
                        catalog_report_id=(registered or {}).get("report_id") or record.get("report_id"),
                    )
                else:
                    record["catalog_status"] = "disabled"
            except Exception as exc:
                record["catalog_status"] = "failed"
                record["status"] = "completed_with_warnings"
                record["warnings"] = list(dict.fromkeys([
                    *(record.get("warnings") or []),
                    f"报告目录登记失败，可幂等修复：{type(exc).__name__}: {exc}",
                ]))
            self.store.save(record)
        except asyncio.CancelledError:
            record = self.store.get(run_id) or record
            record.update(status="cancelled", stage="cancelled", completed_at=_now_local())
            self.store.save(record)
        except Exception as exc:  # persisted for retry and operational metrics
            record = self.store.get(run_id) or record
            record.update(
                status="failed",
                failure_stage=record.get("stage"),
                stage="failed",
                error=f"{type(exc).__name__}: {exc}",
                completed_at=_now_local(),
            )
            self.store.save(record)
        finally:
            self._cancel.pop(run_id, None)

    async def _run_agent_analysis(
        self, *, record: dict[str, Any], brief: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        """Run one research-only method selection pass on frozen inputs."""

        if self.session_service is None:
            raise RuntimeError("Session service is unavailable")
        prompt = build_agent_method_prompt(
            symbol=str(brief["symbol"]),
            horizon="weekly",
            snapshot=dict(brief["analysis_method_snapshot"]),
            cross_horizon_context=dict(brief.get("cross_horizon_context") or {}),
            instrument_context=(
                dict(brief.get("etf_context") or {})
                if brief.get("instrument_type") == "etf"
                else {}
            ),
            allowed_data_gap_codes=list(brief.get("data_gaps") or []),
        )
        session = self.session_service.create_session(
            title=f"WeeklyMethod {record['run_id']} {brief['symbol']}",
            config={
                "internal": True,
                "portfolio_weekly_run": {
                    "research_only": True,
                    "run_id": record["run_id"],
                    "symbol": brief["symbol"],
                },
                "include_shell_tools": False,
            },
        )
        await self.session_service.execute_message(
            session.session_id,
            prompt,
            include_shell_tools=False,
            message_metadata={
                "weekly_run_id": record["run_id"],
                "weekly_run_symbol": brief["symbol"],
            },
        )
        messages = self.session_service.get_messages(session.session_id, limit=20)
        reply = next(
            (
                item
                for item in reversed(messages)
                if (
                    getattr(item, "role", None)
                    or (item.get("role") if isinstance(item, dict) else None)
                )
                == "assistant"
            ),
            None,
        )
        if reply is None:
            raise RuntimeError("weekly method Agent produced no assistant response")
        content = (
            getattr(reply, "content", None)
            if not isinstance(reply, dict)
            else reply.get("content")
        )
        return (
            validate_agent_method_analysis(
                str(content or ""),
                snapshot=dict(brief["analysis_method_snapshot"]),
                allowed_data_gap_codes=set(brief.get("data_gaps") or []),
            ),
            str(session.session_id),
        )

    def _prepare_data(
        self, record: dict[str, Any], holding: dict[str, Any]
    ) -> dict[str, Any]:
        symbol = str(record["symbol"])
        week_start, week_end = str(record["week_start"]), str(record["week_end"])
        start = (date.fromisoformat(week_end) - timedelta(days=650)).isoformat()
        market_service = self.data_service.market_service
        refresh_policy = str(record.get("refresh_policy") or "ensure_fresh")
        refresh_attempted = refresh_policy != "reuse"
        refresh_run: dict[str, Any] | None = None
        if refresh_attempted:
            refresh_run = market_service.refresh_sync(
                symbols=[symbol],
                profile="weekly_report",
                force=refresh_policy == "force",
                start_date=start,
                end_date=week_end,
                items=[("1D", "raw")],
            )
        bars = normalize_daily_bars(
            market_service.store.query_bars(
                symbol=symbol,
                interval="1D",
                adjustment="raw",
                start=start,
                limit=2500,
            ),
            through=week_end,
        )
        expected_sessions = set(record.get("trading_sessions") or [])
        current, previous = split_week_bars(bars, week_start=week_start, week_end=week_end)
        current_days = {item["date"] for item in current}
        if (
            (len(bars) < 20 or not expected_sessions.issubset(current_days))
            and not refresh_attempted
        ):
            refresh_attempted = True
            refresh_run = market_service.refresh_sync(
                symbols=[symbol],
                profile="weekly_report_repair",
                force=True,
                start_date=start,
                end_date=week_end,
                items=[("1D", "raw")],
            )
            bars = normalize_daily_bars(
                market_service.store.query_bars(
                    symbol=symbol,
                    interval="1D",
                    adjustment="raw",
                    start=start,
                    limit=2500,
                ),
                through=week_end,
            )
            current, previous = split_week_bars(bars, week_start=week_start, week_end=week_end)
            current_days = {item["date"] for item in current}

        source_counts = [int(item.get("source_count") or 0) for item in current]
        statuses = {str(item.get("status") or "unresolved") for item in current}
        sources = sorted({str(source) for item in current for source in item.get("sources") or []})
        if not current:
            market_status = "unavailable"
        elif statuses.intersection({"conflict", "unresolved", "unresolved_conflict"}):
            market_status = "conflicted"
        elif source_counts and min(source_counts) < 2:
            market_status = "single_source"
        else:
            market_status = "verified"
        current_complete = bool(expected_sessions) and expected_sessions.issubset(current_days)
        missing_scopes: list[str] = []
        if len(bars) < 20:
            missing_scopes.append("daily_history_lt_20")
        if not current_complete:
            missing_scopes.append("current_week_incomplete")
        if not previous:
            missing_scopes.append("previous_week_unavailable")
        if market_status == "conflicted":
            missing_scopes.append("market_data_conflict")
        decision = "proceed" if len(bars) >= 20 and current_complete else "skip_report"
        instrument_type = infer_portfolio_instrument_type(
            symbol,
            explicit=str(record.get("instrument_type") or ""),
        )
        report_context = self._report_context(
            symbol,
            week_end=week_end,
            instrument_type=instrument_type,
        )
        index_bars: list[dict[str, Any]] = []
        index_refresh_run: dict[str, Any] | None = None
        tracked_index_code = None
        if instrument_type == "etf":
            tracked_index_code = tracked_index_code_from_context(report_context)
            if tracked_index_code:
                if refresh_attempted:
                    index_refresh_run = market_service.refresh_sync(
                        symbols=[tracked_index_code],
                        profile="weekly_report_tracking_index",
                        force=refresh_policy == "force",
                        start_date=start,
                        end_date=week_end,
                        items=[("1D", "raw")],
                    )
                index_bars = normalize_daily_bars(
                    market_service.store.query_bars(
                        symbol=tracked_index_code,
                        interval="1D",
                        adjustment="raw",
                        start=start,
                        limit=2500,
                    ),
                    through=week_end,
                )
                if (
                    (len(index_bars) < 21 or week_end not in {item["date"] for item in index_bars})
                    and not refresh_attempted
                ):
                    index_refresh_run = market_service.refresh_sync(
                        symbols=[tracked_index_code],
                        profile="weekly_report_tracking_index_repair",
                        force=True,
                        start_date=start,
                        end_date=week_end,
                        items=[("1D", "raw")],
                    )
                    index_bars = normalize_daily_bars(
                        market_service.store.query_bars(
                            symbol=tracked_index_code,
                            interval="1D",
                            adjustment="raw",
                            start=start,
                            limit=2500,
                        ),
                        through=week_end,
                    )
                tracking_snapshot = build_etf_tracking_metrics(
                    etf_symbol=symbol,
                    tracked_index_code=tracked_index_code,
                    etf_bars=bars,
                    index_bars=index_bars,
                    week_start=week_start,
                    week_end=week_end,
                )
                report_context = enrich_weekly_context_with_etf_metrics(
                    report_context,
                    tracking_snapshot,
                )
            missing_scopes.extend(
                str(item.get("reason_code"))
                for item in report_context.get("data_gap_details") or []
                if str(item.get("scope") or "")
                in {
                    "product_profile",
                    "tracking_index",
                    "index_relative_strength",
                    "fund_shares",
                    "premium_discount",
                    "official_tracking_quality",
                    "market_tracking_deviation",
                    "component_exposure",
                    "component_research",
                }
                and str(item.get("impact") or "") != "disclosure_only"
            )
        gate = {
            "decision": decision,
            "market_data_status": market_status,
            "daily_bar_count": len(bars),
            "current_week_complete": current_complete,
            "previous_week_available": bool(previous),
            "previous_weekly_report_available": bool(
                (report_context.get("current_reports") or {}).get("weekly")
            ),
            "refresh_attempted": refresh_attempted,
            "refresh_succeeded": bool(current_complete and len(bars) >= 20),
            "single_source_authorized": bool(record.get("single_source_authorized")),
            "short_week": len(expected_sessions) <= 2,
            "missing_scopes": list(dict.fromkeys(missing_scopes)),
        }
        return {
            "bars": bars,
            "analysis_gate": gate,
            "report_context": report_context,
            "manifest": {
                "schema_version": 1,
                "run_id": record["run_id"],
                "symbol": symbol,
                "week_start": week_start,
                "week_end": week_end,
                "interval": "1D",
                "adjustment": "raw",
                "bar_count": len(bars),
                "current_week_bar_count": len(current),
                "previous_week_bar_count": len(previous),
                "sources": sources,
                "source_count": min(source_counts) if source_counts else 0,
                "statuses": sorted(statuses),
                "refresh_attempted": refresh_attempted,
                "refresh_run_id": (refresh_run or {}).get("run_id"),
                "refresh_status": (refresh_run or {}).get("status"),
                "tracked_index_code": tracked_index_code,
                "tracking_index_bar_count": len(index_bars),
                "tracking_index_refresh_run_id": (index_refresh_run or {}).get("run_id"),
                "tracking_index_refresh_status": (index_refresh_run or {}).get("status"),
                "created_at": _now_local(),
            },
        }

    @staticmethod
    def _report_context(
        symbol: str,
        *,
        week_end: str | None = None,
        instrument_type: str | None = None,
    ) -> dict[str, Any]:
        return WeeklyContextAssembler().assemble(
            symbol,
            week_end=week_end,
            instrument_type=instrument_type,
        )

    def _build_brief(
        self,
        *,
        record: dict[str, Any],
        holding: dict[str, Any],
        prepared: dict[str, Any],
        previous_record: dict[str, Any] | None,
        previous_brief: dict[str, Any] | None,
    ) -> dict[str, Any]:
        symbol = str(record["symbol"])
        report_id = str(record["report_id"])
        tick = _tick_size(symbol)
        stats = weekly_market_statistics(
            prepared["bars"],
            week_start=str(record["week_start"]),
            week_end=str(record["week_end"]),
            tick_size=tick,
        )
        view = deterministic_weekly_view(stats)
        instrument_type = _instrument_type(symbol)
        report_context = dict(prepared.get("report_context") or {})
        relative_metrics = (
            (report_context.get("tracking_metrics") or {})
            .get("index_relative_strength", {})
            .get("metrics", {})
        )
        relative_gap = _number(relative_metrics.get("fund_index_return_gap_1w"))
        if instrument_type == "etf" and relative_gap is not None:
            view["relative_strength"] = (
                f"较跟踪指数{'强' if relative_gap > 0 else '弱' if relative_gap < 0 else '持平'}"
                f"（周收益差 {relative_gap * 100:+.2f} 个百分点）"
            )
            view["relative_strength_metrics"] = dict(relative_metrics)
        method_snapshot = build_market_analysis_snapshot(
            prepared["bars"],
            through=str(record["week_end"]),
            symbol=symbol,
            instrument_type=instrument_type,
            adjustment="raw",
            tick_size=tick,
        )
        generated_at = self.now_provider().astimezone(_SHANGHAI).isoformat()
        data_as_of = datetime.combine(
            date.fromisoformat(str(record["week_end"])), time(15, 0), _SHANGHAI
        ).isoformat()
        review_due_at = next_week_review_due(self.calendar, str(record["week_end"]))
        valid_from = generated_at
        valid_until = review_due_at
        gate = prepared["analysis_gate"]
        gap_details: list[dict[str, Any] | None] = list(
            report_context.get("data_gap_details") or []
        )
        gap_details.extend(
            make_gap_detail(
                str(code),
                source="analysis_gate",
                instrument_type=instrument_type,
                data_as_of=str(record["week_end"]),
            )
            for code in gate.get("missing_scopes") or []
        )
        if gate.get("market_data_status") == "single_source":
            gap_details.append(make_gap_detail(
                "market_data_single_source_unapproved",
                source="analysis_gate",
                instrument_type=instrument_type,
                availability="partial",
                data_as_of=str(record["week_end"]),
            ))
        method_gaps = list(method_snapshot.get("data_gaps") or [])
        if method_gaps:
            gap_details.append(make_gap_detail(
                "analysis_method_scope_incomplete",
                source="analysis_method_snapshot",
                instrument_type=instrument_type,
                availability="partial",
                missing_items=method_gaps,
                data_as_of=str(record["week_end"]),
            ))
        if gate.get("short_week"):
            gap_details.append(make_gap_detail(
                "short_trading_week",
                source="analysis_gate",
                instrument_type=instrument_type,
                availability="partial",
                data_as_of=str(record["week_end"]),
            ))
        normalized_gap_details = normalize_gap_details(
            gap_details,
            instrument_type=instrument_type,
        )
        data_gaps = gap_codes(normalized_gap_details)
        quality_status = (
            "passed"
            if not quality_affecting_gaps(normalized_gap_details)
            and len(prepared["bars"]) >= 60
            else "passed_with_gaps"
        )
        coverage_status = "complete" if quality_status == "passed" else "partial"

        claims: list[dict[str, Any]] = []

        def add_claim(section: str, text: Any, *, claim_type: str = "inference") -> str:
            item = _claim(report_id, section, len(claims), text, claim_type=claim_type)
            if item is None:
                raise ValueError(f"weekly claim {section} is empty")
            claims.append(item)
            return str(item["claim_id"])

        summary = view["summary"]
        summary_claim = add_claim("weekly_summary", summary)
        reason_texts = [
            f"本周开高低收为 {stats['open']} / {stats['high']} / {stats['low']} / {stats['close']}。",
            f"本周涨跌 {stats['week_return_pct']:+.2f}%，振幅 {stats['max_amplitude_pct']:.2f}%。",
            f"本周成交量较上周比值为 {stats.get('volume_ratio_vs_previous_week')}。",
        ]
        reason_claims = [add_claim("weekly_reason", text, claim_type="calculation") for text in reason_texts]
        safety_notes = ["任何行动前必须人工复核；本报告不会自动激活监控或执行交易。"]
        analysis_notes: list[str] = []
        risk_texts = [
            *safety_notes,
            *(DATA_GAP_LABELS[code] for code in data_gaps),
        ]
        risk_claims = [add_claim("weekly_risk", text, claim_type="data_gap" if index else "opinion") for index, text in enumerate(risk_texts)]

        primary_levels = method_snapshot.get("primary_levels") or {}
        primary_support = dict(primary_levels.get("support") or {})
        primary_resistance = dict(primary_levels.get("resistance") or {})
        if not primary_support:
            primary_support = {
                "lower": stats["support"],
                "upper": stats["support_upper"],
                "representative_value": stats["support"],
                "confidence": "low",
                "method_ids": ["multi_horizon_structure"],
                "calculation_basis": {
                    "method": "rolling_20_day_low_zone_fallback",
                    "method_version": method_snapshot["registry_version"],
                    "summary": "多方法候选不足，暂以滚动区间下沿作为低置信度基准。",
                    "references": [],
                },
            }
        if not primary_resistance:
            primary_resistance = {
                "lower": stats["resistance_lower"],
                "upper": stats["resistance"],
                "representative_value": stats["resistance"],
                "confidence": "low",
                "method_ids": ["multi_horizon_structure"],
                "calculation_basis": {
                    "method": "rolling_20_day_high_zone_fallback",
                    "method_version": method_snapshot["registry_version"],
                    "summary": "多方法候选不足，暂以滚动区间上沿作为低置信度基准。",
                    "references": [],
                },
            }
        support_basis = dict(primary_support.get("calculation_basis") or {})
        resistance_basis = dict(primary_resistance.get("calculation_basis") or {})
        level_specs = [
            (
                "weekly-support-primary",
                "support",
                "zone",
                float(primary_support["lower"]),
                float(primary_support["upper"]),
                str(support_basis.get("method") or "multi_method_level_evidence"),
                str(support_basis.get("summary") or "多方法结构支撑候选。"),
                primary_support,
            ),
            (
                "weekly-resistance-primary",
                "resistance",
                "zone",
                float(primary_resistance["lower"]),
                float(primary_resistance["upper"]),
                str(resistance_basis.get("method") or "multi_method_level_evidence"),
                str(resistance_basis.get("summary") or "多方法结构阻力候选。"),
                primary_resistance,
            ),
            (
                "weekly-breakout-primary",
                "breakout",
                "price",
                float(primary_resistance["upper"]),
                None,
                str(resistance_basis.get("method") or "multi_method_level_evidence"),
                "突破观察位取主要阻力候选上沿，并保留原始方法证据。",
                primary_resistance,
            ),
            (
                "weekly-breakdown-primary",
                "invalidation",
                "price",
                float(primary_support["lower"]),
                None,
                str(support_basis.get("method") or "multi_method_level_evidence"),
                "趋势失效观察位取主要支撑候选下沿，并保留原始方法证据。",
                primary_support,
            ),
        ]
        key_levels: list[dict[str, Any]] = []
        level_claim_by_role: dict[str, str] = {}
        confidence_strength = {"high": "strong", "medium": "medium", "low": "weak"}
        for ordinal, (role, level_type, kind, lower, upper, method, basis, candidate) in enumerate(level_specs):
            value_text = f"{lower}–{upper}" if kind == "zone" else str(lower)
            claim_id = add_claim(
                "weekly_level",
                f"{level_type} {value_text} CNY（raw）；{basis}",
                claim_type="calculation",
            )
            level_claim_by_role[role] = claim_id
            level: dict[str, Any] = {
                "level_id": _stable_id("level", symbol, role),
                "scenario_family_id": _stable_id("scenario", symbol, role),
                "level_type": level_type,
                "kind": kind,
                "unit": "CNY",
                "adjustment": "raw",
                "strength": confidence_strength.get(
                    str(candidate.get("confidence") or "medium"), "medium"
                ),
                "calculation_basis": {
                    "method": method,
                    "method_label": "多方法结构证据",
                    "method_version": method_snapshot["registry_version"],
                    "formula": basis,
                    "summary": basis,
                    "recommended_value": float(lower),
                    "references": list(
                        (candidate.get("calculation_basis") or {}).get("references")
                        or [
                            {
                                "label": "week_end",
                                "value": float(lower),
                                "date": record["week_end"],
                            },
                            {
                                "label": "atr14",
                                "value": stats["atr14"],
                                "date": record["week_end"],
                            },
                        ]
                    ),
                },
                "claim_ids": [claim_id],
                "method_candidate_id": candidate.get("candidate_id"),
                "method_score": candidate.get("score"),
            }
            if kind == "zone":
                level.update(lower=float(lower), upper=float(upper))
            else:
                level["value"] = float(lower)
            key_levels.append(level)

        actionable = (
            gate.get("market_data_status") == "verified"
            or (
                gate.get("market_data_status") == "single_source"
                and gate.get("single_source_authorized") is True
            )
        ) and gate.get("market_data_status") != "conflicted"
        candidates = self._weekly_candidates(
            record=record,
            key_levels=key_levels,
            level_claim_by_role=level_claim_by_role,
            add_claim=add_claim,
            actionable=actionable,
        )
        previous_bundle = (
            (previous_brief or {}).get("monitoring_bundle")
            if isinstance(previous_brief, dict)
            else None
        )
        scenario_changes = compare_weekly_scenarios(
            candidates,
            previous_bundle if isinstance(previous_bundle, dict) else None,
            current_review_due_at=review_due_at,
        )
        for change in scenario_changes:
            change_claim = add_claim(
                "weekly_change_reason",
                f"{change.get('scenario_family_id')}：{change.get('change_type')}；"
                f"{(change.get('change_details') or {}).get('summary') or ''}",
            )
            change["reason_claim_ids"] = list(dict.fromkeys([
                *(change.get("reason_claim_ids") or []), change_claim
            ]))

        previous_validation = validate_previous_week_scenarios(
            previous_bundle if isinstance(previous_bundle, dict) else None,
            current_week_bars=stats["current_week_bars"],
            all_bars=prepared["bars"],
        )
        for item in previous_validation:
            item["claim_ids"] = [
                add_claim(
                    "weekly_previous_outcome",
                    f"{item.get('scenario_family_id')}：{item.get('outcome')}；{item.get('summary')}",
                    claim_type="calculation",
                )
            ]

        sources = prepared["manifest"].get("sources") or []
        single_source = gate.get("market_data_status") == "single_source"
        warnings = list(data_gaps)
        bundle = {
            "schema_version": 1,
            "symbol": symbol,
            "instrument_type": instrument_type,
            "horizon": "weekly",
            "generated_at": generated_at,
            "data_as_of": data_as_of,
            "valid_from": valid_from,
            "valid_until": valid_until,
            "review_due_at": review_due_at,
            "source_valid_until": review_due_at,
            "expired_reason": None,
            "early_invalidation_conditions": [
                f"日线收盘跌破 {primary_support['lower']} CNY 的主要周度支撑位",
                "行情源发生未解决冲突或核心日线被撤销",
            ],
            "price_basis": {"adjustment": "raw", "currency": "CNY", "tick_size": tick},
            "monitoring_status": "available" if candidates else "not_recommended",
            "price_volume_context": {
                "policy": dict(DEFAULT_PRICE_VOLUME_POLICY),
                "data_mode": "single_source" if single_source else "verified",
                "source_count": int(prepared["manifest"].get("source_count") or 0),
                "sources": sources,
                "single_source_authorized": bool(gate.get("single_source_authorized")),
                "warnings": warnings,
                "refresh_attempted": bool(gate.get("refresh_attempted")),
                "refresh_succeeded": bool(gate.get("refresh_succeeded")),
            },
            "candidates": candidates,
            "scenario_changes": scenario_changes,
            "validation_errors": [],
            "source": "structured_weekly_report",
            "source_report_id": report_id,
            "source_period": {
                "week_start": record["week_start"],
                "week_end": record["week_end"],
                "label": f"{record['week_start']} 至 {record['week_end']}",
            },
            "activation_policy": "manual_confirmation_required",
            "trade_execution": "forbidden",
        }

        return {
            "schema_version": 2,
            "report_kind": "weekly_review",
            "report_audience": str(record.get("report_audience") or "user"),
            "report_id": report_id,
            "run_id": record["run_id"],
            "revision": int(record.get("revision") or 1),
            "symbol": symbol,
            "security_name": str(
                report_context.get("security_name")
                or holding.get("security_name")
                or holding.get("name")
                or holding.get("stock_name")
                or symbol
            ),
            "instrument_type": _instrument_type(symbol),
            "week_start": record["week_start"],
            "week_end": record["week_end"],
            "generated_at": generated_at,
            "data_as_of": data_as_of,
            "valid_from": valid_from,
            "valid_until": valid_until,
            "review_due_at": review_due_at,
            "source_valid_until": review_due_at,
            "quality_status": quality_status,
            "coverage_status": coverage_status,
            "confidence": "medium" if quality_status == "passed" else "low",
            "summary": summary,
            "summary_claim_id": summary_claim,
            "reasons": reason_texts,
            "reason_claim_ids": reason_claims,
            "risks": risk_texts,
            "risk_claim_ids": risk_claims,
            "data_gaps": data_gaps,
            "data_gap_details": copy.deepcopy(normalized_gap_details),
            "analysis_notes": analysis_notes,
            "safety_notes": safety_notes,
            "weekly_view": view,
            "weekly_statistics": {
                key: value
                for key, value in stats.items()
                if key not in {"current_week_bars", "previous_week_bars"}
            },
            "analysis_method_snapshot": method_snapshot,
            "agent_analysis": unavailable_agent_analysis(
                "周报 Agent 分析尚未执行。"
            ),
            "key_levels": key_levels,
            "previous_week_validation": previous_validation,
            "scenario_changes": scenario_changes,
            "previous_weekly_report": (
                {
                    "run_id": previous_record.get("run_id"),
                    "report_id": previous_record.get("report_id"),
                    "week_end": previous_record.get("week_end"),
                    "revision": previous_record.get("revision"),
                }
                if previous_record
                else None
            ),
            "analysis_gate": copy.deepcopy(gate),
            "weekly_context": copy.deepcopy(report_context),
            "data_scopes": copy.deepcopy(report_context.get("scopes") or {}),
            "cross_horizon_context": copy.deepcopy(
                report_context.get("structured_claims") or {}
            ),
            "etf_context": (
                copy.deepcopy(report_context)
                if instrument_type == "etf" else None
            ),
            "context_fingerprint": report_context.get("context_fingerprint"),
            "source_manifest": {
                **copy.deepcopy(prepared["manifest"]),
                "report_context": copy.deepcopy(
                    report_context.get("source_manifest") or {}
                ),
            },
            "monitoring_claims": claims,
            "monitoring_bundle": bundle,
            "side_effects": {
                "model_calls": 0,
                "monitoring_activations": 0,
                "deliveries": 0,
                "trade_executions": 0,
            },
            "trade_execution": "forbidden",
        }

    def _weekly_candidates(
        self,
        *,
        record: dict[str, Any],
        key_levels: list[dict[str, Any]],
        level_claim_by_role: dict[str, str],
        add_claim: Callable[..., str],
        actionable: bool,
    ) -> list[dict[str, Any]]:
        symbol = str(record["symbol"])
        report_id = str(record["report_id"])
        levels_by_type = {
            str(level.get("level_type")): level
            for level in key_levels
            if isinstance(level, dict) and level.get("level_type")
        }
        support_level = levels_by_type["support"]
        resistance_level = levels_by_type["resistance"]
        breakout_level = levels_by_type["breakout"]
        invalidation_level = levels_by_type["invalidation"]
        support_lower = float(support_level["lower"])
        support_upper = float(support_level["upper"])
        resistance_lower = float(resistance_level["lower"])
        breakout_value = float(breakout_level["value"])
        invalidation_value = float(invalidation_level["value"])
        common_interpretation = {
            "price_only": "分钟价格仅用于接近提醒，不能替代日线或周线研究条件。",
            "confirmed": "结构化必需条件全部满足后，仍等待人工复核。",
            "divergence": "价格触发但量价确认不足，维持观察。",
            "invalidated": "价格触及结构化失效位，原场景失效。",
            "insufficient_data": "日线或量价证据不足，仅保留价格提醒。",
            "bullish_case": "向上场景需要真实日线收盘与量价条件共同支持。",
            "bearish_case": "跌破场景只记录风险事实，不触发自动交易。",
        }

        def candidate(
            *,
            role: str,
            label: str,
            intent: str,
            trigger: dict[str, Any],
            original_level: dict[str, Any],
            invalidation: dict[str, Any],
            pending_conditions: list[dict[str, Any]],
            allow_action_ready: bool = False,
        ) -> dict[str, Any]:
            family_id = _stable_id("scenario", symbol, role)
            candidate_id = _stable_id("candidate", report_id, family_id)
            level_claim = level_claim_by_role[role]
            trigger_claim = add_claim(
                "weekly_trigger",
                f"{label} 分钟价格提醒：{json.dumps(trigger, ensure_ascii=False, sort_keys=True)}",
            )
            confirmation_claim = add_claim(
                "weekly_confirmation",
                "；".join(item["source_text"] for item in pending_conditions)
                or "该场景只使用原始价格事实。",
            )
            volume_claim = add_claim(
                "weekly_volume_condition",
                "周报量价只做研究条件；5分钟量能只做 classify_only，不替代日线或周线量能。",
            )
            invalidation_claim = add_claim(
                "weekly_invalidation",
                json.dumps(invalidation, ensure_ascii=False, sort_keys=True),
            )
            action_claim = add_claim(
                "weekly_action",
                "仅生成候选并要求人工确认；不自动激活、不投递、不交易。",
            )
            claim_ids = [
                level_claim,
                trigger_claim,
                confirmation_claim,
                volume_claim,
                invalidation_claim,
                action_claim,
            ]
            price_source_id = _stable_id("condition", family_id, "price", length=16)
            source_conditions = [
                {
                    "condition_id": price_source_id,
                    "source_text": f"原始价格达到 {original_level.get('source_text')}",
                    "role": "required",
                    "coverage_status": "mapped",
                    "reason": "仅映射为分钟级价格接近或穿越提醒。",
                    "evidence_refs": claim_ids[:3],
                },
                *pending_conditions,
            ]
            is_action_ready = actionable and allow_action_ready and not pending_conditions
            entry_condition: dict[str, Any] = {
                "condition_id": _stable_id("condition", candidate_id, "entry", length=16),
                "source_condition_id": price_source_id,
                "kind": "price_zone" if trigger["kind"].startswith("price_zone") else "price_compare",
                "operator": "between" if trigger["kind"].startswith("price_zone") else "gte" if trigger["kind"].endswith("above") else "lte",
                "interval": "1m",
                "consecutive": 1,
                "lookback_bars": 1,
                "freshness_seconds": 900,
                "unit": "CNY",
            }
            if entry_condition["operator"] == "between":
                entry_condition.update(lower=trigger["lower"], upper=trigger["upper"])
            else:
                entry_condition["value"] = trigger["threshold"]
            source_basis = original_level.get("calculation_basis") or {}
            method_references: list[dict[str, Any]] = []
            for reference in list(source_basis.get("references") or [])[:8]:
                if not isinstance(reference, dict):
                    continue
                normalized_reference: dict[str, Any] = {
                    "label": str(
                        reference.get("label")
                        or reference.get("kind")
                        or reference.get("source")
                        or "method_evidence"
                    )[:80]
                }
                reference_value = reference.get("value")
                if reference_value is None:
                    reference_value = reference.get("price")
                if reference_value is not None:
                    normalized_reference["value"] = float(reference_value)
                if reference.get("date"):
                    normalized_reference["date"] = str(reference["date"])[:40]
                method_references.append(normalized_reference)
            if not method_references:
                method_references = [
                    {
                        "label": "week_end_raw_level",
                        "value": float(
                            original_level.get("value")
                            or original_level.get("lower")
                            or trigger.get("threshold")
                            or trigger.get("lower")
                        ),
                        "date": record["week_end"],
                    }
                ]
            basis = {
                "method": str(source_basis.get("method") or "multi_method_level_evidence"),
                "method_label": str(source_basis.get("method_label") or "多方法结构证据"),
                "formula": str(source_basis.get("formula") or "registered deterministic method evidence"),
                "summary": str(source_basis.get("summary") or original_level.get("source_text")),
                "recommended_value": float(
                    original_level.get("value")
                    or original_level.get("lower")
                    or trigger.get("threshold")
                    or trigger.get("lower")
                ),
                "references": method_references,
            }
            return {
                "candidate_id": candidate_id,
                "scenario_id": candidate_id,
                "scenario_family_id": family_id,
                "client_rule_id": _stable_id("weekly_rule", candidate_id),
                "label": label,
                "intent": intent,
                "priority": "high" if intent == "stop_loss" else "normal",
                "evidence_refs": claim_ids,
                "original_level": {
                    key: value
                    for key, value in original_level.items()
                    if key != "calculation_basis"
                },
                "calculation_basis": basis,
                "source_conditions": source_conditions,
                "trigger": trigger,
                "approach_policy": {
                    "distance_bps": 100,
                    "source": "report",
                    "check_interval": "1m",
                },
                "volume_confirmation": {
                    "metric": "same_bucket_5m_volume_ratio",
                    "comparator": "gte",
                    "threshold": 1.5,
                    "min_samples": 5,
                    "unit": "ratio",
                    "mode": "classify_only",
                },
                "entry_conditions": {"operator": "all", "conditions": [entry_condition]},
                "confirmation_conditions": {"operator": "all", "conditions": []},
                "invalidation_conditions": {"operator": "all", "conditions": []},
                "sequence_policy": {
                    "enabled": bool(pending_conditions),
                    "max_wait_bars": 6,
                    "reset_on_invalidation": True,
                },
                "invalidation": invalidation,
                "resolution_policy": {
                    "rejection_hysteresis_bps": 30,
                    "max_observation_bars": 6,
                    "close_action": "unresolved",
                },
                "action_template": {
                    "action": "observe",
                    "sizing": {"kind": "default_policy", "source": "weekly_report"},
                    "confidence_floor": "medium",
                },
                "rationale": basis["summary"],
                "interpretation": copy.deepcopy(common_interpretation),
                "mapping_status": "mapped" if is_action_ready else "partial" if pending_conditions else "mapped",
                "automation_status": "action_ready" if is_action_ready else "watch_only",
                "claim_ids": claim_ids,
                "change_type": "new",
                "previous_candidate_id": None,
                "change_details": {"summary": "本周首次建立。"},
            }

        support_pending = [
            {
                "condition_id": _stable_id("condition", symbol, "weekly_support_daily_close", length=16),
                "source_text": f"日线收盘守住 {support_upper} CNY",
                "role": "required",
                "coverage_status": "awaiting_data",
                "reason": "当前自动引擎不执行日线收盘确认；分钟价格仅作提醒。",
                "evidence_refs": [level_claim_by_role["weekly-support-primary"]],
                "research_condition": {
                    "source_text": f"日线收盘守住 {support_upper} CNY",
                    "kind": "daily_close",
                    "operator": "gte",
                    "interval": "1d",
                    "value": support_upper,
                    "consecutive": 1,
                },
                "executable_mapping": {
                    "coverage_status": "awaiting_data",
                    "reason": "当前实时引擎尚未支持日线收盘确认。",
                },
            }
        ]
        breakout_pending = [
            {
                "condition_id": _stable_id("condition", symbol, "weekly_breakout_daily_close", length=16),
                "source_text": f"日线收盘突破 {breakout_value} CNY",
                "role": "required",
                "coverage_status": "awaiting_data",
                "reason": "当前自动引擎不执行日线收盘确认。",
                "evidence_refs": [level_claim_by_role["weekly-breakout-primary"]],
                "research_condition": {
                    "source_text": f"日线收盘突破 {breakout_value} CNY",
                    "kind": "daily_close",
                    "operator": "gte",
                    "interval": "1d",
                    "value": breakout_value,
                    "consecutive": 1,
                },
                "executable_mapping": {
                    "coverage_status": "awaiting_data",
                    "reason": "当前实时引擎尚未支持日线收盘确认。",
                },
            },
            {
                "condition_id": _stable_id("condition", symbol, "weekly_breakout_daily_volume", length=16),
                "source_text": "当日成交量高于此前5日均量50%",
                "role": "required",
                "coverage_status": "awaiting_data",
                "reason": "当前5分钟同时间桶量比不能替代日线五日均量。",
                "evidence_refs": [level_claim_by_role["weekly-breakout-primary"]],
                "research_condition": {
                    "source_text": "当日成交量高于此前5日均量50%",
                    "kind": "daily_volume_ratio",
                    "operator": "gte",
                    "interval": "1d",
                    "baseline": "previous_5_day_average",
                    "threshold": 1.5,
                    "lookback": 5,
                    "metric": "volume",
                    "unit": "ratio",
                },
                "executable_mapping": {
                    "coverage_status": "awaiting_data",
                    "reason": "当前实时引擎尚未支持日线五日均量条件。",
                },
            },
        ]
        return [
            candidate(
                role="weekly-support-primary",
                label="周度支撑测试",
                intent="watch",
                trigger={
                    "kind": "price_zone_enter",
                    "lower": support_lower,
                    "upper": support_upper,
                    "interval": "1m",
                    "confirmation_count": 1,
                },
                original_level={
                    "kind": "zone",
                    "lower": support_lower,
                    "upper": support_upper,
                    "unit": "CNY",
                    "adjustment": "raw",
                    "source_text": f"周度支撑 {support_lower}–{support_upper} CNY",
                    "calculation_basis": support_level["calculation_basis"],
                },
                invalidation={"kind": "price_cross_below", "level": support_lower},
                pending_conditions=support_pending,
            ),
            candidate(
                role="weekly-breakout-primary",
                label="周度阻力突破",
                intent="breakout",
                trigger={
                    "kind": "price_cross_above",
                    "threshold": breakout_value,
                    "interval": "1m",
                    "confirmation_count": 1,
                },
                original_level={
                    "kind": "price",
                    "value": breakout_value,
                    "unit": "CNY",
                    "adjustment": "raw",
                    "source_text": f"周度突破位 {breakout_value} CNY",
                    "calculation_basis": breakout_level["calculation_basis"],
                },
                invalidation={"kind": "price_cross_below", "level": resistance_lower},
                pending_conditions=breakout_pending,
            ),
            candidate(
                role="weekly-breakdown-primary",
                label="周度趋势失效",
                intent="stop_loss",
                trigger={
                    "kind": "price_cross_below",
                    "threshold": invalidation_value,
                    "interval": "1m",
                    "confirmation_count": 1,
                },
                original_level={
                    "kind": "price",
                    "value": invalidation_value,
                    "unit": "CNY",
                    "adjustment": "raw",
                    "source_text": f"周度失效位 {invalidation_value} CNY",
                    "calculation_basis": invalidation_level["calculation_basis"],
                },
                invalidation={"kind": "price_cross_above", "level": support_upper},
                pending_conditions=[],
                allow_action_ready=True,
            ),
        ]

    async def _render_artifacts(
        self, record: dict[str, Any], brief: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if self.pdf_renderer is None:
            raise RuntimeError("PDF renderer is not configured")
        run_id = str(record["run_id"])
        week_end = str(record["week_end"])
        symbol = str(record["symbol"])
        security_name = str(brief.get("security_name") or symbol)
        stem = (
            f"{week_end}_{_safe_part(symbol, symbol.replace('.', '_'))}_"
            f"{_safe_part(security_name, symbol.replace('.', '_'))}_周度复盘"
        )
        revision = int(record.get("revision") or 1)
        markdown = render_weekly_markdown(brief)
        json_payload = json.dumps(
            brief, ensure_ascii=False, indent=2, sort_keys=True, default=str
        ).encode("utf-8")
        artifacts = [
            self.store.write_artifact(
                run_id,
                kind="weekly_review_json",
                filename=f"{stem}.json",
                payload=json_payload,
                symbol=symbol,
                security_name=security_name,
                media_type="application/json",
                revision=revision,
            ),
            self.store.write_artifact(
                run_id,
                kind="weekly_review_markdown",
                filename=f"{stem}.md",
                payload=markdown.encode("utf-8"),
                symbol=symbol,
                security_name=security_name,
                media_type="text/markdown",
                revision=revision,
            ),
        ]
        pdf = await asyncio.to_thread(
            self.pdf_renderer,
            f"{week_end} {security_name}（{symbol}）周度复盘",
            re.sub(r"^#\s+[^\n]+\n?", "", markdown, count=1),
        )
        if not pdf.startswith(b"%PDF-"):
            raise RuntimeError("weekly PDF renderer returned an invalid PDF")
        artifacts.append(
            self.store.write_artifact(
                run_id,
                kind="weekly_review_pdf",
                filename=f"{stem}.pdf",
                payload=pdf,
                symbol=symbol,
                security_name=security_name,
                media_type="application/pdf",
                revision=revision,
            )
        )
        self.store.write_json(
            run_id,
            "artifacts/manifest.json",
            {
                "schema_version": 1,
                "run_id": run_id,
                "report_id": brief.get("report_id"),
                "revision": revision,
                "artifacts": [
                    {key: value for key, value in item.items() if key != "path"}
                    for item in artifacts
                ],
            },
        )
        return artifacts
