"""One-click, idempotent DailyPortfolioRun orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.channels.research_sessions import resolve_premarket_target_date
from src.data_layer import get_unified_data_service
from src.portfolio.daily.contracts import BriefContractError, fallback_brief, parse_holding_brief
from src.portfolio.daily.reporting import aggregate_portfolio, render_holding_markdown, render_master_markdown
from src.portfolio.daily.store import DailyRunStore, TERMINAL_STATUSES
from src.portfolio.mandate import ensure_assignments, load_mandate, suggest_classifications
from src.portfolio.state import load_state, normalize_symbol


_MIN_REPORT_COVERAGE_RATIO = 0.5


def _now_local() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_symbol(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", symbol)[:40] or "holding"


def _retention_days() -> int:
    try:
        return max(
            1,
            int(os.getenv("VIBE_TRADING_PORTFOLIO_REPORT_RETENTION_DAYS", "90")),
        )
    except ValueError:
        return 90


def _data_status(contexts: list[dict[str, Any]]) -> str:
    statuses = {str(item.get("status") or "offline") for item in contexts}
    series = [
        item
        for context in contexts
        for item in ((context.get("market") or {}).get("series") or [])
    ]
    if not series:
        return "offline" if statuses == {"offline"} else "limited"
    actionabilities = [
        str(item.get("actionability") or "analysis_only") for item in series
    ]
    if not any(item == "price_actionable" for item in actionabilities):
        return "limited"
    if any(item != "price_actionable" for item in actionabilities):
        return "partial"
    if statuses & {"offline", "partial", "limited", "stale_cache"}:
        return "partial"
    return "ok"


def _context_symbol(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    raw = str(value.get("symbol") or value.get("code") or "").strip()
    if not raw:
        return ""
    try:
        return normalize_symbol(raw).upper()
    except Exception:
        return raw.upper()


def _analysis_gate(
    contexts: list[dict[str, Any]], symbols: list[str]
) -> dict[str, Any]:
    """Decide whether enough frozen data exists to justify model/report work.

    A holding is eligible only when its context contains both a market payload
    and at least one research document.  If fewer than half of the holdings are
    eligible, the whole report is stopped before any model Session is created.
    """

    normalized = []
    for raw in symbols:
        symbol = normalize_symbol(raw).upper()
        if symbol and symbol not in normalized:
            normalized.append(symbol)

    market_symbols: set[str] = set()
    research_symbols: set[str] = set()
    for context in contexts:
        market = context.get("market") if isinstance(context, dict) else None
        if isinstance(market, dict):
            for series in market.get("series") or []:
                symbol = _context_symbol(series)
                if symbol and (
                    series.get("latest") is not None
                    or bool(series.get("bars"))
                    or int(series.get("bar_count") or 0) > 0
                ):
                    market_symbols.add(symbol)
            for quote in market.get("quotes") or []:
                symbol = _context_symbol(quote)
                if symbol and quote.get("last_price") is not None:
                    market_symbols.add(symbol)

        research = context.get("research") if isinstance(context, dict) else None
        if not isinstance(research, dict):
            continue
        for domain in research.values():
            if not isinstance(domain, dict):
                continue
            items = domain.get("items")
            if not isinstance(items, dict):
                continue
            for raw_symbol, item in items.items():
                if not isinstance(item, dict) or not item.get("documents"):
                    continue
                try:
                    research_symbols.add(normalize_symbol(str(raw_symbol)).upper())
                except Exception:
                    research_symbols.add(str(raw_symbol).upper())

    eligible = [
        symbol
        for symbol in normalized
        if symbol in market_symbols and symbol in research_symbols
    ]
    total = len(normalized)
    ratio = len(eligible) / total if total else 0.0
    decision = "proceed" if total and ratio >= _MIN_REPORT_COVERAGE_RATIO else "skip_report"
    return {
        "decision": decision,
        "minimum_coverage_ratio": _MIN_REPORT_COVERAGE_RATIO,
        "coverage_ratio": round(ratio, 4),
        "eligible_count": len(eligible),
        "total_count": total,
        "eligible_symbols": eligible,
        "missing_symbols": [symbol for symbol in normalized if symbol not in eligible],
        "missing_market_symbols": [symbol for symbol in normalized if symbol not in market_symbols],
        "missing_research_symbols": [symbol for symbol in normalized if symbol not in research_symbols],
        "model_sessions_started": 0,
    }


def _contexts_for_symbol(
    contexts: list[dict[str, Any]], symbol: str
) -> list[dict[str, Any]]:
    """Project a multi-holding context down to one symbol to avoid token waste."""

    projected: list[dict[str, Any]] = []
    for context in contexts:
        item = {
            key: value
            for key, value in context.items()
            if key not in {"symbols", "market", "research"}
        }
        item["symbols"] = [symbol]

        market = context.get("market")
        if isinstance(market, dict):
            market_item = {
                key: value
                for key, value in market.items()
                if key not in {"series", "bars_handles", "quotes", "runs"}
            }
            market_item["series"] = [
                value for value in market.get("series") or [] if _context_symbol(value) == symbol
            ]
            market_item["bars_handles"] = [
                value
                for value in market.get("bars_handles") or []
                if _context_symbol(value) == symbol
            ]
            market_item["quotes"] = [
                value for value in market.get("quotes") or [] if _context_symbol(value) == symbol
            ]
            item["market"] = market_item
        else:
            item["market"] = market

        research = context.get("research")
        if isinstance(research, dict):
            research_item: dict[str, Any] = {}
            for domain_name, domain in research.items():
                if not isinstance(domain, dict):
                    research_item[domain_name] = domain
                    continue
                domain_item = {key: value for key, value in domain.items() if key != "items"}
                items = domain.get("items")
                domain_item["items"] = (
                    {symbol: items[symbol]} if isinstance(items, dict) and symbol in items else {}
                )
                research_item[domain_name] = domain_item
            item["research"] = research_item
        else:
            item["research"] = research
        projected.append(item)
    return projected


def _latest_timestamp(value: Any) -> str | None:
    candidates: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {
                    "as_of",
                    "retrieved_at",
                    "verified_at",
                    "updated_at",
                    "published_at",
                    "timestamp",
                } and child:
                    candidates.append(str(child))
                elif isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return max(candidates, default=None)


def _manifest_symbol_entries(
    contexts: list[dict[str, Any]], symbols: list[str]
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for symbol in symbols:
        projected = _contexts_for_symbol(contexts, symbol)
        domains: dict[str, dict[str, Any]] = {}

        def merge_domain(name: str, candidate: dict[str, Any]) -> None:
            existing = domains.get(name)
            if existing is None:
                domains[name] = candidate
                return
            if existing.get("status") == "available" and candidate.get("status") != "available":
                return
            if candidate.get("status") == "available" and existing.get("status") != "available":
                domains[name] = candidate
                return
            if str(candidate.get("as_of") or "") >= str(existing.get("as_of") or ""):
                domains[name] = candidate

        for context in projected:
            market = context.get("market")
            if isinstance(market, dict):
                present = any(
                    bool(market.get(key))
                    for key in ("series", "quotes", "bars_handles")
                )
                merge_domain("market", {
                    "status": "available" if present else "missing",
                    "as_of": _latest_timestamp(market)
                    or str(context.get("retrieved_at") or "")
                    or None,
                    "source": market.get("source") or market.get("actual_source"),
                    "cache_status": market.get("cache_status") or market.get("status"),
                    "conflict_status": market.get("conflict_status"),
                    "error": market.get("error"),
                })
            research = context.get("research")
            if isinstance(research, dict):
                for name, payload in research.items():
                    item = None
                    if isinstance(payload, dict):
                        items = payload.get("items")
                        if isinstance(items, dict):
                            item = items.get(symbol)
                    documents = (
                        list(item.get("documents") or [])
                        if isinstance(item, dict)
                        else []
                    )
                    merge_domain(str(name), {
                        "status": "available" if documents else "missing",
                        "as_of": _latest_timestamp(payload)
                        or str(context.get("retrieved_at") or "")
                        or None,
                        "source": (
                            item.get("source") or item.get("mode")
                            if isinstance(item, dict)
                            else None
                        ),
                        "cache_status": (
                            item.get("cache_status") or item.get("mode")
                            if isinstance(item, dict)
                            else payload.get("status")
                            if isinstance(payload, dict)
                            else None
                        ),
                        "conflict_status": (
                            item.get("conflict_status")
                            if isinstance(item, dict)
                            else None
                        ),
                        "error": (
                            item.get("error")
                            if isinstance(item, dict)
                            else payload.get("error")
                            if isinstance(payload, dict)
                            else None
                        ),
                    })
        entries.append({"symbol": symbol, "domains": domains})
    return entries


class DailyPortfolioRunService:
    def __init__(
        self,
        *,
        store: DailyRunStore | None = None,
        session_service: Any | None = None,
        data_service: Any | None = None,
        pdf_renderer: Callable[[str, str], bytes] | None = None,
        state_loader: Callable[[], Any] = load_state,
        mandate_path: Path | None = None,
        max_workers: int = 3,
        recover_incomplete: bool = True,
    ) -> None:
        self.store = store or DailyRunStore()
        self.session_service = session_service
        self.data_service = data_service or get_unified_data_service()
        self.pdf_renderer = pdf_renderer
        self.state_loader = state_loader
        self.mandate_path = mandate_path
        self.max_workers = max(1, min(int(max_workers), 4))
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel: dict[str, asyncio.Event] = {}
        self._worker_sessions: dict[str, set[str]] = {}
        if recover_incomplete:
            self.store.mark_incomplete_interrupted()

    def list_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        return self.store.list(limit)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self.store.get(run_id)

    def _input_freshness(
        self, portfolio: dict[str, Any], mandate: dict[str, Any]
    ) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        try:
            current_state = self.state_loader()
            current_portfolio = (
                current_state.to_dict()
                if hasattr(current_state, "to_dict")
                else dict(current_state)
            )
            if _stable_hash(current_portfolio) != _stable_hash(portfolio):
                reasons.append("portfolio_state_changed")
        except Exception:  # noqa: BLE001 - freshness is informative, not fatal
            reasons.append("portfolio_state_unavailable")
        try:
            current_mandate = load_mandate(self.mandate_path)
            if int(current_mandate.get("version") or 0) != int(
                mandate.get("version") or 0
            ):
                reasons.append("mandate_version_changed")
        except Exception:  # noqa: BLE001 - freshness is informative, not fatal
            reasons.append("mandate_unavailable")
        return bool(reasons), reasons

    def _launch(
        self,
        run_id: str,
        *,
        portfolio: dict[str, Any],
        mandate: dict[str, Any],
        retry_from_run_id: str | None = None,
        retry_symbol: str | None = None,
    ) -> None:
        cancel_event = asyncio.Event()
        self._cancel[run_id] = cancel_event
        self._worker_sessions[run_id] = set()
        self._tasks[run_id] = asyncio.create_task(
            self._execute(
                run_id,
                portfolio=portfolio,
                mandate=mandate,
                cancel_event=cancel_event,
                retry_from_run_id=retry_from_run_id,
                retry_symbol=retry_symbol,
            )
        )
        self._tasks[run_id].add_done_callback(lambda _: self._tasks.pop(run_id, None))

    async def start(
        self,
        *,
        market_date: str | None = None,
        refresh_policy: str = "ensure_fresh",
        report_profile: str = "master_with_holding_appendices",
        trigger: str = "manual",
        force_new: bool = False,
    ) -> dict[str, Any]:
        if refresh_policy not in {"ensure_fresh", "force", "reuse"}:
            raise ValueError("refresh_policy must be ensure_fresh, force, or reuse")
        state_obj = self.state_loader()
        portfolio = state_obj.to_dict() if hasattr(state_obj, "to_dict") else dict(state_obj)
        holdings = list(portfolio.get("holdings") or [])
        if not holdings:
            raise ValueError("当前没有持仓，无法生成组合盘前报告。")

        mandate = ensure_assignments(holdings, path=self.mandate_path)
        market_date = market_date or resolve_premarket_target_date().isoformat()
        snapshot_id = _stable_hash(portfolio)[:24]
        idempotency_key = _stable_hash(
            {
                "market_date": market_date,
                "portfolio_snapshot_id": snapshot_id,
                "portfolio_updated_at": portfolio.get("updated_at"),
                "mandate_version": mandate["version"],
                "report_profile": report_profile,
            }
        )
        if not force_new:
            existing = self.store.find_idempotent(idempotency_key)
            if existing:
                return {**existing, "deduplicated": True}

        revision = self.store.next_revision(idempotency_key)
        run_id = f"dpr_{market_date.replace('-', '')}_r{revision}_{uuid.uuid4().hex[:8]}"
        record = self.store.create(
            {
                "schema_version": 1,
                "run_id": run_id,
                "market_date": market_date,
                "trigger": trigger,
                "refresh_policy": refresh_policy,
                "report_profile": report_profile,
                "portfolio_snapshot_id": snapshot_id,
                "mandate_version": mandate["version"],
                "idempotency_key": idempotency_key,
                "revision": revision,
                "artifact_revision": revision,
                "status": "queued",
                "stage": "queued",
                "progress": {"completed": 0, "total": len(holdings), "percent": 0},
                "holding_total": len(holdings),
                "holding_completed": 0,
                "holding_failed": 0,
                "warnings": [],
                "error": None,
            }
        )
        self._launch(run_id, portfolio=portfolio, mandate=mandate)
        return record

    async def wait(self, run_id: str) -> dict[str, Any]:
        task = self._tasks.get(run_id)
        if task:
            await task
        record = self.store.get(run_id)
        if not record:
            raise KeyError(run_id)
        return record

    async def cancel(self, run_id: str) -> dict[str, Any]:
        record = self.store.get(run_id)
        if not record:
            raise KeyError(run_id)
        if record.get("status") in TERMINAL_STATUSES:
            return record
        event = self._cancel.setdefault(run_id, asyncio.Event())
        event.set()
        if self.session_service is not None:
            for session_id in self._worker_sessions.get(run_id, set()):
                self.session_service.cancel_current(session_id)
        record.update({"status": "cancelling", "stage": "cancelling"})
        return self.store.save(record)

    async def retry(self, run_id: str, *, symbol: str | None = None) -> dict[str, Any]:
        previous = self.store.get(run_id)
        if not previous:
            raise KeyError(run_id)
        allowed = {
            "failed",
            "cancelled",
            "interrupted",
            "completed",
            "completed_with_warnings",
        }
        if previous.get("status") not in allowed:
            raise ValueError("only terminal or interrupted runs can be retried")
        portfolio = self.store.read_json(run_id, "inputs/portfolio_snapshot.json")
        mandate = self.store.read_json(run_id, "inputs/mandate_snapshot.json")
        if not isinstance(portfolio, dict) or not isinstance(mandate, dict):
            raise ValueError("frozen inputs are unavailable for retry")
        holdings = list(portfolio.get("holdings") or [])
        retry_symbol = normalize_symbol(symbol).upper() if symbol else None
        holding_symbols = {
            normalize_symbol(str(item.get("symbol") or item.get("code") or "")).upper()
            for item in holdings
        }
        if retry_symbol and retry_symbol not in holding_symbols:
            raise ValueError(f"symbol is not in the frozen portfolio: {retry_symbol}")

        idempotency_key = str(previous.get("idempotency_key") or "")
        revision = self.store.next_revision(idempotency_key)
        market_date = str(previous.get("market_date") or "")
        new_run_id = f"dpr_{market_date.replace('-', '')}_r{revision}_{uuid.uuid4().hex[:8]}"
        record = self.store.create(
            {
                "schema_version": 1,
                "run_id": new_run_id,
                "market_date": market_date,
                "trigger": "retry",
                "refresh_policy": previous.get("refresh_policy") or "ensure_fresh",
                "report_profile": previous.get("report_profile")
                or "master_with_holding_appendices",
                "portfolio_snapshot_id": previous.get("portfolio_snapshot_id"),
                "portfolio_updated_at": portfolio.get("updated_at"),
                "mandate_version": previous.get("mandate_version"),
                "idempotency_key": idempotency_key,
                "revision": revision,
                "artifact_revision": revision,
                "parent_run_id": run_id,
                "retry_symbol": retry_symbol,
                "retry_scope": "holding" if retry_symbol else "run",
                "status": "queued",
                "stage": "queued",
                "progress": {"completed": 0, "total": len(holdings), "percent": 0},
                "holding_total": len(holdings),
                "holding_completed": 0,
                "holding_failed": 0,
                "warnings": [],
                "error": None,
            }
        )
        self._launch(
            new_run_id,
            portfolio=portfolio,
            mandate=mandate,
            retry_from_run_id=run_id,
            retry_symbol=retry_symbol,
        )
        return record

    async def _execute(
        self,
        run_id: str,
        *,
        portfolio: dict[str, Any],
        mandate: dict[str, Any],
        cancel_event: asyncio.Event,
        retry_from_run_id: str | None = None,
        retry_symbol: str | None = None,
    ) -> None:
        record = self.store.get(run_id) or {}
        try:
            record.update({"status": "running", "stage": "freezing_inputs", "started_at": _now_local()})
            record = self.store.save(record)
            self.store.write_json(run_id, "inputs/portfolio_snapshot.json", portfolio)
            self.store.write_json(run_id, "inputs/mandate_snapshot.json", mandate)
            if cancel_event.is_set():
                raise asyncio.CancelledError

            record.update({"stage": "refreshing_data"})
            record = self.store.save(record)
            prior_manifest = (
                self.store.read_json(retry_from_run_id, "inputs/data_manifest.json")
                if retry_from_run_id and retry_symbol
                else None
            )
            if isinstance(prior_manifest, dict) and isinstance(
                prior_manifest.get("contexts"), list
            ):
                contexts = list(prior_manifest["contexts"])
                data_batch_id = str(
                    prior_manifest.get("data_batch_id") or f"batch_{retry_from_run_id}"
                )
                reused_data_batch = True
            else:
                contexts = await self._load_data(
                    portfolio.get("holdings") or [],
                    refresh_policy=str(record["refresh_policy"]),
                )
                data_batch_id = f"batch_{run_id}"
                reused_data_batch = False
            status = _data_status(contexts)
            symbols = [
                normalize_symbol(str(item.get("symbol") or item.get("code") or "")).upper()
                for item in portfolio.get("holdings") or []
            ]
            gate = _analysis_gate(contexts, [item for item in symbols if item])
            manifest = {
                "schema_version": 1,
                "run_id": run_id,
                "market_date": record["market_date"],
                "refresh_policy": record["refresh_policy"],
                "data_batch_id": data_batch_id,
                "reused_data_batch": reused_data_batch,
                "data_status": status,
                "analysis_gate": gate,
                "symbols": _manifest_symbol_entries(
                    contexts, [item for item in symbols if item]
                ),
                "contexts": contexts,
                "created_at": _now_local(),
            }
            self.store.write_json(run_id, "inputs/data_manifest.json", manifest)
            record.update(
                {
                    "data_batch_id": data_batch_id,
                    "reused_data_batch": reused_data_batch,
                }
            )
            record = self.store.save(record)
            if cancel_event.is_set():
                raise asyncio.CancelledError

            if gate["decision"] == "skip_report":
                coverage = round(float(gate["coverage_ratio"]) * 100)
                warning = (
                    f"关键数据覆盖仅 {gate['eligible_count']}/{gate['total_count']}（{coverage}%）；"
                    "超过半数持仓缺少“行情 + 至少一类研究数据”，已在模型分析前停止。"
                    "未创建个股研究 Session，也未生成 PDF。"
                )
                input_outdated, outdated_reasons = self._input_freshness(
                    portfolio, mandate
                )
                record.update(
                    {
                        "status": "completed_with_warnings",
                        "stage": "skipped_data_unavailable",
                        "data_status": status,
                        "analysis_gate": gate,
                        "progress": {
                            "completed": 0,
                            "total": len(portfolio.get("holdings") or []),
                            "percent": 0,
                        },
                        "warnings": [warning],
                        "workers": [],
                        "artifacts": [],
                        "input_outdated": input_outdated,
                        "input_outdated_reasons": outdated_reasons,
                        "completed_at": _now_local(),
                    }
                )
                self.store.save(record)
                await asyncio.to_thread(
                    self.store.enforce_retention, keep_days=_retention_days()
                )
                return

            record.update(
                {"stage": "analyzing_holdings", "data_status": status, "analysis_gate": gate}
            )
            record = self.store.save(record)
            briefs, workers = await self._analyze_holdings(
                run_id,
                portfolio=portfolio,
                mandate=mandate,
                contexts=contexts,
                eligible_symbols=set(gate["eligible_symbols"]),
                cancel_event=cancel_event,
                retry_from_run_id=retry_from_run_id,
                retry_symbol=retry_symbol,
            )
            if cancel_event.is_set():
                raise asyncio.CancelledError

            record = self.store.get(run_id) or record
            gate["model_sessions_started"] = sum(
                1 for item in workers if item.get("session_id")
            )
            record.update({"stage": "aggregating", "workers": workers})
            record["analysis_gate"] = gate
            record = self.store.save(record)
            aggregate = aggregate_portfolio(portfolio=portfolio, mandate=mandate, briefs=briefs)
            self.store.write_json(run_id, "outputs/aggregate.json", aggregate)
            self.store.write_json(run_id, "outputs/decision.json", aggregate["decision"])
            for sleeve in aggregate.get("sleeves") or []:
                self.store.write_json(
                    run_id,
                    f"outputs/sleeves/{_safe_symbol(str(sleeve.get('id') or 'unknown'))}.json",
                    sleeve,
                )
            artifacts = await self._render_artifacts(
                run_id,
                market_date=str(record["market_date"]),
                portfolio=portfolio,
                mandate=mandate,
                aggregate=aggregate,
                data_status=status,
                revision=int(record.get("artifact_revision") or 1),
            )
            warning_workers = [item for item in workers if item.get("status") == "degraded"]
            warnings = list(aggregate.get("warnings") or [])
            skipped_workers = [
                item for item in workers if item.get("status") == "skipped_data_unavailable"
            ]
            if skipped_workers:
                warnings.append(
                    f"{len(skipped_workers)} 个标的数据不足，已跳过模型分析以节省 token。"
                )
            if warning_workers:
                warnings.append(f"{len(warning_workers)} 个标的使用了保守降级结果。")
            input_outdated, outdated_reasons = self._input_freshness(
                portfolio, mandate
            )
            record = self.store.get(run_id) or record
            record.update(
                {
                    "status": "completed_with_warnings" if warnings else "completed",
                    "stage": "completed",
                    "progress": {
                        "completed": len(portfolio.get("holdings") or []),
                        "total": len(portfolio.get("holdings") or []),
                        "percent": 100,
                    },
                    "warnings": warnings,
                    "artifacts": artifacts,
                    "summary": aggregate.get("counts"),
                    "holding_completed": len(workers),
                    "holding_failed": len(warning_workers),
                    "input_outdated": input_outdated,
                    "input_outdated_reasons": outdated_reasons,
                    "completed_at": _now_local(),
                }
            )
            self.store.save(record)
            if retry_from_run_id:
                self.store.supersede_artifacts(
                    retry_from_run_id, replacement_run_id=run_id
                )
            await asyncio.to_thread(
                suggest_classifications,
                portfolio.get("holdings") or [],
                path=self.mandate_path,
            )
            await asyncio.to_thread(
                self.store.enforce_retention, keep_days=_retention_days()
            )
        except asyncio.CancelledError:
            record = self.store.get(run_id) or record
            record.update({"status": "cancelled", "stage": "cancelled", "completed_at": _now_local()})
            self.store.save(record)
        except Exception as exc:  # noqa: BLE001 - persisted for UI and Feishu recovery
            record = self.store.get(run_id) or record
            record.update(
                {
                    "status": "failed",
                    "stage": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "completed_at": _now_local(),
                }
            )
            self.store.save(record)
        finally:
            self._cancel.pop(run_id, None)
            self._worker_sessions.pop(run_id, None)

    async def _load_data(
        self, holdings: list[dict[str, Any]], *, refresh_policy: str
    ) -> list[dict[str, Any]]:
        symbols = [
            normalize_symbol(str(item.get("symbol") or item.get("code") or ""))
            for item in holdings
        ]
        symbols = [item for item in symbols if item]
        force_live = True if refresh_policy == "force" else False if refresh_policy == "reuse" else None
        tasks = []
        for start in range(0, len(symbols), 25):
            chunk = symbols[start : start + 25]
            tasks.append(
                asyncio.to_thread(
                    self.data_service.get_context,
                    symbols=chunk,
                    purpose="premarket",
                    include=["market", "fundamentals", "news", "reports"],
                    force_live=force_live,
                )
            )
        return list(await asyncio.gather(*tasks))

    async def _analyze_holdings(
        self,
        run_id: str,
        *,
        portfolio: dict[str, Any],
        mandate: dict[str, Any],
        contexts: list[dict[str, Any]],
        eligible_symbols: set[str],
        cancel_event: asyncio.Event,
        retry_from_run_id: str | None = None,
        retry_symbol: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        semaphore = asyncio.Semaphore(self.max_workers)
        holdings = list(portfolio.get("holdings") or [])
        completed = 0
        progress_lock = asyncio.Lock()

        async def run_one(holding: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            nonlocal completed
            symbol = normalize_symbol(str(holding.get("symbol") or holding.get("code") or "")).upper()
            worker = {
                "symbol": symbol,
                "status": "running",
                "session_id": None,
                "error": None,
                "attempts": 0,
            }
            async with semaphore:
                if cancel_event.is_set():
                    raise asyncio.CancelledError
                previous_brief = (
                    self.store.read_json(
                        retry_from_run_id,
                        f"outputs/holdings/{_safe_symbol(symbol)}/brief.json",
                    )
                    if retry_from_run_id and retry_symbol and symbol != retry_symbol
                    else None
                )
                if isinstance(previous_brief, dict):
                    brief = previous_brief
                    worker.update({"status": "reused", "attempts": 0})
                elif symbol not in eligible_symbols:
                    brief = fallback_brief(
                        symbol,
                        "关键数据覆盖不足，已跳过模型分析以避免无效 token 消耗。",
                    )
                    worker.update({"status": "skipped_data_unavailable"})
                else:
                    symbol_contexts = _contexts_for_symbol(contexts, symbol)
                    symbol_data_status = _data_status(symbol_contexts)
                    last_error: Exception | None = None
                    for attempt in (1, 2):
                        worker["attempts"] = attempt
                        try:
                            brief, session_id = await self._analyze_one(
                                run_id,
                                holding=holding,
                                assignment=(mandate.get("assignments") or {}).get(symbol) or {},
                                contexts=symbol_contexts,
                                data_status=symbol_data_status,
                            )
                            worker.update(
                                {
                                    "status": "completed" if session_id else "degraded",
                                    "session_id": session_id,
                                    "error": None if session_id else "Session 服务未启用",
                                }
                            )
                            break
                        except Exception as exc:  # noqa: BLE001 - bounded repair retry
                            last_error = exc
                    else:
                        assert last_error is not None
                        brief = fallback_brief(
                            symbol,
                            f"个股分析连续两次失败：{type(last_error).__name__}: {last_error}",
                        )
                        worker.update({"status": "degraded", "error": str(last_error)})
                brief = self._decorate_brief(
                    run_id,
                    holding=holding,
                    assignment=(mandate.get("assignments") or {}).get(symbol) or {},
                    mandate=mandate,
                    portfolio=portfolio,
                    contexts=_contexts_for_symbol(contexts, symbol),
                    brief=brief,
                )
                self.store.write_json(
                    run_id,
                    f"outputs/holdings/{_safe_symbol(symbol)}/brief.json",
                    brief,
                )
                async with progress_lock:
                    completed += 1
                    record = self.store.get(run_id) or {}
                    total = len(holdings)
                    record["progress"] = {
                        "completed": completed,
                        "total": total,
                        "percent": round(completed / max(total, 1) * 100),
                    }
                    self.store.save(record)
                return brief, worker

        pairs = await asyncio.gather(*(run_one(holding) for holding in holdings))
        return [item[0] for item in pairs], [item[1] for item in pairs]

    def _decorate_brief(
        self,
        run_id: str,
        *,
        holding: dict[str, Any],
        assignment: dict[str, Any],
        mandate: dict[str, Any],
        portfolio: dict[str, Any],
        contexts: list[dict[str, Any]],
        brief: dict[str, Any],
    ) -> dict[str, Any]:
        """Attach snapshot and portfolio context to the strict worker payload."""

        result = dict(brief)
        symbol = normalize_symbol(
            str(holding.get("symbol") or holding.get("code") or "")
        ).upper()
        def number(value: Any) -> float:
            try:
                return float(value or 0)
            except (TypeError, ValueError):
                return 0.0

        current_value = number(holding.get("market_value"))
        if not current_value:
            current_value = number(holding.get("quantity")) * number(
                holding.get("last_price") or holding.get("cost_price")
            )
        all_values = []
        for item in portfolio.get("holdings") or []:
            value = number(item.get("market_value"))
            if not value:
                value = number(item.get("quantity")) * number(
                    item.get("last_price") or item.get("cost_price")
                )
            all_values.append(value)
        nav = sum(all_values) + number(portfolio.get("cash"))
        sleeve_id = str(assignment.get("active_sleeve_id") or "unassigned")
        assignments = mandate.get("assignments") or {}
        sleeve_value = sum(
            value
            for value, item in zip(all_values, portfolio.get("holdings") or [], strict=False)
            if str(
                assignments.get(
                    normalize_symbol(
                        str(item.get("symbol") or item.get("code") or "")
                    ).upper(),
                    {},
                ).get("active_sleeve_id") or "unassigned"
            )
            == sleeve_id
        )
        cost_price = number(holding.get("cost_price"))
        last_price = number(holding.get("last_price"))
        pnl_pct = (
            round((last_price / cost_price - 1) * 100, 4)
            if cost_price > 0 and last_price > 0
            else None
        )
        data_status = _data_status(contexts)
        confidence_number = {"low": 0.35, "medium": 0.65, "high": 0.85}.get(
            str(result.get("confidence") or "low"), 0.35
        )
        view_action = {
            "add": "increase_candidate",
            "reduce": "reduce_candidate",
            "exit": "exit_candidate",
            "observe": "observe",
        }.get(str(result.get("action") or "observe"), "observe")
        record = self.store.get(run_id) or {}
        result.update(
            {
                "run_id": run_id,
                "snapshot_id": record.get("portfolio_snapshot_id"),
                "report_profile": "daily_update",
                "name": str(holding.get("name") or symbol),
                "sleeve_id": sleeve_id,
                "data_status": data_status,
                "data_as_of": _latest_timestamp(contexts),
                "material_change": False,
                "change_summary": [],
                "portfolio_context": {
                    "market_value": round(current_value, 2),
                    "portfolio_weight": round(current_value / nav, 6) if nav else None,
                    "sleeve_weight": (
                        round(current_value / sleeve_value, 6) if sleeve_value else None
                    ),
                    "cost_price": cost_price or None,
                    "pnl_pct": pnl_pct,
                },
                "view": {
                    "action": view_action,
                    "priority": (
                        "high"
                        if result.get("action") in {"exit", "reduce"}
                        else "normal"
                    ),
                    "confidence": confidence_number,
                    "rationale": list(result.get("reasons") or []),
                    "invalidating_conditions": list(result.get("risks") or []),
                },
                "conditional_observations": list(result.get("condition_orders") or []),
                "source_refs": [],
                "generated_at": _now_local(),
            }
        )
        return result

    async def _analyze_one(
        self,
        run_id: str,
        *,
        holding: dict[str, Any],
        assignment: dict[str, Any],
        contexts: list[dict[str, Any]],
        data_status: str,
    ) -> tuple[dict[str, Any], str | None]:
        symbol = normalize_symbol(str(holding.get("symbol") or holding.get("code") or "")).upper()
        if self.session_service is None:
            return fallback_brief(symbol, "Session 服务未启用，保守降级为观察。"), None
        compact_context = json.dumps(contexts, ensure_ascii=False, default=str)
        if len(compact_context) > 28_000:
            compact_context = compact_context[:28_000] + "…"
        prompt = f"""你是组合晨会中的个股研究 Worker。只分析 {symbol}，不得调用或推断任何真实交易动作。
