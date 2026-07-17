"""Thread-aware usage recording helpers."""

from __future__ import annotations

import logging
import re
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

from src.usage.normalization import normalize_usage
from src.usage.store import UsageStore

logger = logging.getLogger(__name__)

_CURRENT_RECORDER: ContextVar["UsageRecorder | None"] = ContextVar(
    "usage_recorder", default=None
)
_CURRENT_PARENT_TOOL: ContextVar[str | None] = ContextVar(
    "usage_parent_tool_call", default=None
)
_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*[^\s,&]+"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def classify_tool(tool_name: str, tool: Any | None = None) -> str:
    explicit = getattr(tool, "usage_category", None)
    if explicit:
        return str(explicit)
    name = (tool_name or "").lower()
    if name.startswith("mcp__") or getattr(tool, "_adapter", None) is not None:
        return "mcp"
    if name in {"web_search", "read_url", "get_stock_news", "get_research_reports", "read_document"}:
        return "web"
    if any(token in name for token in ("market", "quote", "price", "kline", "stock_profile", "data_context")):
        return "market"
    if any(token in name for token in ("financial", "fundamental", "statement", "sec_filing", "fred_macro")):
        return "financial"
    if any(token in name for token in ("read_file", "write_file", "edit_file", "publish", "render_report", "save_skill", "patch_skill", "delete_skill")):
        return "file"
    if any(token in name for token in ("calc", "backtest", "factor", "pattern", "pricing", "analysis", "audit")):
        return "compute"
    if any(token in name for token in ("portfolio_state", "session_search", "remember", "goal", "hypothesis", "journal")):
        return "local_data"
    return "other"


def summarize_query(value: Any, category: str | None = None) -> str | None:
    if not isinstance(value, dict):
        return None
    parts: list[str] = []
    query = value.get("query") or value.get("prompt")
    if query:
        parts.append(str(query))
    url = value.get("url")
    if url:
        try:
            parts.append(urlsplit(str(url)).hostname or "web page")
        except ValueError:
            parts.append("web page")
    symbols = value.get("symbols") or value.get("codes") or value.get("code")
    if symbols:
        if isinstance(symbols, (list, tuple, set)):
            parts.append(", ".join(str(item) for item in list(symbols)[:4]))
        else:
            parts.append(str(symbols))
    for key in ("interval", "timeframe", "period", "source", "requested_source", "action"):
        if value.get(key) not in (None, ""):
            parts.append(str(value[key]))
    if not parts and category == "mcp":
        parts.extend(str(key) for key in list(value)[:4])
    if not parts:
        return None
    text = " · ".join(parts)
    text = _SECRET_PATTERN.sub(r"\1=[redacted]", text)
    text = " ".join(text.split())
    return text[:80]


