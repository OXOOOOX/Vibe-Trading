"""REST routes for persisted Deep Report records and artifacts."""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.reports.profile import get_report_profile
from src.research.enrichment import (
    build_extended_research_plan,
    render_extended_research_plan,
)


class ResumeDeepReportRequest(BaseModel):
    content: str = Field("继续完善这份穿透式深度研究报告。", min_length=1, max_length=5000)


class RefreshDeepReportRequest(BaseModel):
    instructions: str = Field("使用最新可验证数据重新研究。", min_length=1, max_length=5000)
    research_depth: Literal["standard", "extended"] = "standard"
    consent_to_extended_research: bool = False
    historical_annual_years: int = Field(8, ge=2, le=12)


class RepairDeepReportRequest(BaseModel):
    instructions: str = Field(
        "使用现有 FinancialSnapshot、Fact 和 Evidence 修复未通过校验的章节。",
        min_length=1,
        max_length=5000,
    )


class ReviseDeepReportRequest(BaseModel):
    section_ids: list[str] = Field(..., min_length=1, max_length=8)
    instructions: str = Field(..., min_length=1, max_length=5000)


class ResolveEquityRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)


class RefreshETFUniverseRequest(BaseModel):
    force_refresh: bool = True
    as_of: str | None = Field(None, max_length=64)
    event_symbols: list[str] = Field(default_factory=list, max_length=20)


class PrewarmETFUniverseRequest(BaseModel):
    force_refresh: bool = False


_QUALIFIED_SYMBOL_RE = re.compile(
    r"(?<![A-Z0-9])(?:\d{6}\.(?:SH|SZ|BJ)|\d{5}\.HK|[A-Z][A-Z0-9.-]{0,14}\.US)(?![A-Z0-9])",
    re.I,
)


def _clean_equity_query(value: str) -> str:
    exact = _QUALIFIED_SYMBOL_RE.search(value.upper())
    if exact:
        return exact.group(0).upper()
    cleaned = re.sub(
        r"(?:请|帮我|一下|生成|重新|做一份|做|单股|穿透式|穿透|深度|研究|分析|报告)",
        " ",
        value,
        flags=re.I,
    )
    cleaned = re.sub(r"[，。！？、,:;；（）()\[\]{}]+", " ", cleaned)
    return " ".join(cleaned.split())[:100]


def _instrument_type(candidate: dict[str, Any]) -> str:
    provider_type = str(candidate.get("type") or "").strip().casefold()
    if provider_type in {"etf", "mutualfund", "fund"}:
        return "etf"
    if provider_type in {"index", "marketindex"}:
        return "index"
    symbol = str(candidate.get("symbol") or "").upper()
    code = symbol.split(".", 1)[0]
    if symbol.endswith(".SH") and code.startswith("5"):
        return "etf"
    if symbol.endswith(".SZ") and code.startswith(("15", "16")):
        return "etf"
    return "company_equity"


def _qualified_cn_etf_symbol(query: str) -> str | None:
    normalized = query.upper()
    if re.fullmatch(r"5\d{5}(?:\.SH)?", normalized):
        return normalized if normalized.endswith(".SH") else f"{normalized}.SH"
    if re.fullmatch(r"1[56]\d{4}(?:\.SZ)?", normalized):
        return normalized if normalized.endswith(".SZ") else f"{normalized}.SZ"
    return None


def _service_or_503(get_service: Callable[[], Any]) -> Any:
    service = get_service()
    if service is None:
        raise HTTPException(status_code=503, detail="Deep Report service is unavailable")
    return service


