"""Web reader tool: fetch a URL as Markdown text via the Jina Reader API."""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from src.agent.progress import emit_progress
from src.agent.tools import BaseTool
from src.security.scanner import with_security_warnings
from src.usage import get_current_usage_recorder, record_current_resource

logger = logging.getLogger(__name__)

_JINA_PREFIX = "https://r.jina.ai/"
_TIMEOUT = 30
_MAX_LENGTH = 8000
_CACHED_MARKER = "Warning: This is a cached snapshot"


def _url_allowed(url: str) -> tuple[bool, str]:
    """Return whether a URL is safe to forward to the remote reader service."""
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return False, "target URL is not allowed"

    if parsed.scheme.lower() not in {"http", "https"}:
        return False, "target URL is not allowed"
    if not parsed.hostname:
        return False, "target URL is not allowed"
    if parsed.username or parsed.password:
        return False, "target URL is not allowed"

    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return False, "target URL is not allowed"

    ip_host = host.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(ip_host)
    except ValueError:
        return True, ""

    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    ):
        return False, "target URL is not allowed"
    return True, ""


def read_url(url: str, no_cache: bool = False, subject_key: str = "") -> str:
    """Fetch web page content via the Jina Reader API.

    The full URL (including query string) is sent to the third-party Jina
    Reader service (r.jina.ai); never pass credentials/tokens or private
    addresses. Results may be a cached snapshot.

    Args:
        url: Target URL.
        no_cache: When true, ask the reader for a fresh (uncached) fetch.
        subject_key: Optional normalized security symbol. Deep/research sessions
            pass it so every opened page is attached to the subject dossier.

    Returns:
        JSON result with title, content, url; ``cached: true`` is added
        when the reader served a stale snapshot.
    """
    target_url = url.strip()
    allowed, error = _url_allowed(target_url)
    if not allowed:
        return json.dumps({"status": "error", "error": error}, ensure_ascii=False)

    request_started = time.perf_counter()
    try:
        headers = {"Accept": "text/markdown"}
        if no_cache:
            headers["x-no-cache"] = "true"
        emit_progress(
            "fetching",
            message=f"GET {target_url[:60]}{'…' if len(target_url) > 60 else ''}",
        )
        resp = requests.get(
            f"{_JINA_PREFIX}{target_url}",
            headers=headers,
            timeout=_TIMEOUT,
        )
        emit_progress("parsing", message="extracting markdown")
        if resp.status_code != 200:
            record_current_resource(
                provider="jina_reader",
                category="web",
                status="error",
                elapsed_ms=int((time.perf_counter() - request_started) * 1000),
                cache_mode="network",
                query={"url": target_url},
                network_request=True,
                cache_access=False,
                metadata={"http_status": resp.status_code},
            )
            logger.warning("read_url upstream HTTP %s: %s", resp.status_code, resp.text[:500])
            return json.dumps({
                "status": "error",
                "error": f"remote reader returned HTTP {resp.status_code}: {resp.text[:500]}",
            }, ensure_ascii=False)

        full_text = resp.text
        text = full_text
        title = ""
        for line in text.split("\n"):
            if line.startswith("Title:"):
                title = line[6:].strip()
                break

        published_at = ""
        published_match = re.search(
            r"(?im)^(?:Published Time|Publication Date|Published At):\s*(.+?)\s*$",
            full_text,
        )
        if published_match:
            published_at = published_match.group(1).strip()

        knowledge_payload: dict[str, object] = {}
        try:
            from src.research import (
                get_research_knowledge_store,
                knowledge_enabled,
                market_for_symbol,
            )

            if knowledge_enabled():
                knowledge = get_research_knowledge_store()
                stored = knowledge.store_document(
                    url=target_url,
                    content=full_text,
                    title=title,
                    published_at=published_at or None,
                    cached_status=("jina_cache" if _CACHED_MARKER in full_text else "network"),
                )
                normalized_subject = str(subject_key or "").strip().upper()
                if normalized_subject:
                    recorder = get_current_usage_recorder()
                    origin_id = (
                        str(getattr(recorder, "attempt_id", "") or "")
                        or str(getattr(recorder, "session_id", "") or "")
                        or f"read-url:{stored.document_ref}"
                    )
                    document = knowledge.document(stored.document_ref) or {}
                    knowledge.record_source_observation(
                        document_ref=stored.document_ref,
                        subject_key=normalized_subject,
                        source_kind=(
                            "official_filing"
                            if document.get("source_class") == "regulatory_filing"
                            else "news"
                        ),
                        origin_type="deep_research_web",
                        origin_id=origin_id,
                        market=market_for_symbol(normalized_subject),
                        provider_id="jina_reader",
                        verification_status=(
                            "historical_context"
                            if _CACHED_MARKER in full_text
                            else "live_retrieved"
                        ),
                        body_status="full_text",
                        metadata={"title": title, "url": target_url},
                    )
                knowledge_payload = {
                    "document_ref": stored.document_ref,
                    "canonical_url": stored.canonical_url,
                    "content_hash": stored.content_hash,
                    "published_at": published_at or None,
                    "chunk_catalog": stored.chunk_catalog,
                }
        except Exception as exc:
            # Knowledge capture is a shadow-write path. A local indexing issue
            # must not turn a successfully fetched source into a failed read.
            logger.warning("research knowledge document capture failed: %s", exc)

        if len(text) > _MAX_LENGTH:
            text = text[:_MAX_LENGTH] + (
                f"\n\n... (preview truncated, total {len(full_text)} chars; "
                "use read_research_document with document_ref/chunk_ref for the remainder)"
            )

        result = {
            "status": "ok",
            "title": title,
            "url": target_url,
            "content": text,
            "length": len(full_text),
            **knowledge_payload,
        }
        if _CACHED_MARKER in full_text:
            result["cached"] = True
        cached = bool(result.get("cached"))
        record_current_resource(
            provider="jina_reader",
            category="web",
            status="ok",
            elapsed_ms=int((time.perf_counter() - request_started) * 1000),
            cache_mode="cache_hit" if cached else ("cache_refresh" if no_cache else "network"),
            query={"url": target_url},
            network_request=True,
            cache_access=cached,
        )
        result = with_security_warnings(result, fields=("content",))
        return json.dumps(result, ensure_ascii=False)

    except requests.Timeout:
        record_current_resource(
            provider="jina_reader",
            category="web",
            status="error",
            elapsed_ms=int((time.perf_counter() - request_started) * 1000),
            cache_mode="network",
            query={"url": target_url},
            network_request=True,
            cache_access=False,
            metadata={"error_type": "timeout"},
        )
        return json.dumps({"status": "error", "error": f"Request timed out ({_TIMEOUT}s)"}, ensure_ascii=False)
    except Exception as exc:
        record_current_resource(
            provider="jina_reader",
            category="web",
            status="error",
            elapsed_ms=int((time.perf_counter() - request_started) * 1000),
            cache_mode="network",
            query={"url": target_url},
            network_request=True,
            cache_access=False,
            metadata={"error_type": type(exc).__name__},
        )
        logger.warning("read_url request failed: %s", exc)
        return json.dumps(
            {"status": "error", "error": f"remote reader request failed: {exc}"},
            ensure_ascii=False,
        )


