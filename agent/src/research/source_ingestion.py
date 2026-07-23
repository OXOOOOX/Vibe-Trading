"""Unified, append-only ingestion for research sources collected by every workflow."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlsplit

from .knowledge import ResearchKnowledgeStore, get_research_knowledge_store


_OFFICIAL_HOSTS = (
    "sse.com.cn",
    "star.sse.com.cn",
    "szse.cn",
    "bse.cn",
    "cninfo.com.cn",
    "hkexnews.hk",
    "sec.gov",
    "99fund.com",
    "csindex.com.cn",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _official_url(value: str) -> bool:
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme.lower() in {"http", "https"} and any(
        host == domain or host.endswith(f".{domain}") for domain in _OFFICIAL_HOSTS
    )


def market_for_symbol(symbol: str) -> str:
    normalized = str(symbol or "").strip().upper()
    if normalized.endswith((".SH", ".SZ", ".BJ")):
        return "CN"
    if normalized.endswith(".HK") or (normalized.isdigit() and len(normalized) == 5):
        return "HK"
    return "US"


@dataclass(frozen=True)
class CollectedSource:
    subject_key: str
    source_kind: str
    provider_id: str
    publisher: str
    title: str
    source_locator: str
    content: str
    provider_record_id: str = ""
    market: str = ""
    published_at: str | None = None
    retrieved_at: str | None = None
    verification_status: str = "source_recorded"
    body_status: str = "metadata_only"
    source_class: str = "mainstream_media"
    metadata: dict[str, Any] = field(default_factory=dict)


class SourceIngestionService:
    """Normalize provider payloads into the existing content-addressed knowledge store."""

    def __init__(self, store: ResearchKnowledgeStore | None = None) -> None:
        self.store = store or get_research_knowledge_store()

    def ingest(
        self,
        source: CollectedSource,
        *,
        origin_type: str,
        origin_id: str,
    ) -> dict[str, Any]:
        locator = str(source.source_locator or "").strip()
        record_id = str(source.provider_record_id or "").strip()
        if not locator:
            digest = hashlib.sha256(
                json.dumps(
                    [source.subject_key, source.source_kind, source.title, source.published_at],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:24]
            record_id = record_id or digest
            locator = (
                f"provider://{source.provider_id or 'unknown'}/"
                f"{source.source_kind}/{source.subject_key.upper()}/{record_id}"
            )
        body = str(source.content or "").strip()
        if not body:
            body = json.dumps(
                {
                    "title": source.title,
                    "publisher": source.publisher,
                    "published_at": source.published_at,
                    "metadata": source.metadata,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        stored = self.store.store_document(
            url=locator,
            content=body,
            title=source.title,
            publisher=source.publisher or source.provider_id,
            source_class=source.source_class,
            published_at=source.published_at,
            cached_status=("network" if source.body_status == "full_text" else "provider_snapshot"),
            aliases=(source.subject_key, source.provider_id, source.source_kind),
        )
        requested_status = source.verification_status
        # A content-addressed document may have been stored first through a
        # provider alias.  Authenticate the observation against the locator
        # supplied for this retrieval, not the canonical URL chosen by an
        # earlier deduplicated observation.
        if requested_status == "official_primary" and not _official_url(locator):
            requested_status = "source_recorded"
        observation = self.store.record_source_observation(
            document_ref=stored.document_ref,
            subject_key=source.subject_key,
            market=source.market or market_for_symbol(source.subject_key),
            source_kind=source.source_kind,
            provider_id=source.provider_id,
            provider_record_id=record_id or stored.document_ref,
            verification_status=requested_status,
            body_status=source.body_status,
            origin_type=origin_type,
            origin_id=origin_id,
            observed_at=source.retrieved_at or _utc_now(),
            metadata={**source.metadata, "title": source.title},
            observed_source_locator=locator,
            observed_source_class=source.source_class,
        )
        return {
            **observation,
            "content_hash": stored.content_hash,
            "source_locator": stored.canonical_url,
        }

    def ingest_provider_documents(
        self,
        *,
        kind: str,
        symbol: str,
        documents: Iterable[dict[str, Any]],
        provider_id: str,
        origin_type: str,
        origin_id: str,
    ) -> list[dict[str, Any]]:
        source_kind = {
            "fundamental": "structured_financial",
            "report": "broker_research",
            "news": "news",
            "filing": "official_filing",
        }.get(kind, kind or "other")
        result: list[dict[str, Any]] = []
        for raw in documents:
            item = dict(raw)
            title = str(item.get("title") or item.get("name") or "Untitled").strip()
            published_at = str(
                item.get("published_at")
                or item.get("publish_date")
                or item.get("published")
                or item.get("publication_date")
                or item.get("date")
                or ""
            ) or None
            locator = str(
                item.get("url")
                or item.get("link")
                or item.get("source_url")
                or item.get("canonical_url")
                or item.get("article_url")
                or item.get("web_url")
                or item.get("href")
                or ""
            ).strip()
            retrieved_at = str(
                item.get("retrieved_at")
                or item.get("fetched_at")
                or ""
            ) or _utc_now()
            is_official = _official_url(locator)
            payload_text = json.dumps(item, ensure_ascii=False, indent=2, sort_keys=True, default=str)
            body_status = "structured_payload" if kind == "fundamental" else (
                "excerpt" if item.get("content") or item.get("body") or item.get("snippet") or item.get("summary") else "metadata_only"
            )
            source_class = (
                "regulatory_filing"
                if is_official
                else "broker_research"
                if kind == "report"
                else "mainstream_media"
            )
            provider_record_id = str(
                item.get("id")
                or item.get("announcement_id")
                or item.get("accession_number")
                or locator
                or hashlib.sha256(
                    f"{symbol}|{kind}|{title}|{published_at}".encode("utf-8")
                ).hexdigest()[:24]
            )
            result.append(
                self.ingest(
                    CollectedSource(
                        subject_key=symbol,
                        market=market_for_symbol(symbol),
                        source_kind=source_kind,
                        provider_id=provider_id or "unknown",
                        provider_record_id=provider_record_id,
                        publisher=str(
                            item.get("publisher")
                            or item.get("source")
                            or item.get("brokerage")
                            or provider_id
                            or "unknown"
                        ),
                        title=title,
                        source_locator=locator,
                        content=payload_text,
                        published_at=published_at,
                        retrieved_at=retrieved_at,
                        verification_status=(
                            "official_primary"
                            if is_official and body_status == "full_text"
                            else "live_retrieved"
                            if locator and body_status != "metadata_only"
                            else "source_recorded"
                        ),
                        body_status=body_status,
                        source_class=source_class,
                        metadata={
                            "provider_payload": True,
                            "cache_kind": kind,
                            "summary": item.get("summary") or item.get("snippet"),
                            "analyst": item.get("analyst") or item.get("analysts"),
                            "association_scope": (
                                item.get("association_scope")
                                or item.get("relation_scope")
                                or ("direct_subject" if kind == "report" else None)
                            ),
                            "related_symbol": item.get("related_symbol") or item.get("symbol"),
                        },
                    ),
                    origin_type=origin_type,
                    origin_id=origin_id,
                )
            )
        return result

    def ingest_data_manifest(
        self,
        manifest: dict[str, Any],
        *,
        origin_type: str,
        origin_id: str,
    ) -> dict[str, int]:
        """Replay frozen data-context research documents into run observations."""

        counts = {"documents": 0, "contexts": 0}
        contexts = manifest.get("contexts")
        if not isinstance(contexts, list):
            contexts = [manifest.get("context")] if isinstance(manifest.get("context"), dict) else []
        for context in contexts:
            if not isinstance(context, dict):
                continue
            counts["contexts"] += 1
            research = context.get("research")
            if not isinstance(research, dict):
                continue
            for domain_name, kind in (
                ("fundamentals", "fundamental"),
                ("news", "news"),
                ("reports", "report"),
                ("official_filings", "filing"),
            ):
                domain = research.get(domain_name)
                if not isinstance(domain, dict):
                    continue
                items = domain.get("items")
                if not isinstance(items, dict):
                    continue
                for symbol, payload in items.items():
                    if not isinstance(payload, dict):
                        continue
                    documents: list[dict[str, Any]] = []
                    for raw in payload.get("documents") or payload.get("sources") or []:
                        if not isinstance(raw, dict):
                            continue
                        item = dict(raw.get("payload") or {}) if isinstance(raw.get("payload"), dict) else dict(raw)
                        for key in ("title", "published_at", "source", "url", "snippet"):
                            if key not in item and raw.get(key) is not None:
                                item[key] = raw.get(key)
                        documents.append(item)
                    provider = str(payload.get("source") or "")
                    if not provider and payload.get("documents"):
                        first = next(
                            (row for row in payload.get("documents") or [] if isinstance(row, dict)),
                            {},
                        )
                        provider = str(first.get("source") or "")
                    archived = self.ingest_provider_documents(
                        kind=kind,
                        symbol=str(symbol),
                        documents=documents,
                        provider_id=provider or domain_name,
                        origin_type=origin_type,
                        origin_id=origin_id,
                    )
                    counts["documents"] += len(archived)
        return counts


_service: SourceIngestionService | None = None


def get_source_ingestion_service() -> SourceIngestionService:
    global _service
    if _service is None:
        _service = SourceIngestionService()
    return _service


__all__ = [
    "CollectedSource",
    "SourceIngestionService",
    "get_source_ingestion_service",
    "market_for_symbol",
]