def register_deep_report_routes(
    app: FastAPI,
    dependency: Callable[..., Any],
    *,
    get_service: Callable[[], Any],
    get_dispatcher: Callable[[], Any],
    pdf_renderer: Callable[[str, str], bytes],
) -> None:
    """Register report listing, artifact, resume, and revision routes."""

    auth = [Depends(dependency)]

    @app.get("/reports", dependencies=auth)
    async def list_deep_reports(limit: int = Query(100, ge=1, le=500)):
        service = _service_or_503(get_service)
        return [record.to_dict() for record in service.list(limit=limit)]

    @app.get("/research/knowledge/search", dependencies=auth)
    async def search_research_knowledge(
        query: str = Query("", max_length=500),
        symbol: str = Query("", max_length=32),
        domains: list[str] | None = Query(None),
        metrics: list[str] | None = Query(None),
        as_of: str | None = Query(None, max_length=64),
        limit: int = Query(20, ge=1, le=100),
    ):
        from src.research import get_research_knowledge_store, knowledge_enabled

        if not knowledge_enabled():
            raise HTTPException(status_code=503, detail="Research knowledge is disabled")
        return get_research_knowledge_store().search(
            query=query,
            symbol=symbol,
            domains=domains or [],
            metrics=metrics or [],
            as_of=as_of,
            limit=limit,
        )

    @app.get("/research/symbols/{symbol}/history", dependencies=auth)
    async def research_symbol_history(
        symbol: str,
        limit: int = Query(20, ge=1, le=100),
    ):
        from src.research import get_research_knowledge_store, knowledge_enabled

        if not knowledge_enabled():
            raise HTTPException(status_code=503, detail="Research knowledge is disabled")
        return get_research_knowledge_store().history(symbol, limit=limit)

    @app.get("/research/etf/{symbol}/reuse-metrics", dependencies=auth)
    async def get_etf_reuse_metrics(symbol: str):
        """Return P0-P3 cache, token, and refresh-decision baselines."""

        from src.reports.etf_research import get_etf_research_store

        return get_etf_research_store().baseline_metrics(symbol)

    @app.get("/research/etf/{symbol}/universe", dependencies=auth)
    async def get_etf_universe_status(symbol: str):
        """Return mapping/provider/cache/P4A status without contacting providers."""

        from src.reports.etf_universe_provider import get_etf_universe_service

        return await asyncio.to_thread(get_etf_universe_service().status, symbol)

    @app.get("/research/etf/{symbol}/universe/snapshot", dependencies=auth)
    async def get_latest_etf_universe_snapshot(symbol: str):
        from src.reports.etf_universe_provider import get_etf_universe_service

        snapshot = await asyncio.to_thread(get_etf_universe_service().latest_snapshot, symbol)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="ETF universe snapshot not found")
        return snapshot.to_dict()

    @app.post("/research/etf/{symbol}/universe/refresh", dependencies=auth)
    async def refresh_etf_universe(symbol: str, payload: RefreshETFUniverseRequest):
        from src.reports.etf_universe_provider import (
            ETFUniverseUnavailableError,
            get_etf_universe_service,
        )

        try:
            result = await asyncio.to_thread(
                get_etf_universe_service().get_or_refresh,
                symbol,
                payload.force_refresh,
                payload.as_of,
                payload.event_symbols,
            )
        except ETFUniverseUnavailableError as exc:
            raise HTTPException(
                status_code=503,
                detail={"message": str(exc), "attempts": exc.attempts},
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result.to_dict()

    @app.post("/research/etf/universe/prewarm", dependencies=auth)
    async def prewarm_etf_universe(payload: PrewarmETFUniverseRequest):
        from src.reports.etf_universe_provider import get_etf_universe_service

        return await asyncio.to_thread(
            get_etf_universe_service().prewarm_current_holdings,
            force_refresh=payload.force_refresh,
        )

    @app.get("/research/sources/{document_ref}", dependencies=auth)
    async def get_research_source(
        document_ref: str,
        query: str = Query("", max_length=500),
        chunk_refs: list[str] | None = Query(None),
        limit: int = Query(8, ge=1, le=30),
    ):
        from src.research import get_research_knowledge_store, knowledge_enabled

        if not knowledge_enabled():
            raise HTTPException(status_code=503, detail="Research knowledge is disabled")
        try:
            return get_research_knowledge_store().read_document(
                document_ref,
                query=query,
                chunk_refs=chunk_refs or [],
                limit=limit,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Research source not found") from exc

    @app.post("/reports/resolve-instrument", dependencies=auth)
    @app.post("/reports/resolve-equity", dependencies=auth)
    async def resolve_equity(payload: ResolveEquityRequest):
        from src.tools.symbol_search_tool import SymbolSearchTool

        query = _clean_equity_query(payload.query)
        if not query:
            raise HTTPException(status_code=400, detail="请输入证券名称或准确代码")
        etf_symbol = _qualified_cn_etf_symbol(query)
        raw = await asyncio.to_thread(SymbolSearchTool().execute, query=query, limit=8)
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=503, detail="证券解析服务返回无效结果") from exc
        if (not isinstance(envelope, dict) or not envelope.get("ok")) and not etf_symbol:
            raise HTTPException(
                status_code=503,
                detail=str((envelope or {}).get("error") or "证券解析服务暂不可用"),
            )
        data = envelope.get("data") or {} if isinstance(envelope, dict) else {}
        candidates = [
            dict(item) for item in (data.get("candidates") or [])
            if isinstance(item, dict)
            and re.fullmatch(
                r"(?:\d{6}\.(?:SH|SZ|BJ)|\d{5}\.HK|[A-Z][A-Z0-9.-]{0,14}\.US)",
                str(item.get("symbol") or "").upper(),
            )
        ]
        if etf_symbol and not any(
            str(item.get("symbol") or "").upper() == etf_symbol for item in candidates
        ):
            mapping: dict[str, Any] = {}
            try:
                from src.reports.etf_universe_provider import get_etf_universe_service

                etf_status = await asyncio.to_thread(
                    get_etf_universe_service().status,
                    etf_symbol,
                )
                mapping = dict(etf_status.get("mapping") or {})
            except Exception:
                mapping = {}
            index_name = str(mapping.get("index_name") or "").strip()
            candidates.insert(0, {
                "symbol": etf_symbol,
                "name": f"{index_name}ETF" if index_name else f"ETF {etf_symbol.split('.', 1)[0]}",
                "market": "cn_etf",
                "type": "etf",
                "source": "etf_code_rule" + ("+index_mapping" if mapping else ""),
            })
        exact_symbol = _QUALIFIED_SYMBOL_RE.fullmatch(query.upper())
        resolved = None
        if exact_symbol:
            resolved = next(
                (item for item in candidates if str(item.get("symbol") or "").upper() == query.upper()),
                None,
            )
        if resolved is None and re.fullmatch(r"\d{6}", query):
            matching_codes = [
                item for item in candidates
                if str(item.get("symbol") or "").upper().split(".", 1)[0] == query
            ]
            if len(matching_codes) == 1:
                resolved = matching_codes[0]
        if resolved is None and re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", query.upper()):
            expected_us = f"{query.upper()}.US"
            resolved = next(
                (item for item in candidates if str(item.get("symbol") or "").upper() == expected_us),
                None,
            )
        if resolved is None:
            normalized_query = query.casefold()
            exact_names = [
                item for item in candidates
                if str(item.get("name") or "").strip().casefold() == normalized_query
            ]
            if len(exact_names) == 1:
                resolved = exact_names[0]
        if resolved is None and len(candidates) == 1:
            resolved = candidates[0]
        options = [
            {
                "symbol": str(item.get("symbol") or "").upper(),
                "security_name": str(item.get("name") or item.get("symbol") or ""),
                "market": item.get("market"),
                "source": item.get("source"),
                "instrument_type": _instrument_type(item),
            }
            for item in candidates[:5]
        ]
        if resolved is None:
            return {
                "status": "ambiguous" if options else "not_found",
                "query": query,
                "options": options,
                "source_statuses": data.get("sources") or {},
            }
        return {
            "status": "resolved",
            "query": query,
            "symbol": str(resolved.get("symbol") or "").upper(),
            "security_name": str(resolved.get("name") or resolved.get("symbol") or ""),
            "market": resolved.get("market"),
            "source": resolved.get("source"),
            "instrument_type": _instrument_type(resolved),
            "options": options,
        }

    @app.get("/reports/{report_id}", dependencies=auth)
    async def get_deep_report(report_id: str, include_content: bool = False):
        service = _service_or_503(get_service)
        record = service.get(report_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Deep Report not found")
        payload = record.to_dict()
        payload["subject_profile"] = service.subject_profile(report_id)
        if not payload.get("research_coverage") or not payload.get("history_delta"):
            try:
                from src.research import get_research_knowledge_store, knowledge_enabled

                if knowledge_enabled():
                    knowledge = get_research_knowledge_store()
                    payload["research_coverage"] = (
                        payload.get("research_coverage") or knowledge.coverage(report_id) or {}
                    )
                    payload["history_delta"] = (
                        payload.get("history_delta") or knowledge.delta(report_id) or {}
                    )
            except Exception:
                pass
        if include_content:
            try:
                payload["content"] = service.read_markdown(report_id)
                payload["content_role"] = service.content_role(report_id)
            except FileNotFoundError:
                payload["content"] = None
                payload["content_role"] = None
        return payload

    @app.get("/reports/{report_id}/artifacts/{artifact_id}", dependencies=auth)
    async def get_deep_report_artifact(
        report_id: str,
        artifact_id: Literal[
            "markdown", "pdf", "diagnostic", "diff", "monitoring_bundle"
        ],
        refresh: bool = Query(False),
        download: bool = Query(True),
    ):
        service = _service_or_503(get_service)
        record = service.get(report_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Deep Report not found")
        try:
            if artifact_id == "pdf":
                path, record = service.ensure_pdf(report_id, pdf_renderer, force=refresh)
                filename = next(
                    (
                        str(item.get("filename"))
                        for item in record.artifacts
                        if item.get("artifact_id") == "pdf"
                    ),
                    path.name,
                )
                return FileResponse(
                    path,
                    media_type="application/pdf",
                    filename=filename,
                    content_disposition_type="attachment" if download else "inline",
                )
            path = service.artifact_path(report_id, artifact_id)
        except (FileNotFoundError, KeyError):
            raise HTTPException(status_code=404, detail="Deep Report artifact not found")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        filename = next(
            (
                str(item.get("filename"))
                for item in record.artifacts
                if item.get("artifact_id") == artifact_id
            ),
            Path(path).name,
        )
        return FileResponse(
            path,
            media_type=(
                "application/json"
                if artifact_id == "monitoring_bundle"
                else "text/markdown; charset=utf-8"
            ),
            filename=filename,
            content_disposition_type="attachment" if download else "inline",
        )

    async def _follow_up(report_id: str, payload: ResumeDeepReportRequest):
        service = _service_or_503(get_service)
        record = service.get(report_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Deep Report not found")
        dispatcher = get_dispatcher()
        if dispatcher is None:
            raise HTTPException(status_code=503, detail="Session dispatcher is unavailable")
        try:
            result = await dispatcher.submit(
                session_id=record.session_id,
                content=payload.content,
                source="api",
                source_metadata={
                    "response_mode": "chat",
                    "linked_report_id": record.report_id,
                },
                include_shell_tools=False,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {**result, "parent_report_id": record.report_id}

    @app.post("/reports/{report_id}/followups", dependencies=auth)
    async def follow_up_deep_report(report_id: str, payload: ResumeDeepReportRequest):
        return await _follow_up(report_id, payload)

    @app.post("/reports/{report_id}/resume", dependencies=auth)
    async def resume_deep_report(report_id: str, payload: ResumeDeepReportRequest):
        """Backward-compatible alias; resume is now a non-mutating follow-up."""

        return await _follow_up(report_id, payload)

    @app.post("/reports/{report_id}/refresh", dependencies=auth)
    async def refresh_deep_report(report_id: str, payload: RefreshDeepReportRequest):
        service = _service_or_503(get_service)
        record = service.get(report_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Deep Report not found")
        extended = payload.research_depth == "extended"
        if extended and not payload.consent_to_extended_research:
            raise HTTPException(
                status_code=400,
                detail="Extended research requires explicit user consent",
            )
        if extended and record.status != "completed":
            raise HTTPException(
                status_code=409,
                detail="Only a completed report can start evidence enrichment",
            )
        dispatcher = get_dispatcher()
        if dispatcher is None:
            raise HTTPException(status_code=503, detail="Session dispatcher is unavailable")
        content = payload.instructions
        source_metadata = {
            "response_mode": "deep_report",
            "report_profile": record.profile,
            "parent_report_id": record.report_id,
            "revision_mode": "full_refresh",
        }
        if extended:
            profile = get_report_profile(record.profile)
            section_labels = dict(profile["required_sections"])
            gap_labels = [
                section_labels.get(module_id, module_id)
                for module_id, module in record.analysis_modules.items()
                if module.status not in {"passed", "not_requested"}
            ]
            gap_text = "、".join(dict.fromkeys(gap_labels)) or "报告中已标记的数据缺口"
            content = (
                f"用户已明确同意对 {record.security_name or record.symbol}（{record.symbol}）进行扩展资料搜集，"
                "并已知悉这会增加研究耗时和 Token 消耗。\n\n"
                f"优先补齐：{gap_text}。\n"
                "先继续读取可核验的财报、公告、监管披露、指数或基金文件以及网页正文，"
                "重点补齐缺少的往年数据、可比期间和关键来源，再生成新的不可变 revision。"
                "不得为了消除缺口而编造、补零或采用无法追溯的数据；扩展搜集后仍无法核实的内容必须继续标明缺口。\n\n"
                f"用户补充要求：{payload.instructions}"
            )
            enrichment_plan = build_extended_research_plan(
                record,
                historical_years=payload.historical_annual_years,
            )
            rendered_plan = render_extended_research_plan(enrichment_plan)
            if rendered_plan:
                content = f"{content}\n\n{rendered_plan}"
            source_metadata.update({
                "research_depth": "extended",
                "extended_research_consent": True,
                "generation_reason": "用户同意补齐缺失资料后重新生成",
            })
            if enrichment_plan.get("tasks"):
                source_metadata["research_enrichment_plan"] = enrichment_plan
        try:
            result = await dispatcher.submit(
                session_id=record.session_id,
                content=content,
                source="api",
                source_metadata=source_metadata,
                include_shell_tools=False,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        response = {
            **result,
            "parent_report_id": record.report_id,
            "revision_mode": "full_refresh",
        }
        if extended:
            response.update({
                "research_depth": "extended",
                "token_notice_acknowledged": True,
            })
        return response

    @app.post("/reports/{report_id}/revisions", dependencies=auth)
    async def revise_deep_report(report_id: str, payload: ReviseDeepReportRequest):
        service = _service_or_503(get_service)
        record = service.get(report_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Deep Report not found")
        allowed_ids = {
            key for key, _ in get_report_profile(record.profile)["required_sections"]
        }
        invalid = sorted(set(payload.section_ids) - allowed_ids)
        if invalid:
            raise HTTPException(status_code=400, detail=f"Unknown section ids: {', '.join(invalid)}")
        dispatcher = get_dispatcher()
        if dispatcher is None:
            raise HTTPException(status_code=503, detail="Session dispatcher is unavailable")
        content = (
            f"更新报告 {record.report_id} 的指定章节：{', '.join(payload.section_ids)}。\n\n"
            f"修订要求：{payload.instructions}"
        )
        try:
            result = await dispatcher.submit(
                session_id=record.session_id,
                content=content,
                source="api",
                source_metadata={
                    "response_mode": "deep_report",
                    "report_profile": record.profile,
                    "parent_report_id": record.report_id,
                    "revision_sections": payload.section_ids,
                    "revision_mode": "section_revision",
                },
                include_shell_tools=False,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            **result,
            "parent_report_id": record.report_id,
            "revision_sections": payload.section_ids,
            "revision_mode": "section_revision",
        }

    @app.post("/reports/{report_id}/repair", dependencies=auth)
    async def repair_deep_report(report_id: str, payload: RepairDeepReportRequest):
        service = _service_or_503(get_service)
        record = service.get(report_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Deep Report not found")
        if record.quality_status != "failed_validation":
            raise HTTPException(status_code=409, detail="Only a failed-validation report needs repair")
        blockers = service.repair_blockers(report_id)
        if blockers:
            raise HTTPException(
                status_code=409,
                detail=(
                    "当前报告包含章节修复无法消除的确定性硬门控："
                    + "、".join(blockers)
                    + "。请使用“用新数据更新”创建 full refresh revision。"
                ),
            )
        repair_context = service.repair_context(report_id)
        repair_content = (
            f"{payload.instructions.strip()}\n\n"
            f"{repair_context['prompt_block']}"
        )
        dispatcher = get_dispatcher()
        if dispatcher is None:
            raise HTTPException(status_code=503, detail="Session dispatcher is unavailable")
        try:
            result = await dispatcher.submit(
                session_id=record.session_id,
                content=repair_content,
                source="api",
                source_metadata={
                    "response_mode": "deep_report",
                    "report_profile": record.profile,
                    "parent_report_id": record.report_id,
                    "revision_sections": repair_context["section_ids"],
                    "revision_mode": "repair",
                },
                include_shell_tools=False,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            **result,
            "parent_report_id": record.report_id,
            "revision_sections": repair_context["section_ids"],
            "revision_mode": "repair",
        }

    @app.post("/reports/{report_id}/archive", dependencies=auth)
    async def archive_deep_report(report_id: str):
        from src.tools.obsidian_publish_tool import PublishObsidianNoteTool

        service = _service_or_503(get_service)
        record = service.get(report_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Deep Report not found")
        if record.status != "completed" or record.quality_status == "failed_validation":
            raise HTTPException(status_code=409, detail="Only a validated report can be archived")
        try:
            content = service.read_markdown(report_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=409, detail="Formal Markdown artifact is unavailable") from exc
        artifact = next(
            (item for item in record.artifacts if item.get("artifact_id") == "markdown"),
            None,
        )
        filename = str((artifact or {}).get("filename") or f"{record.report_id}.md")
        relative_dir = os.getenv("VIBE_TRADING_DEEP_REPORT_OBSIDIAN_DIR", "QQQ/Invest").strip("/\\")
        raw = await asyncio.to_thread(
            PublishObsidianNoteTool().execute,
            path=f"{relative_dir}/{filename}",
            content=content,
            overwrite=False,
        )
        result = json.loads(raw)
        if result.get("status") != "ok":
            raise HTTPException(status_code=409, detail=str(result.get("error") or "Archive failed"))
        return result
