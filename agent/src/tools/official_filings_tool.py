"""Read-only research tool that refreshes and returns authenticated official filings."""

from __future__ import annotations

import json
from typing import Any, Callable

from src.agent.tools import BaseTool
from src.research import get_official_filing_service


class OfficialFilingsTool(BaseTool):
    name = "get_official_filings"
    description = (
        "Refresh authenticated company filings from official A-share, HKEX, or SEC sources. "
        "Only full documents fetched from approved official domains are returned as "
        "official_primary; search snippets are never evidence."
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "Canonical symbol, for example 600036.SH, 00700.HK, or AAPL.",
            },
            "force": {"type": "boolean", "default": False},
            "limit": {"type": "integer", "minimum": 1, "maximum": 12, "default": 6},
            "annual_years": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1990, "maximum": 2100},
                "maxItems": 12,
                "description": (
                    "Optional reporting years to backfill. When present, the tool performs "
                    "year-targeted official annual-report discovery and archives the full "
                    "documents plus structured snapshots in the shared subject dossier."
                ),
            },
        },
        "required": ["symbol"],
    }
    is_readonly = True
    repeatable = True

    def __init__(
        self,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.event_callback = event_callback

    def execute(self, **kwargs: Any) -> str:
        symbol = str(kwargs.get("symbol") or "").strip().upper()
        if not symbol:
            return json.dumps({"ok": False, "error": "symbol is required"}, ensure_ascii=False)
        try:
            service = get_official_filing_service()
            limit = max(1, min(int(kwargs.get("limit", 6)), 12))
            annual_years = sorted({
                int(value) for value in (kwargs.get("annual_years") or [])
            }, reverse=True)
            if annual_years:
                refresh = service.backfill_annual_reports(
                    symbol,
                    years=annual_years,
                    force=bool(kwargs.get("force", False)),
                    limit_per_provider_year=min(limit, 4),
                )
            else:
                refresh = service.refresh(
                    symbol,
                    force=bool(kwargs.get("force", False)),
                    limit_per_provider=limit,
                )
            sources = service.store.list_subject_sources(
                symbol,
                source_kind="official_filing",
                verification_status="official_primary",
                limit=limit,
            )
            result = {"ok": True, "refresh": refresh, **sources}
            if self.event_callback is not None and annual_years:
                self.event_callback(
                    "report.official_filing_refresh",
                    {
                        "symbol": symbol,
                        "annual_years": annual_years,
                        "refresh": refresh,
                    },
                )
            return json.dumps(
                result,
                ensure_ascii=False,
                default=str,
            )
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


__all__ = ["OfficialFilingsTool"]