输入已经冻结；不要重新获取组合持仓或行情。数据状态为 {data_status}。只有冻结数据中
actionability=price_actionable 的行情才可支持价格和仓位判断；analysis_only 时不得输出
精确买卖价、仓位比例、加减仓数量或强价格敏感结论。

持仓事实：{json.dumps(holding, ensure_ascii=False, default=str)}
分组事实：{json.dumps(assignment, ensure_ascii=False, default=str)}
冻结数据上下文：{compact_context}

只输出一个 JSON 对象，不要 Markdown，不要解释。字段：
{{"summary":"一句话", "action":"observe|add|reduce|exit", "confidence":"low|medium|high", "suggested_amount":数字或null, "reasons":["..."], "risks":["..."], "watch_points":["..."], "condition_orders":[{{"trigger":"...", "response":"...", "priority":"normal|high"}}], "data_limited":false}}
若数据不完整、冲突或陈旧，必须 action=observe、suggested_amount=null、data_limited=true。"""
        session = self.session_service.create_session(
            title=f"DailyRun {run_id} {symbol}",
            config={
                "internal": True,
                "portfolio_daily_run": {"research_only": True, "run_id": run_id, "symbol": symbol},
                "include_shell_tools": False,
            },
        )
        self._worker_sessions.setdefault(run_id, set()).add(session.session_id)
        await self.session_service.execute_message(
            session.session_id,
            prompt,
            include_shell_tools=False,
            message_metadata={"daily_run_id": run_id, "daily_run_symbol": symbol},
        )
        messages = self.session_service.get_messages(session.session_id, limit=20)
        reply = next((item for item in reversed(messages) if item.role == "assistant"), None)
        if reply is None:
            raise BriefContractError("worker session did not produce an assistant response")
        brief = parse_holding_brief(reply.content, symbol=symbol)
        if data_status in {"limited", "offline"}:
            brief.update(
                {
                    "action": "observe",
                    "suggested_amount": None,
                    "data_limited": True,
                }
            )
        return brief, session.session_id

    async def _render_artifacts(
        self,
        run_id: str,
        *,
        market_date: str,
        portfolio: dict[str, Any],
        mandate: dict[str, Any],
        aggregate: dict[str, Any],
        data_status: str,
        revision: int,
    ) -> list[dict[str, Any]]:
        if self.pdf_renderer is None:
            raise RuntimeError("PDF renderer is not configured")
        holdings = {
            normalize_symbol(str(item.get("symbol") or item.get("code") or "")).upper(): item
            for item in portfolio.get("holdings") or []
        }
        artifacts: list[dict[str, Any]] = []
        holding_markdowns: list[str] = []
        for brief in aggregate["briefs"]:
            symbol = str(brief["symbol"])
            markdown = render_holding_markdown(
                market_date=market_date,
                holding=holdings.get(symbol, {}),
                brief=brief,
                data_status=data_status,
            )
            holding_markdowns.append(markdown)
            title = f"{symbol} 每日更新 {market_date}"
            artifacts.append(
                self.store.write_artifact(
                    run_id,
                    kind="holding_daily_json",
                    symbol=symbol,
                    filename=f"{market_date}_{_safe_symbol(symbol)}_daily.json",
                    payload=json.dumps(
                        brief, ensure_ascii=False, indent=2, sort_keys=True
                    ).encode("utf-8"),
                    media_type="application/json",
                    revision=revision,
                )
            )
            artifacts.append(
                self.store.write_artifact(
                    run_id,
                    kind="holding_daily_markdown",
                    symbol=symbol,
                    filename=f"{market_date}_{_safe_symbol(symbol)}_daily.md",
                    payload=markdown.encode("utf-8"),
                    media_type="text/markdown",
                    revision=revision,
                )
            )
            pdf = await asyncio.to_thread(self.pdf_renderer, title, markdown)
            if not pdf.startswith(b"%PDF-"):
                raise RuntimeError(f"invalid PDF for {symbol}")
            artifact = self.store.write_artifact(
                run_id,
                kind="holding_daily_pdf",
                symbol=symbol,
                filename=f"{market_date}_{_safe_symbol(symbol)}_daily.pdf",
                payload=pdf,
                revision=revision,
            )
            artifacts.append(artifact)
        master = render_master_markdown(
            market_date=market_date, portfolio=portfolio, mandate=mandate, aggregate=aggregate
        )
        if holding_markdowns:
            master += "\n\n---\n\n# 个股每日更新附录\n\n" + "\n\n---\n\n".join(
                holding_markdowns
            )
        pdf = await asyncio.to_thread(
            self.pdf_renderer, f"组合盘前综合报告 {market_date}", master
        )
        if not pdf.startswith(b"%PDF-"):
            raise RuntimeError("invalid master PDF")
        artifacts.insert(
            0,
            self.store.write_artifact(
                run_id,
                kind="master_pdf",
                filename=f"{market_date}_portfolio_morning_report.pdf",
                payload=pdf,
                revision=revision,
            ),
        )
        artifacts.append(
            self.store.write_artifact(
                run_id,
                kind="master_markdown",
                filename=f"{market_date}_portfolio_morning_report.md",
                payload=master.encode("utf-8"),
                media_type="text/markdown",
                revision=revision,
            )
        )
        artifacts.append(
            self.store.write_artifact(
                run_id,
                kind="portfolio_decision_json",
                filename=f"{market_date}_portfolio_decision.json",
                payload=json.dumps(
                    aggregate["decision"], ensure_ascii=False, indent=2, sort_keys=True
                ).encode("utf-8"),
                media_type="application/json",
                revision=revision,
            )
        )
        manifest_artifacts = []
        run_root = self.store.run_dir(run_id).resolve()
        for artifact in artifacts:
            public_artifact = {
                key: value for key, value in artifact.items() if key != "path"
            }
            artifact_path = Path(str(artifact.get("path") or "")).resolve()
            if run_root in artifact_path.parents:
                public_artifact["relative_path"] = artifact_path.relative_to(
                    run_root
                ).as_posix()
            manifest_artifacts.append(public_artifact)
        self.store.write_json(
            run_id,
            "artifact_manifest.json",
            {
                "schema_version": 1,
                "run_id": run_id,
                "revision": revision,
                "artifacts": manifest_artifacts,
                "created_at": _now_local(),
            },
        )
        return artifacts
