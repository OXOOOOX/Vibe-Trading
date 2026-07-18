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

from src.reports.profile import EQUITY_DEEP_RESEARCH_PROFILE


class ResumeDeepReportRequest(BaseModel):
    content: str = Field("继续完善这份穿透式深度研究报告。", min_length=1, max_length=5000)


class RefreshDeepReportRequest(BaseModel):
    instructions: str = Field("使用最新可验证数据重新研究。", min_length=1, max_length=5000)


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

    @app.post("/reports/resolve-equity", dependencies=auth)
    async def resolve_equity(payload: ResolveEquityRequest):
        from src.tools.symbol_search_tool import SymbolSearchTool

        query = _clean_equity_query(payload.query)
        if not query:
            raise HTTPException(status_code=400, detail="请输入上市公司名称或准确股票代码")
        raw = await asyncio.to_thread(SymbolSearchTool().execute, query=query, limit=8)
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=503, detail="证券解析服务返回无效结果") from exc
        if not isinstance(envelope, dict) or not envelope.get("ok"):
            raise HTTPException(
                status_code=503,
                detail=str((envelope or {}).get("error") or "证券解析服务暂不可用"),
            )
        data = envelope.get("data") or {}
        candidates = [
            dict(item) for item in (data.get("candidates") or [])
            if isinstance(item, dict)
            and re.fullmatch(
                r"(?:\d{6}\.(?:SH|SZ|BJ)|\d{5}\.HK|[A-Z][A-Z0-9.-]{0,14}\.US)",
                str(item.get("symbol") or "").upper(),
            )
        ]
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
            "options": options,
        }

    @app.get("/reports/{report_id}", dependencies=auth)
    async def get_deep_report(report_id: str, include_content: bool = False):
        service = _service_or_503(get_service)
        record = service.get(report_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Deep Report not found")
        payload = record.to_dict()
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
        artifact_id: Literal["markdown", "pdf", "diagnostic", "diff"],
        refresh: bool = Query(False),
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
                return FileResponse(path, media_type="application/pdf", filename=filename)
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
        return FileResponse(path, media_type="text/markdown; charset=utf-8", filename=filename)

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
        dispatcher = get_dispatcher()
        if dispatcher is None:
            raise HTTPException(status_code=503, detail="Session dispatcher is unavailable")
        try:
            result = await dispatcher.submit(
                session_id=record.session_id,
                content=payload.instructions,
                source="api",
                source_metadata={
                    "response_mode": "deep_report",
                    "report_profile": record.profile,
                    "parent_report_id": record.report_id,
                    "revision_mode": "full_refresh",
                },
                include_shell_tools=False,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            **result,
            "parent_report_id": record.report_id,
            "revision_mode": "full_refresh",
        }

    @app.post("/reports/{report_id}/revisions", dependencies=auth)
    async def revise_deep_report(report_id: str, payload: ReviseDeepReportRequest):
        service = _service_or_503(get_service)
        record = service.get(report_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Deep Report not found")
        allowed_ids = {
            key for key, _ in EQUITY_DEEP_RESEARCH_PROFILE["required_sections"]
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
        dispatcher = get_dispatcher()
        if dispatcher is None:
            raise HTTPException(status_code=503, detail="Session dispatcher is unavailable")
        try:
            result = await dispatcher.submit(
                session_id=record.session_id,
                content=payload.instructions,
                source="api",
                source_metadata={
                    "response_mode": "deep_report",
                    "report_profile": record.profile,
                    "parent_report_id": record.report_id,
                    "revision_mode": "repair",
                },
                include_shell_tools=False,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            **result,
            "parent_report_id": record.report_id,
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
