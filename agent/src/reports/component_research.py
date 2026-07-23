"""P4B1 deterministic component-research discovery and cross-ETF reuse.

This module never calls a model and never creates report prose or artifacts.
Canonical digests contain knowledge IDs only; ETF-specific selection context
is persisted separately in bindings.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from src.research.knowledge import ResearchKnowledgeStore, get_research_knowledge_store

from .contracts import (
    ComponentDigestResolution,
    ComponentResearchDigest,
    ComponentResearchDimension,
    ComponentResearchStatus,
    ETFComponentDigestBinding,
    ETFComponentSelection,
    utc_now,
)
from .etf_research import stable_fingerprint


COMPONENT_RESEARCH_SCHEMA_VERSION = 1
COMPONENT_RESEARCH_RULE_VERSION = "p4b1-v1"
ESTIMATED_INPUT_TOKENS_PER_AVOIDED_CALL = 2_000
ESTIMATED_OUTPUT_TOKENS_PER_AVOIDED_CALL = 600
ESTIMATION_BASIS = (
    "estimate: one component-summary model call avoided for each reusable or "
    "partial_reusable digest; 2000 input and 600 output tokens per call"
)

RESEARCH_DIMENSIONS: tuple[ComponentResearchDimension, ...] = (
    "business_exposure",
    "earnings_trend",
    "valuation",
    "catalysts",
    "risks",
    "holder_governance",
    "material_events",
)
CORE_DIMENSIONS: frozenset[ComponentResearchDimension] = frozenset(
    {"business_exposure", "earnings_trend", "risks"}
)
FRESHNESS_DAYS: dict[ComponentResearchDimension, int] = {
    "business_exposure": 90,
    "earnings_trend": 120,
    "valuation": 30,
    "catalysts": 30,
    "risks": 90,
    "holder_governance": 120,
    "material_events": 30,
}

_QUALIFIED_SYMBOL = re.compile(
    r"^(?:\d{6}\.(?:SH|SZ|BJ)|\d{5}\.HK|[A-Z][A-Z0-9.-]{0,14}\.US)$",
    re.I,
)
_REJECTED_EVIDENCE_STATUSES = {
    "failed_validation", "invalid", "rejected", "superseded", "unverified",
}
_VALID_FACT_STATUSES = {"pass", "warning", "not_comparable"}

# Exact section mappings take precedence.  Chinese headings are retained
# because legacy report indexing used display headings as section IDs.
SECTION_DIMENSION_MAP: dict[str, tuple[ComponentResearchDimension, ...]] = {
    "business_exposure": ("business_exposure",),
    "earnings_trend": ("earnings_trend",),
    "catalysts": ("catalysts",),
    "risks": ("risks",),
    "holder_governance": ("holder_governance",),
    "material_events": ("material_events",),
    "business_position": ("business_exposure",),
    "公司业务与产业位置": ("business_exposure",),
    "financial_quality": ("earnings_trend",),
    "三张报表与财务质量": ("earnings_trend",),
    "accounting_review": ("risks",),
    "会计科目异常与核查清单": ("risks",),
    "valuation": ("valuation",),
    "implied_expectations": ("valuation",),
    "市值隐含预期": ("valuation",),
    "terminal_narrative": ("business_exposure", "catalysts"),
    "长期经营情景与叙事阶段": ("business_exposure", "catalysts"),
    "counter_thesis": ("risks", "catalysts"),
    "反方论证、风险与催化剂": ("risks", "catalysts"),
    "conclusion_watchlist": ("catalysts", "risks", "material_events"),
    "结论与跟踪框架": ("catalysts", "risks", "material_events"),
    "daily_reason": ("material_events",),
    "daily_risk": ("risks",),
    "daily_condition": ("catalysts", "material_events"),
    "daily_invalidation": ("risks",),
    "portfolio_risk": ("risks",),
}

# Bounded fallback rules are deliberately small and auditable.  They classify
# research dimensions only; they are never used to associate a security.
KEYWORD_DIMENSION_RULES: dict[ComponentResearchDimension, tuple[str, ...]] = {
    "business_exposure": (
        "主营", "业务", "产品", "客户", "供应链", "行业", "竞争格局", "市占率",
        "business", "industry", "customer",
    ),
    "earnings_trend": (
        "营收", "收入", "利润", "毛利", "现金流", "业绩", "财报",
        "revenue", "earnings", "profit",
    ),
    "valuation": (
        "估值", "市盈率", "市净率", "市值隐含", "折现率", "pe", "pb", "valuation",
    ),
    "catalysts": (
        "催化", "订单", "放量", "新产品", "产能", "政策支持", "增长驱动", "catalyst",
    ),
    "risks": (
        "风险", "反方", "下行", "减值", "竞争加剧", "不确定", "失效条件", "risk",
    ),
    "holder_governance": (
        "股东", "持仓", "机构", "治理", "董事", "控股", "holder", "governance",
    ),
    "material_events": (
        "公告", "重大事件", "回购", "并购", "重组", "处罚", "解禁", "material event",
    ),
}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")
    )


def _loads(value: Any, fallback: Any) -> Any:
    if value is None or value == "":
        return fallback
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _parse_time(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(raw, "%Y%m%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalized_time(value: Any, *, field_name: str) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        raise ValueError(f"{field_name} must be a timezone-aware ISO timestamp")
    return parsed.isoformat()


def normalize_component_symbol(value: str) -> str:
    """Require an already market-qualified code; never infer from a name."""

    symbol = str(value or "").strip().upper()
    if not _QUALIFIED_SYMBOL.fullmatch(symbol):
        raise ValueError("component research requires a market-qualified security symbol")
    return symbol


def _dimensions_for_claim(section_id: str | None, text: str) -> tuple[
    list[ComponentResearchDimension], list[str]
]:
    section = str(section_id or "").strip()
    mapped = SECTION_DIMENSION_MAP.get(section) or SECTION_DIMENSION_MAP.get(section.casefold())
    if mapped:
        return list(mapped), [f"section:{section}->{item}" for item in mapped]

    lowered = str(text or "").casefold()
    dimensions: list[ComponentResearchDimension] = []
    reasons: list[str] = []
    for dimension in RESEARCH_DIMENSIONS:
        for keyword in KEYWORD_DIMENSION_RULES[dimension]:
            if keyword.casefold() in lowered:
                dimensions.append(dimension)
                reasons.append(f"keyword:{keyword}->{dimension}")
                break
    return dimensions, reasons


def _claim_ids_from_viewpoint(row: sqlite3.Row) -> set[str]:
    result = {str(row["summary_claim_id"] or "")}
    for key in (
        "reason_claim_ids_json", "risk_claim_ids_json", "condition_claim_ids_json",
        "invalidation_claim_ids_json",
    ):
        result.update(str(item) for item in _loads(row[key], []) if str(item))
    result.discard("")
    return result


@dataclass(slots=True)
class _ClaimCandidate:
    claim_id: str
    report_id: str
    report_kind: str
    report_quality: str
    report_coverage: str
    report_data_as_of: str
    section_id: str
    dimensions: list[ComponentResearchDimension]
    selection_reasons: list[str]
    fact_ids: list[str]
    evidence_ids: list[str]
    expires_by_dimension: dict[ComponentResearchDimension, str]
    stale_dimensions: set[ComponentResearchDimension]
    active: bool
    has_lineage: bool


@dataclass(slots=True)
class _KnowledgeDiscovery:
    digest: ComponentResearchDigest
    persistable: bool


class ComponentResearchDigestStore:
    """P4B1 tables co-located in the shared research-cache SQLite database."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        knowledge_store: ResearchKnowledgeStore | None = None,
    ) -> None:
        if knowledge_store is not None:
            path = Path(knowledge_store.path)
        self.path = path or Path(
            os.getenv(
                "VIBE_TRADING_RESEARCH_CACHE_DB",
                "~/.vibe-trading/cache/research_cache.sqlite3",
            )
        ).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        with self._lock, self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS component_research_digests (
                    digest_id TEXT PRIMARY KEY,
                    component_symbol TEXT NOT NULL,
                    analysis_as_of TEXT NOT NULL,
                    research_data_as_of TEXT,
                    freshness_expires_at TEXT,
                    status TEXT NOT NULL,
                    quality TEXT NOT NULL,
                    knowledge_fingerprint TEXT NOT NULL,
                    input_fingerprint TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(component_symbol, input_fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_component_digest_symbol_time
                    ON component_research_digests(component_symbol, analysis_as_of DESC);
                CREATE INDEX IF NOT EXISTS idx_component_digest_input
                    ON component_research_digests(component_symbol, input_fingerprint);

                CREATE TABLE IF NOT EXISTS etf_component_digest_bindings (
                    binding_id TEXT PRIMARY KEY,
                    etf_symbol TEXT NOT NULL,
                    selection_id TEXT NOT NULL,
                    component_symbol TEXT NOT NULL,
                    digest_id TEXT,
                    digest_status TEXT NOT NULL,
                    selection_data_as_of TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(selection_id, component_symbol),
                    FOREIGN KEY(digest_id) REFERENCES component_research_digests(digest_id)
                );
                CREATE INDEX IF NOT EXISTS idx_component_binding_selection
                    ON etf_component_digest_bindings(selection_id, component_symbol);
                CREATE INDEX IF NOT EXISTS idx_component_binding_digest
                    ON etf_component_digest_bindings(digest_id);

                CREATE TABLE IF NOT EXISTS component_digest_resolutions (
                    resolution_id TEXT PRIMARY KEY,
                    etf_symbol TEXT NOT NULL,
                    selection_id TEXT NOT NULL,
                    analysis_as_of TEXT NOT NULL,
                    knowledge_fingerprint TEXT NOT NULL,
                    input_fingerprint TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(selection_id, input_fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_component_resolution_selection
                    ON component_digest_resolutions(selection_id, analysis_as_of DESC);
                CREATE INDEX IF NOT EXISTS idx_component_resolution_etf
                    ON component_digest_resolutions(etf_symbol, analysis_as_of DESC);

                CREATE TABLE IF NOT EXISTS component_research_audit (
                    audit_id TEXT PRIMARY KEY,
                    etf_symbol TEXT,
                    component_symbol TEXT,
                    operation TEXT NOT NULL,
                    object_id TEXT,
                    cache_hit INTEGER NOT NULL DEFAULT 0,
                    digest_status TEXT,
                    avoided_model_calls INTEGER NOT NULL DEFAULT 0,
                    model_calls INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_component_research_audit_time
                    ON component_research_audit(created_at DESC);
                """
            )

    def audit(
        self,
        *,
        operation: str,
        etf_symbol: str | None = None,
        component_symbol: str | None = None,
        object_id: str | None = None,
        cache_hit: bool = False,
        digest_status: str | None = None,
        avoided_model_calls: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO component_research_audit(
                    audit_id,etf_symbol,component_symbol,operation,object_id,cache_hit,
                    digest_status,avoided_model_calls,model_calls,input_tokens,output_tokens,
                    metadata_json,created_at
                ) VALUES (?,?,?,?,?,?,?,?,0,0,0,?,?)""",
                (
                    f"compaudit_{uuid.uuid4().hex[:20]}", etf_symbol, component_symbol,
                    operation, object_id, int(cache_hit), digest_status,
                    max(0, int(avoided_model_calls)), _canonical_json(metadata or {}), utc_now(),
                ),
            )

    def get_digest_by_input(
        self, component_symbol: str, input_fingerprint: str
    ) -> ComponentResearchDigest | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT payload_json FROM component_research_digests
                   WHERE component_symbol=? AND input_fingerprint=?""",
                (component_symbol, input_fingerprint),
            ).fetchone()
        return ComponentResearchDigest.from_dict(_loads(row["payload_json"], {})) if row else None

    def get_digest(self, digest_id: str) -> ComponentResearchDigest | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM component_research_digests WHERE digest_id=?",
                (digest_id,),
            ).fetchone()
        return ComponentResearchDigest.from_dict(_loads(row["payload_json"], {})) if row else None

    def current_digest(
        self, component_symbol: str, *, analysis_as_of: str | None = None
    ) -> ComponentResearchDigest | None:
        sql = (
            "SELECT payload_json FROM component_research_digests "
            "WHERE component_symbol=?"
        )
        params: list[Any] = [component_symbol]
        if analysis_as_of:
            sql += " AND analysis_as_of<=?"
            params.append(analysis_as_of)
        sql += " ORDER BY analysis_as_of DESC,created_at DESC LIMIT 1"
        with self.connect() as connection:
            row = connection.execute(sql, params).fetchone()
        return ComponentResearchDigest.from_dict(_loads(row["payload_json"], {})) if row else None

    def save_digest(self, digest: ComponentResearchDigest) -> ComponentResearchDigest:
        with self._lock, self.connect() as connection:
            connection.execute(
                """INSERT INTO component_research_digests(
                    digest_id,component_symbol,analysis_as_of,research_data_as_of,
                    freshness_expires_at,status,quality,knowledge_fingerprint,
                    input_fingerprint,payload_json,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(component_symbol,input_fingerprint) DO NOTHING""",
                (
                    digest.digest_id, digest.component_symbol, digest.analysis_as_of,
                    digest.research_data_as_of, digest.freshness_expires_at,
                    digest.status, digest.quality, digest.knowledge_fingerprint,
                    digest.input_fingerprint, _canonical_json(digest.to_dict()), digest.created_at,
                ),
            )
        return self.get_digest_by_input(
            digest.component_symbol, digest.input_fingerprint
        ) or digest

    def get_resolution_by_input(
        self, selection_id: str, input_fingerprint: str
    ) -> ComponentDigestResolution | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT payload_json FROM component_digest_resolutions
                   WHERE selection_id=? AND input_fingerprint=?""",
                (selection_id, input_fingerprint),
            ).fetchone()
        if not row:
            return None
        return ComponentDigestResolution.from_dict(_loads(row["payload_json"], {}))

    def resolution_for_selection(self, selection_id: str) -> ComponentDigestResolution | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT payload_json FROM component_digest_resolutions
                   WHERE selection_id=? ORDER BY analysis_as_of DESC,created_at DESC LIMIT 1""",
                (selection_id,),
            ).fetchone()
        return ComponentDigestResolution.from_dict(_loads(row["payload_json"], {})) if row else None

    def latest_resolution_for_etf(self, etf_symbol: str) -> ComponentDigestResolution | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT payload_json FROM component_digest_resolutions
                   WHERE etf_symbol=? ORDER BY analysis_as_of DESC,created_at DESC LIMIT 1""",
                (etf_symbol,),
            ).fetchone()
        return ComponentDigestResolution.from_dict(_loads(row["payload_json"], {})) if row else None

    def save_resolution(self, resolution: ComponentDigestResolution) -> ComponentDigestResolution:
        stored_payload = resolution.to_dict()
        stored_payload["cache_hit"] = False
        with self._lock, self.connect() as connection:
            for binding in resolution.bindings:
                connection.execute(
                    """INSERT INTO etf_component_digest_bindings(
                        binding_id,etf_symbol,selection_id,component_symbol,digest_id,
                        digest_status,selection_data_as_of,payload_json,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(selection_id,component_symbol) DO UPDATE SET
                        binding_id=excluded.binding_id,digest_id=excluded.digest_id,
                        digest_status=excluded.digest_status,payload_json=excluded.payload_json""",
                    (
                        binding.binding_id, binding.etf_symbol, binding.selection_id,
                        binding.component_symbol, binding.digest_id, binding.digest_status,
                        binding.selection_data_as_of, _canonical_json(binding.to_dict()),
                        binding.created_at,
                    ),
                )
            connection.execute(
                """INSERT INTO component_digest_resolutions(
                    resolution_id,etf_symbol,selection_id,analysis_as_of,
                    knowledge_fingerprint,input_fingerprint,payload_json,created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(selection_id,input_fingerprint) DO NOTHING""",
                (
                    resolution.resolution_id, resolution.etf_symbol, resolution.selection_id,
                    resolution.analysis_as_of, resolution.knowledge_fingerprint,
                    resolution.input_fingerprint, _canonical_json(stored_payload), utc_now(),
                ),
            )
        return self.get_resolution_by_input(
            resolution.selection_id, resolution.input_fingerprint
        ) or resolution

    def metrics(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT COUNT(*) requests,SUM(cache_hit) cache_hits,
                          SUM(CASE WHEN operation='digest_build' THEN 1 ELSE 0 END) digest_builds,
                          SUM(CASE WHEN operation='digest_cache_hit' THEN 1 ELSE 0 END) digest_cache_hits,
                          SUM(avoided_model_calls) avoided_model_calls,
                          SUM(model_calls) model_calls,SUM(input_tokens) input_tokens,
                          SUM(output_tokens) output_tokens
                   FROM component_research_audit"""
            ).fetchone()
            shared = connection.execute(
                """SELECT COUNT(*) FROM (
                       SELECT digest_id FROM etf_component_digest_bindings
                       WHERE digest_id IS NOT NULL GROUP BY digest_id
                       HAVING COUNT(DISTINCT etf_symbol)>1
                   )"""
            ).fetchone()[0]
        requests = int(row["requests"] or 0)
        hits = int(row["cache_hits"] or 0)
        return {
            "requests": requests,
            "cache_hits": hits,
            "cache_hit_ratio": round(hits / requests, 4) if requests else 0.0,
            "digest_builds": int(row["digest_builds"] or 0),
            "digest_cache_hits": int(row["digest_cache_hits"] or 0),
            "cross_etf_shared_digest_count": int(shared or 0),
            "estimated_avoided_model_calls": int(row["avoided_model_calls"] or 0),
            "model_calls": int(row["model_calls"] or 0),
            "input_tokens": int(row["input_tokens"] or 0),
            "output_tokens": int(row["output_tokens"] or 0),
        }


class ComponentResearchDigestService:
    """Resolve P4A selections against published, time-bounded knowledge."""

    def __init__(
        self,
        knowledge_store: ResearchKnowledgeStore | None = None,
        *,
        store: ComponentResearchDigestStore | None = None,
        now_provider: Callable[[], str] = utc_now,
    ) -> None:
        self.knowledge = knowledge_store or get_research_knowledge_store()
        self.store = store or ComponentResearchDigestStore(knowledge_store=self.knowledge)
        self.now_provider = now_provider
        self._flight_guard = threading.Lock()
        self._flights: dict[str, threading.Lock] = {}

    def _discover(self, component_symbol: str, analysis_as_of: str) -> _KnowledgeDiscovery:
        cutoff = _parse_time(analysis_as_of)
        if cutoff is None:
            raise ValueError("invalid analysis_as_of")
        warnings: list[str] = []
        future_report_count = 0
        future_claim_count = 0
        reports_without_claims = 0

        with self.knowledge.connect() as connection:
            raw_reports = connection.execute(
                """SELECT * FROM report_catalog_entries
                   WHERE subject_type='symbol' AND subject_key=? AND UPPER(COALESCE(symbol,''))=?
                   ORDER BY data_as_of DESC,generated_at DESC,source_revision DESC""",
                (component_symbol, component_symbol),
            ).fetchall()
            reports: list[dict[str, Any]] = []
            link_by_report: dict[str, dict[str, Any]] = {}
            for raw in raw_reports:
                report = dict(raw)
                report_time = _parse_time(report.get("data_as_of"))
                generated = _parse_time(report.get("generated_at"))
                if report_time is None or report_time > cutoff or generated is None or generated > cutoff:
                    future_report_count += 1
                    continue
                if report.get("status") != "published" or report.get("report_quality_status") == "failed_validation":
                    continue
                link = connection.execute(
                    """SELECT * FROM report_knowledge_links
                       WHERE report_id=? AND revision=? LIMIT 1""",
                    (report["report_id"], int(report.get("source_revision") or 1)),
                ).fetchone()
                if link is None:
                    reports_without_claims += 1
                    continue
                link_value = dict(link)
                link_value["claim_ids"] = [str(item) for item in _loads(link["claim_ids_json"], [])]
                link_value["fact_ids"] = [str(item) for item in _loads(link["fact_ids_json"], [])]
                link_value["evidence_ids"] = [
                    str(item) for item in _loads(link["evidence_ids_json"], [])
                ]
                if not link_value["claim_ids"]:
                    reports_without_claims += 1
                reports.append(report)
                link_by_report[str(report["report_id"])] = link_value

            report_by_id = {str(item["report_id"]): item for item in reports}
            all_claim_ids = sorted({
                claim_id
                for item in link_by_report.values()
                for claim_id in item["claim_ids"]
                if claim_id
            })
            claims: dict[str, dict[str, Any]] = {}
            if all_claim_ids:
                rows = connection.execute(
                    f"SELECT * FROM claim_records WHERE claim_id IN ({','.join('?' for _ in all_claim_ids)})",
                    all_claim_ids,
                ).fetchall()
                for raw in rows:
                    item = dict(raw)
                    if item.get("origin_type") != "report" or item.get("origin_id") not in report_by_id:
                        continue
                    created = _parse_time(item.get("created_at"))
                    if created is None or created > cutoff:
                        future_claim_count += 1
                        continue
                    item["fact_ids"] = [str(value) for value in _loads(item.pop("fact_ids_json"), [])]
                    item["evidence_ids"] = [
                        str(value) for value in _loads(item.pop("evidence_ids_json"), [])
                    ]
                    claims[str(item["claim_id"])] = item

            all_fact_ids = sorted({
                fact_id
                for claim in claims.values()
                for fact_id in claim["fact_ids"]
                if fact_id
            } | {
                fact_id
                for link in link_by_report.values()
                for fact_id in link["fact_ids"]
                if fact_id
            })
            facts: dict[str, dict[str, Any]] = {}
            if all_fact_ids:
                rows = connection.execute(
                    f"SELECT * FROM fact_records WHERE fact_id IN ({','.join('?' for _ in all_fact_ids)})",
                    all_fact_ids,
                ).fetchall()
                for raw in rows:
                    item = dict(raw)
                    created = _parse_time(item.get("created_at"))
                    if item.get("symbol") != component_symbol or created is None or created > cutoff:
                        continue
                    item["evidence_ids"] = [
                        str(value) for value in _loads(item.pop("evidence_ids_json"), [])
                    ]
                    item["input_fact_ids"] = [
                        str(value) for value in _loads(item.pop("input_fact_ids_json"), [])
                    ]
                    facts[str(item["fact_id"])] = item

            all_evidence_ids = sorted({
                evidence_id
                for claim in claims.values()
                for evidence_id in claim["evidence_ids"]
                if evidence_id
            } | {
                evidence_id
                for fact in facts.values()
                for evidence_id in fact["evidence_ids"]
                if evidence_id
            } | {
                evidence_id
                for link in link_by_report.values()
                for evidence_id in link["evidence_ids"]
                if evidence_id
            })
            evidence: dict[str, dict[str, Any]] = {}
            if all_evidence_ids:
                rows = connection.execute(
                    f"""SELECT e.*,d.published_at AS document_published_at,
                               d.retrieved_at AS document_retrieved_at,d.superseded_by AS document_superseded_by
                        FROM evidence_records e JOIN source_documents d USING(document_ref)
                        WHERE e.evidence_id IN ({','.join('?' for _ in all_evidence_ids)})""",
                    all_evidence_ids,
                ).fetchall()
                for raw in rows:
                    item = dict(raw)
                    effective = (
                        _parse_time(item.get("valid_from"))
                        or _parse_time(item.get("document_published_at"))
                        or _parse_time(item.get("created_at"))
                    )
                    if item.get("symbol") != component_symbol or effective is None or effective > cutoff:
                        continue
                    item["chunk_refs"] = _loads(item.pop("chunk_refs_json"), [])
                    evidence[str(item["evidence_id"])] = item

            viewpoint_expiries: dict[str, list[str]] = {}
            if report_by_id:
                rows = connection.execute(
                    f"SELECT * FROM report_viewpoints WHERE report_id IN ({','.join('?' for _ in report_by_id)})",
                    list(report_by_id),
                ).fetchall()
                for row in rows:
                    valid_until = str(row["valid_until"] or "")
                    if not valid_until:
                        continue
                    for claim_id in _claim_ids_from_viewpoint(row):
                        viewpoint_expiries.setdefault(claim_id, []).append(valid_until)

            candidates: list[_ClaimCandidate] = []
            for claim_id in sorted(claims):
                claim = claims[claim_id]
                report = report_by_id[str(claim["origin_id"])]
                dimensions, reasons = _dimensions_for_claim(
                    claim.get("section_id"), str(claim.get("text") or "")
                )
                if not dimensions:
                    continue
                valid_fact_ids = [
                    item for item in claim["fact_ids"]
                    if item in facts
                    and facts[item].get("validation_status") in _VALID_FACT_STATUSES
                    and not facts[item].get("superseded_by")
                ]
                valid_evidence_ids = [
                    item for item in claim["evidence_ids"]
                    if item in evidence
                    and str(evidence[item].get("status") or "").casefold()
                    not in _REJECTED_EVIDENCE_STATUSES
                    and not evidence[item].get("document_superseded_by")
                ]
                active = (
                    claim.get("claim_status") == "prior_claim"
                    and not claim.get("superseded_by")
                )
                has_lineage = bool(valid_fact_ids or valid_evidence_ids)
                expiries: dict[ComponentResearchDimension, str] = {}
                stale_dimensions: set[ComponentResearchDimension] = set()
                report_time = _parse_time(report["data_as_of"])
                assert report_time is not None
                explicit_expiries = [
                    value for value in viewpoint_expiries.get(claim_id, []) if _parse_time(value)
                ]
                explicit_expiries.extend(
                    str(evidence[item].get("valid_until") or "")
                    for item in valid_evidence_ids
                    if _parse_time(evidence[item].get("valid_until"))
                )
                for dimension in dimensions:
                    expiry = report_time + timedelta(days=FRESHNESS_DAYS[dimension])
                    explicit = [_parse_time(value) for value in explicit_expiries]
                    expiry_candidates = [item for item in explicit if item is not None]
                    if expiry_candidates:
                        expiry = min([expiry, *expiry_candidates])
                    expiries[dimension] = expiry.isoformat()
                    if not active or expiry < cutoff:
                        stale_dimensions.add(dimension)
                    if dimension == "valuation" and any(
                        token in (str(claim.get("section_id") or "") + str(claim.get("text") or "")).casefold()
                        for token in ("implied_expectations", "市值隐含", "当前价格", "市盈率", "市净率", "pe", "pb")
                    ):
                        stale_dimensions.add(dimension)
                        warnings.append("price_sensitive_valuation_without_verified_market_snapshot")
                candidates.append(_ClaimCandidate(
                    claim_id=claim_id,
                    report_id=str(report["report_id"]),
                    report_kind=str(report["report_kind"]),
                    report_quality=str(report["report_quality_status"]),
                    report_coverage=str(report["coverage_status"]),
                    report_data_as_of=str(report["data_as_of"]),
                    section_id=str(claim.get("section_id") or ""),
                    dimensions=dimensions,
                    selection_reasons=reasons,
                    fact_ids=valid_fact_ids,
                    evidence_ids=valid_evidence_ids,
                    expires_by_dimension=expiries,
                    stale_dimensions=stale_dimensions,
                    active=active,
                    has_lineage=has_lineage,
                ))

            candidate_fact_ids = sorted({
                fact_id for candidate in candidates for fact_id in candidate.fact_ids
            })
            conflicts: list[dict[str, Any]] = []
            rows = connection.execute(
                """SELECT * FROM fact_conflicts
                   WHERE resolution_status='needs_third_source' AND comparison_key LIKE ?
                   ORDER BY conflict_id""",
                (f"{component_symbol}|%",),
            ).fetchall()
            selected_fact_set = set(candidate_fact_ids)
            for raw in rows:
                item = dict(raw)
                created = _parse_time(item.get("created_at"))
                fact_ids = [str(value) for value in _loads(item.pop("fact_ids_json"), [])]
                if created is not None and created <= cutoff and selected_fact_set.intersection(fact_ids):
                    item["fact_ids"] = fact_ids
                    conflicts.append(item)

        if future_report_count:
            warnings.append(f"future_reports_excluded:{future_report_count}")
        if future_claim_count:
            warnings.append(f"future_claims_excluded:{future_claim_count}")
        if reports_without_claims:
            warnings.append(f"reports_without_indexed_claims:{reports_without_claims}")
        if candidates and any(not item.has_lineage for item in candidates):
            warnings.append("claims_without_fact_or_evidence_lineage")

        def rank(candidate: _ClaimCandidate, dimension: ComponentResearchDimension) -> tuple[Any, ...]:
            fresh = dimension not in candidate.stale_dimensions
            quality_rank = 2 if candidate.report_quality == "passed" else 1
            coverage_rank = 2 if candidate.report_coverage == "complete" else 1
            structural_rank = 1 if candidate.report_kind in {"deep_research", "weekly_review"} else 0
            if dimension in {"catalysts", "material_events"}:
                structural_rank = 1 - structural_rank
            return (
                int(fresh), quality_rank, coverage_rank, structural_rank,
                _parse_time(candidate.report_data_as_of) or datetime.min.replace(tzinfo=timezone.utc),
                candidate.claim_id,
            )

        selected_by_dimension: dict[ComponentResearchDimension, list[_ClaimCandidate]] = {}
        coverage_dimensions: list[ComponentResearchDimension] = []
        stale_dimensions: list[ComponentResearchDimension] = []
        missing_dimensions: list[ComponentResearchDimension] = []
        for dimension in RESEARCH_DIMENSIONS:
            dimension_candidates = [item for item in candidates if dimension in item.dimensions]
            dimension_candidates.sort(key=lambda item: rank(item, dimension), reverse=True)
            fresh = [item for item in dimension_candidates if dimension not in item.stale_dimensions]
            selected = (fresh or dimension_candidates)[:8]
            selected_by_dimension[dimension] = selected
            if fresh:
                coverage_dimensions.append(dimension)
            elif dimension_candidates:
                stale_dimensions.append(dimension)
            else:
                missing_dimensions.append(dimension)

        selected_candidates = {
            item.claim_id: item
            for values in selected_by_dimension.values()
            for item in values
        }
        selected_fact_ids = sorted({
            fact_id for item in selected_candidates.values() for fact_id in item.fact_ids
        })
        selected_evidence_ids = sorted({
            evidence_id for item in selected_candidates.values() for evidence_id in item.evidence_ids
        })
        selected_report_ids = sorted({item.report_id for item in selected_candidates.values()})
        selected_conflicts = [
            item for item in conflicts
            if set(item.get("fact_ids") or []).intersection(selected_fact_ids)
        ]
        claim_ids_by_dimension = {
            dimension: [item.claim_id for item in selected_by_dimension[dimension]]
            for dimension in RESEARCH_DIMENSIONS
        }
        claim_selection_reasons = {
            item.claim_id: list(item.selection_reasons)
            for item in selected_candidates.values()
        }

        knowledge_payload = {
            "schema_version": COMPONENT_RESEARCH_SCHEMA_VERSION,
            "rule_version": COMPONENT_RESEARCH_RULE_VERSION,
            "component_symbol": component_symbol,
            "reports": [{
                "report_id": item["report_id"],
                "source_revision": item["source_revision"],
                "report_kind": item["report_kind"],
                "quality": item["report_quality_status"],
                "coverage": item["coverage_status"],
                "data_as_of": item["data_as_of"],
                "generated_at": item["generated_at"],
            } for item in reports],
            "claims": [{
                "claim_id": item.get("claim_id"),
                "origin_id": item.get("origin_id"),
                "section_id": item.get("section_id"),
                "claim_type": item.get("claim_type"),
                "claim_status": item.get("claim_status"),
                "superseded_by": item.get("superseded_by"),
                "created_at": item.get("created_at"),
                "fact_ids": item.get("fact_ids"),
                "evidence_ids": item.get("evidence_ids"),
            } for item in sorted(claims.values(), key=lambda value: str(value.get("claim_id")))],
            "facts": [{
                "fact_id": item.get("fact_id"),
                "validation_status": item.get("validation_status"),
                "superseded_by": item.get("superseded_by"),
                "created_at": item.get("created_at"),
                "evidence_ids": item.get("evidence_ids"),
            } for item in sorted(facts.values(), key=lambda value: str(value.get("fact_id")))],
            "evidence": [{
                "evidence_id": item.get("evidence_id"),
                "status": item.get("status"),
                "valid_from": item.get("valid_from"),
                "valid_until": item.get("valid_until"),
                "document_published_at": item.get("document_published_at"),
                "document_superseded_by": item.get("document_superseded_by"),
            } for item in sorted(evidence.values(), key=lambda value: str(value.get("evidence_id")))],
            "conflicts": [{
                "conflict_id": item.get("conflict_id"),
                "fact_ids": item.get("fact_ids"),
                "resolution_status": item.get("resolution_status"),
                "created_at": item.get("created_at"),
            } for item in selected_conflicts],
            "coverage_dimensions": coverage_dimensions,
            "stale_dimensions": stale_dimensions,
        }
        knowledge_fingerprint = stable_fingerprint("componentknowledge", knowledge_payload)
        input_fingerprint = stable_fingerprint("componentinput", {
            "component_symbol": component_symbol,
            "analysis_as_of": analysis_as_of,
            "knowledge_fingerprint": knowledge_fingerprint,
            "rule_version": COMPONENT_RESEARCH_RULE_VERSION,
        })

        if selected_conflicts:
            status: ComponentResearchStatus = "conflicted"
            quality = "insufficient"
        elif coverage_dimensions:
            all_dimensions_fresh = set(coverage_dimensions) == set(RESEARCH_DIMENSIONS)
            complete_lineage = all(item.has_lineage for item in selected_candidates.values())
            complete_sources = all(
                item.report_quality == "passed" and item.report_coverage == "complete"
                for item in selected_candidates.values()
            )
            if all_dimensions_fresh and complete_lineage and complete_sources:
                status = "reusable"
                quality = "complete"
            else:
                status = "partial_reusable"
                quality = "partial"
        elif stale_dimensions:
            status = "stale"
            quality = "insufficient"
        else:
            status = "missing"
            quality = "insufficient"

        selected_times = [
            _parse_time(item.report_data_as_of) for item in selected_candidates.values()
        ]
        research_data_as_of = max(
            (item for item in selected_times if item is not None), default=None
        )
        fresh_expiries = [
            _parse_time(item.expires_by_dimension[dimension])
            for dimension, values in selected_by_dimension.items()
            if dimension in coverage_dimensions
            for item in values
            if dimension not in item.stale_dimensions
        ]
        freshness_expires_at = min(
            (item for item in fresh_expiries if item is not None), default=None
        )
        latest_report = max(
            reports,
            key=lambda item: _parse_time(item["data_as_of"])
            or datetime.min.replace(tzinfo=timezone.utc),
            default=None,
        )
        digest_id = stable_fingerprint("componentdigest", {
            "component_symbol": component_symbol,
            "analysis_as_of": analysis_as_of,
            "knowledge_fingerprint": knowledge_fingerprint,
            "rule_version": COMPONENT_RESEARCH_RULE_VERSION,
        })
        digest = ComponentResearchDigest(
            digest_id=digest_id,
            schema_version=COMPONENT_RESEARCH_SCHEMA_VERSION,
            component_symbol=component_symbol,
            security_name=(
                str(latest_report.get("security_name") or component_symbol)
                if latest_report else component_symbol
            ),
            entity_id=None,
            analysis_as_of=analysis_as_of,
            research_data_as_of=research_data_as_of.isoformat() if research_data_as_of else None,
            created_at=self.now_provider(),
            freshness_expires_at=(
                freshness_expires_at.isoformat() if freshness_expires_at else None
            ),
            status=status,
            quality=quality,  # type: ignore[arg-type]
            coverage_dimensions=coverage_dimensions,
            missing_dimensions=missing_dimensions,
            stale_dimensions=stale_dimensions,
            source_report_ids=selected_report_ids,
            claim_ids_by_dimension=claim_ids_by_dimension,
            fact_ids=selected_fact_ids,
            evidence_ids=selected_evidence_ids,
            conflict_ids=[str(item["conflict_id"]) for item in selected_conflicts],
            knowledge_fingerprint=knowledge_fingerprint,
            input_fingerprint=input_fingerprint,
            claim_selection_reasons=claim_selection_reasons,
            warnings=sorted(set(warnings)),
        )
        # ``missing`` is persisted as a structural zero-knowledge cache record,
        # not as generated research prose.  Bindings still keep ``digest_id``
        # null for missing components, while repeated selections avoid rebuilding
        # the same deterministic absence result.
        return _KnowledgeDiscovery(digest=digest, persistable=True)

    def _digest_for_component(
        self, component_symbol: str, analysis_as_of: str
    ) -> tuple[ComponentResearchDigest, bool]:
        discovery = self._discover(component_symbol, analysis_as_of)
        flight_key = discovery.digest.input_fingerprint
        with self._flight_guard:
            flight_lock = self._flights.setdefault(flight_key, threading.Lock())
        with flight_lock:
            cached = self.store.get_digest_by_input(
                component_symbol, discovery.digest.input_fingerprint
            )
            if cached is not None:
                self.store.audit(
                    operation="digest_cache_hit", component_symbol=component_symbol,
                    object_id=cached.digest_id, cache_hit=True, digest_status=cached.status,
                    metadata={"input_fingerprint": cached.input_fingerprint},
                )
                return cached, True
            digest = (
                self.store.save_digest(discovery.digest)
                if discovery.persistable else discovery.digest
            )
            self.store.audit(
                operation="digest_build", component_symbol=component_symbol,
                object_id=digest.digest_id if discovery.persistable else None,
                digest_status=digest.status,
                metadata={
                    "input_fingerprint": digest.input_fingerprint,
                    "persisted": discovery.persistable,
                },
            )
            return digest, False

    def resolve_selection(
        self,
        selection: ETFComponentSelection,
        analysis_as_of: str,
        *,
        selection_data_as_of: str | None = None,
    ) -> ComponentDigestResolution:
        normalized_analysis = _normalized_time(analysis_as_of, field_name="analysis_as_of")
        normalized_selection_time = _normalized_time(
            selection_data_as_of or analysis_as_of,
            field_name="selection_data_as_of",
        )
        if _parse_time(normalized_selection_time) > _parse_time(normalized_analysis):  # type: ignore[operator]
            raise ValueError("selection_data_as_of must not be later than analysis_as_of")
        etf_symbol = normalize_component_symbol(selection.etf_symbol)
        component_rows: list[tuple[Any, ComponentResearchDigest, bool]] = []
        for selected in selection.selected:
            symbol = normalize_component_symbol(selected.symbol)
            digest, cache_hit = self._digest_for_component(symbol, normalized_analysis)
            component_rows.append((selected, digest, cache_hit))

        resolution_knowledge_fingerprint = stable_fingerprint(
            "selectionknowledge",
            {
                "selection_id": selection.selection_id,
                "components": [
                    {
                        "symbol": digest.component_symbol,
                        "knowledge_fingerprint": digest.knowledge_fingerprint,
                        "digest_input_fingerprint": digest.input_fingerprint,
                    }
                    for _selected, digest, _hit in component_rows
                ],
            },
        )
        resolution_input = stable_fingerprint("resolutioninput", {
            "selection_id": selection.selection_id,
            "selection_input_fingerprint": selection.input_fingerprint,
            "analysis_as_of": normalized_analysis,
            "selection_data_as_of": normalized_selection_time,
            "knowledge_fingerprint": resolution_knowledge_fingerprint,
            "rule_version": COMPONENT_RESEARCH_RULE_VERSION,
        })
        cached_resolution = self.store.get_resolution_by_input(
            selection.selection_id, resolution_input
        )
        if cached_resolution is not None:
            result = replace(cached_resolution, cache_hit=True)
            self.store.audit(
                operation="resolution_cache_hit", etf_symbol=etf_symbol,
                object_id=result.resolution_id, cache_hit=True,
                avoided_model_calls=result.estimated_avoided_model_calls,
                metadata={"selection_id": selection.selection_id},
            )
            return result

        bindings: list[ETFComponentDigestBinding] = []
        for rank, (selected, digest, _digest_hit) in enumerate(component_rows, start=1):
            binding_id = stable_fingerprint("componentbinding", {
                "etf_symbol": etf_symbol,
                "selection_id": selection.selection_id,
                "component_symbol": digest.component_symbol,
            })
            binding_warnings = list(digest.warnings)
            if selection.quality == "partial":
                binding_warnings.append("partial_component_universe")
            binding_warnings.extend(selection.warnings)
            bindings.append(ETFComponentDigestBinding(
                binding_id=binding_id,
                etf_symbol=etf_symbol,
                selection_id=selection.selection_id,
                component_symbol=digest.component_symbol,
                component_name=selected.name,
                digest_id=None if digest.status == "missing" else digest.digest_id,
                digest_status=digest.status,
                component_weight=selected.weight,
                selection_score=selected.score,
                marginal_explanation_gain=selected.marginal_explanation_gain,
                forced=selected.forced,
                selection_reasons=list(selected.reasons),
                price_contribution=selected.price_contribution,
                earnings_contribution=selected.earnings_contribution,
                selected_rank=rank,
                selection_data_as_of=normalized_selection_time,
                created_at=self.now_provider(),
                warnings=sorted(set(binding_warnings)),
            ))

        counts = {
            status: sum(1 for item in bindings if item.digest_status == status)
            for status in (
                "reusable", "partial_reusable", "stale", "missing", "conflicted"
            )
        }
        avoided_calls = counts["reusable"] + counts["partial_reusable"]
        digest_ids = sorted({item.digest_id for item in bindings if item.digest_id})
        reusable_total = counts["reusable"] + counts["partial_reusable"]
        selected_count = len(bindings)
        resolution_id = stable_fingerprint("componentresolution", {
            "etf_symbol": etf_symbol,
            "selection_id": selection.selection_id,
            "analysis_as_of": normalized_analysis,
            "input_fingerprint": resolution_input,
            "binding_ids": [item.binding_id for item in bindings],
            "digest_ids": digest_ids,
        })
        resolution = ComponentDigestResolution(
            resolution_id=resolution_id,
            etf_symbol=etf_symbol,
            selection_id=selection.selection_id,
            analysis_as_of=normalized_analysis,
            selected_count=selected_count,
            reusable_count=counts["reusable"],
            partial_reusable_count=counts["partial_reusable"],
            stale_count=counts["stale"],
            missing_count=counts["missing"],
            conflicted_count=counts["conflicted"],
            bindings=bindings,
            digest_ids=digest_ids,
            reuse_ratio=round(reusable_total / selected_count, 6) if selected_count else 0.0,
            estimated_avoided_model_calls=avoided_calls,
            estimated_avoided_input_tokens=(
                avoided_calls * ESTIMATED_INPUT_TOKENS_PER_AVOIDED_CALL
            ),
            estimated_avoided_output_tokens=(
                avoided_calls * ESTIMATED_OUTPUT_TOKENS_PER_AVOIDED_CALL
            ),
            estimation_basis=ESTIMATION_BASIS,
            knowledge_fingerprint=resolution_knowledge_fingerprint,
            input_fingerprint=resolution_input,
            warnings=sorted(set(selection.warnings)),
        )
        stored = self.store.save_resolution(resolution)
        self.store.audit(
            operation="resolution_build", etf_symbol=etf_symbol,
            object_id=stored.resolution_id,
            avoided_model_calls=stored.estimated_avoided_model_calls,
            metadata={
                "selection_id": selection.selection_id,
                "selected_count": selected_count,
                "digest_cache_hits": sum(int(item[2]) for item in component_rows),
            },
        )
        return stored

    def current_digest(
        self, component_symbol: str, *, analysis_as_of: str | None = None
    ) -> ComponentResearchDigest | None:
        symbol = normalize_component_symbol(component_symbol)
        cutoff = _normalized_time(
            analysis_as_of or self.now_provider(),
            field_name="analysis_as_of",
        )
        return self.store.current_digest(symbol, analysis_as_of=cutoff)

    def resolution_for_selection(self, selection_id: str) -> ComponentDigestResolution | None:
        return self.store.resolution_for_selection(selection_id)

    def materialize_resolution(
        self,
        resolution: ComponentDigestResolution,
    ) -> dict[str, Any]:
        """Return the bounded, source-backed view needed by a formal ETF report.

        The method is read-only.  It never regenerates a Digest and never calls a
        model; it only expands IDs already frozen in ``resolution``.
        """

        digests: dict[str, dict[str, Any]] = {}
        claim_ids: set[str] = set()
        fact_ids: set[str] = set()
        evidence_ids: set[str] = set()
        for digest_id in resolution.digest_ids:
            digest = self.store.get_digest(digest_id)
            if digest is None:
                continue
            payload = digest.to_dict()
            digests[digest_id] = payload
            claim_ids.update(
                claim_id
                for values in digest.claim_ids_by_dimension.values()
                for claim_id in values
            )
            fact_ids.update(digest.fact_ids)
            evidence_ids.update(digest.evidence_ids)

        claims: dict[str, dict[str, Any]] = {}
        facts: list[dict[str, Any]] = []
        evidence: list[dict[str, Any]] = []
        with self.knowledge.connect() as connection:
            if claim_ids:
                rows = connection.execute(
                    f"SELECT * FROM claim_records WHERE claim_id IN "
                    f"({','.join('?' for _ in claim_ids)})",
                    sorted(claim_ids),
                ).fetchall()
                for row in rows:
                    item = dict(row)
                    item["fact_ids"] = [
                        str(value) for value in _loads(item.pop("fact_ids_json"), [])
                    ]
                    item["evidence_ids"] = [
                        str(value) for value in _loads(item.pop("evidence_ids_json"), [])
                    ]
                    claims[str(item["claim_id"])] = item
                    fact_ids.update(item["fact_ids"])
                    evidence_ids.update(item["evidence_ids"])
            if fact_ids:
                rows = connection.execute(
                    f"SELECT * FROM fact_records WHERE fact_id IN "
                    f"({','.join('?' for _ in fact_ids)})",
                    sorted(fact_ids),
                ).fetchall()
                for row in rows:
                    item = dict(row)
                    item["input_fact_ids"] = [
                        str(value) for value in _loads(item.pop("input_fact_ids_json"), [])
                    ]
                    item["evidence_ids"] = [
                        str(value) for value in _loads(item.pop("evidence_ids_json"), [])
                    ]
                    item.setdefault("calculation_version", "research-knowledge-v2")
                    item.setdefault("metadata", {
                        "currency": item.get("currency"),
                        "scope_key": item.get("scope_key"),
                    })
                    facts.append(item)
                    evidence_ids.update(item["evidence_ids"])
            if evidence_ids:
                rows = connection.execute(
                    f"""SELECT e.*,d.canonical_url,d.publisher,d.source_class,
                               d.independence_group,d.published_at AS document_published_at,
                               d.retrieved_at,d.content_hash,d.title
                        FROM evidence_records e
                        JOIN source_documents d USING(document_ref)
                        WHERE e.evidence_id IN ({','.join('?' for _ in evidence_ids)})""",
                    sorted(evidence_ids),
                ).fetchall()
                for row in rows:
                    item = dict(row)
                    chunk_refs = [
                        str(value) for value in _loads(item.pop("chunk_refs_json"), [])
                    ]
                    evidence.append({
                        "evidence_id": item["evidence_id"],
                        "symbol": item["symbol"],
                        "domain": item["domain"],
                        "source": item.get("publisher") or item.get("title") or "研究资料",
                        "source_locator": item.get("canonical_url") or "",
                        "retrieved_at": item.get("retrieved_at") or item.get("created_at"),
                        "published_at": (
                            item.get("document_published_at") or item.get("valid_from")
                        ),
                        "content_hash": item.get("content_hash") or "",
                        "summary": item.get("summary") or "",
                        "status": item.get("status") or "verified",
                        "metadata": {
                            "document_ref": item.get("document_ref"),
                            "chunk_refs": chunk_refs,
                            "scope_key": item.get("scope_key"),
                            "source_strength": item.get("source_strength"),
                            "source_class": item.get("source_class"),
                            "independence_group": item.get("independence_group"),
                            "title": item.get("title"),
                        },
                    })

        for digest_id, digest in digests.items():
            summaries: dict[str, str] = {}
            for dimension, ids in dict(digest.get("claim_ids_by_dimension") or {}).items():
                selected = next(
                    (
                        str(claims[claim_id].get("text") or "").strip()
                        for claim_id in ids
                        if claim_id in claims and str(claims[claim_id].get("text") or "").strip()
                    ),
                    "",
                )
                if selected:
                    summaries[str(dimension)] = selected
            digest["summaries_by_dimension"] = summaries

        return {
            "resolution_id": resolution.resolution_id,
            "digests": digests,
            "claims": list(claims.values()),
            "facts": facts,
            "evidence": evidence,
            "model_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }

    def component_research_profile(self, subject_key: str) -> dict[str, Any] | None:
        symbol = normalize_component_symbol(subject_key)
        resolution = self.store.latest_resolution_for_etf(symbol)
        if resolution is not None:
            components: list[dict[str, Any]] = []
            for binding in resolution.bindings:
                digest = self.store.get_digest(binding.digest_id) if binding.digest_id else None
                components.append({
                    "symbol": binding.component_symbol,
                    "name": binding.component_name,
                    "weight": binding.component_weight,
                    "forced": binding.forced,
                    "selection_reasons": list(binding.selection_reasons),
                    "digest_id": binding.digest_id,
                    "digest_status": binding.digest_status,
                    "coverage_dimensions": (
                        list(digest.coverage_dimensions) if digest else []
                    ),
                    "research_data_as_of": digest.research_data_as_of if digest else None,
                    "freshness_expires_at": digest.freshness_expires_at if digest else None,
                    "warnings": list(binding.warnings),
                })
            return {
                "selection_id": resolution.selection_id,
                "resolution_id": resolution.resolution_id,
                "selected_count": resolution.selected_count,
                "reusable_count": resolution.reusable_count,
                "partial_reusable_count": resolution.partial_reusable_count,
                "stale_count": resolution.stale_count,
                "missing_count": resolution.missing_count,
                "conflicted_count": resolution.conflicted_count,
                "reuse_ratio": resolution.reuse_ratio,
                "components": components,
                "model_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
            }
        digest = self.store.current_digest(symbol)
        if digest is None:
            return None
        return {
            "component_symbol": digest.component_symbol,
            "digest_id": digest.digest_id,
            "digest_status": digest.status,
            "quality": digest.quality,
            "coverage_dimensions": list(digest.coverage_dimensions),
            "missing_dimensions": list(digest.missing_dimensions),
            "stale_dimensions": list(digest.stale_dimensions),
            "research_data_as_of": digest.research_data_as_of,
            "freshness_expires_at": digest.freshness_expires_at,
            "warnings": list(digest.warnings),
            "model_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }


_shared_service: ComponentResearchDigestService | None = None
_shared_lock = threading.Lock()


def get_component_research_service() -> ComponentResearchDigestService:
    global _shared_service
    if _shared_service is None:
        with _shared_lock:
            if _shared_service is None:
                _shared_service = ComponentResearchDigestService()
    return _shared_service
