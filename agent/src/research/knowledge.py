"""Versioned, source-grounded research knowledge storage.

The store deliberately shares the existing ``research_cache.sqlite3`` file,
but keeps large source bodies in a content-addressed object directory.  The
schema is append-only and can be disabled without changing the legacy cache
read path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_TRACKING_QUERY_KEYS = {
    "fbclid", "gclid", "mc_cid", "mc_eid", "spm", "from", "source",
}
_SOURCE_CLASS_STRENGTH = {
    "regulatory_filing": "A",
    "company_disclosure": "A",
    "official_statistics": "A",
    "industry_association": "B",
    "broker_research": "B",
    "commercial_research": "C",
    "mainstream_media": "C",
    "research_session": "D",
}
_FRESHNESS_DAYS = {
    "consensus": 7,
    "competition": 180,
    "tam": 365,
}
_DEFAULT_DOMAINS = (
    "identity_market",
    "financial_statements",
    "business_position",
    "company_actions",
    "consensus",
    "industry_tam_competition",
    "catalysts_risks",
    "prior_conditions",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", "off"}


def knowledge_enabled() -> bool:
    return _flag("VIBE_TRADING_RESEARCH_KNOWLEDGE_ENABLED", "1")


def history_reuse_enabled() -> bool:
    return _flag("VIBE_TRADING_RESEARCH_HISTORY_REUSE_ENABLED", "0")


def normalize_url(value: str) -> str:
    """Return a deterministic URL without fragments or tracking parameters."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return raw
    scheme = parsed.scheme.lower()
    host = parsed.hostname.rstrip(".").lower()
    port = parsed.port
    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    query = [
        (key, val)
        for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_QUERY_KEYS
    ]
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query)), ""))


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def _chinese_bigrams(text: str) -> str:
    runs = re.findall(r"[\u3400-\u9fff]+", str(text or ""))
    tokens: list[str] = []
    for run in runs:
        tokens.extend(run[index:index + 2] for index in range(max(0, len(run) - 1)))
    return " ".join(tokens)


def _search_text(text: str, aliases: Iterable[str] = ()) -> str:
    value = " ".join([str(text or ""), *[str(item or "") for item in aliases]])
    bigrams = _chinese_bigrams(value)
    return f"{value}\n{bigrams}" if bigrams else value


def _fts_query(query: str) -> str:
    parts = re.findall(r"[A-Za-z0-9_.-]+|[\u3400-\u9fff]+", str(query or ""))
    tokens: list[str] = []
    for part in parts:
        if re.fullmatch(r"[\u3400-\u9fff]+", part) and len(part) > 1:
            tokens.extend(part[index:index + 2] for index in range(len(part) - 1))
        else:
            tokens.append(part)
    escaped = [f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens if token]
    return " OR ".join(escaped)


def _source_class(url: str, publisher: str = "") -> str:
    host = (urlsplit(url).hostname or "").lower()
    label = publisher.casefold()
    if any(name in host for name in ("sse.com.cn", "szse.cn", "bse.cn", "cninfo.com.cn", "hkexnews.hk", "sec.gov")):
        return "regulatory_filing"
    if any(name in host for name in ("stats.gov", "gov.cn")):
        return "official_statistics"
    if any(token in label for token in ("证券", "research", "broker")):
        return "broker_research"
    if any(token in label for token in ("协会", "association")):
        return "industry_association"
    return "mainstream_media"


def _publisher_group(url: str, publisher: str) -> str:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    return host or re.sub(r"\s+", "_", publisher.strip().casefold()) or "unknown"


