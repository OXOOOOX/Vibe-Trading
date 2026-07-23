"""Read-only Agent tools for the shared research knowledge layer."""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools import BaseTool
from src.research import get_research_knowledge_store, knowledge_enabled
from src.security.scanner import with_security_warnings


class ReadResearchDocumentTool(BaseTool):
    name = "read_research_document"
    description = (
        "Read selected chunks from a source document previously opened by read_url. "
        "Use document_ref plus either a query or explicit chunk_refs; this is how to "
        "read content beyond the initial webpage preview."
    )
    parameters = {
        "type": "object",
        "properties": {
            "document_ref": {"type": "string"},
            "query": {"type": "string"},
            "chunk_refs": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 8},
        },
        "required": ["document_ref"],
    }
    is_readonly = True
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        if not knowledge_enabled():
            return json.dumps({"status": "disabled", "error": "research knowledge is disabled"}, ensure_ascii=False)
        try:
            result = get_research_knowledge_store().read_document(
                str(kwargs.get("document_ref") or ""),
                query=str(kwargs.get("query") or ""),
                chunk_refs=kwargs.get("chunk_refs") or [],
                limit=int(kwargs.get("limit") or 8),
            )
        except KeyError:
            return json.dumps({"status": "error", "error": "document_ref not found"}, ensure_ascii=False)
        return json.dumps(
            with_security_warnings({"status": "ok", **result}, fields=("chunks.*.text",)),
            ensure_ascii=False,
        )


class QueryResearchKnowledgeTool(BaseTool):
    name = "query_research_knowledge"
    description = (
        "Query verified historical research Facts, source Evidence, prior Claims, and "
        "report-to-report changes. command=financials replays validated structured "
        "official filing snapshots without downloading or OCRing the filing again. "
        "Prior Claims are context only and never new Evidence."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["history", "search", "inspect", "diff", "financials"],
            },
            "symbol": {"type": "string"},
            "query": {"type": "string"},
            "domains": {"type": "array", "items": {"type": "string"}},
            "metrics": {"type": "array", "items": {"type": "string"}},
            "as_of": {"type": "string"},
            "refs": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        },
        "required": ["command"],
    }
    is_readonly = True
    repeatable = True

    def execute(self, **kwargs: Any) -> str:
        if not knowledge_enabled():
            return json.dumps({"status": "disabled", "error": "research knowledge is disabled"}, ensure_ascii=False)
        command = str(kwargs.get("command") or "search")
        store = get_research_knowledge_store()
        if command == "history":
            result = store.history(str(kwargs.get("symbol") or ""), limit=int(kwargs.get("limit") or 20))
        elif command == "financials":
            result = store.list_financial_snapshots(
                str(kwargs.get("symbol") or ""),
                validated_only=True,
                limit=int(kwargs.get("limit") or 20),
            )
        elif command == "diff":
            refs = [str(item) for item in kwargs.get("refs") or [] if str(item)]
            if not refs:
                return json.dumps({"status": "error", "error": "diff requires refs[0]=report_id"}, ensure_ascii=False)
            result = store.delta(refs[0]) or {"report_id": refs[0], "status": "not_indexed"}
        elif command == "inspect":
            refs = [str(item) for item in kwargs.get("refs") or [] if str(item)]
            if refs and refs[0].startswith("doc_"):
                try:
                    result = store.read_document(refs[0], query=str(kwargs.get("query") or ""), limit=int(kwargs.get("limit") or 20))
                except KeyError:
                    return json.dumps({"status": "error", "error": "document_ref not found"}, ensure_ascii=False)
            else:
                result = store.search(
                    query=str(kwargs.get("query") or ""), symbol=str(kwargs.get("symbol") or ""),
                    domains=kwargs.get("domains") or [], metrics=kwargs.get("metrics") or [],
                    as_of=str(kwargs.get("as_of") or "") or None, limit=int(kwargs.get("limit") or 20),
                )
        else:
            result = store.search(
                query=str(kwargs.get("query") or ""), symbol=str(kwargs.get("symbol") or ""),
                domains=kwargs.get("domains") or [], metrics=kwargs.get("metrics") or [],
                as_of=str(kwargs.get("as_of") or "") or None, limit=int(kwargs.get("limit") or 20),
            )
        return json.dumps({"status": "ok", "command": command, "data": result}, ensure_ascii=False)