class WebReaderTool(BaseTool):
    """Web reader tool."""

    name = "read_url"
    description = "Fetch web page content: provide a URL and receive the page as Markdown text. Useful for reading docs, articles, API references, etc."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL of the web page to read"},
            "no_cache": {"type": "boolean", "description": "Request a fresh (uncached) fetch", "default": False},
            "subject_key": {
                "type": "string",
                "description": "Optional normalized security symbol; attaches the opened page to that subject dossier.",
            },
        },
        "required": ["url"],
    }
    repeatable = True

    def __init__(
        self,
        default_session_id: str | None = None,
        event_callback: Callable[[str, dict], None] | None = None,
        session_store: Any | None = None,
    ) -> None:
        self.default_session_id = str(default_session_id or "")
        self.event_callback = event_callback
        self._session_store = session_store

    def _session_subject_key(self) -> str:
        if not self.default_session_id:
            return ""
        try:
            if self._session_store is None:
                from src.session.store import SessionStore

                self._session_store = SessionStore(
                    Path(__file__).resolve().parents[2] / "sessions"
                )
            session = self._session_store.get_session(self.default_session_id)
            research = dict((session.config or {}).get("research_session") or {})
            return str(
                research.get("resolved_symbol") or research.get("symbol") or ""
            ).strip().upper()
        except Exception:
            return ""

    def execute(self, **kwargs) -> str:
        """Fetch web page."""
        subject_key = str(
            kwargs.get("subject_key") or self._session_subject_key() or ""
        ).strip().upper()
        return read_url(
            kwargs["url"],
            no_cache=bool(kwargs.get("no_cache", False)),
            subject_key=subject_key,
        )
