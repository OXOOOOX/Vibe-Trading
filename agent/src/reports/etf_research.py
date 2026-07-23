"""Reusable ETF research snapshots, module cache, routing, and audit metrics.

The tables in this module live in the existing research-cache SQLite file.
They store normalized ETF state and cache metadata only; source documents,
Facts, Evidence, Claims, and formal reports remain owned by their existing
stores.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .contracts import (
    ETFAnalysisDecision,
    ETFAnalysisMode,
    ETFModuleCacheResult,
    ETFResearchSnapshot,
    ETFSnapshotType,
    utc_now,
)


ETF_PROFILE_VERSION = "1.0"
ETF_PROMPT_VERSION = "1.0"
DEFAULT_TOKEN_BUDGET = {"input_tokens": 24_000, "output_tokens": 6_000}
_SNAPSHOT_TYPES = {
    "identity", "universe", "market", "holder", "index_methodology",
    "product_metrics", "share_history", "peer_group",
}
_MODULE_IDS = {
    "identity",
    "universe",
    "aggregate_fundamentals",
    "price_volume",
    "flow_liquidity",
    "product_profile",
    "peer_flow",
    "holder_structure",
    "holding_penetration",
    "scenarios_watchlist",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def stable_fingerprint(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:24]}"


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_etf_snapshot(
    *,
    symbol: str,
    snapshot_type: ETFSnapshotType,
    data_as_of: str,
    payload: dict[str, Any],
    coverage_ratio: float,
    source_ids: Iterable[str] = (),
    fact_ids: Iterable[str] = (),
    evidence_ids: Iterable[str] = (),
    retrieved_at: str | None = None,
    freshness_expires_at: str | None = None,
    minimum_coverage: float = 0.60,
) -> ETFResearchSnapshot:
    """Validate normalized ETF observations and return an immutable snapshot.

    ``coverage_ratio`` remains the backwards-compatible snapshot-level field.
    For universe snapshots it now means required-field coverage; known weight
    coverage is stored separately in ``payload["observed_weight_coverage"]``.
    A sourced, top-ranked partial universe is therefore reusable by P4A even
    when its disclosed weights cover substantially less than 60% of the index.
    """

    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol or "." not in normalized_symbol:
        raise ValueError("ETF snapshot requires a market-qualified symbol")
    if snapshot_type not in _SNAPSHOT_TYPES:
        raise ValueError(f"unsupported ETF snapshot type: {snapshot_type}")
    if not isinstance(payload, dict) or not payload:
        raise ValueError("ETF snapshot payload must not be empty")
    if _parse_time(data_as_of) is None:
        raise ValueError("ETF snapshot requires an ISO data_as_of timestamp")
    coverage = float(coverage_ratio)
    if not 0.0 <= coverage <= 1.0:
        raise ValueError("coverage_ratio must be between 0 and 1")

    normalized_sources = sorted({str(item) for item in source_ids if str(item)})
    normalized_facts = sorted({str(item) for item in fact_ids if str(item)})
    normalized_evidence = sorted({str(item) for item in evidence_ids if str(item)})
    universe_complete = bool(payload.get("universe_complete"))
    partial_top_ranked = bool(payload.get("partial_components_are_top_ranked"))
    observed_components = int(payload.get("observed_component_count") or 0)
    expected_components = int(payload.get("expected_component_count") or 0)
    observed_weight_coverage = float(payload.get("observed_weight_coverage") or 0.0)
    universe_semantics_present = snapshot_type == "universe" and any(
        key in payload
        for key in (
            "observed_component_count",
            "observed_weight_coverage",
            "required_field_coverage",
            "partial_components_are_top_ranked",
        )
    )

    if not normalized_sources and not normalized_evidence:
        quality = "failed_validation"
    elif universe_semantics_present and universe_complete:
        if (
            observed_components <= 0
            or expected_components <= 0
            or observed_components < expected_components
            or observed_weight_coverage < 0.90
            or coverage < minimum_coverage
        ):
            quality = "failed_validation"
        elif coverage < 1.0 or observed_weight_coverage < 0.999:
            quality = "passed_with_gaps"
        else:
            quality = "passed"
    elif universe_semantics_present and partial_top_ranked:
        if observed_components <= 0 or observed_weight_coverage <= 0.0 or coverage < minimum_coverage:
            quality = "failed_validation"
        else:
            quality = "passed_with_gaps"
    elif universe_semantics_present:
        quality = "failed_validation"
    elif coverage < minimum_coverage:
        quality = "failed_validation"
    elif snapshot_type == "market" and payload.get("price_verified") is not True:
        quality = "failed_validation"
    elif coverage < 1.0:
        quality = "passed_with_gaps"
    else:
        quality = "passed"

    semantic = {
        "schema_version": 1,
        "symbol": normalized_symbol,
        "snapshot_type": snapshot_type,
        "data_as_of": data_as_of,
        "coverage_ratio": coverage,
        "payload": payload,
        "source_ids": normalized_sources,
        "fact_ids": normalized_facts,
        "evidence_ids": normalized_evidence,
    }
    content_hash = hashlib.sha256(_canonical_json(semantic).encode("utf-8")).hexdigest()
    return ETFResearchSnapshot(
        snapshot_id=f"etfsnap_{content_hash[:24]}",
        symbol=normalized_symbol,
        snapshot_type=snapshot_type,
        data_as_of=data_as_of,
        retrieved_at=retrieved_at or utc_now(),
        coverage_ratio=coverage,
        quality_status=quality,  # type: ignore[arg-type]
        content_hash=content_hash,
        payload=dict(payload),
        source_ids=normalized_sources,
        fact_ids=normalized_facts,
        evidence_ids=normalized_evidence,
        freshness_expires_at=freshness_expires_at,
    )


def snapshot_is_reusable(
    snapshot: ETFResearchSnapshot,
    *,
    now: str | None = None,
    price_sensitive: bool = False,
    max_market_age_seconds: int = 1_800,
) -> bool:
    """Fail closed for weak or stale data, especially price-sensitive checks."""

    if snapshot.quality_status == "failed_validation":
        return False
    current = _parse_time(now or utc_now())
    if current is None:
        return False
    if snapshot.freshness_expires_at:
        expires = _parse_time(snapshot.freshness_expires_at)
        if expires is None or current > expires:
            return False
    if price_sensitive:
        if snapshot.snapshot_type != "market" or snapshot.payload.get("price_verified") is not True:
            return False
        observed = _parse_time(snapshot.data_as_of)
        if observed is None or (current - observed).total_seconds() > max_market_age_seconds:
            return False
    return True


def module_input_fingerprint(
    *,
    module_id: str,
    snapshot_ids: Iterable[str],
    dependency_ids: Iterable[str] = (),
    profile_version: str = ETF_PROFILE_VERSION,
    prompt_version: str = ETF_PROMPT_VERSION,
    model_id: str = "deterministic",
) -> str:
    if module_id not in _MODULE_IDS:
        raise ValueError(f"unsupported ETF research module: {module_id}")
    return stable_fingerprint("etfinput", {
        "module_id": module_id,
        "snapshot_ids": sorted({str(item) for item in snapshot_ids if str(item)}),
        "dependency_ids": sorted({str(item) for item in dependency_ids if str(item)}),
        "profile_version": profile_version,
        "prompt_version": prompt_version,
        "model_id": model_id,
    })


class ETFResearchStore:
    """ETF state tables co-located with the shared research knowledge database."""

    def __init__(self, path: Path | None = None, *, research_store: Any | None = None) -> None:
        if research_store is not None:
            path = Path(research_store.path)
        self.path = path or Path(
            os.getenv(
                "VIBE_TRADING_RESEARCH_CACHE_DB",
                "~/.vibe-trading/cache/research_cache.sqlite3",
            )
        ).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._single_flight_guard = threading.Lock()
        self._single_flight: dict[str, threading.Lock] = {}
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def initialize(self) -> None:
        with self._lock, self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS etf_research_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    snapshot_type TEXT NOT NULL,
                    data_as_of TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    freshness_expires_at TEXT,
                    coverage_ratio REAL NOT NULL,
                    quality_status TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    source_ids_json TEXT NOT NULL,
                    fact_ids_json TEXT NOT NULL,
                    evidence_ids_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(symbol, snapshot_type, content_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_etf_snapshots_symbol_type_time
                    ON etf_research_snapshots(symbol, snapshot_type, data_as_of DESC);

                CREATE TABLE IF NOT EXISTS etf_module_cache (
                    cache_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    module_id TEXT NOT NULL,
                    input_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    profile_version TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    UNIQUE(symbol, module_id, input_fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_etf_module_cache_lookup
                    ON etf_module_cache(symbol, module_id, input_fingerprint);

                CREATE TABLE IF NOT EXISTS etf_analysis_runs (
                    decision_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    input_fingerprint TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    changed_snapshot_types_json TEXT NOT NULL,
                    refresh_modules_json TEXT NOT NULL,
                    stale_sections_json TEXT NOT NULL,
                    reused_report_id TEXT,
                    token_budget_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_etf_analysis_runs_symbol_time
                    ON etf_analysis_runs(symbol, created_at DESC);

                CREATE TABLE IF NOT EXISTS etf_reuse_audit (
                    audit_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    object_id TEXT,
                    module_id TEXT,
                    cache_hit INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    saved_tokens INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_etf_reuse_audit_symbol_time
                    ON etf_reuse_audit(symbol, created_at DESC);
                """
            )

    def _audit(
        self,
        *,
        symbol: str,
        operation: str,
        object_id: str | None = None,
        module_id: str | None = None,
        cache_hit: bool = False,
        input_tokens: int = 0,
        output_tokens: int = 0,
        saved_tokens: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO etf_reuse_audit(
                    audit_id, symbol, operation, object_id, module_id, cache_hit,
                    input_tokens, output_tokens, saved_tokens, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"etfaudit_{uuid.uuid4().hex[:20]}", symbol, operation, object_id,
                    module_id, int(cache_hit), max(0, int(input_tokens)),
                    max(0, int(output_tokens)), max(0, int(saved_tokens)),
                    _canonical_json(metadata or {}), utc_now(),
                ),
            )

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row) -> ETFResearchSnapshot:
        return ETFResearchSnapshot(
            snapshot_id=row["snapshot_id"],
            symbol=row["symbol"],
            snapshot_type=row["snapshot_type"],
            data_as_of=row["data_as_of"],
            retrieved_at=row["retrieved_at"],
            coverage_ratio=float(row["coverage_ratio"]),
            quality_status=row["quality_status"],
            content_hash=row["content_hash"],
            payload=json.loads(row["payload_json"]),
            source_ids=json.loads(row["source_ids_json"]),
            fact_ids=json.loads(row["fact_ids_json"]),
            evidence_ids=json.loads(row["evidence_ids_json"]),
            freshness_expires_at=row["freshness_expires_at"],
        )

    def save_snapshot(self, snapshot: ETFResearchSnapshot) -> tuple[ETFResearchSnapshot, bool]:
        """Save once by semantic content; return ``(snapshot, reused)``."""

        with self._lock, self.connect() as connection:
            existing = connection.execute(
                """SELECT * FROM etf_research_snapshots
                   WHERE symbol = ? AND snapshot_type = ? AND content_hash = ?""",
                (snapshot.symbol, snapshot.snapshot_type, snapshot.content_hash),
            ).fetchone()
            if existing is not None:
                stored = self._snapshot_from_row(existing)
                reused = True
            else:
                connection.execute(
                    """INSERT INTO etf_research_snapshots(
                        snapshot_id, symbol, snapshot_type, data_as_of, retrieved_at,
                        freshness_expires_at, coverage_ratio, quality_status, content_hash,
                        source_ids_json, fact_ids_json, evidence_ids_json, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        snapshot.snapshot_id, snapshot.symbol, snapshot.snapshot_type,
                        snapshot.data_as_of, snapshot.retrieved_at,
                        snapshot.freshness_expires_at, snapshot.coverage_ratio,
                        snapshot.quality_status, snapshot.content_hash,
                        _canonical_json(snapshot.source_ids), _canonical_json(snapshot.fact_ids),
                        _canonical_json(snapshot.evidence_ids), _canonical_json(snapshot.payload),
                        utc_now(),
                    ),
                )
                stored = snapshot
                reused = False
        self._audit(
            symbol=snapshot.symbol,
            operation="snapshot_save",
            object_id=stored.snapshot_id,
            cache_hit=reused,
            metadata={"snapshot_type": snapshot.snapshot_type},
        )
        return stored, reused

    def get_snapshot(self, snapshot_id: str) -> ETFResearchSnapshot | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM etf_research_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        return self._snapshot_from_row(row) if row is not None else None

    def latest_snapshot(self, symbol: str, snapshot_type: ETFSnapshotType) -> ETFResearchSnapshot | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM etf_research_snapshots
                   WHERE symbol = ? AND snapshot_type = ?
                   ORDER BY data_as_of DESC, created_at DESC LIMIT 1""",
                (symbol.upper(), snapshot_type),
            ).fetchone()
        return self._snapshot_from_row(row) if row is not None else None

    def latest_snapshot_by_created_at(
        self,
        symbol: str,
        snapshot_type: ETFSnapshotType,
    ) -> ETFResearchSnapshot | None:
        """Return the newest immutable revision, including backdated corrections."""

        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM etf_research_snapshots
                   WHERE symbol = ? AND snapshot_type = ?
                   ORDER BY created_at DESC, data_as_of DESC LIMIT 1""",
                (symbol.upper(), snapshot_type),
            ).fetchone()
        return self._snapshot_from_row(row) if row is not None else None

    def recent_snapshots(
        self,
        symbol: str,
        snapshot_type: ETFSnapshotType,
        *,
        limit: int = 25,
    ) -> list[ETFResearchSnapshot]:
        """Return recent immutable revisions for source-aware cache fallback."""

        capped = max(1, min(int(limit), 100))
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM etf_research_snapshots
                   WHERE symbol = ? AND snapshot_type = ?
                   ORDER BY created_at DESC, data_as_of DESC LIMIT ?""",
                (symbol.upper(), snapshot_type, capped),
            ).fetchall()
        return [self._snapshot_from_row(row) for row in rows]

    def latest_module_result(self, symbol: str, module_id: str) -> ETFModuleCacheResult | None:
        """Return the most recently stored module result for status APIs."""

        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM etf_module_cache
                   WHERE symbol = ? AND module_id = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (symbol.upper(), module_id),
            ).fetchone()
        return self._module_from_row(row) if row is not None else None

    def record_universe_audit(
        self,
        *,
        symbol: str,
        operation: str,
        object_id: str | None = None,
        cache_hit: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Persist a provider/cache audit event without exposing token values."""

        self._audit(
            symbol=symbol.upper(),
            operation=operation,
            object_id=object_id,
            cache_hit=cache_hit,
            metadata=metadata,
        )

    @staticmethod
    def _module_from_row(row: sqlite3.Row) -> ETFModuleCacheResult:
        return ETFModuleCacheResult(
            cache_id=row["cache_id"], symbol=row["symbol"], module_id=row["module_id"],
            input_fingerprint=row["input_fingerprint"], status=row["status"],
            result=json.loads(row["result_json"]), profile_version=row["profile_version"],
            prompt_version=row["prompt_version"], model_id=row["model_id"],
            input_tokens=int(row["input_tokens"]), output_tokens=int(row["output_tokens"]),
            created_at=row["created_at"], expires_at=row["expires_at"],
        )

    def get_module_result(
        self,
        *,
        symbol: str,
        module_id: str,
        input_fingerprint: str,
        now: str | None = None,
    ) -> ETFModuleCacheResult | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM etf_module_cache
                   WHERE symbol = ? AND module_id = ? AND input_fingerprint = ?""",
                (symbol.upper(), module_id, input_fingerprint),
            ).fetchone()
        if row is None:
            return None
        result = self._module_from_row(row)
        if result.expires_at:
            current = _parse_time(now or utc_now())
            expires = _parse_time(result.expires_at)
            if current is None or expires is None or current > expires:
                return None
        return result

    def _save_module_result(self, result: ETFModuleCacheResult) -> ETFModuleCacheResult:
        with self._lock, self.connect() as connection:
            connection.execute(
                """INSERT INTO etf_module_cache(
                    cache_id, symbol, module_id, input_fingerprint, status, result_json,
                    profile_version, prompt_version, model_id, input_tokens, output_tokens,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, module_id, input_fingerprint) DO UPDATE SET
                    status = excluded.status,
                    result_json = excluded.result_json,
                    profile_version = excluded.profile_version,
                    prompt_version = excluded.prompt_version,
                    model_id = excluded.model_id,
                    input_tokens = excluded.input_tokens,
                    output_tokens = excluded.output_tokens,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at""",
                (
                    result.cache_id, result.symbol, result.module_id,
                    result.input_fingerprint, result.status, _canonical_json(result.result),
                    result.profile_version, result.prompt_version, result.model_id,
                    result.input_tokens, result.output_tokens, result.created_at, result.expires_at,
                ),
            )
        return self.get_module_result(
            symbol=result.symbol,
            module_id=result.module_id,
            input_fingerprint=result.input_fingerprint,
        ) or result

    def execute_module(
        self,
        *,
        symbol: str,
        module_id: str,
        input_fingerprint: str,
        runner: Callable[[], dict[str, Any]],
        profile_version: str = ETF_PROFILE_VERSION,
        prompt_version: str = ETF_PROMPT_VERSION,
        model_id: str = "deterministic",
        estimated_input_tokens: int = 0,
        estimated_output_tokens: int = 0,
        token_budget: dict[str, int] | None = None,
        expires_at: str | None = None,
    ) -> tuple[ETFModuleCacheResult, bool]:
        """Run a module once per fingerprint, including concurrent callers."""

        if module_id not in _MODULE_IDS:
            raise ValueError(f"unsupported ETF research module: {module_id}")
        budget = {**DEFAULT_TOKEN_BUDGET, **(token_budget or {})}
        if estimated_input_tokens > int(budget["input_tokens"]):
            raise ValueError("ETF module input token budget exceeded")
        if estimated_output_tokens > int(budget["output_tokens"]):
            raise ValueError("ETF module output token budget exceeded")
        normalized_symbol = symbol.upper()
        flight_key = f"{normalized_symbol}:{module_id}:{input_fingerprint}"
        with self._single_flight_guard:
            flight_lock = self._single_flight.setdefault(flight_key, threading.Lock())
        with flight_lock:
            cached = self.get_module_result(
                symbol=normalized_symbol,
                module_id=module_id,
                input_fingerprint=input_fingerprint,
            )
            if cached is not None:
                saved = cached.input_tokens + cached.output_tokens
                self._audit(
                    symbol=normalized_symbol, operation="module_execute",
                    object_id=cached.cache_id, module_id=module_id, cache_hit=True,
                    saved_tokens=saved,
                    metadata={"input_fingerprint": input_fingerprint, "model_id": model_id},
                )
                return cached, True

            raw = dict(runner() or {})
            status = str(raw.pop("status", "passed"))
            allowed_statuses = {
                "pending", "running", "passed", "warning", "failed_validation",
                "insufficient_evidence", "not_requested",
            }
            if status not in allowed_statuses:
                raise ValueError(f"invalid ETF module status: {status}")
            input_tokens = max(0, int(raw.pop("input_tokens", estimated_input_tokens) or 0))
            output_tokens = max(0, int(raw.pop("output_tokens", estimated_output_tokens) or 0))
            if input_tokens > int(budget["input_tokens"]) or output_tokens > int(budget["output_tokens"]):
                raise ValueError("ETF module actual token usage exceeded budget")
            result = ETFModuleCacheResult(
                cache_id=stable_fingerprint("etfmodule", {
                    "symbol": normalized_symbol,
                    "module_id": module_id,
                    "input_fingerprint": input_fingerprint,
                }),
                symbol=normalized_symbol,
                module_id=module_id,
                input_fingerprint=input_fingerprint,
                status=status,  # type: ignore[arg-type]
                result=raw,
                profile_version=profile_version,
                prompt_version=prompt_version,
                model_id=model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                expires_at=expires_at,
            )
            stored = self._save_module_result(result)
            self._audit(
                symbol=normalized_symbol, operation="module_execute",
                object_id=stored.cache_id, module_id=module_id, cache_hit=False,
                input_tokens=input_tokens, output_tokens=output_tokens,
                metadata={"input_fingerprint": input_fingerprint, "model_id": model_id},
            )
            return stored, False

    def record_decision(self, decision: ETFAnalysisDecision) -> ETFAnalysisDecision:
        with self._lock, self.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO etf_analysis_runs(
                    decision_id, symbol, input_fingerprint, mode, reasons_json,
                    changed_snapshot_types_json, refresh_modules_json, stale_sections_json,
                    reused_report_id, token_budget_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision.decision_id, decision.symbol, decision.input_fingerprint,
                    decision.mode, _canonical_json(decision.reasons),
                    _canonical_json(decision.changed_snapshot_types),
                    _canonical_json(decision.refresh_modules),
                    _canonical_json(decision.stale_sections), decision.reused_report_id,
                    _canonical_json(decision.token_budget), decision.created_at,
                ),
            )
        self._audit(
            symbol=decision.symbol, operation="analysis_decision",
            object_id=decision.decision_id, cache_hit=decision.mode == "reuse",
            metadata={
                "mode": decision.mode,
                "refresh_modules": decision.refresh_modules,
                "stale_sections": decision.stale_sections,
            },
        )
        return decision

    def baseline_metrics(self, symbol: str | None = None) -> dict[str, Any]:
        where = "WHERE symbol = ?" if symbol else ""
        params: tuple[Any, ...] = (symbol.upper(),) if symbol else ()
        with self.connect() as connection:
            row = connection.execute(
                f"""SELECT COUNT(*) AS requests,
                           SUM(cache_hit) AS cache_hits,
                           SUM(CASE WHEN operation = 'module_execute' AND cache_hit = 0 THEN 1 ELSE 0 END) AS module_runs,
                           SUM(CASE WHEN operation = 'module_execute' AND cache_hit = 0
                               AND COALESCE(json_extract(metadata_json, '$.model_id'), 'deterministic') != 'deterministic'
                               THEN 1 ELSE 0 END) AS model_runs,
                           SUM(CASE WHEN operation = 'module_execute' AND cache_hit = 0
                               AND COALESCE(json_extract(metadata_json, '$.model_id'), 'deterministic') = 'deterministic'
                               THEN 1 ELSE 0 END) AS deterministic_runs,
                           SUM(input_tokens) AS input_tokens,
                           SUM(output_tokens) AS output_tokens,
                           SUM(saved_tokens) AS saved_tokens
                    FROM etf_reuse_audit {where}""",
                params,
            ).fetchone()
            decisions = connection.execute(
                f"SELECT mode, COUNT(*) AS count FROM etf_analysis_runs {where} GROUP BY mode",
                params,
            ).fetchall()
        requests = int(row["requests"] or 0)
        hits = int(row["cache_hits"] or 0)
        return {
            "symbol": symbol.upper() if symbol else None,
            "requests": requests,
            "cache_hits": hits,
            "cache_hit_ratio": round(hits / requests, 4) if requests else 0.0,
            "module_runs": int(row["module_runs"] or 0),
            "model_runs": int(row["model_runs"] or 0),
            "deterministic_runs": int(row["deterministic_runs"] or 0),
            "input_tokens": int(row["input_tokens"] or 0),
            "output_tokens": int(row["output_tokens"] or 0),
            "saved_tokens": int(row["saved_tokens"] or 0),
            "decision_counts": {item["mode"]: int(item["count"]) for item in decisions},
        }