@dataclass
class UsageRecorder:
    store: UsageStore
    scope_type: str
    scope_id: str
    session_id: str | None = None
    attempt_id: str | None = None
    notify: Callable[[int], None] | None = None
    _tool_events: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)

    def __post_init__(self) -> None:
        self.store.start_scope(self.scope_type, self.scope_id)

    def _base(self) -> dict[str, Any]:
        return {
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "session_id": self.session_id,
            "attempt_id": self.attempt_id,
        }

    def _write(self, event: dict[str, Any]) -> int | None:
        try:
            revision, changed = self.store.upsert_event(event)
            if changed and self.notify is not None:
                self.notify(revision)
            return revision
        except Exception:
            logger.warning("Usage event persistence failed", exc_info=True)
            return None

    def record_llm(
        self,
        usage: Any,
        *,
        provider: str,
        model: str,
        status: str,
        elapsed_ms: int,
        started_at: str | None = None,
    ) -> str:
        normalized = normalize_usage(usage)
        event_id = f"llm:{uuid.uuid4().hex}"
        now = _utc_now()
        event = {
            **self._base(),
            "event_id": event_id,
            "parent_tool_call_id": get_current_parent_tool_call_id(),
            "kind": "llm_call",
            "category": "llm",
            "provider": provider or "unknown",
            "model": model or "unknown",
            "status": status,
            "started_at": started_at or now,
            "completed_at": now,
            "elapsed_ms": max(0, int(elapsed_ms)),
            "metadata": {"usage_reported": normalized is not None},
        }
        if normalized is not None:
            event.update(normalized)
        self._write(event)
        return event_id

    def tool_event_id(self, tool_call_id: str, *, prefix: str | None = None) -> str:
        namespace = prefix or self.attempt_id or "attempt"
        return f"tool:{self.scope_type}:{self.scope_id}:{namespace}:{tool_call_id}"

    def start_tool(
        self,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        event_id: str | None = None,
        parent_tool_call_id: str | None = None,
        category: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        resolved_event_id = event_id or self.tool_event_id(tool_call_id)
        event = {
            **self._base(),
            "event_id": resolved_event_id,
            "parent_tool_call_id": parent_tool_call_id,
            "kind": "tool_call",
            "category": category or classify_tool(tool_name),
            "tool_name": tool_name,
            "status": "running",
            "started_at": _utc_now(),
            "query_summary": summarize_query(arguments or {}, category),
            "metadata": {"tool_call_id": tool_call_id, **(metadata or {})},
        }
        with self._lock:
            self._tool_events[resolved_event_id] = event
        self._write(event)
        return resolved_event_id

    def finish_tool(
        self,
        event_id: str,
        *,
        status: str,
        elapsed_ms: int,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            existing = self._tool_events.get(event_id)
            merged_metadata = {**((existing or {}).get("metadata") or {}), **(metadata or {})}
            if (
                existing
                and existing.get("status") == status
                and existing.get("elapsed_ms") == max(0, int(elapsed_ms))
                and (existing.get("metadata") or {}) == merged_metadata
                and status in {"ok", "error", "cancelled"}
            ):
                return
            event = dict(existing or {**self._base(), "event_id": event_id, "kind": "tool_call"})
            event["status"] = status
            event["completed_at"] = _utc_now()
            event["elapsed_ms"] = max(0, int(elapsed_ms))
            event["metadata"] = merged_metadata
            self._tool_events[event_id] = event
        self._write(event)

    def record_resource(
        self,
        *,
        provider: str,
        category: str,
        status: str,
        elapsed_ms: int,
        cache_mode: str = "unknown",
        query: dict[str, Any] | None = None,
        query_summary: str | None = None,
        network_request: bool | None = None,
        cache_access: bool | None = None,
        parent_tool_call_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> str:
        now = _utc_now()
        if network_request is None:
            network_request = cache_mode in {"network", "cache_refresh"}
        if cache_access is None:
            cache_access = cache_mode in {"cache_hit", "cache_refresh", "stale_fallback"}
        resolved_id = event_id or f"resource:{uuid.uuid4().hex}"
        self._write(
            {
                **self._base(),
                "event_id": resolved_id,
                "parent_tool_call_id": parent_tool_call_id or get_current_parent_tool_call_id(),
                "kind": "resource_call",
                "category": category,
                "provider": provider or "unknown",
                "status": status,
                "started_at": now,
                "completed_at": now,
                "elapsed_ms": max(0, int(elapsed_ms)),
                "cache_mode": cache_mode,
                "network_request": network_request,
                "cache_access": cache_access,
                "query_summary": (query_summary or summarize_query(query or {}, category) or "")[:80] or None,
                "metadata": metadata or {},
            }
        )
        return resolved_id


@contextmanager
def bind_usage_recorder(
    recorder: UsageRecorder | None,
    parent_tool_call_id: str | None = None,
) -> Iterator[None]:
    recorder_token = _CURRENT_RECORDER.set(recorder)
    parent_token = _CURRENT_PARENT_TOOL.set(parent_tool_call_id)
    try:
        yield
    finally:
        _CURRENT_PARENT_TOOL.reset(parent_token)
        _CURRENT_RECORDER.reset(recorder_token)


def get_current_usage_recorder() -> UsageRecorder | None:
    return _CURRENT_RECORDER.get()


def get_current_parent_tool_call_id() -> str | None:
    return _CURRENT_PARENT_TOOL.get()


def record_current_resource(**kwargs: Any) -> str | None:
    recorder = get_current_usage_recorder()
    if recorder is None:
        return None
    return recorder.record_resource(**kwargs)
