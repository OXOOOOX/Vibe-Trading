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
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .source_classification import classify_source_kind


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
_VERIFICATION_STATUSES = {
    "official_primary",
    "live_retrieved",
    "source_recorded",
    "historical_context",
}
_FRESHNESS_DAYS = {
    "consensus": 7,
    "competition": 180,
    "tam": 365,
}
_NON_CONFLICTING_DERIVED_METRICS = {
    # These values describe research coverage at a point in the pipeline. A
    # later resolution is an update to analysis completeness, not an
    # independent-source contradiction requiring a third source.
    "etf_component_research_coverage",
    "etf_component_fully_supported_coverage",
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
_ETF_DOMAINS = (
    "fund_identity",
    "index_methodology",
    "holdings_universe",
    "portfolio_fundamentals",
    "market_microstructure",
    "creation_redemption",
    "tracking_quality",
    "peer_comparison",
    "component_research",
    "scenario_monitoring",
)
_ETF_OPTIONAL_DOMAINS = {"peer_comparison"}
_ETF_TWO_SOURCE_DOMAINS = {"portfolio_fundamentals", "component_research"}
_ETF_LIVE_DOMAINS = {
    "holdings_universe",
    "market_microstructure",
    "creation_redemption",
    "tracking_quality",
}

_IDENTITY_COMPARISON_METRICS = {
    "fund_name",
    "manager",
    "exchange",
    "tracked_index_code",
    "tracked_index_name",
    "index_name",
    "index_code",
    "management_fee_rate",
    "custody_fee_rate",
    "methodology_version",
    "version",
}
_ROLLING_COMPARISON_POLICIES: dict[str, dict[str, Decimal]] = {
    "fund_units": {"relative": Decimal("0.001")},
    "etf_fund_units": {"relative": Decimal("0.001")},
    "exchange_market_value": {"relative": Decimal("0.005")},
    "current_price": {"relative": Decimal("0.005")},
    "unit_nav": {"relative": Decimal("0.001")},
    "premium_discount_rate": {"absolute": Decimal("0.001")},
    "etf_selected_weight_coverage": {"absolute": Decimal("0.01")},
    "etf_component_research_coverage": {"absolute": Decimal("0.01")},
    "etf_component_fully_supported_coverage": {"absolute": Decimal("0.01")},
    "etf_fund_units_change_1d": {"relative": Decimal("0.01")},
    "etf_estimated_net_flow_1d": {"relative": Decimal("0.01")},
    "peer_member_current_fund_units": {"relative": Decimal("0.001")},
    "peer_member_prior_fund_units": {"relative": Decimal("0.001")},
    "peer_member_fund_units_change_1d": {"relative": Decimal("0.01")},
    "peer_member_estimated_net_flow_1d": {"relative": Decimal("0.01")},
    "peer_group_estimated_net_flow_1d": {"relative": Decimal("0.01")},
    "peer_group_inflow_member_ratio_1d": {"absolute": Decimal("0.01")},
    "peer_group_member_count": {"absolute": Decimal("0")},
    "peer_group_unit_change_coverage": {"absolute": Decimal("0.01")},
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", "off"}


def knowledge_enabled() -> bool:
    return _flag("VIBE_TRADING_RESEARCH_KNOWLEDGE_ENABLED", "0")


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


def _official_url(value: str) -> bool:
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme.lower() in {"http", "https"} and any(
        host == domain or host.endswith(f".{domain}") for domain in _OFFICIAL_HOSTS
    )


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
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
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

    def _backup_before_report_library_migration(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        backup = self.path.with_suffix(self.path.suffix + ".pre-report-library-v2.bak")
        if backup.exists():
            return
        source = sqlite3.connect(self.path)
        target = sqlite3.connect(backup)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

    def _backup_before_source_archive_migration(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        backup = self.path.with_suffix(self.path.suffix + ".pre-source-archive-v3.bak")
        if backup.exists():
            return
        source = sqlite3.connect(self.path)
        target = sqlite3.connect(backup)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

    def _backup_before_structured_financial_migration(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        backup = self.path.with_suffix(self.path.suffix + ".pre-structured-financial-v4.bak")
        if backup.exists():
            return
        source = sqlite3.connect(self.path)
        target = sqlite3.connect(backup)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

    def _backup_before_claim_support_migration(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        backup = self.path.with_suffix(self.path.suffix + ".pre-claim-support-v5.bak")
        if backup.exists():
            return
        source = sqlite3.connect(self.path)
        target = sqlite3.connect(backup)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

    def _backup_before_artifact_contract_migration(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        backup = self.path.with_suffix(self.path.suffix + ".pre-artifact-contract-v6.bak")
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
                report_library_migrated = probe.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_catalog_entries'"
                ).fetchone()
                source_archive_migrated = probe.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_observations'"
                ).fetchone()
                structured_financial_migrated = probe.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='structured_document_extractions'"
                ).fetchone()
                report_knowledge_link_exists = probe.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_knowledge_links'"
                ).fetchone()
                report_link_columns = {
                    str(row[1])
                    for row in probe.execute("PRAGMA table_info(report_knowledge_links)").fetchall()
                } if report_knowledge_link_exists else set()
                claim_support_migrated = (
                    not report_knowledge_link_exists
                    or "claim_support_json" in report_link_columns
                )
                artifact_link_exists = probe.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_artifact_links'"
                ).fetchone()
                artifact_link_columns = {
                    str(row[1])
                    for row in probe.execute("PRAGMA table_info(report_artifact_links)").fetchall()
                } if artifact_link_exists else set()
                artifact_contract_migrated = (
                    not artifact_link_exists
                    or {
                        "materialization_status", "materialization_error",
                    }.issubset(artifact_link_columns)
                )
            if not migrated:
                self._backup_before_migration()
            elif not report_library_migrated:
                self._backup_before_report_library_migration()
            elif not source_archive_migrated:
                self._backup_before_source_archive_migration()
            elif not structured_financial_migrated:
                self._backup_before_structured_financial_migration()
            elif not claim_support_migrated:
                self._backup_before_claim_support_migration()
            elif not artifact_contract_migrated:
                self._backup_before_artifact_contract_migration()
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
                        claim_support_json TEXT NOT NULL DEFAULT '{}',
                        coverage_snapshot_id TEXT,
                        PRIMARY KEY(report_id, revision)
                    );
                    CREATE TABLE IF NOT EXISTS source_observations (
                        observation_id TEXT PRIMARY KEY,
                        document_ref TEXT NOT NULL REFERENCES source_documents(document_ref),
                        subject_key TEXT NOT NULL,
                        market TEXT NOT NULL DEFAULT '',
                        source_kind TEXT NOT NULL,
                        provider_id TEXT NOT NULL DEFAULT '',
                        provider_record_id TEXT NOT NULL DEFAULT '',
                        verification_status TEXT NOT NULL,
                        body_status TEXT NOT NULL,
                        origin_type TEXT NOT NULL,
                        origin_id TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        UNIQUE(document_ref, subject_key, origin_type, origin_id, provider_record_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_source_observations_subject
                        ON source_observations(subject_key, observed_at DESC, observation_id DESC);
                    CREATE INDEX IF NOT EXISTS idx_source_observations_document
                        ON source_observations(document_ref, observed_at DESC);
                    CREATE TABLE IF NOT EXISTS report_source_links (
                        report_id TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        document_ref TEXT NOT NULL REFERENCES source_documents(document_ref),
                        relation_type TEXT NOT NULL,
                        evidence_ids_json TEXT NOT NULL DEFAULT '[]',
                        fact_ids_json TEXT NOT NULL DEFAULT '[]',
                        claim_ids_json TEXT NOT NULL DEFAULT '[]',
                        section_ids_json TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL,
                        PRIMARY KEY(report_id, revision, document_ref, relation_type)
                    );
                    CREATE INDEX IF NOT EXISTS idx_report_source_links_document
                        ON report_source_links(document_ref, report_id);
                    CREATE TABLE IF NOT EXISTS research_note_subjects (
                        note_claim_id TEXT PRIMARY KEY REFERENCES claim_records(claim_id),
                        subject_key TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        message_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_research_note_subjects_subject
                        ON research_note_subjects(subject_key, created_at DESC);
                    CREATE TABLE IF NOT EXISTS research_note_resolutions (
                        note_claim_id TEXT NOT NULL REFERENCES claim_records(claim_id),
                        report_id TEXT NOT NULL,
                        report_claim_id TEXT NOT NULL REFERENCES claim_records(claim_id),
                        resolution_status TEXT NOT NULL,
                        resolved_at TEXT NOT NULL,
                        PRIMARY KEY(note_claim_id, report_id, report_claim_id)
                    );
                    CREATE TABLE IF NOT EXISTS structured_document_extractions (
                        extraction_id TEXT PRIMARY KEY,
                        document_ref TEXT NOT NULL REFERENCES source_documents(document_ref),
                        subject_key TEXT NOT NULL,
                        extractor_id TEXT NOT NULL,
                        extractor_version TEXT NOT NULL,
                        source_content_hash TEXT NOT NULL,
                        extraction_method TEXT NOT NULL,
                        status TEXT NOT NULL,
                        result_hash TEXT,
                        object_path TEXT,
                        validation_json TEXT NOT NULL DEFAULT '{}',
                        metrics_count INTEGER NOT NULL DEFAULT 0,
                        ocr_performed INTEGER NOT NULL DEFAULT 0,
                        error TEXT NOT NULL DEFAULT '',
                        extracted_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(
                            document_ref, subject_key, extractor_id,
                            extractor_version, source_content_hash
                        )
                    );
                    CREATE INDEX IF NOT EXISTS idx_structured_extractions_subject
                        ON structured_document_extractions(
                            subject_key, status, extracted_at DESC
                        );
                    CREATE TABLE IF NOT EXISTS financial_statement_snapshots (
                        snapshot_id TEXT PRIMARY KEY,
                        extraction_id TEXT NOT NULL UNIQUE
                            REFERENCES structured_document_extractions(extraction_id),
                        document_ref TEXT NOT NULL REFERENCES source_documents(document_ref),
                        subject_key TEXT NOT NULL,
                        market TEXT NOT NULL DEFAULT '',
                        filing_type TEXT NOT NULL DEFAULT '',
                        reporting_period_end TEXT NOT NULL DEFAULT '',
                        currency TEXT NOT NULL DEFAULT '',
                        unit_scale TEXT NOT NULL DEFAULT '',
                        validation_status TEXT NOT NULL,
                        metrics_json TEXT NOT NULL DEFAULT '[]',
                        evidence_ids_json TEXT NOT NULL DEFAULT '[]',
                        fact_ids_json TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_financial_snapshots_subject
                        ON financial_statement_snapshots(
                            subject_key, reporting_period_end DESC, created_at DESC
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
                    CREATE TABLE IF NOT EXISTS report_catalog_entries (
                        report_id TEXT PRIMARY KEY,
                        family_id TEXT NOT NULL,
                        report_kind TEXT NOT NULL,
                        subject_type TEXT NOT NULL,
                        subject_key TEXT NOT NULL,
                        symbol TEXT,
                        security_name TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL,
                        report_quality_status TEXT NOT NULL,
                        coverage_status TEXT NOT NULL,
                        generated_at TEXT NOT NULL,
                        data_as_of TEXT NOT NULL,
                        source_type TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        source_revision INTEGER NOT NULL DEFAULT 1,
                        knowledge_link_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(source_type, source_id, source_revision)
                    );
                    CREATE INDEX IF NOT EXISTS idx_report_catalog_subject
                        ON report_catalog_entries(subject_type, subject_key, data_as_of DESC, generated_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_report_catalog_symbol
                        ON report_catalog_entries(symbol, data_as_of DESC, generated_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_report_catalog_kind
                        ON report_catalog_entries(report_kind, status, generated_at DESC);
                    CREATE TABLE IF NOT EXISTS report_viewpoints (
                        viewpoint_id TEXT PRIMARY KEY,
                        report_id TEXT NOT NULL REFERENCES report_catalog_entries(report_id) ON DELETE CASCADE,
                        horizon TEXT NOT NULL,
                        stance TEXT NOT NULL,
                        action TEXT NOT NULL,
                        confidence TEXT NOT NULL,
                        summary_claim_id TEXT,
                        reason_claim_ids_json TEXT NOT NULL DEFAULT '[]',
                        risk_claim_ids_json TEXT NOT NULL DEFAULT '[]',
                        condition_claim_ids_json TEXT NOT NULL DEFAULT '[]',
                        invalidation_claim_ids_json TEXT NOT NULL DEFAULT '[]',
                        valid_from TEXT,
                        valid_until TEXT,
                        created_at TEXT NOT NULL,
                        UNIQUE(report_id, horizon)
                    );
                    CREATE INDEX IF NOT EXISTS idx_report_viewpoints_horizon
                        ON report_viewpoints(horizon, report_id);
                    CREATE TABLE IF NOT EXISTS report_artifact_links (
                        report_id TEXT NOT NULL REFERENCES report_catalog_entries(report_id) ON DELETE CASCADE,
                        artifact_id TEXT NOT NULL,
                        artifact_role TEXT NOT NULL,
                        filename TEXT NOT NULL,
                        media_type TEXT NOT NULL,
                        source_locator TEXT NOT NULL,
                        sha256 TEXT,
                        available INTEGER NOT NULL DEFAULT 1,
                        revision INTEGER NOT NULL DEFAULT 1,
                        materialization_status TEXT,
                        materialization_error TEXT,
                        PRIMARY KEY(report_id, artifact_id)
                    );
                    CREATE TABLE IF NOT EXISTS report_relations (
                        relation_id TEXT PRIMARY KEY,
                        from_report_id TEXT NOT NULL REFERENCES report_catalog_entries(report_id) ON DELETE CASCADE,
                        to_report_id TEXT NOT NULL,
                        relation_type TEXT NOT NULL,
                        horizon TEXT,
                        created_at TEXT NOT NULL,
                        UNIQUE(from_report_id, to_report_id, relation_type, horizon)
                    );
                    CREATE INDEX IF NOT EXISTS idx_report_relations_target
                        ON report_relations(to_report_id, relation_type);
                    CREATE TABLE IF NOT EXISTS viewpoint_delta_cache (
                        comparison_id TEXT PRIMARY KEY,
                        input_hash TEXT NOT NULL UNIQUE,
                        payload_json TEXT NOT NULL,
                        ai_summary_json TEXT,
                        ai_model TEXT,
                        prompt_version TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS report_library_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    INSERT OR IGNORE INTO research_knowledge_schema(version, applied_at) VALUES (1, datetime('now'));
                    INSERT OR IGNORE INTO research_knowledge_schema(version, applied_at) VALUES (2, datetime('now'));
                    INSERT OR IGNORE INTO research_knowledge_schema(version, applied_at) VALUES (3, datetime('now'));
                    INSERT OR IGNORE INTO research_knowledge_schema(version, applied_at) VALUES (4, datetime('now'));
                    """
                )
                link_columns = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(report_knowledge_links)").fetchall()
                }
                if "claim_support_json" not in link_columns:
                    conn.execute(
                        "ALTER TABLE report_knowledge_links ADD COLUMN claim_support_json TEXT NOT NULL DEFAULT '{}'"
                    )
                conn.execute(
                    "INSERT OR IGNORE INTO research_knowledge_schema(version, applied_at) VALUES (5, datetime('now'))"
                )
                artifact_columns = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(report_artifact_links)").fetchall()
                }
                if "materialization_status" not in artifact_columns:
                    conn.execute(
                        "ALTER TABLE report_artifact_links ADD COLUMN materialization_status TEXT"
                    )
                if "materialization_error" not in artifact_columns:
                    conn.execute(
                        "ALTER TABLE report_artifact_links ADD COLUMN materialization_error TEXT"
                    )
                conn.execute(
                    "INSERT OR IGNORE INTO research_knowledge_schema(version, applied_at) VALUES (6, datetime('now'))"
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
            with tmp.open("w", encoding="utf-8", newline="") as handle:
                handle.write(body)
            tmp.replace(object_path)
        effective_publisher = publisher.strip() or (urlsplit(canonical).hostname or "")
        classification = source_class or _source_class(canonical, effective_publisher)
        independence = _publisher_group(canonical, effective_publisher)
        chunk_rows = _chunks(body, content_hash)
        retrieved_at = _utc_now()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT publisher,published_at,title FROM source_documents WHERE document_ref=?",
                (document_ref,),
            ).fetchone()
            if existing is not None:
                # Provider payloads often gain a publication timestamp or a
                # better title after the first metadata-only capture.  Enrich
                # the same content-addressed document instead of creating a
                # duplicate or leaving the archive permanently incomplete.
                conn.execute(
                    """UPDATE source_documents SET
                           publisher=CASE WHEN publisher='' AND ?<>'' THEN ? ELSE publisher END,
                           published_at=CASE
                               WHEN COALESCE(published_at,'')='' AND ?<>'' THEN ?
                               ELSE published_at
                           END,
                           title=CASE WHEN title='' AND ?<>'' THEN ? ELSE title END
                       WHERE document_ref=?""",
                    (
                        effective_publisher,
                        effective_publisher,
                        str(published_at or ""),
                        str(published_at or ""),
                        str(title or ""),
                        str(title or ""),
                        document_ref,
                    ),
                )
                return StoredDocument(
                    document_ref=document_ref,
                    canonical_url=canonical,
                    content_hash=content_hash,
                    object_path=str(object_path),
                    cached=cached_status != "network",
                    chunk_catalog=[
                        {
                            key: item[key]
                            for key in (
                                "chunk_ref",
                                "heading_path",
                                "page_or_paragraph_locator",
                            )
                        }
                        for item in chunk_rows
                    ],
                )
            prior = conn.execute(
                "SELECT document_ref, content_hash FROM source_documents WHERE canonical_url=? ORDER BY retrieved_at DESC LIMIT 1",
                (canonical,),
            ).fetchone()
            conn.execute(
                "INSERT OR IGNORE INTO source_documents(document_ref, canonical_url, publisher, source_class, independence_group, published_at, retrieved_at, content_hash, object_path, cached_status, title) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (document_ref, canonical, effective_publisher, classification, independence, published_at, retrieved_at, content_hash, str(object_path), cached_status, title),
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

    def structured_extraction(
        self,
        *,
        document_ref: str,
        subject_key: str,
        extractor_id: str,
        extractor_version: str,
    ) -> dict[str, Any] | None:
        """Return a completed extraction for the exact immutable source version."""

        document = self.document(document_ref)
        if document is None:
            return None
        with self.connect() as conn:
            row = conn.execute(
                """SELECT * FROM structured_document_extractions
                   WHERE document_ref=? AND subject_key=? AND extractor_id=?
                     AND extractor_version=? AND source_content_hash=?""",
                (
                    document_ref,
                    str(subject_key or "").upper(),
                    extractor_id,
                    extractor_version,
                    document["content_hash"],
                ),
            ).fetchone()
        if row is None:
            return None
        result = self._decode_json_columns(dict(row), ("validation_json",))
        object_path = Path(str(result.get("object_path") or ""))
        if object_path.is_file():
            try:
                payload = json.loads(object_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                result["result"] = payload
        result["ocr_performed"] = bool(result.get("ocr_performed"))
        return result

    def record_structured_extraction(
        self,
        *,
        document_ref: str,
        subject_key: str,
        extractor_id: str,
        extractor_version: str,
        extraction_method: str,
        status: str,
        result: dict[str, Any] | None = None,
        validation: dict[str, Any] | None = None,
        ocr_performed: bool = False,
        error: str = "",
        evidence_ids: Iterable[str] = (),
        fact_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Persist one immutable extraction result and its reusable snapshot."""

        document = self.document(document_ref)
        if document is None:
            raise KeyError(document_ref)
        normalized_subject = str(subject_key or "").strip().upper()
        if not normalized_subject:
            raise ValueError("structured extraction subject_key is required")
        now = _utc_now()
        extraction_id = _stable_id(
            "extract",
            document_ref,
            normalized_subject,
            extractor_id,
            extractor_version,
            document["content_hash"],
        )
        payload = dict(result or {})
        metrics = [dict(item) for item in payload.get("metrics") or [] if isinstance(item, dict)]
        result_hash: str | None = None
        object_path: Path | None = None
        if payload:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            result_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            structured_dir = self.object_dir / "structured"
            structured_dir.mkdir(parents=True, exist_ok=True)
            object_path = structured_dir / f"{result_hash}.json"
            if not object_path.exists():
                temporary = object_path.with_suffix(".json.tmp")
                temporary.write_text(encoded, encoding="utf-8")
                temporary.replace(object_path)
        validation_payload = dict(validation or payload.get("validation") or {})
        evidence_list = sorted({str(item) for item in evidence_ids if str(item)})
        fact_list = sorted({str(item) for item in fact_ids if str(item)})
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO structured_document_extractions(
                       extraction_id,document_ref,subject_key,extractor_id,
                       extractor_version,source_content_hash,extraction_method,
                       status,result_hash,object_path,validation_json,metrics_count,
                       ocr_performed,error,extracted_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(
                       document_ref,subject_key,extractor_id,extractor_version,
                       source_content_hash
                   ) DO UPDATE SET extraction_method=excluded.extraction_method,
                       status=excluded.status,result_hash=excluded.result_hash,
                       object_path=excluded.object_path,
                       validation_json=excluded.validation_json,
                       metrics_count=excluded.metrics_count,
                       ocr_performed=excluded.ocr_performed,error=excluded.error,
                       updated_at=excluded.updated_at""",
                (
                    extraction_id,
                    document_ref,
                    normalized_subject,
                    extractor_id,
                    extractor_version,
                    document["content_hash"],
                    str(extraction_method or "unknown"),
                    str(status or "failed"),
                    result_hash,
                    str(object_path) if object_path else None,
                    json.dumps(validation_payload, ensure_ascii=False, sort_keys=True),
                    len(metrics),
                    1 if ocr_performed else 0,
                    str(error or "")[:2000],
                    now,
                    now,
                ),
            )
            snapshot_id = _stable_id("financial_snapshot", extraction_id)
            if metrics:
                conn.execute(
                    """INSERT INTO financial_statement_snapshots(
                           snapshot_id,extraction_id,document_ref,subject_key,
                           market,filing_type,reporting_period_end,currency,
                           unit_scale,validation_status,metrics_json,
                           evidence_ids_json,fact_ids_json,created_at,updated_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(extraction_id) DO UPDATE SET
                           market=excluded.market,filing_type=excluded.filing_type,
                           reporting_period_end=excluded.reporting_period_end,
                           currency=excluded.currency,unit_scale=excluded.unit_scale,
                           validation_status=excluded.validation_status,
                           metrics_json=excluded.metrics_json,
                           evidence_ids_json=excluded.evidence_ids_json,
                           fact_ids_json=excluded.fact_ids_json,
                           updated_at=excluded.updated_at""",
                    (
                        snapshot_id,
                        extraction_id,
                        document_ref,
                        normalized_subject,
                        str(payload.get("market") or ""),
                        str(payload.get("filing_type") or ""),
                        str(payload.get("reporting_period_end") or ""),
                        str(payload.get("currency") or ""),
                        str(payload.get("unit_scale") or ""),
                        str(status or "failed"),
                        json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                        json.dumps(evidence_list),
                        json.dumps(fact_list),
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    "DELETE FROM financial_statement_snapshots WHERE extraction_id=?",
                    (extraction_id,),
                )
        return {
            "extraction_id": extraction_id,
            "document_ref": document_ref,
            "subject_key": normalized_subject,
            "status": str(status or "failed"),
            "metrics_count": len(metrics),
            "ocr_performed": bool(ocr_performed),
            "result_hash": result_hash,
            "object_path": str(object_path) if object_path else None,
            "evidence_ids": evidence_list,
            "fact_ids": fact_list,
        }

    def list_financial_snapshots(
        self,
        subject_key: str,
        *,
        validated_only: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        normalized_subject = str(subject_key or "").strip().upper()
        capped = max(1, min(int(limit), 200))
        sql = """SELECT s.*,e.extractor_id,e.extractor_version,
                        e.extraction_method,e.ocr_performed,e.result_hash,
                        e.validation_json,e.error,d.title,d.publisher,
                        d.canonical_url,d.published_at,d.content_hash
                 FROM financial_statement_snapshots s
                 JOIN structured_document_extractions e USING(extraction_id)
                 JOIN source_documents d USING(document_ref)
                 WHERE s.subject_key=?"""
        params: list[Any] = [normalized_subject]
        if validated_only:
            sql += " AND s.validation_status='validated'"
        sql += " ORDER BY s.reporting_period_end DESC,s.created_at DESC LIMIT ?"
        params.append(capped)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        snapshots: list[dict[str, Any]] = []
        for row in rows:
            item = self._decode_json_columns(
                dict(row),
                (
                    "metrics_json",
                    "evidence_ids_json",
                    "fact_ids_json",
                    "validation_json",
                ),
            )
            item["ocr_performed"] = bool(item.get("ocr_performed"))
            item["source_url"] = item.pop("canonical_url", None)
            snapshots.append(item)
        return {
            "subject_key": normalized_subject,
            "snapshots": snapshots,
            "count": len(snapshots),
        }

    @staticmethod
    def _document_verification_status(
        document: dict[str, Any],
        requested: str,
        body_status: str,
    ) -> str:
        """Grade traceability without treating a publisher label as authentication."""

        desired = requested if requested in _VERIFICATION_STATUSES else "source_recorded"
        locator = str(document.get("canonical_url") or "")
        object_path = Path(str(document.get("object_path") or ""))
        body_present = (
            body_status in {"full_text", "structured_payload", "excerpt"}
            and bool(document.get("content_hash"))
            and object_path.is_file()
            and object_path.stat().st_size > 0
        )
        if desired == "official_primary":
            return (
                "official_primary"
                if _official_url(locator)
                and str(document.get("source_class") or "") in {
                    "official", "regulatory", "exchange", "company_filing",
                    "regulatory_filing", "company_disclosure",
                    "audited_financial_statement", "official_statistics",
                    "index_provider", "index_methodology", "fund_manager",
                    "fund_product",
                }
                and body_status == "full_text"
                and body_present
                else "source_recorded"
            )
        if desired == "live_retrieved":
            parsed = urlsplit(locator)
            return (
                "live_retrieved"
                if parsed.scheme.lower() in {"http", "https"} and body_present
                else "source_recorded"
            )
        return desired

    def record_source_observation(
        self,
        *,
        document_ref: str,
        subject_key: str,
        source_kind: str,
        origin_type: str,
        origin_id: str,
        market: str = "",
        provider_id: str = "",
        provider_record_id: str = "",
        verification_status: str = "source_recorded",
        body_status: str = "metadata_only",
        observed_at: str | None = None,
        metadata: dict[str, Any] | None = None,
        observed_source_locator: str = "",
        observed_source_class: str = "",
    ) -> dict[str, Any]:
        document = self.document(document_ref)
        if document is None:
            raise KeyError(document_ref)
        normalized_subject = str(subject_key or "").strip().upper()
        if not normalized_subject:
            raise ValueError("source observation subject_key is required")
        observed = str(observed_at or _utc_now())
        verification_document = dict(document)
        if observed_source_locator:
            verification_document["canonical_url"] = observed_source_locator
        if observed_source_class:
            verification_document["source_class"] = observed_source_class
        status = self._document_verification_status(
            verification_document,
            str(verification_status or "source_recorded"),
            str(body_status or "metadata_only"),
        )
        observation_id = _stable_id(
            "sourceobs",
            document_ref,
            normalized_subject,
            origin_type,
            origin_id,
            provider_record_id,
        )
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO source_observations(
                       observation_id,document_ref,subject_key,market,source_kind,
                       provider_id,provider_record_id,verification_status,body_status,
                       origin_type,origin_id,observed_at,metadata_json
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(document_ref,subject_key,origin_type,origin_id,provider_record_id)
                   DO UPDATE SET market=excluded.market,source_kind=excluded.source_kind,
                       provider_id=excluded.provider_id,
                       verification_status=excluded.verification_status,
                       body_status=excluded.body_status,observed_at=excluded.observed_at,
                       metadata_json=excluded.metadata_json""",
                (
                    observation_id,
                    document_ref,
                    normalized_subject,
                    str(market or "").upper(),
                    str(source_kind or "other"),
                    str(provider_id or ""),
                    str(provider_record_id or ""),
                    status,
                    str(body_status or "metadata_only"),
                    str(origin_type or "unknown"),
                    str(origin_id or "unknown"),
                    observed,
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
        return {
            "observation_id": observation_id,
            "document_ref": document_ref,
            "subject_key": normalized_subject,
            "verification_status": status,
            "observed_at": observed,
        }

    @staticmethod
    def _decode_source_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        try:
            metadata = json.loads(item.pop("metadata_json", "{}") or "{}")
        except json.JSONDecodeError:
            metadata = {}
        locator = str(item.pop("canonical_url", "") or "")
        item["source_locator"] = locator
        item["source_url"] = locator if locator.startswith(("https://", "http://")) else None
        item["metadata"] = metadata if isinstance(metadata, dict) else {}
        item["used_by_report_count"] = int(item.get("used_by_report_count") or 0)
        item["structured_metrics_count"] = int(item.get("structured_metrics_count") or 0)
        item["ocr_performed"] = bool(item.get("ocr_performed"))
        raw_validation = item.pop("structured_validation_json", "{}") or "{}"
        try:
            structured_validation = json.loads(raw_validation)
        except (TypeError, json.JSONDecodeError):
            structured_validation = {}
        failed_checks = (
            structured_validation.get("failed_checks", [])
            if isinstance(structured_validation, dict)
            else []
        )
        item["structured_failed_checks"] = [
            str(value) for value in failed_checks if str(value)
        ]
        item["structured_auto_repair_available"] = item.get("structured_status") in {
            "needs_review",
            "failed",
        }
        return item

    def list_subject_sources(
        self,
        subject_key: str,
        *,
        source_kind: str = "",
        verification_status: str = "",
        used_by_report: bool | None = None,
        publisher: str = "",
        published_since: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        normalized = str(subject_key or "").strip().upper()
        capped = max(1, min(int(limit), 100))
        clauses = ["ranked.subject_key=?", "ranked.rn=1"]
        params: list[Any] = [normalized]
        if source_kind:
            clauses.append("ranked.source_kind=?")
            params.append(source_kind)
        if verification_status:
            clauses.append("ranked.verification_status=?")
            params.append(verification_status)
        if used_by_report is True:
            clauses.append("EXISTS (SELECT 1 FROM report_source_links used WHERE used.document_ref=ranked.document_ref)")
        elif used_by_report is False:
            clauses.append("NOT EXISTS (SELECT 1 FROM report_source_links used WHERE used.document_ref=ranked.document_ref)")
        if publisher.strip():
            clauses.append("d.publisher LIKE ?")
            params.append(f"%{publisher.strip()}%")
        if published_since:
            clauses.append("COALESCE(d.published_at,ranked.observed_at)>=?")
            params.append(str(published_since))
        if cursor:
            with self.connect() as conn:
                cursor_row = conn.execute(
                    "SELECT observed_at,observation_id FROM source_observations WHERE observation_id=?",
                    (cursor,),
                ).fetchone()
            if cursor_row:
                clauses.append(
                    "(ranked.observed_at < ? OR (ranked.observed_at=? AND ranked.observation_id<?))"
                )
                params.extend(
                    [cursor_row["observed_at"], cursor_row["observed_at"], cursor_row["observation_id"]]
                )
        params.append(capped + 1)
        sql = f"""
            WITH ranked AS (
                SELECT o.*, ROW_NUMBER() OVER (
                    PARTITION BY o.document_ref ORDER BY
                        CASE o.verification_status
                            WHEN 'official_primary' THEN 4
                            WHEN 'live_retrieved' THEN 3
                            WHEN 'source_recorded' THEN 2
                            ELSE 1
                        END DESC,
                        CASE o.body_status
                            WHEN 'full_text' THEN 4
                            WHEN 'structured_payload' THEN 3
                            WHEN 'excerpt' THEN 2
                            ELSE 1
                        END DESC,
                        o.observed_at DESC,o.observation_id DESC
                ) AS rn
                FROM source_observations o WHERE o.subject_key=?
            )
            SELECT ranked.*,d.canonical_url,d.publisher,d.source_class,d.published_at,
                   d.retrieved_at,d.content_hash,d.cached_status,d.title,d.superseded_by,
                   (SELECT COUNT(DISTINCT r.report_id) FROM report_source_links r
                    WHERE r.document_ref=ranked.document_ref) AS used_by_report_count,
                   (SELECT e.status FROM structured_document_extractions e
                    WHERE e.document_ref=ranked.document_ref
                      AND e.subject_key=ranked.subject_key
                    ORDER BY e.updated_at DESC LIMIT 1) AS structured_status,
                   COALESCE((SELECT e.metrics_count FROM structured_document_extractions e
                    WHERE e.document_ref=ranked.document_ref
                      AND e.subject_key=ranked.subject_key
                    ORDER BY e.updated_at DESC LIMIT 1),0) AS structured_metrics_count,
                   COALESCE((SELECT e.ocr_performed FROM structured_document_extractions e
                    WHERE e.document_ref=ranked.document_ref
                      AND e.subject_key=ranked.subject_key
                    ORDER BY e.updated_at DESC LIMIT 1),0) AS ocr_performed,
                   COALESCE((SELECT e.extractor_version FROM structured_document_extractions e
                    WHERE e.document_ref=ranked.document_ref
                      AND e.subject_key=ranked.subject_key
                    ORDER BY e.updated_at DESC LIMIT 1),'') AS structured_extractor_version,
                   COALESCE((SELECT e.validation_json FROM structured_document_extractions e
                    WHERE e.document_ref=ranked.document_ref
                      AND e.subject_key=ranked.subject_key
                    ORDER BY e.updated_at DESC LIMIT 1),'{{}}') AS structured_validation_json,
                   COALESCE((SELECT e.error FROM structured_document_extractions e
                    WHERE e.document_ref=ranked.document_ref
                      AND e.subject_key=ranked.subject_key
                    ORDER BY e.updated_at DESC LIMIT 1),'') AS structured_error
            FROM ranked JOIN source_documents d USING(document_ref)
            WHERE {' AND '.join(clauses[1:])}
            ORDER BY ranked.observed_at DESC,ranked.observation_id DESC LIMIT ?
        """
        # The subject predicate is already embedded in the CTE; remove its duplicate parameter.
        query_params = [normalized, *params[1:]]
        with self.connect() as conn:
            rows = conn.execute(sql, query_params).fetchall()
        has_more = len(rows) > capped
        selected = rows[:capped]
        items = [self._decode_source_row(row) for row in selected]
        return {
            "subject_key": normalized,
            "sources": items,
            "next_cursor": items[-1]["observation_id"] if has_more and items else None,
        }

    def list_report_sources(self, report_id: str, *, limit: int = 100) -> dict[str, Any]:
        capped = max(1, min(int(limit), 200))
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT r.*,d.canonical_url,d.publisher,d.source_class,d.published_at,
                          d.retrieved_at,d.content_hash,d.cached_status,d.title,d.superseded_by,
                          (SELECT e.status FROM structured_document_extractions e
                            WHERE e.document_ref=r.document_ref
                            ORDER BY e.updated_at DESC LIMIT 1) AS structured_status,
                          COALESCE((SELECT e.metrics_count FROM structured_document_extractions e
                            WHERE e.document_ref=r.document_ref
                            ORDER BY e.updated_at DESC LIMIT 1),0) AS structured_metrics_count,
                          COALESCE((SELECT e.ocr_performed FROM structured_document_extractions e
                            WHERE e.document_ref=r.document_ref
                            ORDER BY e.updated_at DESC LIMIT 1),0) AS ocr_performed,
                          COALESCE((SELECT e.extractor_version FROM structured_document_extractions e
                            WHERE e.document_ref=r.document_ref
                            ORDER BY e.updated_at DESC LIMIT 1),'') AS structured_extractor_version,
                          COALESCE((SELECT e.validation_json FROM structured_document_extractions e
                            WHERE e.document_ref=r.document_ref
                    ORDER BY e.updated_at DESC LIMIT 1),'{{}}') AS structured_validation_json,
                          COALESCE((SELECT e.error FROM structured_document_extractions e
                            WHERE e.document_ref=r.document_ref
                            ORDER BY e.updated_at DESC LIMIT 1),'') AS structured_error,
                          COALESCE((SELECT o.verification_status FROM source_observations o
                            WHERE o.document_ref=r.document_ref ORDER BY
                              CASE o.verification_status
                                WHEN 'official_primary' THEN 4
                                WHEN 'live_retrieved' THEN 3
                                WHEN 'source_recorded' THEN 2
                                ELSE 1 END DESC,
                              o.observed_at DESC LIMIT 1),
                            'source_recorded') AS verification_status,
                          COALESCE((SELECT o.source_kind FROM source_observations o
                            WHERE o.document_ref=r.document_ref ORDER BY
                              CASE o.verification_status
                                WHEN 'official_primary' THEN 4
                                WHEN 'live_retrieved' THEN 3
                                WHEN 'source_recorded' THEN 2
                                ELSE 1 END DESC,
                              o.observed_at DESC LIMIT 1),
                            'other') AS source_kind,
                          COALESCE((SELECT o.body_status FROM source_observations o
                            WHERE o.document_ref=r.document_ref ORDER BY
                              CASE o.verification_status
                                WHEN 'official_primary' THEN 4
                                WHEN 'live_retrieved' THEN 3
                                WHEN 'source_recorded' THEN 2
                                ELSE 1 END DESC,
                              o.observed_at DESC LIMIT 1),
                            'metadata_only') AS body_status,
                          COALESCE((SELECT o.provider_id FROM source_observations o
                            WHERE o.document_ref=r.document_ref ORDER BY
                              CASE o.verification_status
                                WHEN 'official_primary' THEN 4
                                WHEN 'live_retrieved' THEN 3
                                WHEN 'source_recorded' THEN 2
                                ELSE 1 END DESC,
                              o.observed_at DESC LIMIT 1),
                            '') AS provider_id,
                          COALESCE((SELECT o.metadata_json FROM source_observations o
                            WHERE o.document_ref=r.document_ref ORDER BY
                              CASE o.verification_status
                                WHEN 'official_primary' THEN 4
                                WHEN 'live_retrieved' THEN 3
                                WHEN 'source_recorded' THEN 2
                                ELSE 1 END DESC,
                              o.observed_at DESC LIMIT 1),
                            '{}') AS metadata_json,
                          1 AS used_by_report_count
                   FROM report_source_links r JOIN source_documents d USING(document_ref)
                   WHERE r.report_id=? ORDER BY d.published_at DESC,d.retrieved_at DESC LIMIT ?""",
                (report_id, capped),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = self._decode_source_row(row)
            for key in ("evidence_ids_json", "fact_ids_json", "claim_ids_json", "section_ids_json"):
                raw = item.pop(key, "[]")
                try:
                    item[key.removesuffix("_json")] = json.loads(raw or "[]")
                except json.JSONDecodeError:
                    item[key.removesuffix("_json")] = []
            items.append(item)
        return {"report_id": report_id, "sources": items}

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
        is_etf = str(profile) == "etf_deep_research"
        domain_ids = _ETF_DOMAINS if is_etf else _DEFAULT_DOMAINS
        for domain in domain_ids:
            minimum = (
                2
                if domain == "industry_tam_competition"
                or (is_etf and domain in _ETF_TWO_SOURCE_DOMAINS)
                else 1
            )
            domains.append({
                "domain": domain,
                "required": (
                    domain not in _ETF_OPTIONAL_DOMAINS
                    if is_etf
                    else domain not in {"consensus"}
                ),
                "preferred_source_classes": ["regulatory_filing", "company_disclosure", "official_statistics", "industry_association", "broker_research"],
                "minimum_independent_sources": minimum,
                "freshness_policy": (
                    "live_first"
                    if domain in (
                        _ETF_LIVE_DOMAINS
                        if is_etf
                        else {"identity_market", "company_actions", "consensus"}
                    )
                    else "version_or_ttl"
                ),
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
            "coverage_policy_version": (
                "etf-coverage-v1" if is_etf else "equity-coverage-v1"
            ),
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
        """Serialize evidence writes and absorb short WAL writer contention."""

        delays = (0.0, 0.1, 0.3, 0.9)
        last_error: sqlite3.OperationalError | None = None
        for delay in delays:
            if delay:
                time.sleep(delay)
            try:
                with self._lock:
                    return self._register_bundle_once(bundle)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                last_error = exc
        assert last_error is not None
        raise last_error

    def _register_bundle_once(self, bundle: dict[str, Any]) -> dict[str, Any]:
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
                scope_key = str(
                    item.get("scope_key")
                    or metadata.get("scope_key")
                    or metadata.get("scope")
                    or (
                        metadata.get("component_symbol")
                        if str(item.get("metric") or "") == "etf_component_weight"
                        else ""
                    )
                    or ""
                )
                conn.execute(
                    "INSERT OR REPLACE INTO fact_records(fact_id, symbol, metric, value, unit, currency, period, scope_key, formula, input_fact_ids_json, evidence_ids_json, validation_status, superseded_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        fact_id, str(item.get("symbol") or "").upper(), str(item.get("metric") or ""), None if item.get("value") is None else str(item.get("value")), str(item.get("unit") or ""), str(metadata.get("currency") or item.get("currency") or ""), str(item.get("period") or ""), scope_key, item.get("formula"), json.dumps(item.get("input_fact_ids") or []), json.dumps(item.get("evidence_ids") or []), str(item.get("validation_status") or "pass"), item.get("superseded_by"), _utc_now(),
                    ),
                )
                self._supersede_corrected_facts(conn, fact_id)
                priority_conflict = self._apply_source_priority(conn, fact_id)
                if priority_conflict:
                    conflicts.append(priority_conflict)
                conflict = self._detect_conflict(conn, fact_id)
                if conflict:
                    conflicts.append(conflict)
        return {"evidence_count": len(evidence_rows), "fact_count": len(fact_rows), "conflicts": conflicts}

    @staticmethod
    def _supersede_corrected_facts(conn: sqlite3.Connection, fact_id: str) -> None:
        current = conn.execute("SELECT * FROM fact_records WHERE fact_id=?", (fact_id,)).fetchone()
        if not current:
            return
        current_docs = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT e.document_ref FROM evidence_records e JOIN json_each(?) j ON e.evidence_id=j.value",
                (current["evidence_ids_json"],),
            )
        }
        if not current_docs:
            return
        peers = conn.execute(
            "SELECT * FROM fact_records WHERE symbol=? AND metric=? AND period=? AND scope_key=? AND unit=? AND currency=? AND superseded_by IS NULL AND fact_id<>?",
            (
                current["symbol"], current["metric"], current["period"],
                current["scope_key"], current["unit"], current["currency"], fact_id,
            ),
        ).fetchall()
        for peer in peers:
            placeholders = ",".join("?" for _ in current_docs)
            corrected = conn.execute(
                f"SELECT 1 FROM evidence_records e JOIN json_each(?) j ON e.evidence_id=j.value JOIN source_documents d ON d.document_ref=e.document_ref WHERE d.superseded_by IN ({placeholders}) LIMIT 1",
                (peer["evidence_ids_json"], *current_docs),
            ).fetchone()
            if corrected:
                conn.execute("UPDATE fact_records SET superseded_by=? WHERE fact_id=?", (fact_id, peer["fact_id"]))

    @staticmethod
    def _fact_source_tier(conn: sqlite3.Connection, fact: sqlite3.Row) -> tuple[int, str]:
        """Return the strongest immutable source tier supporting one fact.

        A completed structured extraction from an official document is the
        strongest source.  Official source text follows it, then independent
        triangulation, a single named provider, and finally search/session
        material.  The numeric rank is intentionally internal; the stable
        reader-facing tier name is persisted in conflict reasons.
        """

        rows = conn.execute(
            """SELECT d.document_ref,d.canonical_url,d.source_class,
                      d.independence_group,e.source_strength,
                      EXISTS(
                          SELECT 1 FROM structured_document_extractions x
                           WHERE x.document_ref=d.document_ref
                             AND x.status IN ('completed','passed','success','validated')
                      ) AS has_structured_extraction
                 FROM evidence_records e
                 JOIN json_each(?) j ON e.evidence_id=j.value
                 JOIN source_documents d ON d.document_ref=e.document_ref""",
            (fact["evidence_ids_json"],),
        ).fetchall()
        if not rows:
            return (5, "search_lead")
        official_rows = [
            row for row in rows
            if _official_url(str(row["canonical_url"] or ""))
            or str(row["source_class"] or "")
            in {"regulatory_filing", "company_disclosure", "official_statistics"}
        ]
        if any(int(row["has_structured_extraction"] or 0) for row in official_rows):
            return (0, "official_structured")
        if official_rows:
            return (1, "official_text")
        groups = {
            str(row["independence_group"] or "")
            for row in rows if str(row["independence_group"] or "")
        }
        if len(groups) >= 2:
            return (2, "independent_triangulation")
        if any(str(row["source_strength"] or "").upper() in {"A", "B", "C"} for row in rows):
            return (3, "single_provider")
        return (4, "search_lead")

    def _apply_source_priority(
        self,
        conn: sqlite3.Connection,
        fact_id: str,
    ) -> dict[str, Any] | None:
        """Choose a stronger same-scope fact while preserving both raw rows."""

        current = conn.execute(
            "SELECT * FROM fact_records WHERE fact_id=?", (fact_id,)
        ).fetchone()
        if not current or current["superseded_by"]:
            return None
        peers = conn.execute(
            """SELECT * FROM fact_records
                WHERE symbol=? AND metric=? AND period=? AND scope_key=?
                  AND unit=? AND currency=? AND superseded_by IS NULL
                  AND fact_id<>?""",
            (
                current["symbol"], current["metric"], current["period"],
                current["scope_key"], current["unit"], current["currency"],
                fact_id,
            ),
        ).fetchall()
        if not peers:
            return None
        current_rank, current_tier = self._fact_source_tier(conn, current)
        ranked_peers = [
            (self._fact_source_tier(conn, peer), peer) for peer in peers
        ]
        best_peer_tier, best_peer = min(ranked_peers, key=lambda item: item[0][0])
        best_peer_rank, best_peer_name = best_peer_tier
        if current_rank == best_peer_rank:
            return None
        if current_rank < best_peer_rank:
            winner = current
            winner_tier = current_tier
            losers = [
                peer for (rank, _tier), peer in ranked_peers if rank > current_rank
            ]
        else:
            winner = best_peer
            winner_tier = best_peer_name
            losers = [current]
        for loser in losers:
            conn.execute(
                "UPDATE fact_records SET superseded_by=? WHERE fact_id=? AND superseded_by IS NULL",
                (winner["fact_id"], loser["fact_id"]),
            )
        differing = [
            loser for loser in losers
            if not self._values_consistent(winner["value"], loser["value"])
        ]
        if not differing:
            return None
        comparison = (
            current["symbol"], current["metric"], current["period"],
            current["scope_key"], current["unit"], current["currency"],
        )
        ids = sorted({
            str(winner["fact_id"]),
            *(str(item["fact_id"]) for item in differing),
        })
        comparison_key = "|".join(str(item) for item in comparison)
        conflict_id = _stable_id("conflict", comparison_key, "source_priority", ids)
        reason = (
            f"selected {winner['fact_id']} by source priority {winner_tier}; "
            "lower-priority raw facts remain queryable through superseded_by"
        )
        payload = {
            "conflict_id": conflict_id,
            "comparison_key": comparison_key,
            "fact_ids": ids,
            "conflict_type": "value_conflict",
            "resolution_status": "resolved_source_priority",
            "resolution_reason": reason,
            "selected_fact_id": str(winner["fact_id"]),
            "selected_source_tier": winner_tier,
        }
        conn.execute(
            """INSERT OR REPLACE INTO fact_conflicts(
                   conflict_id,comparison_key,fact_ids_json,conflict_type,
                   resolution_status,resolution_reason,created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                conflict_id, comparison_key, json.dumps(ids),
                payload["conflict_type"], payload["resolution_status"],
                reason, _utc_now(),
            ),
        )
        return payload

    def _detect_conflict(self, conn: sqlite3.Connection, fact_id: str) -> dict[str, Any] | None:
        current = conn.execute("SELECT * FROM fact_records WHERE fact_id=?", (fact_id,)).fetchone()
        if not current or current["value"] is None or current["superseded_by"]:
            return None
        if str(current["metric"] or "") in _NON_CONFLICTING_DERIVED_METRICS:
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
        scope_peers = conn.execute(
            "SELECT * FROM fact_records WHERE symbol=? AND metric=? AND period=? AND fact_id<>? AND superseded_by IS NULL AND (scope_key<>? OR unit<>? OR currency<>?) LIMIT 1",
            (
                current["symbol"], current["metric"], current["period"], fact_id,
                current["scope_key"], current["unit"], current["currency"],
            ),
        ).fetchone()
        if scope_peers:
            comparison_key = "|".join(str(item) for item in comparison[:3])
            ids = sorted({fact_id, str(scope_peers["fact_id"])})
            conflict_id = _stable_id("conflict", comparison_key, "scope_mismatch", ids)
            conn.execute(
                "INSERT OR REPLACE INTO fact_conflicts(conflict_id, comparison_key, fact_ids_json, conflict_type, resolution_status, resolution_reason, created_at) VALUES (?, ?, ?, 'scope_mismatch', 'not_conflict', ?, ?)",
                (
                    conflict_id, comparison_key, json.dumps(ids),
                    "different scope, unit, or currency; present side by side without selecting one",
                    _utc_now(),
                ),
            )
        return None

    @staticmethod
    def _values_consistent(left: Any, right: Any, *, metric: str = "") -> bool:
        try:
            a, b = Decimal(str(left)), Decimal(str(right))
        except (InvalidOperation, TypeError, ValueError):
            return str(left).strip().casefold() == str(right).strip().casefold()
        if str(metric) in _IDENTITY_COMPARISON_METRICS:
            return a == b
        policy = _ROLLING_COMPARISON_POLICIES.get(str(metric), {})
        if "absolute" in policy:
            tolerance = policy["absolute"]
        else:
            tolerance = max(abs(a), abs(b), Decimal("1")) * policy.get(
                "relative", Decimal("0.01")
            )
        return abs(a - b) <= tolerance

    def search(self, *, query: str = "", symbol: str = "", domains: Iterable[str] = (), metrics: Iterable[str] = (), limit: int = 20, as_of: str | None = None) -> dict[str, Any]:
        capped = max(1, min(int(limit), 100))
        normalized_symbol = symbol.strip().upper()
        domain_values = [str(item) for item in domains if str(item)]
        metric_values = [str(item) for item in metrics if str(item)]
        with self.connect() as conn:
            fact_sql = "SELECT * FROM fact_records WHERE superseded_by IS NULL"
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
            claim_fts = fts or (_fts_query(normalized_symbol) if normalized_symbol else "")
            if claim_fts:
                claims_sql = "SELECT c.* FROM claim_records_fts f JOIN claim_records c USING(claim_id) WHERE claim_records_fts MATCH ? AND c.claim_status NOT IN ('rejected_prior')"
                claim_params.append(claim_fts)
            claims_sql += " ORDER BY c.created_at DESC LIMIT ?" if claim_fts else " ORDER BY created_at DESC LIMIT ?"
            claim_params.append(capped)
            claims = [self._decode_json_columns(dict(row), ("fact_ids_json", "evidence_ids_json")) for row in conn.execute(claims_sql, claim_params).fetchall()]
        return {"facts": facts, "evidence": evidence, "prior_claims": claims, "chunks": chunks}

    def preferred_official_fact_bundle(self, symbol: str) -> dict[str, Any]:
        """Return active official facts with report-ready Evidence lineage."""

        normalized = str(symbol or "").strip().upper()
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT f.*,e.evidence_id,e.document_ref,e.chunk_refs_json,
                          e.domain,e.source_strength,e.valid_from,e.valid_until,
                          e.status AS evidence_status,e.summary,
                          d.canonical_url,d.publisher,d.source_class,
                          d.independence_group,d.published_at,d.retrieved_at,
                          d.content_hash,d.title,
                          (SELECT x.extraction_id
                             FROM structured_document_extractions x
                            WHERE x.document_ref=d.document_ref
                              AND x.status IN ('validated','completed','passed','success')
                            ORDER BY x.extracted_at DESC LIMIT 1
                          ) AS structured_extraction_id
                     FROM fact_records f
                     JOIN json_each(f.evidence_ids_json) j
                     JOIN evidence_records e ON e.evidence_id=j.value
                     JOIN source_documents d ON d.document_ref=e.document_ref
                    WHERE f.symbol=? AND f.superseded_by IS NULL
                    ORDER BY f.period DESC,f.created_at DESC""",
                (normalized,),
            ).fetchall()
        facts_by_id: dict[str, dict[str, Any]] = {}
        evidence_by_id: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not (
                _official_url(str(row["canonical_url"] or ""))
                or str(row["source_class"] or "")
                in {"regulatory_filing", "company_disclosure", "official_statistics"}
            ):
                continue
            evidence_id = str(row["evidence_id"])
            evidence_by_id[evidence_id] = {
                "evidence_id": evidence_id,
                "symbol": normalized,
                "domain": str(row["domain"] or "financial_statements"),
                "source": str(row["publisher"] or "官方披露机构"),
                "source_locator": str(row["canonical_url"] or ""),
                "retrieved_at": row["retrieved_at"],
                "published_at": row["published_at"] or row["valid_from"],
                "content_hash": str(row["content_hash"] or ""),
                "summary": str(row["summary"] or row["title"] or "官方披露资料"),
                "status": str(row["evidence_status"] or "verified"),
                "metadata": {
                    "document_ref": str(row["document_ref"] or ""),
                    "chunk_refs": json.loads(row["chunk_refs_json"] or "[]"),
                    "source_strength": str(row["source_strength"] or "A"),
                    "source_class": str(row["source_class"] or "regulatory_filing"),
                    "independence_group": str(row["independence_group"] or ""),
                    "publisher": str(row["publisher"] or ""),
                    "title": str(row["title"] or ""),
                    "structured_status": (
                        "validated" if row["structured_extraction_id"] else None
                    ),
                    "structured_extraction_id": row["structured_extraction_id"],
                    "valid_until": row["valid_until"],
                },
            }
            fact_id = str(row["fact_id"])
            fact = facts_by_id.setdefault(fact_id, {
                "fact_id": fact_id,
                "symbol": normalized,
                "metric": str(row["metric"] or ""),
                "value": row["value"],
                "unit": str(row["unit"] or ""),
                "currency": str(row["currency"] or ""),
                "period": str(row["period"] or ""),
                "scope_key": str(row["scope_key"] or ""),
                "formula": row["formula"],
                "input_fact_ids": json.loads(row["input_fact_ids_json"] or "[]"),
                "evidence_ids": [],
                "validation_status": str(row["validation_status"] or "pass"),
                "metadata": {
                    "currency": str(row["currency"] or ""),
                    "scope_key": str(row["scope_key"] or ""),
                    "source_tier": (
                        "official_structured"
                        if row["structured_extraction_id"] else "official_text"
                    ),
                },
            })
            fact["evidence_ids"].append(evidence_id)
        for fact in facts_by_id.values():
            fact["evidence_ids"] = list(dict.fromkeys(fact["evidence_ids"]))
        return {
            "symbol": normalized,
            "facts": list(facts_by_id.values()),
            "evidence": list(evidence_by_id.values()),
        }

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

    @staticmethod
    def _source_kind_for_evidence(document: dict[str, Any], domain: str) -> str:
        return classify_source_kind(document, domain)

    def _write_report_source_links(
        self,
        *,
        report_id: str,
        revision: int,
        symbol: str,
        evidence_ids: set[str],
        fact_ids: set[str],
        claims: list[dict[str, Any]],
    ) -> None:
        if not evidence_ids:
            return
        placeholders = ",".join("?" for _ in evidence_ids)
        with self.connect() as conn:
            evidence_rows = conn.execute(
                f"SELECT e.*,d.* FROM evidence_records e JOIN source_documents d USING(document_ref) WHERE e.evidence_id IN ({placeholders})",
                sorted(evidence_ids),
            ).fetchall()
            fact_rows: list[sqlite3.Row] = []
            if fact_ids:
                fact_rows = conn.execute(
                    f"SELECT fact_id,evidence_ids_json FROM fact_records WHERE fact_id IN ({','.join('?' for _ in fact_ids)})",
                    sorted(fact_ids),
                ).fetchall()
            facts_by_evidence: dict[str, set[str]] = {}
            for row in fact_rows:
                try:
                    ids = json.loads(row["evidence_ids_json"] or "[]")
                except json.JSONDecodeError:
                    ids = []
                for evidence_id in ids:
                    facts_by_evidence.setdefault(str(evidence_id), set()).add(str(row["fact_id"]))

            claims_by_evidence: dict[str, set[str]] = {}
            sections_by_evidence: dict[str, set[str]] = {}
            for raw in claims:
                claim_id = str(raw.get("claim_id") or "")
                section_id = str(raw.get("section_id") or "")
                for evidence_id in raw.get("evidence_ids") or []:
                    key = str(evidence_id)
                    if claim_id:
                        claims_by_evidence.setdefault(key, set()).add(claim_id)
                    if section_id:
                        sections_by_evidence.setdefault(key, set()).add(section_id)

            observations: list[tuple[str, str, str]] = []
            by_document: dict[str, dict[str, Any]] = {}
            for raw_row in evidence_rows:
                row = dict(raw_row)
                document_ref = str(row["document_ref"])
                entry = by_document.setdefault(
                    document_ref,
                    {
                        "document": row,
                        "evidence_ids": set(),
                        "fact_ids": set(),
                        "claim_ids": set(),
                        "section_ids": set(),
                        "domains": set(),
                    },
                )
                evidence_id = str(row["evidence_id"])
                entry["evidence_ids"].add(evidence_id)
                entry["fact_ids"].update(facts_by_evidence.get(evidence_id, set()))
                entry["claim_ids"].update(claims_by_evidence.get(evidence_id, set()))
                entry["section_ids"].update(sections_by_evidence.get(evidence_id, set()))
                entry["domains"].add(str(row.get("domain") or "other"))

            for document_ref, entry in by_document.items():
                relation_type = "cited" if entry["claim_ids"] else "supporting"
                conn.execute(
                    """INSERT INTO report_source_links(
                           report_id,revision,document_ref,relation_type,evidence_ids_json,
                           fact_ids_json,claim_ids_json,section_ids_json,created_at
                       ) VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(report_id,revision,document_ref,relation_type) DO UPDATE SET
                           evidence_ids_json=excluded.evidence_ids_json,
                           fact_ids_json=excluded.fact_ids_json,
                           claim_ids_json=excluded.claim_ids_json,
                           section_ids_json=excluded.section_ids_json""",
                    (
                        report_id,
                        int(revision),
                        document_ref,
                        relation_type,
                        json.dumps(sorted(entry["evidence_ids"])),
                        json.dumps(sorted(entry["fact_ids"])),
                        json.dumps(sorted(entry["claim_ids"])),
                        json.dumps(sorted(entry["section_ids"])),
                        _utc_now(),
                    ),
                )
                document = entry["document"]
                domain = sorted(entry["domains"])[0] if entry["domains"] else "other"
                observations.append(
                    (document_ref, self._source_kind_for_evidence(document, domain), domain)
                )

        for document_ref, source_kind, domain in observations:
            document = self.document(document_ref) or {}
            requested = (
                "official_primary"
                if str(document.get("source_class") or "") == "regulatory_filing"
                else "live_retrieved"
            )
            self.record_source_observation(
                document_ref=document_ref,
                subject_key=symbol or "PORTFOLIO",
                source_kind=source_kind,
                origin_type="report",
                origin_id=report_id,
                provider_id=str(document.get("publisher") or ""),
                provider_record_id=document_ref,
                verification_status=requested,
                body_status="full_text",
                metadata={"domain": domain, "report_id": report_id},
            )

    @staticmethod
    def _note_resolution_directives(claim: dict[str, Any]) -> list[tuple[str, str]]:
        metadata = dict(claim.get("metadata") or {})
        default_status = str(
            claim.get("note_resolution") or metadata.get("note_resolution") or "confirmed"
        )
        result: list[tuple[str, str]] = []
        for key, status in (
            ("note_claim_ids", default_status),
            ("research_note_ids", default_status),
            ("contradicts_note_ids", "contradicted"),
            ("supersedes_note_ids", "superseded"),
        ):
            values = claim.get(key)
            if values is None:
                values = metadata.get(key)
            for note_id in values or []:
                if str(note_id):
                    result.append((str(note_id), status))
        return result

    def _resolve_research_notes_from_report(
        self,
        *,
        report_id: str,
        symbol: str,
        quality_status: str,
        claims: list[dict[str, Any]],
    ) -> None:
        if quality_status == "failed_validation":
            return
        with self.connect() as conn:
            for claim in claims:
                claim_id = str(claim.get("claim_id") or "")
                supported = bool(claim.get("evidence_ids") or claim.get("fact_ids"))
                if not claim_id or not supported:
                    continue
                for note_id, status in self._note_resolution_directives(claim):
                    note = conn.execute(
                        "SELECT subject_key FROM research_note_subjects WHERE note_claim_id=?",
                        (note_id,),
                    ).fetchone()
                    if not note or str(note["subject_key"] or "").upper() != symbol.upper():
                        continue
                    normalized_status = (
                        status if status in {"confirmed", "contradicted", "superseded"} else "confirmed"
                    )
                    conn.execute(
                        """INSERT OR REPLACE INTO research_note_resolutions(
                               note_claim_id,report_id,report_claim_id,resolution_status,resolved_at
                           ) VALUES (?,?,?,?,?)""",
                        (note_id, report_id, claim_id, normalized_status, _utc_now()),
                    )

    def list_research_notes(
        self,
        subject_key: str,
        *,
        status: str = "",
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        normalized = str(subject_key or "").strip().upper()
        allowed_statuses = {"", "unverified", "confirmed", "contradicted", "superseded"}
        if status not in allowed_statuses:
            raise ValueError("unsupported research note status")
        capped = max(1, min(int(limit), 200))
        clauses = ["n.subject_key=?"]
        params: list[Any] = [normalized]
        if status:
            clauses.append("COALESCE(r.resolution_status,'unverified')=?")
            params.append(status)
        if cursor:
            with self.connect() as conn:
                cursor_row = conn.execute(
                    "SELECT created_at,note_claim_id FROM research_note_subjects WHERE note_claim_id=?",
                    (cursor,),
                ).fetchone()
            if cursor_row:
                clauses.append("(n.created_at<? OR (n.created_at=? AND n.note_claim_id<?))")
                params.extend(
                    [cursor_row["created_at"], cursor_row["created_at"], cursor_row["note_claim_id"]]
                )
        where = " AND ".join(clauses)
        latest_resolution = """
            LEFT JOIN research_note_resolutions r ON r.note_claim_id=n.note_claim_id
             AND r.resolved_at=(SELECT MAX(r2.resolved_at) FROM research_note_resolutions r2
                                WHERE r2.note_claim_id=n.note_claim_id)
        """
        with self.connect() as conn:
            count_rows = conn.execute(
                f"""SELECT COALESCE(r.resolution_status,'unverified') AS derived_status,COUNT(*) AS count
                    FROM research_note_subjects n {latest_resolution}
                    WHERE n.subject_key=? GROUP BY COALESCE(r.resolution_status,'unverified')""",
                (normalized,),
            ).fetchall()
            rows = conn.execute(
                f"""SELECT n.*,c.text,c.claim_status,c.created_at AS claim_created_at,
                           COALESCE(r.resolution_status,'unverified') AS derived_status
                    FROM research_note_subjects n
                    JOIN claim_records c ON c.claim_id=n.note_claim_id
                    {latest_resolution}
                    WHERE {where}
                    ORDER BY n.created_at DESC,n.note_claim_id DESC LIMIT ?""",
                (*params, capped + 1),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows[:capped]:
                item = dict(row)
                resolutions = conn.execute(
                    "SELECT * FROM research_note_resolutions WHERE note_claim_id=? ORDER BY resolved_at DESC",
                    (item["note_claim_id"],),
                ).fetchall()
                item["resolutions"] = [dict(value) for value in resolutions]
                result.append(item)
        counts = {"unverified": 0, "confirmed": 0, "contradicted": 0, "superseded": 0}
        for row in count_rows:
            counts[str(row["derived_status"])] = int(row["count"] or 0)
        return {
            "subject_key": normalized,
            "notes": result,
            "counts": counts,
            "total_count": sum(counts.values()),
            "next_cursor": result[-1]["note_claim_id"] if len(rows) > capped and result else None,
        }

    def link_report(
        self,
        *,
        report_id: str,
        revision: int,
        symbol: str,
        quality_status: str,
        evidence: list[dict[str, Any]],
        facts: list[dict[str, Any]],
        claims: list[dict[str, Any]],
        claim_support: dict[str, Any] | None = None,
        coverage_snapshot_id: str | None = None,
        base_report_id: str | None = None,
    ) -> dict[str, Any]:
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
            evidence_ids = sorted({
                str(item.get("evidence_id"))
                for item in evidence
                if item.get("evidence_id")
            } | {
                str(evidence_id)
                for item in claims
                for evidence_id in item.get("evidence_ids") or []
                if str(evidence_id)
            } | {
                str(evidence_id)
                for item in facts
                for evidence_id in item.get("evidence_ids") or []
                if str(evidence_id)
            })
            fact_ids = sorted({
                str(item.get("fact_id")) for item in facts if item.get("fact_id")
            } | {
                str(fact_id)
                for item in claims
                for fact_id in item.get("fact_ids") or []
                if str(fact_id)
            })
            conn.execute(
                "INSERT OR REPLACE INTO report_knowledge_links(report_id, revision, evidence_ids_json, fact_ids_json, claim_ids_json, claim_support_json, coverage_snapshot_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    report_id,
                    int(revision),
                    json.dumps(evidence_ids),
                    json.dumps(fact_ids),
                    json.dumps(claim_ids),
                    json.dumps(claim_support or {}, ensure_ascii=False),
                    coverage_snapshot_id,
                ),
            )
        self._write_report_source_links(
            report_id=report_id,
            revision=revision,
            symbol=symbol,
            evidence_ids=set(evidence_ids),
            fact_ids=set(fact_ids),
            claims=[
                {
                    **dict(item),
                    "claim_id": str(
                        item.get("claim_id")
                        or _stable_id("claim", report_id, item.get("section_id"), item.get("text"))
                    ),
                }
                for item in claims
            ],
        )
        self._resolve_research_notes_from_report(
            report_id=report_id,
            symbol=symbol,
            quality_status=quality_status,
            claims=[
                {
                    **dict(item),
                    "claim_id": str(
                        item.get("claim_id")
                        or _stable_id("claim", report_id, item.get("section_id"), item.get("text"))
                    ),
                }
                for item in claims
            ],
        )
        delta = self.compute_delta(report_id=report_id, base_report_id=base_report_id)
        return delta

    def compute_delta(self, *, report_id: str, base_report_id: str | None) -> dict[str, Any]:
        current = self._report_facts(report_id)
        previous = self._report_facts(base_report_id) if base_report_id else []
        old_by_key = self._delta_fact_index(previous)
        new_by_key = self._delta_fact_index(current)
        result: dict[str, Any] = {"base_report_id": base_report_id, "added": [], "updated": [], "confirmed": [], "superseded": [], "contradicted": [], "stale": [], "still_unverified": []}
        for key, item in new_by_key.items():
            prior = old_by_key.get(key)
            if prior is None:
                result["added"].append(self._delta_item(item))
            elif self._values_consistent(
                prior.get("value"), item.get("value"), metric=str(item.get("metric") or "")
            ):
                result["confirmed"].append(self._delta_item(item))
            else:
                result["updated"].append({"before": self._delta_item(prior), "after": self._delta_item(item)})
        for key, item in old_by_key.items():
            if key not in new_by_key:
                result["stale"].append(self._delta_item(item))
        result["contradicted"] = self.unresolved_conflicts(
            self._report_symbol(report_id),
            fact_ids=[str(item.get("fact_id") or "") for item in current],
        )
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO research_deltas(report_id, base_report_id, payload_json, created_at) VALUES (?, ?, ?, ?)", (report_id, base_report_id, json.dumps(result, ensure_ascii=False), _utc_now()))
        return result

    def preview_delta(self, facts: list[dict[str, Any]], *, base_report_id: str | None) -> dict[str, Any]:
        """Compare an unpublished ledger with an immutable prior report."""

        previous = self._report_facts(base_report_id) if base_report_id else []
        old_by_key = self._delta_fact_index(previous)
        current: list[dict[str, Any]] = []
        for raw in facts:
            if not raw.get("fact_id"):
                continue
            item = dict(raw)
            metadata = dict(item.get("metadata") or {})
            item["scope_key"] = str(
                item.get("scope_key")
                or metadata.get("scope_key")
                or metadata.get("scope")
                or (
                    metadata.get("component_symbol")
                    if str(item.get("metric") or "") == "etf_component_weight"
                    else ""
                )
                or ""
            )
            current.append(self._fact_payload(item))
        new_by_key = self._delta_fact_index(current)
        result: dict[str, Any] = {
            "base_report_id": base_report_id,
            "added": [], "updated": [], "confirmed": [], "superseded": [],
            "contradicted": [], "stale": [], "still_unverified": [],
        }
        for key, item in new_by_key.items():
            prior = old_by_key.get(key)
            if prior is None:
                result["added"].append(self._delta_item(item))
            elif self._values_consistent(
                prior.get("value"), item.get("value"), metric=str(item.get("metric") or "")
            ):
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
        metric = str(item.get("metric") or "")
        keys = (
            ("symbol", "metric", "scope_key", "unit", "currency")
            if metric in _IDENTITY_COMPARISON_METRICS
            or metric in _ROLLING_COMPARISON_POLICIES
            else ("symbol", "metric", "period", "scope_key", "unit", "currency")
        )
        return tuple(str(item.get(key) or "") for key in keys)

    @classmethod
    def _delta_fact_index(
        cls, items: Iterable[dict[str, Any]]
    ) -> dict[tuple[str, ...], dict[str, Any]]:
        """Project rolling metrics to their latest observation per report."""

        indexed: dict[tuple[str, ...], dict[str, Any]] = {}
        for item in items:
            key = cls._comparison_key(item)
            current = indexed.get(key)
            if current is None or (
                str(item.get("period") or ""), str(item.get("created_at") or "")
            ) > (
                str(current.get("period") or ""),
                str(current.get("created_at") or ""),
            ):
                indexed[key] = item
        return indexed

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

    def unresolved_conflicts(
        self,
        symbol: str,
        *,
        fact_ids: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        normalized = symbol.strip().upper()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM fact_conflicts WHERE resolution_status='needs_third_source' AND comparison_key LIKE ? ORDER BY created_at DESC",
                (f"{normalized}|%",),
            ).fetchall()
        result = [self._decode_json_columns(dict(row), ("fact_ids_json",)) for row in rows]
        selected = {str(item) for item in fact_ids if str(item)}
        if selected:
            result = [item for item in result if selected & set(item.get("fact_ids") or [])]
        # Conflict rows are immutable audit records, while a Fact can later be
        # corrected to a more precise scope. Only expose conflicts whose live,
        # unsuperseded Facts still share one comparison key.
        live: list[dict[str, Any]] = []
        with self.connect() as conn:
            for item in result:
                ids = [str(value) for value in item.get("fact_ids") or [] if str(value)]
                if len(ids) < 2:
                    continue
                rows = conn.execute(
                    f"SELECT * FROM fact_records WHERE fact_id IN ({','.join('?' for _ in ids)})",
                    ids,
                ).fetchall()
                if len(rows) != len(ids) or any(row["superseded_by"] for row in rows):
                    continue
                keys = {
                    tuple(str(row[key] or "") for key in (
                        "symbol", "metric", "period", "scope_key", "unit", "currency",
                    ))
                    for row in rows
                }
                if any(
                    str(row["metric"] or "") in _NON_CONFLICTING_DERIVED_METRICS
                    for row in rows
                ):
                    continue
                if len(keys) == 1:
                    live.append(item)
        return live

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
        created_at = _utc_now()
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO claim_records(claim_id, origin_type, origin_id, section_id, claim_type, text, fact_ids_json, evidence_ids_json, claim_status, superseded_by, created_at) VALUES (?, 'research_session', ?, NULL, ?, ?, '[]', '[]', ?, NULL, ?)",
                (claim_id, session_id, "opinion", content, status, created_at),
            )
            conn.execute("DELETE FROM claim_records_fts WHERE claim_id=?", (claim_id,))
            conn.execute("INSERT INTO claim_records_fts(claim_id, origin_id, search_text) VALUES (?, ?, ?)", (claim_id, session_id, _search_text(content, (symbol,))))
            if str(symbol or "").strip():
                conn.execute(
                    """INSERT OR REPLACE INTO research_note_subjects(
                           note_claim_id,subject_key,session_id,message_id,role,created_at
                       ) VALUES (?,?,?,?,?,?)""",
                    (
                        claim_id,
                        str(symbol).strip().upper(),
                        session_id,
                        message_id,
                        role,
                        created_at,
                    ),
                )
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

    def backfill_report_source_links(self) -> dict[str, int]:
        """Rebuild document links from stable evidence/fact/claim IDs."""

        with self.connect() as conn:
            rows = conn.execute(
                """SELECT l.*,COALESCE(e.symbol,e.subject_key,'') AS symbol
                   FROM report_knowledge_links l
                   LEFT JOIN report_catalog_entries e ON e.report_id=l.report_id
                   ORDER BY l.report_id,l.revision"""
            ).fetchall()
        counts = {"reports": 0, "links": 0}
        for row in rows:
            try:
                evidence_ids = set(json.loads(row["evidence_ids_json"] or "[]"))
                fact_ids = set(json.loads(row["fact_ids_json"] or "[]"))
                claim_ids = list(json.loads(row["claim_ids_json"] or "[]"))
            except json.JSONDecodeError:
                continue
            claims: list[dict[str, Any]] = []
            if claim_ids:
                with self.connect() as conn:
                    claim_rows = conn.execute(
                        f"SELECT * FROM claim_records WHERE claim_id IN ({','.join('?' for _ in claim_ids)})",
                        claim_ids,
                    ).fetchall()
                claims = [
                    self._decode_json_columns(
                        dict(item),
                        ("fact_ids_json", "evidence_ids_json"),
                    )
                    for item in claim_rows
                ]
            before = 0
            with self.connect() as conn:
                before = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM report_source_links WHERE report_id=? AND revision=?",
                        (row["report_id"], row["revision"]),
                    ).fetchone()[0]
                )
            self._write_report_source_links(
                report_id=str(row["report_id"]),
                revision=int(row["revision"]),
                symbol=str(row["symbol"] or "PORTFOLIO"),
                evidence_ids={str(item) for item in evidence_ids if str(item)},
                fact_ids={str(item) for item in fact_ids if str(item)},
                claims=claims,
            )
            with self.connect() as conn:
                after = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM report_source_links WHERE report_id=? AND revision=?",
                        (row["report_id"], row["revision"]),
                    ).fetchone()[0]
                )
            counts["reports"] += 1
            counts["links"] += max(0, after - before)
        return counts

    def source_archive_integrity(self) -> dict[str, int]:
        with self.connect() as conn:
            orphan_observations = int(
                conn.execute(
                    """SELECT COUNT(*) FROM source_observations o
                       LEFT JOIN source_documents d USING(document_ref)
                       WHERE d.document_ref IS NULL"""
                ).fetchone()[0]
            )
            orphan_report_sources = int(
                conn.execute(
                    """SELECT COUNT(*) FROM report_source_links r
                       LEFT JOIN source_documents d USING(document_ref)
                       WHERE d.document_ref IS NULL"""
                ).fetchone()[0]
            )
            orphan_note_resolutions = int(
                conn.execute(
                    """SELECT COUNT(*) FROM research_note_resolutions r
                       LEFT JOIN claim_records n ON n.claim_id=r.note_claim_id
                       LEFT JOIN claim_records c ON c.claim_id=r.report_claim_id
                       WHERE n.claim_id IS NULL OR c.claim_id IS NULL"""
                ).fetchone()[0]
            )
            orphan_structured_extractions = int(
                conn.execute(
                    """SELECT COUNT(*) FROM structured_document_extractions e
                       LEFT JOIN source_documents d USING(document_ref)
                       WHERE d.document_ref IS NULL"""
                ).fetchone()[0]
            )
            orphan_financial_snapshots = int(
                conn.execute(
                    """SELECT COUNT(*) FROM financial_statement_snapshots s
                       LEFT JOIN structured_document_extractions e USING(extraction_id)
                       LEFT JOIN source_documents d ON d.document_ref=s.document_ref
                       WHERE e.extraction_id IS NULL OR d.document_ref IS NULL"""
                ).fetchone()[0]
            )
        return {
            "orphan_observations": orphan_observations,
            "orphan_report_sources": orphan_report_sources,
            "orphan_note_resolutions": orphan_note_resolutions,
            "orphan_structured_extractions": orphan_structured_extractions,
            "orphan_financial_snapshots": orphan_financial_snapshots,
        }

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