class ETFAnalysisRouter:
    """Map observable ETF changes to the smallest sufficient research refresh."""

    _MODULES_BY_SNAPSHOT = {
        "identity": ["identity"],
        "universe": ["universe", "aggregate_fundamentals", "holding_penetration"],
        "market": ["price_volume", "flow_liquidity", "scenarios_watchlist"],
        "holder": ["holder_structure", "holding_penetration", "scenarios_watchlist"],
    }
    _SECTIONS_BY_SNAPSHOT = {
        "identity": ["index_and_product"],
        "universe": ["exposure_structure", "aggregate_fundamentals", "holding_penetration"],
        "market": ["price_volume_structure", "flow_liquidity_tracking", "scenarios_watchlist"],
        "holder": ["flow_liquidity_tracking", "holding_penetration", "scenarios_watchlist"],
    }
    _MATERIAL_MARKET_TRIGGERS = {
        "share_change_1d", "share_change_5d", "volume_breakout", "key_level_break",
        "tracking_error_jump", "premium_discount_anomaly", "claim_invalidated",
    }

    def decide(
        self,
        *,
        symbol: str,
        changed_snapshot_types: Iterable[ETFSnapshotType] = (),
        trigger_flags: Iterable[str] = (),
        prior_report_id: str | None = None,
        force_full: bool = False,
        token_budget: dict[str, int] | None = None,
    ) -> ETFAnalysisDecision:
        changed = sorted({str(item) for item in changed_snapshot_types if str(item)})
        invalid = sorted(set(changed) - _SNAPSHOT_TYPES)
        if invalid:
            raise ValueError(f"unsupported changed snapshot types: {', '.join(invalid)}")
        triggers = sorted({str(item) for item in trigger_flags if str(item)})
        refresh_modules = sorted({
            module for snapshot_type in changed
            for module in self._MODULES_BY_SNAPSHOT[snapshot_type]
        })
        stale_sections = sorted({
            section for snapshot_type in changed
            for section in self._SECTIONS_BY_SNAPSHOT[snapshot_type]
        })

        reasons: list[str] = []
        mode: ETFAnalysisMode
        if force_full:
            mode = "full_refresh"
            reasons.append("force_full_refresh")
        elif not prior_report_id:
            mode = "full_refresh"
            reasons.append("no_prior_structural_report")
        elif not changed and not triggers:
            mode = "reuse"
            reasons.append("no_material_input_change")
        elif "identity" in changed or "index_rule_changed" in triggers:
            mode = "full_refresh"
            reasons.append("product_or_index_contract_changed")
        elif "universe" in changed or "holder" in changed:
            mode = "section_revision"
            reasons.append("structural_snapshot_changed")
        elif set(changed) <= {"market"} and not (set(triggers) & self._MATERIAL_MARKET_TRIGGERS):
            mode = "monitor_delta"
            reasons.append("market_delta_without_research_trigger")
        elif set(changed) <= {"market"}:
            mode = "partial_refresh"
            reasons.extend(sorted(set(triggers) & self._MATERIAL_MARKET_TRIGGERS))
        else:
            mode = "section_revision"
            reasons.append("dependent_research_sections_stale")

        fingerprint = stable_fingerprint("etfdecision", {
            "symbol": symbol.upper(),
            "changed": changed,
            "triggers": triggers,
            "prior_report_id": prior_report_id,
            "force_full": force_full,
            "profile_version": ETF_PROFILE_VERSION,
        })
        return ETFAnalysisDecision(
            decision_id=fingerprint.replace("etfdecision_", "etfdecision_", 1),
            symbol=symbol.upper(),
            input_fingerprint=fingerprint,
            mode=mode,
            reasons=reasons,
            changed_snapshot_types=changed,  # type: ignore[arg-type]
            refresh_modules=[] if mode in {"reuse", "monitor_delta"} else refresh_modules,
            stale_sections=[] if mode in {"reuse", "monitor_delta"} else stale_sections,
            reused_report_id=prior_report_id if mode in {"reuse", "monitor_delta"} else None,
            token_budget={**DEFAULT_TOKEN_BUDGET, **(token_budget or {})},
        )


_shared_store: ETFResearchStore | None = None
_shared_lock = threading.Lock()


def get_etf_research_store() -> ETFResearchStore:
    global _shared_store
    with _shared_lock:
        if _shared_store is None:
            _shared_store = ETFResearchStore()
        return _shared_store