def _parse_time(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            result = datetime.strptime(raw[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _chunks(markdown: str, content_hash: str, *, max_chars: int = 1800) -> list[dict[str, str]]:
    """Split Markdown by headings/paragraphs while retaining stable locators."""

    headings: list[str] = []
    paragraphs: list[tuple[str, str]] = []
    buffer: list[str] = []

    def flush() -> None:
        text = "\n".join(buffer).strip()
        if text:
            paragraphs.append((" > ".join(headings), text))
        buffer.clear()

    for line in str(markdown or "").splitlines():
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            flush()
            level = len(heading.group(1))
            headings[:] = headings[: level - 1]
            headings.append(heading.group(2).strip())
            continue
        if not line.strip():
            flush()
        else:
            buffer.append(line.rstrip())
    flush()

    result: list[dict[str, str]] = []
    ordinal = 0
    for heading_path, paragraph in paragraphs or [("", str(markdown or ""))]:
        remaining = paragraph
        while remaining:
            if len(remaining) <= max_chars:
                piece, remaining = remaining, ""
            else:
                cut = max(remaining.rfind("。", 0, max_chars), remaining.rfind("\n", 0, max_chars), remaining.rfind(" ", 0, max_chars))
                cut = cut + 1 if cut >= max_chars // 2 else max_chars
                piece, remaining = remaining[:cut], remaining[cut:]
            text = piece.strip()
            if not text:
                continue
            ordinal += 1
            text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            result.append({
                "chunk_ref": _stable_id("chunk", content_hash, ordinal, text_hash),
                "heading_path": heading_path,
                "page_or_paragraph_locator": f"paragraph:{ordinal}",
                "text_hash": text_hash,
                "text": text,
            })
    return result


@dataclass(frozen=True)
class StoredDocument:
    document_ref: str
    canonical_url: str
    content_hash: str
    object_path: str
    cached: bool
    chunk_catalog: list[dict[str, str]]


class ResearchKnowledgeStore:
    """SQLite metadata/FTS plus content-addressed research bodies."""

    def __init__(self, path: Path | None = None, object_dir: Path | None = None) -> None:
        self.path = path or Path(os.getenv("VIBE_TRADING_RESEARCH_CACHE_DB", "~/.vibe-trading/cache/research_cache.sqlite3")).expanduser()
        self.object_dir = object_dir or Path(os.getenv("VIBE_TRADING_RESEARCH_OBJECT_DIR", str(self.path.parent / "research_objects"))).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.object_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _backup_before_migration(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        backup = self.path.with_suffix(self.path.suffix + ".pre-knowledge-v1.bak")
        if backup.exists():
            return
        source = sqlite3.connect(self.path)
        target = sqlite3.connect(backup)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

    def initialize(self) -> None:
        with self._lock:
            with self.connect() as probe:
                migrated = probe.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_documents'"
                ).fetchone()
            if not migrated:
                self._backup_before_migration()
            with self.connect() as conn:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS research_knowledge_schema (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS source_documents (
                        document_ref TEXT PRIMARY KEY,
                        canonical_url TEXT NOT NULL,
                        publisher TEXT NOT NULL DEFAULT '',
                        source_class TEXT NOT NULL,
                        independence_group TEXT NOT NULL,
                        published_at TEXT,
                        retrieved_at TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        object_path TEXT NOT NULL,
                        cached_status TEXT NOT NULL DEFAULT 'network',
                        superseded_by TEXT,
                        title TEXT NOT NULL DEFAULT '',
                        UNIQUE(canonical_url, content_hash)
                    );
                    CREATE INDEX IF NOT EXISTS idx_source_documents_url ON source_documents(canonical_url, retrieved_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_source_documents_hash ON source_documents(content_hash);
                    CREATE TABLE IF NOT EXISTS source_chunks (
                        chunk_ref TEXT PRIMARY KEY,
                        document_ref TEXT NOT NULL REFERENCES source_documents(document_ref),
                        heading_path TEXT NOT NULL DEFAULT '',
                        page_or_paragraph_locator TEXT NOT NULL,
                        text_hash TEXT NOT NULL,
                        search_text TEXT NOT NULL,
                        UNIQUE(document_ref, text_hash, page_or_paragraph_locator)
                    );
                    CREATE VIRTUAL TABLE IF NOT EXISTS source_chunks_fts USING fts5(
                        chunk_ref UNINDEXED, document_ref UNINDEXED, search_text
                    );
                    CREATE TABLE IF NOT EXISTS evidence_records (
                        evidence_id TEXT PRIMARY KEY,
                        document_ref TEXT NOT NULL REFERENCES source_documents(document_ref),
                        chunk_refs_json TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        domain TEXT NOT NULL,
                        scope_key TEXT NOT NULL DEFAULT '',
                        source_strength TEXT NOT NULL,
                        valid_from TEXT,
                        valid_until TEXT,
                        status TEXT NOT NULL,
                        summary TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_evidence_symbol_domain ON evidence_records(symbol, domain, status);
                    CREATE TABLE IF NOT EXISTS fact_records (
                        fact_id TEXT PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        metric TEXT NOT NULL,
                        value TEXT,
                        unit TEXT NOT NULL DEFAULT '',
                        currency TEXT NOT NULL DEFAULT '',
                        period TEXT NOT NULL DEFAULT '',
                        scope_key TEXT NOT NULL DEFAULT '',
                        formula TEXT,
                        input_fact_ids_json TEXT NOT NULL,
                        evidence_ids_json TEXT NOT NULL,
                        validation_status TEXT NOT NULL,
                        superseded_by TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_fact_comparison ON fact_records(symbol, metric, period, scope_key, unit, currency);
                    CREATE TABLE IF NOT EXISTS claim_records (
                        claim_id TEXT PRIMARY KEY,
                        origin_type TEXT NOT NULL,
                        origin_id TEXT NOT NULL,
                        section_id TEXT,
                        claim_type TEXT NOT NULL,
                        text TEXT NOT NULL,
                        fact_ids_json TEXT NOT NULL,
                        evidence_ids_json TEXT NOT NULL,
                        claim_status TEXT NOT NULL,
                        superseded_by TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE VIRTUAL TABLE IF NOT EXISTS claim_records_fts USING fts5(
                        claim_id UNINDEXED, origin_id UNINDEXED, search_text
                    );
                    CREATE TABLE IF NOT EXISTS report_knowledge_links (
                        report_id TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        evidence_ids_json TEXT NOT NULL,
                        fact_ids_json TEXT NOT NULL,
                        claim_ids_json TEXT NOT NULL,
                        coverage_snapshot_id TEXT,
                        PRIMARY KEY(report_id, revision)
                    );
                    CREATE TABLE IF NOT EXISTS fact_conflicts (
                        conflict_id TEXT PRIMARY KEY,
                        comparison_key TEXT NOT NULL,
                        fact_ids_json TEXT NOT NULL,
                        conflict_type TEXT NOT NULL,
                        resolution_status TEXT NOT NULL,
                        resolution_reason TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_fact_conflicts_key ON fact_conflicts(comparison_key, resolution_status);
                    CREATE TABLE IF NOT EXISTS research_coverage_snapshots (
                        coverage_snapshot_id TEXT PRIMARY KEY,
                        report_id TEXT,
                        symbol TEXT NOT NULL,
                        profile TEXT NOT NULL,
                        as_of TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS research_deltas (
                        report_id TEXT PRIMARY KEY,
                        base_report_id TEXT,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    INSERT OR IGNORE INTO research_knowledge_schema(version, applied_at) VALUES (1, datetime('now'));
                    """
                )

    def store_document(
        self,
        *,
        url: str,
        content: str,
        title: str = "",
        publisher: str = "",
        source_class: str | None = None,
        published_at: str | None = None,
        cached_status: str = "network",
        aliases: Iterable[str] = (),
    ) -> StoredDocument:
        canonical = normalize_url(url)
        body = str(content or "")
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        document_ref = _stable_id("doc", canonical, content_hash)
        object_path = self.object_dir / f"{content_hash}.md"
        if not object_path.exists():
            tmp = object_path.with_suffix(".md.tmp")
            tmp.write_text(body, encoding="utf-8", newline="")
            tmp.replace(object_path)
        classification = source_class or _source_class(canonical, publisher)
        independence = _publisher_group(canonical, publisher)
        chunk_rows = _chunks(body, content_hash)
        retrieved_at = _utc_now()
        with self.connect() as conn:
            prior = conn.execute(
                "SELECT document_ref, content_hash FROM source_documents WHERE canonical_url=? ORDER BY retrieved_at DESC LIMIT 1",
                (canonical,),
            ).fetchone()
            conn.execute(
                "INSERT OR IGNORE INTO source_documents(document_ref, canonical_url, publisher, source_class, independence_group, published_at, retrieved_at, content_hash, object_path, cached_status, title) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (document_ref, canonical, publisher, classification, independence, published_at, retrieved_at, content_hash, str(object_path), cached_status, title),
            )
            if prior and prior["content_hash"] != content_hash:
                conn.execute("UPDATE source_documents SET superseded_by=? WHERE document_ref=? AND superseded_by IS NULL", (document_ref, prior["document_ref"]))
            for item in chunk_rows:
                search_value = _search_text(f"{title}\n{item['heading_path']}\n{item['text']}", aliases)
                conn.execute(
                    "INSERT OR IGNORE INTO source_chunks(chunk_ref, document_ref, heading_path, page_or_paragraph_locator, text_hash, search_text) VALUES (?, ?, ?, ?, ?, ?)",
                    (item["chunk_ref"], document_ref, item["heading_path"], item["page_or_paragraph_locator"], item["text_hash"], search_value),
                )
                conn.execute("DELETE FROM source_chunks_fts WHERE chunk_ref=?", (item["chunk_ref"],))
                conn.execute("INSERT INTO source_chunks_fts(chunk_ref, document_ref, search_text) VALUES (?, ?, ?)", (item["chunk_ref"], document_ref, search_value))
        return StoredDocument(
            document_ref=document_ref,
            canonical_url=canonical,
            content_hash=content_hash,
            object_path=str(object_path),
            cached=cached_status != "network",
            chunk_catalog=[{key: item[key] for key in ("chunk_ref", "heading_path", "page_or_paragraph_locator")} for item in chunk_rows],
        )

    def document(self, document_ref: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM source_documents WHERE document_ref=?", (document_ref,)).fetchone()
        return dict(row) if row else None

    def read_document(self, document_ref: str, *, query: str = "", chunk_refs: Iterable[str] = (), limit: int = 8) -> dict[str, Any]:
        document = self.document(document_ref)
        if not document:
            raise KeyError(document_ref)
        refs = [str(item) for item in chunk_refs if str(item)]
        with self.connect() as conn:
            if refs:
                placeholders = ",".join("?" for _ in refs)
                rows = conn.execute(
                    f"SELECT * FROM source_chunks WHERE document_ref=? AND chunk_ref IN ({placeholders}) ORDER BY page_or_paragraph_locator",
                    (document_ref, *refs),
                ).fetchall()
            elif query.strip() and _fts_query(query):
                rows = conn.execute(
                    "SELECT c.*, bm25(source_chunks_fts) AS score FROM source_chunks_fts JOIN source_chunks c USING(chunk_ref) WHERE source_chunks_fts MATCH ? AND c.document_ref=? ORDER BY score LIMIT ?",
                    (_fts_query(query), document_ref, max(1, min(limit, 30))),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM source_chunks WHERE document_ref=? ORDER BY page_or_paragraph_locator LIMIT ?",
                    (document_ref, max(1, min(limit, 30))),
                ).fetchall()
        object_path = Path(str(document["object_path"]))
        full = object_path.read_text(encoding="utf-8") if object_path.exists() else ""
        by_hash = {hashlib.sha256(item.encode("utf-8")).hexdigest(): item for item in self._all_chunk_texts(full)}
        chunks = []
        for raw in rows:
            item = dict(raw)
            item["text"] = by_hash.get(str(item["text_hash"]), "")
            item.pop("search_text", None)
            chunks.append(item)
        return {"document": document, "chunks": chunks, "total_length": len(full)}

    @staticmethod
    def _all_chunk_texts(content: str) -> list[str]:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return [item["text"] for item in _chunks(content, digest)]

    def create_coverage_plan(self, *, symbol: str, profile: str, as_of: str, report_id: str | None = None, prior_report_id: str | None = None) -> dict[str, Any]:
        domains = []
        for domain in _DEFAULT_DOMAINS:
            minimum = 2 if domain == "industry_tam_competition" else 1
            domains.append({
                "domain": domain,
                "required": domain not in {"consensus"},
                "preferred_source_classes": ["regulatory_filing", "company_disclosure", "official_statistics", "industry_association", "broker_research"],
                "minimum_independent_sources": minimum,
                "freshness_policy": "live_first" if domain in {"identity_market", "company_actions", "consensus"} else "version_or_ttl",
                "status": "pending",
                "unresolved_questions": [],
            })
        payload = {
            "symbol": symbol.upper(),
            "profile": profile,
            "as_of": as_of,
            "domains": domains,
            "acquisition_budget": "adaptive_coverage",
            "prior_report_id": prior_report_id,
        }
        snapshot_id = _stable_id("coverage", report_id or symbol, as_of, payload)
        payload["coverage_snapshot_id"] = snapshot_id
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO research_coverage_snapshots(coverage_snapshot_id, report_id, symbol, profile, as_of, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (snapshot_id, report_id, symbol.upper(), profile, as_of, json.dumps(payload, ensure_ascii=False), _utc_now()),
            )
        return payload

    def coverage(self, report_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload_json FROM research_coverage_snapshots WHERE report_id=? ORDER BY created_at DESC LIMIT 1", (report_id,)).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def register_bundle(self, bundle: dict[str, Any]) -> dict[str, Any]:
        evidence_rows = [dict(item) for item in bundle.get("evidence") or [] if isinstance(item, dict)]
        fact_rows = [dict(item) for item in bundle.get("facts") or [] if isinstance(item, dict)]
        conflicts: list[dict[str, Any]] = []
        with self.connect() as conn:
            for item in evidence_rows:
                document_ref = str((item.get("metadata") or {}).get("document_ref") or "")
                if not document_ref or not conn.execute("SELECT 1 FROM source_documents WHERE document_ref=?", (document_ref,)).fetchone():
                    continue
                metadata = dict(item.get("metadata") or {})
                conn.execute(
                    "INSERT OR REPLACE INTO evidence_records(evidence_id, document_ref, chunk_refs_json, symbol, domain, scope_key, source_strength, valid_from, valid_until, status, summary, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item["evidence_id"], document_ref, json.dumps(metadata.get("chunk_refs") or []), str(item.get("symbol") or "").upper(), str(item.get("domain") or "other"), str(metadata.get("scope_key") or ""), str(metadata.get("source_strength") or "D"), item.get("published_at"), metadata.get("valid_until"), str(item.get("status") or "verified"), str(item.get("summary") or ""), _utc_now(),
                    ),
                )
            for item in fact_rows:
                fact_id = str(item.get("fact_id") or "")
                if not fact_id:
                    continue
                metadata = dict(item.get("metadata") or {})
                conn.execute(
                    "INSERT OR REPLACE INTO fact_records(fact_id, symbol, metric, value, unit, currency, period, scope_key, formula, input_fact_ids_json, evidence_ids_json, validation_status, superseded_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        fact_id, str(item.get("symbol") or "").upper(), str(item.get("metric") or ""), None if item.get("value") is None else str(item.get("value")), str(item.get("unit") or ""), str(metadata.get("currency") or item.get("currency") or ""), str(item.get("period") or ""), str(metadata.get("scope_key") or metadata.get("scope") or ""), item.get("formula"), json.dumps(item.get("input_fact_ids") or []), json.dumps(item.get("evidence_ids") or []), str(item.get("validation_status") or "pass"), item.get("superseded_by"), _utc_now(),
                    ),
                )
                conflict = self._detect_conflict(conn, fact_id)
                if conflict:
                    conflicts.append(conflict)
        return {"evidence_count": len(evidence_rows), "fact_count": len(fact_rows), "conflicts": conflicts}

    def _detect_conflict(self, conn: sqlite3.Connection, fact_id: str) -> dict[str, Any] | None:
        current = conn.execute("SELECT * FROM fact_records WHERE fact_id=?", (fact_id,)).fetchone()
        if not current or current["value"] is None:
            return None
        comparison = (current["symbol"], current["metric"], current["period"], current["scope_key"], current["unit"], current["currency"])
        peers = conn.execute(
            "SELECT * FROM fact_records WHERE symbol=? AND metric=? AND period=? AND scope_key=? AND unit=? AND currency=? AND superseded_by IS NULL AND fact_id<>?",
            (*comparison, fact_id),
        ).fetchall()
        for peer in peers:
            if self._values_consistent(current["value"], peer["value"]):
                continue
            comparison_key = "|".join(str(item) for item in comparison)
            ids = sorted({fact_id, str(peer["fact_id"])})
            conflict_id = _stable_id("conflict", comparison_key, ids)
            payload = {
                "conflict_id": conflict_id,
                "comparison_key": comparison_key,
                "fact_ids": ids,
                "conflict_type": "value_conflict",
                "resolution_status": "needs_third_source",
                "resolution_reason": "same scope and period contain materially different values",
            }
            conn.execute(
                "INSERT OR REPLACE INTO fact_conflicts(conflict_id, comparison_key, fact_ids_json, conflict_type, resolution_status, resolution_reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (conflict_id, comparison_key, json.dumps(ids), payload["conflict_type"], payload["resolution_status"], payload["resolution_reason"], _utc_now()),
            )
            return payload
        return None

    @staticmethod
    def _values_consistent(left: Any, right: Any) -> bool:
        try:
            a, b = Decimal(str(left)), Decimal(str(right))
        except InvalidOperation:
            return str(left).strip().casefold() == str(right).strip().casefold()
        tolerance = max(abs(a), abs(b), Decimal("1")) * Decimal("0.01")
        return abs(a - b) <= tolerance

    def search(self, *, query: str = "", symbol: str = "", domains: Iterable[str] = (), metrics: Iterable[str] = (), limit: int = 20, as_of: str | None = None) -> dict[str, Any]:
        capped = max(1, min(int(limit), 100))
        normalized_symbol = symbol.strip().upper()
        domain_values = [str(item) for item in domains if str(item)]
        metric_values = [str(item) for item in metrics if str(item)]
        with self.connect() as conn:
            fact_sql = "SELECT * FROM fact_records WHERE 1=1"
            params: list[Any] = []
            if normalized_symbol:
                fact_sql += " AND symbol=?"
                params.append(normalized_symbol)
            if metric_values:
                fact_sql += f" AND metric IN ({','.join('?' for _ in metric_values)})"
                params.extend(metric_values)
            fact_sql += " ORDER BY period DESC, created_at DESC LIMIT ?"
            params.append(capped)
            facts = [self._fact_payload(dict(row), as_of=as_of) for row in conn.execute(fact_sql, params).fetchall()]

            evidence_sql = "SELECT e.*, d.canonical_url, d.publisher, d.source_class, d.independence_group, d.published_at AS document_published_at FROM evidence_records e JOIN source_documents d USING(document_ref) WHERE 1=1"
            evidence_params: list[Any] = []
            if normalized_symbol:
                evidence_sql += " AND e.symbol=?"
                evidence_params.append(normalized_symbol)
            if domain_values:
                evidence_sql += f" AND e.domain IN ({','.join('?' for _ in domain_values)})"
                evidence_params.extend(domain_values)
            evidence_sql += " ORDER BY e.created_at DESC LIMIT ?"
            evidence_params.append(capped)
            evidence = [self._decode_json_columns(dict(row), ("chunk_refs_json",)) for row in conn.execute(evidence_sql, evidence_params).fetchall()]

            chunks: list[dict[str, Any]] = []
            fts = _fts_query(query)
            if fts:
                rows = conn.execute(
                    "SELECT c.chunk_ref, c.document_ref, c.heading_path, c.page_or_paragraph_locator, d.canonical_url, d.publisher, d.source_class, bm25(source_chunks_fts) AS score FROM source_chunks_fts JOIN source_chunks c USING(chunk_ref) JOIN source_documents d ON d.document_ref=c.document_ref WHERE source_chunks_fts MATCH ? ORDER BY score LIMIT ?",
                    (fts, capped),
                ).fetchall()
                chunks = [dict(row) for row in rows]
            claims_sql = "SELECT * FROM claim_records WHERE claim_status NOT IN ('rejected_prior')"
            claim_params: list[Any] = []
            if fts:
                claims_sql = "SELECT c.* FROM claim_records_fts f JOIN claim_records c USING(claim_id) WHERE claim_records_fts MATCH ? AND c.claim_status NOT IN ('rejected_prior')"
                claim_params.append(fts)
            claims_sql += " ORDER BY c.created_at DESC LIMIT ?" if fts else " ORDER BY created_at DESC LIMIT ?"
            claim_params.append(capped)
            claims = [self._decode_json_columns(dict(row), ("fact_ids_json", "evidence_ids_json")) for row in conn.execute(claims_sql, claim_params).fetchall()]
        return {"facts": facts, "evidence": evidence, "prior_claims": claims, "chunks": chunks}

    def _fact_payload(self, item: dict[str, Any], *, as_of: str | None = None) -> dict[str, Any]:
        result = self._decode_json_columns(item, ("input_fact_ids_json", "evidence_ids_json"))
        result["freshness_status"] = self._freshness_status(result, as_of=as_of)
        return result

    @staticmethod
    def _decode_json_columns(item: dict[str, Any], names: Iterable[str]) -> dict[str, Any]:
        result = dict(item)
        for name in names:
            raw = result.pop(name, "[]")
            try:
                result[name.removesuffix("_json")] = json.loads(raw or "[]")
            except json.JSONDecodeError:
                result[name.removesuffix("_json")] = []
        return result

    def _freshness_status(self, fact: dict[str, Any], *, as_of: str | None = None) -> str:
        if fact.get("superseded_by"):
            return "superseded"
        domain = str(fact.get("metric") or "").casefold()
        ttl = next((days for key, days in _FRESHNESS_DAYS.items() if key in domain), None)
        if ttl is None:
            return "valid"
        timestamp = _parse_time(fact.get("created_at"))
        reference = _parse_time(as_of) or datetime.now(timezone.utc)
        return "stale" if timestamp and reference - timestamp > timedelta(days=ttl) else "valid"

    def link_report(self, *, report_id: str, revision: int, symbol: str, quality_status: str, evidence: list[dict[str, Any]], facts: list[dict[str, Any]], claims: list[dict[str, Any]], coverage_snapshot_id: str | None = None, base_report_id: str | None = None) -> dict[str, Any]:
        self.register_bundle({"evidence": evidence, "facts": facts})
        claim_ids: list[str] = []
        with self.connect() as conn:
            for raw in claims:
                item = dict(raw)
                claim_id = str(item.get("claim_id") or _stable_id("claim", report_id, item.get("section_id"), item.get("text")))
                claim_status = "rejected_prior" if quality_status == "failed_validation" else "prior_claim"
                conn.execute(
                    "INSERT OR REPLACE INTO claim_records(claim_id, origin_type, origin_id, section_id, claim_type, text, fact_ids_json, evidence_ids_json, claim_status, superseded_by, created_at) VALUES (?, 'report', ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                    (claim_id, report_id, item.get("section_id"), str(item.get("claim_type") or "opinion"), str(item.get("text") or ""), json.dumps(item.get("fact_ids") or []), json.dumps(item.get("evidence_ids") or []), claim_status, _utc_now()),
                )
                conn.execute("DELETE FROM claim_records_fts WHERE claim_id=?", (claim_id,))
                conn.execute("INSERT INTO claim_records_fts(claim_id, origin_id, search_text) VALUES (?, ?, ?)", (claim_id, report_id, _search_text(str(item.get("text") or ""), (symbol,))))
                claim_ids.append(claim_id)
            evidence_ids = sorted({str(item.get("evidence_id")) for item in evidence if item.get("evidence_id")})
            fact_ids = sorted({str(item.get("fact_id")) for item in facts if item.get("fact_id")})
            conn.execute(
                "INSERT OR REPLACE INTO report_knowledge_links(report_id, revision, evidence_ids_json, fact_ids_json, claim_ids_json, coverage_snapshot_id) VALUES (?, ?, ?, ?, ?, ?)",
                (report_id, int(revision), json.dumps(evidence_ids), json.dumps(fact_ids), json.dumps(claim_ids), coverage_snapshot_id),
            )
        delta = self.compute_delta(report_id=report_id, base_report_id=base_report_id)
        return delta

    def compute_delta(self, *, report_id: str, base_report_id: str | None) -> dict[str, Any]:
        current = self._report_facts(report_id)
        previous = self._report_facts(base_report_id) if base_report_id else []
        old_by_key = {self._comparison_key(item): item for item in previous}
        new_by_key = {self._comparison_key(item): item for item in current}
        result: dict[str, Any] = {"base_report_id": base_report_id, "added": [], "updated": [], "confirmed": [], "superseded": [], "contradicted": [], "stale": [], "still_unverified": []}
        for key, item in new_by_key.items():
            prior = old_by_key.get(key)
            if prior is None:
                result["added"].append(self._delta_item(item))
            elif self._values_consistent(prior.get("value"), item.get("value")):
                result["confirmed"].append(self._delta_item(item))
            else:
                result["updated"].append({"before": self._delta_item(prior), "after": self._delta_item(item)})
        for key, item in old_by_key.items():
            if key not in new_by_key:
                result["stale"].append(self._delta_item(item))
        with self.connect() as conn:
            conflicts = conn.execute("SELECT * FROM fact_conflicts WHERE resolution_status!='resolved' AND comparison_key LIKE ?", (f"%{self._report_symbol(report_id)}%",)).fetchall()
            result["contradicted"] = [self._decode_json_columns(dict(row), ("fact_ids_json",)) for row in conflicts]
            conn.execute("INSERT OR REPLACE INTO research_deltas(report_id, base_report_id, payload_json, created_at) VALUES (?, ?, ?, ?)", (report_id, base_report_id, json.dumps(result, ensure_ascii=False), _utc_now()))
        return result

    def preview_delta(self, facts: list[dict[str, Any]], *, base_report_id: str | None) -> dict[str, Any]:
        """Compare an unpublished ledger with an immutable prior report."""

        previous = self._report_facts(base_report_id) if base_report_id else []
        old_by_key = {self._comparison_key(item): item for item in previous}
        current = [self._fact_payload(dict(item)) for item in facts if item.get("fact_id")]
        new_by_key = {self._comparison_key(item): item for item in current}
        result: dict[str, Any] = {
            "base_report_id": base_report_id,
            "added": [], "updated": [], "confirmed": [], "superseded": [],
            "contradicted": [], "stale": [], "still_unverified": [],
        }
        for key, item in new_by_key.items():
            prior = old_by_key.get(key)
            if prior is None:
                result["added"].append(self._delta_item(item))
            elif self._values_consistent(prior.get("value"), item.get("value")):
                result["confirmed"].append(self._delta_item(item))
            else:
                result["updated"].append({"before": self._delta_item(prior), "after": self._delta_item(item)})
        for key, item in old_by_key.items():
            if key not in new_by_key:
                result["stale"].append(self._delta_item(item))
        return result

    def _report_symbol(self, report_id: str) -> str:
        facts = self._report_facts(report_id)
        return str(facts[0].get("symbol") or "") if facts else ""

    def _report_facts(self, report_id: str | None) -> list[dict[str, Any]]:
        if not report_id:
            return []
        with self.connect() as conn:
            row = conn.execute("SELECT fact_ids_json FROM report_knowledge_links WHERE report_id=? ORDER BY revision DESC LIMIT 1", (report_id,)).fetchone()
            if not row:
                return []
            ids = json.loads(row["fact_ids_json"] or "[]")
            if not ids:
                return []
            rows = conn.execute(f"SELECT * FROM fact_records WHERE fact_id IN ({','.join('?' for _ in ids)})", ids).fetchall()
        return [self._fact_payload(dict(item)) for item in rows]

    @staticmethod
    def _comparison_key(item: dict[str, Any]) -> tuple[str, ...]:
        return tuple(str(item.get(key) or "") for key in ("symbol", "metric", "period", "scope_key", "unit", "currency"))

    @staticmethod
    def _delta_item(item: dict[str, Any]) -> dict[str, Any]:
        return {key: item.get(key) for key in ("fact_id", "symbol", "metric", "value", "unit", "currency", "period", "scope_key", "freshness_status")}

    def delta(self, report_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload_json FROM research_deltas WHERE report_id=?", (report_id,)).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def history(self, symbol: str, *, limit: int = 20) -> dict[str, Any]:
        normalized = symbol.strip().upper()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT l.report_id, l.revision, l.coverage_snapshot_id, COUNT(DISTINCT f.fact_id) AS fact_count FROM report_knowledge_links l LEFT JOIN json_each(l.fact_ids_json) j LEFT JOIN fact_records f ON f.fact_id=j.value WHERE f.symbol=? GROUP BY l.report_id, l.revision ORDER BY l.revision DESC LIMIT ?",
                (normalized, max(1, min(limit, 100))),
            ).fetchall()
        return {"symbol": normalized, "reports": [dict(row) for row in rows]}

    def linked_context(self, report_id: str, *, limit: int = 30) -> str:
        facts = self._report_facts(report_id)[:limit]
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM claim_records WHERE origin_id=? AND claim_status='prior_claim' ORDER BY created_at DESC LIMIT ?", (report_id, limit)).fetchall()
        claims = [dict(row) for row in rows]
        payload = {
            "report_id": report_id,
            "verified_facts": [self._delta_item(item) for item in facts if item.get("freshness_status") == "valid"],
            "prior_claims_not_evidence": [{"section_id": item.get("section_id"), "text": item.get("text")} for item in claims],
            "history_delta": self.delta(report_id),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def index_research_session(self, *, session_id: str, symbol: str, role: str, content: str, message_id: str) -> str:
        claim_id = _stable_id("claim", "research_session", session_id, message_id, role, content)
        status = "hypothesis" if role == "user" else "unverified_prior_claim"
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO claim_records(claim_id, origin_type, origin_id, section_id, claim_type, text, fact_ids_json, evidence_ids_json, claim_status, superseded_by, created_at) VALUES (?, 'research_session', ?, NULL, ?, ?, '[]', '[]', ?, NULL, ?)",
                (claim_id, session_id, "opinion", content, status, _utc_now()),
            )
            conn.execute("DELETE FROM claim_records_fts WHERE claim_id=?", (claim_id,))
            conn.execute("INSERT INTO claim_records_fts(claim_id, origin_id, search_text) VALUES (?, ?, ?)", (claim_id, session_id, _search_text(content, (symbol,))))
        return claim_id

    def backfill_reports(self, reports_dir: Path) -> dict[str, int]:
        counts = {"reports": 0, "evidence": 0, "facts": 0, "claims": 0}
        for report_dir in sorted(reports_dir.glob("report_*")):
            manifest_path = report_dir / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            analysis_dir = report_dir / "analysis"
            evidence = self._read_jsonl(analysis_dir / "evidence.jsonl")
            facts = self._read_jsonl(analysis_dir / "facts.jsonl")
            claims = self._read_jsonl(report_dir / "claims.jsonl")
            upgraded_evidence: list[dict[str, Any]] = []
            for item in evidence:
                metadata = dict(item.get("metadata") or {})
                if not metadata.get("document_ref"):
                    stored = self.store_document(
                        url=str(item.get("source_locator") or f"legacy://{item.get('evidence_id') or 'unknown'}"),
                        content=str(item.get("summary") or ""),
                        title=str(item.get("source") or "legacy evidence"),
                        publisher=str(item.get("source") or ""),
                        published_at=item.get("published_at"),
                        cached_status="legacy_excerpt",
                    )
                    metadata.update({"document_ref": stored.document_ref, "chunk_refs": [row["chunk_ref"] for row in stored.chunk_catalog], "source_strength": "D"})
                    item = {**item, "metadata": metadata}
                upgraded_evidence.append(item)
            coverage = self.create_coverage_plan(symbol=str(manifest.get("symbol") or ""), profile=str(manifest.get("profile") or "equity_deep_research"), as_of=str(manifest.get("data_as_of") or manifest.get("updated_at") or _utc_now()), report_id=str(manifest.get("report_id") or report_dir.name), prior_report_id=manifest.get("parent_report_id"))
            self.link_report(
                report_id=str(manifest.get("report_id") or report_dir.name),
                revision=int(manifest.get("revision") or 1),
                symbol=str(manifest.get("symbol") or ""),
                quality_status=str(manifest.get("quality_status") or "failed_validation"),
                evidence=upgraded_evidence,
                facts=facts,
                claims=claims,
                coverage_snapshot_id=coverage["coverage_snapshot_id"],
                base_report_id=manifest.get("parent_report_id"),
            )
            counts["reports"] += 1
            counts["evidence"] += len(upgraded_evidence)
            counts["facts"] += len(facts)
            counts["claims"] += len(claims)
        return counts

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        result = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                result.append(item)
        return result


_shared_store: ResearchKnowledgeStore | None = None
_shared_lock = threading.Lock()


def get_research_knowledge_store() -> ResearchKnowledgeStore:
    global _shared_store
    if _shared_store is None:
        with _shared_lock:
            if _shared_store is None:
                _shared_store = ResearchKnowledgeStore()
    return _shared_store
