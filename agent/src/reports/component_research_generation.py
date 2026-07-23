"""P4B2 controlled component research generation and unified publication.

The module is deliberately fail-closed.  Plan creation is deterministic and
does not call a model.  Live execution additionally requires both feature
gates, the exact pilot authorization, a frozen evidence fingerprint, database
budget reservation, and provider-reported token usage.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from src.research.knowledge import ResearchKnowledgeStore, get_research_knowledge_store

from .component_research import (
    ComponentResearchDigestService,
    ComponentResearchDigestStore,
    _dimensions_for_claim,
    normalize_component_symbol,
)
from .contracts import (
    ComponentDigestResolution,
    ComponentResearchDimension,
    ComponentResearchEvidencePack,
    ComponentResearchGenerationJob,
    ComponentResearchGenerationPlan,
    ComponentResearchGenerationPolicy,
    ComponentResearchPreflightResult,
    ComponentResearchPublishResult,
    ETFComponentSelection,
    ETFConcentrationMetrics,
    ETFSelectedComponent,
    utc_now,
)
from .etf_research import stable_fingerprint


COMPONENT_RESEARCH_GENERATION_POLICY_VERSION = "p4b2-policy-v1"
COMPONENT_RESEARCH_PROFILE_ID = "component_research_digest_v1"
COMPONENT_RESEARCH_PROMPT_VERSION = "component-research-digest-v1"
PILOT_ETF_SYMBOL = "588870.SH"
PILOT_COMPONENT_SYMBOLS = ("688256.SH", "688041.SH", "688981.SH")
PILOT_AUTHORIZATION_TEXT = (
    "已授权 P4B2 首批试运行：仅限 588870.SH 的 688256.SH、688041.SH、688981.SH；"
    "最多 3 次模型调用；单成分输入不超过 6,000 tokens、输出不超过 600 tokens；"
    "全批输入不超过 18,000 tokens、输出不超过 1,800 tokens；不允许自动修复或扩大标的。"
)
PILOT_EXPANDED_OUTPUT_AUTHORIZATION_TEXT = (
    "已授权 P4B2 首批试运行提高输出限额：仅限 588870.SH 的 "
    "688256.SH、688041.SH、688981.SH；最多 3 次模型调用；"
    "单成分输入不超过 6,000 tokens、输出不超过 1,000 tokens；"
    "全批输入不超过 18,000 tokens、输出不超过 3,000 tokens；"
    "不允许自动修复或扩大标的。"
)
PILOT_CODEX_SOFT_LIMIT_AUTHORIZATION_TEXT = (
    "已授权 P4B2 继续使用 Codex 客户端软限制试运行：仅限 588870.SH 的 "
    "688256.SH、688041.SH、688981.SH；最多 3 次新的模型生成调用；"
    "单成分输入不超过 6,000 tokens、输出软限制 1,000 tokens；"
    "全批输入不超过 18,000 tokens、输出软限制 3,000 tokens；"
    "Codex 不发送服务端最大输出参数，响应超限不发布；"
    "不允许自动修复或扩大标的；原 HTTP 400 不重试且不计入本批生成调用。"
)
PILOT_FEATURE_FIRST_AUTHORIZATION_TEXT = (
    "已授权 P4B2 功能优先试运行：仅限 588870.SH 的 "
    "688256.SH、688041.SH、688981.SH；解除本批及当日成分数、"
    "模型调用次数与输出 tokens 阻断；Codex 不发送服务端最大输出参数；"
    "输入仍为单成分不超过 6,000 tokens、全批不超过 18,000 tokens；"
    "保留 Evidence 白名单、结构化校验、未来数据隔离与事务发布门控；"
    "不允许扩大标的。"
)
PLAN_TTL_MINUTES = 30

_TRUE_VALUES = {"1", "true", "yes", "on"}
_CORE_EVIDENCE_DIMENSIONS = {"business_exposure", "earnings_trend", "risks"}
_GENERATION_TABLES = {
    "component_research_evidence_packs",
    "component_research_generation_plans",
    "component_research_generation_jobs",
    "component_research_generation_audit",
    "component_research_budget_ledger",
    "component_research_publish_results",
}
_P4B1_TABLES = {
    "component_research_digests",
    "etf_component_digest_bindings",
    "component_digest_resolutions",
    "component_research_audit",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")
    )


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value)) if value not in (None, "") else fallback
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
            parsed = datetime.fromisoformat(raw[:10])
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalized_time(value: Any, *, field_name: str) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        raise ValueError(f"{field_name} must be an ISO timestamp")
    return parsed.isoformat()


def _normalized_authorization_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).replace(",", "，").replace(";", "；")


def _mapping_value(values: Mapping[str, Any] | None, name: str, default: Any) -> Any:
    if values is not None and name in values:
        return values[name]
    return os.getenv(name, default)


def _env_bool(values: Mapping[str, Any] | None, name: str, default: bool) -> bool:
    raw = _mapping_value(values, name, "1" if default else "0")
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in _TRUE_VALUES


def _bounded_int(
    values: Mapping[str, Any] | None,
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        result = int(_mapping_value(values, name, default))
    except (TypeError, ValueError):
        result = default
    return max(minimum, min(result, maximum))


def component_research_generation_policy(
    values: Mapping[str, Any] | None = None,
) -> ComponentResearchGenerationPolicy:
    """Build the centralized v1 policy, clamped to its audited hard ceilings."""

    return ComponentResearchGenerationPolicy(
        policy_version=COMPONENT_RESEARCH_GENERATION_POLICY_VERSION,
        enabled=_env_bool(values, "ETF_COMPONENT_RESEARCH_GENERATION_ENABLED", False),
        live_run_enabled=_env_bool(
            values, "ETF_COMPONENT_RESEARCH_LIVE_RUN_ENABLED", False
        ),
        eligible_statuses=["missing", "stale", "conflicted"],
        allow_partial_reusable=False,
        max_components_per_etf_run=_bounded_int(
            values,
            "ETF_COMPONENT_RESEARCH_MAX_COMPONENTS_PER_ETF_RUN",
            3,
            minimum=1,
            maximum=3,
        ),
        max_components_per_day=_bounded_int(
            values,
            "ETF_COMPONENT_RESEARCH_MAX_COMPONENTS_PER_DAY",
            5,
            minimum=1,
            maximum=5,
        ),
        max_model_calls_per_component=1,
        max_model_calls_per_day=_bounded_int(
            values,
            "ETF_COMPONENT_RESEARCH_MAX_MODEL_CALLS_PER_DAY",
            5,
            minimum=1,
            maximum=5,
        ),
        max_input_tokens_per_component=_bounded_int(
            values,
            "ETF_COMPONENT_RESEARCH_MAX_INPUT_TOKENS_PER_COMPONENT",
            6000,
            minimum=256,
            maximum=6000,
        ),
        max_output_tokens_per_component=_bounded_int(
            values,
            "ETF_COMPONENT_RESEARCH_MAX_OUTPUT_TOKENS_PER_COMPONENT",
            600,
            minimum=64,
            maximum=1000,
        ),
        max_input_tokens_per_day=_bounded_int(
            values,
            "ETF_COMPONENT_RESEARCH_MAX_INPUT_TOKENS_PER_DAY",
            30000,
            minimum=256,
            maximum=30000,
        ),
        max_output_tokens_per_day=_bounded_int(
            values,
            "ETF_COMPONENT_RESEARCH_MAX_OUTPUT_TOKENS_PER_DAY",
            3000,
            minimum=64,
            maximum=3000,
        ),
        max_auto_repairs=0,
        digest_reuse_days=_bounded_int(
            values,
            "ETF_COMPONENT_RESEARCH_DIGEST_REUSE_DAYS",
            30,
            minimum=1,
            maximum=90,
        ),
        allowed_report_kinds=["component_research"],
        allowed_security_markets=["SH", "SZ", "BJ", "HK"],
    )


@dataclass(frozen=True, slots=True)
class ComponentResearchAuthorization:
    authorization_text: str
    etf_symbol: str
    component_symbols: list[str]
    max_model_calls: int
    max_input_tokens: int
    max_output_tokens: int
    max_auto_repairs: int = 0

    @classmethod
    def from_value(cls, value: Mapping[str, Any] | None) -> "ComponentResearchAuthorization | None":
        if not value:
            return None
        return cls(
            authorization_text=str(value.get("authorization_text") or ""),
            etf_symbol=normalize_component_symbol(str(value.get("etf_symbol") or "")),
            component_symbols=[
                normalize_component_symbol(str(item))
                for item in value.get("component_symbols") or []
            ],
            max_model_calls=int(value.get("max_model_calls") or 0),
            max_input_tokens=int(value.get("max_input_tokens") or 0),
            max_output_tokens=int(value.get("max_output_tokens") or 0),
            max_auto_repairs=int(value.get("max_auto_repairs") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "authorization_text": self.authorization_text,
            "etf_symbol": self.etf_symbol,
            "component_symbols": list(self.component_symbols),
            "max_model_calls": self.max_model_calls,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_auto_repairs": self.max_auto_repairs,
        }


def validate_pilot_authorization(
    authorization: ComponentResearchAuthorization | None,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if authorization is None:
        return False, ["explicit_pilot_authorization_missing"]
    normalized_text = _normalized_authorization_text(authorization.authorization_text)
    expected_model_calls = 3
    expected_output_tokens: int | None = None
    if normalized_text == _normalized_authorization_text(PILOT_AUTHORIZATION_TEXT):
        expected_output_tokens = 1800
    elif normalized_text == _normalized_authorization_text(
        PILOT_EXPANDED_OUTPUT_AUTHORIZATION_TEXT
    ):
        expected_output_tokens = 3000
    elif normalized_text == _normalized_authorization_text(
        PILOT_CODEX_SOFT_LIMIT_AUTHORIZATION_TEXT
    ):
        expected_output_tokens = 3000
    elif normalized_text == _normalized_authorization_text(
        PILOT_FEATURE_FIRST_AUTHORIZATION_TEXT
    ):
        expected_model_calls = -1
        expected_output_tokens = -1
    else:
        reasons.append("authorization_text_mismatch")
    if authorization.etf_symbol != PILOT_ETF_SYMBOL:
        reasons.append("authorization_etf_mismatch")
    if tuple(authorization.component_symbols) != PILOT_COMPONENT_SYMBOLS:
        reasons.append("authorization_component_scope_mismatch")
    if authorization.max_model_calls != expected_model_calls:
        reasons.append("authorization_model_call_limit_mismatch")
    if authorization.max_input_tokens != 18000:
        reasons.append("authorization_input_token_limit_mismatch")
    if (
        expected_output_tokens is not None
        and authorization.max_output_tokens != expected_output_tokens
    ):
        reasons.append("authorization_output_token_limit_mismatch")
    if authorization.max_auto_repairs != 0:
        reasons.append("authorization_auto_repair_must_be_zero")
    return not reasons, reasons


def _feature_first_authorized(
    authorization: ComponentResearchAuthorization | None,
    authorized: bool,
) -> bool:
    return bool(
        authorized
        and authorization is not None
        and authorization.max_model_calls == -1
        and authorization.max_output_tokens == -1
        and _normalized_authorization_text(authorization.authorization_text)
        == _normalized_authorization_text(PILOT_FEATURE_FIRST_AUTHORIZATION_TEXT)
    )


def _market_suffix(symbol: str) -> str:
    return normalize_component_symbol(symbol).rsplit(".", 1)[-1]


def conservative_token_upper_bound(value: Any) -> int:
    """A provider-independent pre-call upper bound: one token cannot use <1 byte."""

    return max(1, len(_canonical_json(value).encode("utf-8")))


_DOMAIN_DIMENSIONS: dict[str, tuple[ComponentResearchDimension, ...]] = {
    "identity_market": ("business_exposure",),
    "business_position": ("business_exposure",),
    "financial_statements": ("earnings_trend",),
    "financial_quality": ("earnings_trend", "risks"),
    "consensus": ("earnings_trend", "valuation"),
    "catalysts_risks": ("catalysts", "risks", "material_events"),
    "company_actions": ("material_events", "holder_governance"),
    "prior_conditions": ("catalysts", "risks"),
}


class ComponentResearchEvidencePackBuilder:
    """Freeze exact-code unified knowledge references before any model call."""

    def __init__(self, knowledge_store: ResearchKnowledgeStore) -> None:
        self.knowledge = knowledge_store

    @staticmethod
    def _row_time(item: Mapping[str, Any], *names: str) -> datetime | None:
        for name in names:
            parsed = _parse_time(item.get(name))
            if parsed is not None:
                return parsed
        return None

    def build(
        self,
        *,
        component_symbol: str,
        security_name: str,
        analysis_as_of: str,
        selection_id: str,
        resolution_id: str,
    ) -> tuple[ComponentResearchEvidencePack, dict[str, Any]]:
        symbol = normalize_component_symbol(component_symbol)
        cutoff_text = _normalized_time(analysis_as_of, field_name="analysis_as_of")
        cutoff = _parse_time(cutoff_text)
        assert cutoff is not None
        warnings: list[str] = []
        future_counts = {"evidence": 0, "facts": 0, "claims": 0}

        with self.knowledge.connect() as conn:
            evidence_rows = [
                dict(row)
                for row in conn.execute(
                    """SELECT e.*,d.canonical_url,d.publisher,d.source_class,
                              d.published_at AS document_published_at,
                              d.retrieved_at AS document_retrieved_at,
                              d.content_hash,d.superseded_by AS document_superseded_by
                       FROM evidence_records e JOIN source_documents d USING(document_ref)
                       WHERE e.symbol=? ORDER BY COALESCE(e.valid_from,d.published_at,e.created_at) DESC,
                                                e.evidence_id""",
                    (symbol,),
                )
            ]
            fact_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM fact_records WHERE symbol=? ORDER BY period DESC,created_at DESC,fact_id",
                    (symbol,),
                )
            ]
            claim_rows = [
                dict(row)
                for row in conn.execute(
                    """SELECT c.* FROM claim_records c
                       JOIN report_catalog_entries r ON r.report_id=c.origin_id
                       WHERE c.origin_type='report' AND r.subject_type='symbol'
                         AND r.subject_key=? AND UPPER(COALESCE(r.symbol,''))=?
                         AND r.status='published' AND r.report_quality_status<>'failed_validation'
                       ORDER BY c.created_at DESC,c.claim_id""",
                    (symbol, symbol),
                )
            ]

        valid_evidence: list[dict[str, Any]] = []
        dimensions: set[ComponentResearchDimension] = set()
        evidence_data_times: list[datetime] = []
        for item in evidence_rows:
            effective = self._row_time(
                item, "valid_from", "document_published_at", "created_at"
            )
            created = _parse_time(item.get("created_at"))
            if effective is None or effective > cutoff or (created is not None and created > cutoff):
                future_counts["evidence"] += 1
                continue
            if str(item.get("status") or "").casefold() not in {"verified", "valid", "pass"}:
                continue
            if item.get("document_superseded_by"):
                continue
            item["chunk_refs"] = _loads(item.pop("chunk_refs_json", "[]"), [])
            valid_evidence.append(item)
            evidence_data_times.append(effective)
            domain = str(item.get("domain") or "")
            dimensions.update(_DOMAIN_DIMENSIONS.get(domain, ()))
            inferred, _ = _dimensions_for_claim(domain, str(item.get("summary") or ""))
            dimensions.update(inferred)

        evidence_by_id = {
            str(item["evidence_id"]): item for item in valid_evidence
        }
        valid_facts: list[dict[str, Any]] = []
        financial_periods: list[str] = []
        for item in fact_rows:
            created = _parse_time(item.get("created_at"))
            if created is None or created > cutoff:
                future_counts["facts"] += 1
                continue
            if str(item.get("validation_status") or "") not in {
                "pass", "warning", "not_comparable"
            } or item.get("superseded_by"):
                continue
            ids = [str(value) for value in _loads(item.pop("evidence_ids_json", "[]"), [])]
            if ids and not set(ids).issubset(evidence_by_id):
                continue
            item["evidence_ids"] = ids
            item["input_fact_ids"] = _loads(item.pop("input_fact_ids_json", "[]"), [])
            valid_facts.append(item)
            inferred, _ = _dimensions_for_claim(
                str(item.get("metric") or ""), str(item.get("metric") or "")
            )
            dimensions.update(inferred)
            metric = str(item.get("metric") or "").casefold()
            if any(token in metric for token in ("revenue", "profit", "income", "营收", "利润")):
                dimensions.add("earnings_trend")
                if item.get("period"):
                    financial_periods.append(str(item["period"]))

        valid_claims: list[dict[str, Any]] = []
        for item in claim_rows:
            created = _parse_time(item.get("created_at"))
            if created is None or created > cutoff:
                future_counts["claims"] += 1
                continue
            if item.get("claim_status") != "prior_claim" or item.get("superseded_by"):
                continue
            evidence_ids = [
                str(value) for value in _loads(item.pop("evidence_ids_json", "[]"), [])
            ]
            fact_ids = [str(value) for value in _loads(item.pop("fact_ids_json", "[]"), [])]
            if evidence_ids and not set(evidence_ids).issubset(evidence_by_id):
                continue
            item["evidence_ids"] = evidence_ids
            item["fact_ids"] = fact_ids
            valid_claims.append(item)
            inferred, _ = _dimensions_for_claim(
                str(item.get("section_id") or ""), str(item.get("text") or "")
            )
            dimensions.update(inferred)

        valid_fact_ids = {str(item["fact_id"]) for item in valid_facts}
        conflicts: list[dict[str, Any]] = []
        if valid_fact_ids:
            with self.knowledge.connect() as conn:
                for row in conn.execute(
                    "SELECT * FROM fact_conflicts WHERE resolution_status='needs_third_source'"
                ):
                    item = dict(row)
                    created = _parse_time(item.get("created_at"))
                    fact_ids = {
                        str(value) for value in _loads(item.pop("fact_ids_json", "[]"), [])
                    }
                    if created is not None and created <= cutoff and fact_ids.intersection(valid_fact_ids):
                        item["fact_ids"] = sorted(fact_ids)
                        conflicts.append(item)

        for kind, count in future_counts.items():
            if count:
                warnings.append(f"future_{kind}_excluded:{count}")
        if conflicts:
            warnings.append("unresolved_structured_conflicts")
        source_classes = {str(item.get("source_class") or "") for item in valid_evidence}
        if source_classes.intersection({"mainstream_media", "research_session"}):
            warnings.append("lower_priority_sources_present")
        if "business_exposure" not in dimensions:
            warnings.append("missing_business_identity_evidence")
        if "earnings_trend" not in dimensions:
            warnings.append("missing_recent_financial_evidence")
        if "risks" not in dimensions:
            warnings.append("missing_risk_counterevidence")
        if "valuation" in dimensions:
            warnings.append("valuation_excluded_without_verified_market_snapshot")
            dimensions.discard("valuation")

        coverage = sorted(dimensions)
        all_dimensions: tuple[ComponentResearchDimension, ...] = (
            "business_exposure",
            "earnings_trend",
            "catalysts",
            "risks",
            "material_events",
            "valuation",
            "holder_governance",
        )
        missing = [item for item in all_dimensions if item not in dimensions]
        required_coverage = len(_CORE_EVIDENCE_DIMENSIONS.intersection(dimensions)) / len(
            _CORE_EVIDENCE_DIMENSIONS
        )
        if required_coverage == 1.0 and not conflicts:
            quality = "complete"
        elif "business_exposure" in dimensions and dimensions.intersection(
            {"earnings_trend", "risks"}
        ):
            quality = "partial"
        else:
            quality = "insufficient"

        # Model context contains bounded excerpts only.  The P4B2 tables persist
        # the IDs/fingerprint, never a second copy of Fact/Evidence prose.
        context = {
            "profile": COMPONENT_RESEARCH_PROFILE_ID,
            "component_symbol": symbol,
            "security_name": security_name,
            "analysis_as_of": cutoff_text,
            "evidence": [
                {
                    "evidence_id": item["evidence_id"],
                    "document_ref": item["document_ref"],
                    "domain": item.get("domain"),
                    "source_strength": item.get("source_strength"),
                    "published_at": item.get("document_published_at") or item.get("valid_from"),
                    "valid_until": item.get("valid_until"),
                    "source_url": item.get("canonical_url"),
                    "publisher": item.get("publisher"),
                    "summary": str(item.get("summary") or "")[:500],
                }
                for item in valid_evidence[:16]
            ],
            "facts": [
                {
                    "fact_id": item["fact_id"],
                    "metric": item.get("metric"),
                    "value": item.get("value"),
                    "unit": item.get("unit"),
                    "period": item.get("period"),
                    "evidence_ids": item.get("evidence_ids"),
                }
                for item in valid_facts[:24]
            ],
            "existing_claims": [
                {
                    "claim_id": item["claim_id"],
                    "section_id": item.get("section_id"),
                    "text": str(item.get("text") or "")[:500],
                    "fact_ids": item.get("fact_ids"),
                    "evidence_ids": item.get("evidence_ids"),
                }
                for item in valid_claims[:16]
            ],
            "conflicts": [
                {
                    "conflict_id": item["conflict_id"],
                    "fact_ids": item.get("fact_ids"),
                    "conflict_type": item.get("conflict_type"),
                }
                for item in conflicts
            ],
            "coverage_dimensions": coverage,
            "missing_dimensions": missing,
            "warnings": sorted(set(warnings)),
        }
        fingerprint = stable_fingerprint("p4b2evidenceinput", context)
        pack_id = stable_fingerprint(
            "p4b2evidencepack",
            {
                "component_symbol": symbol,
                "selection_id": selection_id,
                "resolution_id": resolution_id,
                "analysis_as_of": cutoff_text,
                "input_fingerprint": fingerprint,
            },
        )
        pack = ComponentResearchEvidencePack(
            evidence_pack_id=pack_id,
            component_symbol=symbol,
            security_name=str(security_name or symbol),
            analysis_as_of=cutoff_text,
            selection_id=selection_id,
            resolution_id=resolution_id,
            source_ids=sorted({str(item["document_ref"]) for item in valid_evidence}),
            fact_ids=sorted(valid_fact_ids),
            evidence_ids=sorted(evidence_by_id),
            existing_claim_ids=sorted(str(item["claim_id"]) for item in valid_claims),
            conflict_ids=sorted(str(item["conflict_id"]) for item in conflicts),
            coverage_dimensions=coverage,
            missing_dimensions=missing,
            market_data_status="excluded_unverified" if "valuation" in missing else "not_included",
            financial_period=max(financial_periods, default=None),
            latest_event_at=(
                max(evidence_data_times).isoformat() if evidence_data_times else None
            ),
            required_field_coverage=round(required_coverage, 6),
            quality=quality,  # type: ignore[arg-type]
            warnings=sorted(set(warnings)),
            input_fingerprint=fingerprint,
            evidence_data_as_of=(
                max(evidence_data_times).isoformat() if evidence_data_times else None
            ),
        )
        return pack, context


class ComponentResearchGenerationStore:
    """P4B2 state, audit, idempotency, and atomic daily budget reservations."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        knowledge_store: ResearchKnowledgeStore | None = None,
        auto_initialize: bool = False,
    ) -> None:
        self.path = Path(
            path
            or (knowledge_store.path if knowledge_store is not None else os.getenv(
                "VIBE_TRADING_RESEARCH_CACHE_DB",
                "~/.vibe-trading/cache/research_cache.sqlite3",
            ))
        ).expanduser()
        self._lock = threading.RLock()
        if auto_initialize:
            self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def has_schema(self) -> bool:
        if not self.path.exists():
            return False
        with self.connect() as conn:
            names = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        return _GENERATION_TABLES.issubset(names)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS component_research_evidence_packs (
                    evidence_pack_id TEXT PRIMARY KEY,
                    component_symbol TEXT NOT NULL,
                    input_fingerprint TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_p4b2_evidence_fingerprint
                    ON component_research_evidence_packs(component_symbol,input_fingerprint);
                CREATE TABLE IF NOT EXISTS component_research_generation_plans (
                    plan_id TEXT PRIMARY KEY,
                    etf_symbol TEXT NOT NULL,
                    selection_id TEXT NOT NULL,
                    resolution_id TEXT NOT NULL,
                    analysis_as_of TEXT NOT NULL,
                    dry_run INTEGER NOT NULL,
                    authorized INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_p4b2_plans_selection
                    ON component_research_generation_plans(selection_id,created_at DESC);
                CREATE TABLE IF NOT EXISTS component_research_generation_jobs (
                    job_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL REFERENCES component_research_generation_plans(plan_id),
                    idempotency_key TEXT NOT NULL,
                    component_symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    model_calls INTEGER NOT NULL DEFAULT 0,
                    actual_input_tokens INTEGER NOT NULL DEFAULT 0,
                    actual_output_tokens INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_p4b2_jobs_component
                    ON component_research_generation_jobs(component_symbol,created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_p4b2_jobs_idempotency
                    ON component_research_generation_jobs(idempotency_key,status);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_p4b2_component_single_flight
                    ON component_research_generation_jobs(component_symbol)
                    WHERE status IN ('approved','running');
                CREATE TABLE IF NOT EXISTS component_research_generation_audit (
                    audit_id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    plan_id TEXT,
                    job_id TEXT,
                    component_symbol TEXT,
                    status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_p4b2_audit_time
                    ON component_research_generation_audit(created_at DESC);
                CREATE TABLE IF NOT EXISTS component_research_budget_ledger (
                    ledger_id TEXT PRIMARY KEY,
                    budget_date TEXT NOT NULL,
                    job_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    component_count INTEGER NOT NULL,
                    model_calls INTEGER NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_p4b2_budget_day
                    ON component_research_budget_ledger(budget_date,state);
                CREATE TABLE IF NOT EXISTS component_research_publish_results (
                    publish_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL UNIQUE,
                    component_symbol TEXT NOT NULL,
                    report_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    published_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_p4b2_publish_component
                    ON component_research_publish_results(component_symbol,published_at DESC);
                """
            )

    def audit(
        self,
        operation: str,
        *,
        status: str,
        plan_id: str | None = None,
        job_id: str | None = None,
        component_symbol: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if not self.has_schema():
            return
        created = utc_now()
        audit_id = stable_fingerprint(
            "p4b2audit", [operation, plan_id, job_id, created, metadata or {}]
        )
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO component_research_generation_audit(
                       audit_id,operation,plan_id,job_id,component_symbol,status,metadata_json,created_at
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    audit_id,
                    operation,
                    plan_id,
                    job_id,
                    component_symbol,
                    status,
                    _canonical_json(metadata or {}),
                    created,
                ),
            )

    def save_plan(
        self,
        plan: ComponentResearchGenerationPlan,
        evidence_packs: Mapping[str, ComponentResearchEvidencePack],
    ) -> ComponentResearchGenerationPlan:
        if not self.has_schema():
            raise RuntimeError("P4B2 schema is not initialized")
        with self._lock, self.connect() as conn:
            for pack in evidence_packs.values():
                conn.execute(
                    """INSERT OR IGNORE INTO component_research_evidence_packs(
                           evidence_pack_id,component_symbol,input_fingerprint,payload_json,created_at
                       ) VALUES (?,?,?,?,?)""",
                    (
                        pack.evidence_pack_id,
                        pack.component_symbol,
                        pack.input_fingerprint,
                        _canonical_json(pack.to_dict()),
                        plan.created_at,
                    ),
                )
            conn.execute(
                """INSERT OR IGNORE INTO component_research_generation_plans(
                       plan_id,etf_symbol,selection_id,resolution_id,analysis_as_of,dry_run,
                       authorized,status,payload_json,created_at,expires_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    plan.plan_id,
                    plan.etf_symbol,
                    plan.selection_id,
                    plan.resolution_id,
                    plan.analysis_as_of,
                    int(plan.dry_run),
                    int(plan.authorized),
                    plan.status,
                    _canonical_json(plan.to_dict()),
                    plan.created_at,
                    plan.expires_at,
                ),
            )
            for job in plan.jobs:
                conn.execute(
                    """INSERT OR IGNORE INTO component_research_generation_jobs(
                           job_id,plan_id,idempotency_key,component_symbol,status,model_calls,
                           actual_input_tokens,actual_output_tokens,payload_json,created_at,updated_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        job.job_id,
                        plan.plan_id,
                        job.idempotency_key,
                        job.component_symbol,
                        job.status,
                        job.model_calls,
                        job.actual_input_tokens,
                        job.actual_output_tokens,
                        _canonical_json(job.to_dict()),
                        job.created_at,
                        job.created_at,
                    ),
                )
        return self.get_plan(plan.plan_id) or plan

    def get_plan(self, plan_id: str) -> ComponentResearchGenerationPlan | None:
        if not self.has_schema():
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM component_research_generation_plans WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            job_rows = conn.execute(
                "SELECT payload_json FROM component_research_generation_jobs WHERE plan_id=? ORDER BY created_at",
                (plan_id,),
            ).fetchall()
        if row is None:
            return None
        plan = ComponentResearchGenerationPlan.from_dict(_loads(row[0], {}))
        jobs = [
            ComponentResearchGenerationJob.from_dict(_loads(item[0], {}))
            for item in job_rows
        ]
        return replace(plan, jobs=jobs)

    def get_job(self, job_id: str) -> ComponentResearchGenerationJob | None:
        if not self.has_schema():
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM component_research_generation_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return ComponentResearchGenerationJob.from_dict(_loads(row[0], {})) if row else None

    def plan_id_for_job(self, job_id: str) -> str | None:
        if not self.has_schema():
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT plan_id FROM component_research_generation_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return str(row[0]) if row else None

    def get_evidence_pack(self, pack_id: str) -> ComponentResearchEvidencePack | None:
        if not self.has_schema():
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM component_research_evidence_packs WHERE evidence_pack_id=?",
                (pack_id,),
            ).fetchone()
        return ComponentResearchEvidencePack.from_dict(_loads(row[0], {})) if row else None

    def update_job(self, job: ComponentResearchGenerationJob) -> None:
        if not self.has_schema():
            raise RuntimeError("P4B2 schema is not initialized")
        plan_id: str | None = None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT plan_id FROM component_research_generation_jobs WHERE job_id=?",
                (job.job_id,),
            ).fetchone()
            plan_id = str(row[0]) if row else None
            conn.execute(
                """UPDATE component_research_generation_jobs
                   SET status=?,model_calls=?,actual_input_tokens=?,actual_output_tokens=?,
                       payload_json=?,updated_at=? WHERE job_id=?""",
                (
                    job.status,
                    job.model_calls,
                    job.actual_input_tokens,
                    job.actual_output_tokens,
                    _canonical_json(job.to_dict()),
                    utc_now(),
                    job.job_id,
                ),
            )
        if plan_id:
            self.refresh_plan_status(plan_id)

    def refresh_plan_status(self, plan_id: str) -> str | None:
        if not self.has_schema():
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM component_research_generation_plans WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            if row is None:
                return None
            statuses = [
                str(item[0])
                for item in conn.execute(
                    "SELECT status FROM component_research_generation_jobs WHERE plan_id=?",
                    (plan_id,),
                )
            ]
            if statuses and all(item in {"published", "skipped"} for item in statuses):
                status = "completed"
            elif any(item == "running" for item in statuses):
                status = "running"
            elif any(item in {"planned", "approved", "blocked"} for item in statuses):
                status = (
                    "in_progress"
                    if any(item in {"published", "failed", "cancelled"} for item in statuses)
                    else "planned"
                )
            elif statuses and all(item == "cancelled" for item in statuses):
                status = "cancelled"
            elif any(item == "failed" for item in statuses):
                status = "failed"
            else:
                status = "planned"
            plan = ComponentResearchGenerationPlan.from_dict(_loads(row[0], {}))
            updated = replace(plan, status=status)
            conn.execute(
                "UPDATE component_research_generation_plans SET status=?,payload_json=? WHERE plan_id=?",
                (status, _canonical_json(updated.to_dict()), plan_id),
            )
        return status

    def budget_usage(self, budget_date: str | None = None) -> dict[str, int]:
        target = budget_date or date.today().isoformat()
        if not self.has_schema():
            return {
                "components": 0,
                "model_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
            }
        with self.connect() as conn:
            row = conn.execute(
                """SELECT COALESCE(SUM(component_count),0),COALESCE(SUM(model_calls),0),
                          COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0)
                   FROM component_research_budget_ledger
                   WHERE budget_date=? AND state IN ('reserved','settled')""",
                (target,),
            ).fetchone()
        return {
            "components": int(row[0]),
            "model_calls": int(row[1]),
            "input_tokens": int(row[2]),
            "output_tokens": int(row[3]),
        }

    def reserve_budget(
        self,
        job: ComponentResearchGenerationJob,
        policy: ComponentResearchGenerationPolicy,
        *,
        feature_first: bool = False,
    ) -> tuple[bool, str | None, ComponentResearchPublishResult | None]:
        """Atomically enforce daily budgets, idempotency, and global single-flight."""

        if not self.has_schema():
            return False, "p4b2_schema_not_initialized", None
        today = date.today().isoformat()
        now = utc_now()
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            published = conn.execute(
                """SELECT p.payload_json FROM component_research_generation_jobs j
                   JOIN component_research_publish_results p ON p.job_id=j.job_id
                   WHERE j.idempotency_key=? AND j.status='published' LIMIT 1""",
                (job.idempotency_key,),
            ).fetchone()
            if published:
                conn.rollback()
                return (
                    False,
                    "idempotent_publish_cache_hit",
                    ComponentResearchPublishResult.from_dict(_loads(published[0], {})),
                )
            active = conn.execute(
                """SELECT job_id FROM component_research_generation_jobs
                   WHERE component_symbol=? AND status IN ('approved','running') AND job_id<>?
                   LIMIT 1""",
                (job.component_symbol, job.job_id),
            ).fetchone()
            if active:
                conn.rollback()
                return False, "component_single_flight_active", None
            used = conn.execute(
                """SELECT COALESCE(SUM(component_count),0),COALESCE(SUM(model_calls),0),
                          COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0)
                   FROM component_research_budget_ledger
                   WHERE budget_date=? AND state IN ('reserved','settled')""",
                (today,),
            ).fetchone()
            checks = [
                (
                    int(used[2]) + job.estimated_input_tokens,
                    policy.max_input_tokens_per_day,
                    "daily_input_token_budget_exceeded",
                )
            ]
            if not feature_first:
                checks.extend(
                    [
                        (int(used[0]) + 1, policy.max_components_per_day, "daily_component_budget_exceeded"),
                        (int(used[1]) + 1, policy.max_model_calls_per_day, "daily_model_call_budget_exceeded"),
                        (
                            int(used[3]) + job.estimated_output_tokens,
                            policy.max_output_tokens_per_day,
                            "daily_output_token_budget_exceeded",
                        ),
                    ]
                )
            exceeded = next((reason for value, limit, reason in checks if value > limit), None)
            if exceeded:
                conn.rollback()
                return False, exceeded, None
            ledger_id = stable_fingerprint("p4b2budget", [today, job.job_id])
            conn.execute(
                """INSERT INTO component_research_budget_ledger(
                       ledger_id,budget_date,job_id,state,component_count,model_calls,
                       input_tokens,output_tokens,created_at,updated_at
                   ) VALUES (?,?,?,?,1,1,?,?,?,?)""",
                (
                    ledger_id,
                    today,
                    job.job_id,
                    "reserved",
                    job.estimated_input_tokens,
                    job.estimated_output_tokens,
                    now,
                    now,
                ),
            )
            conn.execute(
                "UPDATE component_research_generation_jobs SET status='running',updated_at=? WHERE job_id=?",
                (now, job.job_id),
            )
            conn.commit()
        return True, None, None

    def settle_budget(
        self,
        job_id: str,
        *,
        actual_input_tokens: int,
        actual_output_tokens: int,
        release: bool = False,
    ) -> None:
        if not self.has_schema():
            return
        with self.connect() as conn:
            conn.execute(
                """UPDATE component_research_budget_ledger
                   SET state=?,input_tokens=?,output_tokens=?,updated_at=? WHERE job_id=?""",
                (
                    "released" if release else "settled",
                    max(0, int(actual_input_tokens)),
                    max(0, int(actual_output_tokens)),
                    utc_now(),
                    job_id,
                ),
            )

    def save_publish_result(self, result: ComponentResearchPublishResult) -> None:
        if not self.has_schema():
            raise RuntimeError("P4B2 schema is not initialized")
        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO component_research_publish_results(
                       publish_id,job_id,component_symbol,report_id,payload_json,published_at
                   ) VALUES (?,?,?,?,?,?)""",
                (
                    result.publish_id,
                    result.job_id,
                    result.component_symbol,
                    result.report_id,
                    _canonical_json(result.to_dict()),
                    result.published_at,
                ),
            )

    def latest_publish(self, component_symbol: str) -> ComponentResearchPublishResult | None:
        symbol = normalize_component_symbol(component_symbol)
        if not self.has_schema():
            return None
        with self.connect() as conn:
            row = conn.execute(
                """SELECT payload_json FROM component_research_publish_results
                   WHERE component_symbol=? ORDER BY published_at DESC LIMIT 1""",
                (symbol,),
            ).fetchone()
        return ComponentResearchPublishResult.from_dict(_loads(row[0], {})) if row else None


class BoundedComponentResearchModelRunner:
    """One no-tool call with a provider cap or mandatory usage-based ceiling."""

    def __call__(
        self,
        *,
        model_id: str,
        messages: list[dict[str, Any]],
        max_output_tokens: int,
    ) -> dict[str, Any]:
        from src.providers.chat import ChatLLM
        from src.providers.llm import build_llm

        llm = build_llm(model_name=model_id)
        bounded = llm.bind(max_tokens=max_output_tokens)
        raw = bounded.invoke(messages, config={"timeout": 120})
        response = ChatLLM._parse_response(raw)
        return {
            "content": str(response.content or ""),
            "usage": dict(response.usage_metadata or {}),
        }


class ComponentResearchGenerationService:
    """Create dry plans, run exact authorized jobs, and publish atomically."""

    def __init__(
        self,
        *,
        knowledge_store: ResearchKnowledgeStore | None = None,
        store: ComponentResearchGenerationStore | None = None,
        digest_service: ComponentResearchDigestService | None = None,
        policy: ComponentResearchGenerationPolicy | None = None,
        model_runner: Callable[..., dict[str, Any]] | None = None,
        model_id: str | None = None,
        now_provider: Callable[[], str] = utc_now,
    ) -> None:
        self.knowledge = knowledge_store or get_research_knowledge_store()
        self.store = store or ComponentResearchGenerationStore(
            knowledge_store=self.knowledge, auto_initialize=False
        )
        # P4B1 construction initializes its tables. Keep it lazy so merely
        # reading P4B2 settings or creating an ephemeral dry plan cannot migrate
        # the real runtime database before the authorized backup step.
        self.digest_service = digest_service
        self.policy = policy or component_research_generation_policy()
        self.model_runner = model_runner or BoundedComponentResearchModelRunner()
        self.model_id = str(model_id or os.getenv("LANGCHAIN_MODEL_NAME") or "unconfigured")
        self.now_provider = now_provider
        self.evidence_builder = ComponentResearchEvidencePackBuilder(self.knowledge)
        self._ephemeral_plans: dict[str, ComponentResearchGenerationPlan] = {}
        self._ephemeral_jobs: dict[str, ComponentResearchGenerationJob] = {}
        self._ephemeral_packs: dict[str, ComponentResearchEvidencePack] = {}
        self._contexts: dict[str, dict[str, Any]] = {}
        self._flight_guard = threading.RLock()
        self._flights: dict[str, threading.Lock] = {}

    def _require_digest_service(self) -> ComponentResearchDigestService:
        if self.digest_service is None:
            self.digest_service = ComponentResearchDigestService(
                knowledge_store=self.knowledge,
                store=ComponentResearchDigestStore(knowledge_store=self.knowledge),
            )
        return self.digest_service

    def refresh_policy(self, values: Mapping[str, Any] | None = None) -> None:
        self.policy = component_research_generation_policy(values)

    def _authorization(
        self, value: Mapping[str, Any] | None
    ) -> tuple[ComponentResearchAuthorization | None, bool, list[str]]:
        authorization = ComponentResearchAuthorization.from_value(value)
        valid, reasons = validate_pilot_authorization(authorization)
        return authorization, valid, reasons

    def _resolution_by_id(self, resolution_id: str) -> ComponentDigestResolution | None:
        with self.knowledge.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='component_digest_resolutions'"
            ).fetchone()
            if not exists:
                return None
            row = conn.execute(
                "SELECT payload_json FROM component_digest_resolutions WHERE resolution_id=?",
                (resolution_id,),
            ).fetchone()
        return ComponentDigestResolution.from_dict(_loads(row[0], {})) if row else None

    def _budget_remaining(self, *, feature_first: bool = False) -> dict[str, int]:
        used = self.store.budget_usage()
        return {
            "components": (
                -1
                if feature_first
                else max(0, self.policy.max_components_per_day - used["components"])
            ),
            "model_calls": (
                -1
                if feature_first
                else max(0, self.policy.max_model_calls_per_day - used["model_calls"])
            ),
            "input_tokens": max(
                0, self.policy.max_input_tokens_per_day - used["input_tokens"]
            ),
            "output_tokens": (
                -1
                if feature_first
                else max(0, self.policy.max_output_tokens_per_day - used["output_tokens"])
            ),
        }

    def create_plan(
        self,
        resolution: ComponentDigestResolution,
        *,
        requested_components: list[str],
        dry_run: bool = True,
        authorization: Mapping[str, Any] | None = None,
        persist: bool = True,
    ) -> ComponentResearchGenerationPlan:
        requested = [normalize_component_symbol(item) for item in requested_components]
        if not requested and resolution.selected_count:
            raise ValueError("requested_components must explicitly name the selected scope")
        if len(set(requested)) != len(requested):
            raise ValueError("requested_components must be a unique exact-code list")
        if len(requested) > self.policy.max_components_per_etf_run:
            raise ValueError("requested component scope exceeds the per-ETF hard limit")
        etf_symbol = normalize_component_symbol(resolution.etf_symbol)
        binding_by_symbol = {item.component_symbol: item for item in resolution.bindings}
        unknown = [item for item in requested if item not in binding_by_symbol]
        if unknown:
            raise ValueError("P4A did not select requested components: " + ",".join(unknown))
        auth_value, authorized, auth_reasons = self._authorization(authorization)
        feature_first = _feature_first_authorized(auth_value, authorized)
        authorization_contract = (
            _normalized_authorization_text(auth_value.authorization_text)
            if authorized and auth_value is not None
            else "dry_run_only"
        )
        if not dry_run and not authorized:
            raise PermissionError("live plan requires exact pilot authorization: " + ",".join(auth_reasons))
        if not dry_run and auth_value is not None and requested != auth_value.component_symbols:
            raise PermissionError("live plan scope must exactly equal the authorized component scope")
        created_at = _normalized_time(self.now_provider(), field_name="created_at")
        expires_at = (_parse_time(created_at) + timedelta(minutes=PLAN_TTL_MINUTES)).isoformat()  # type: ignore[operator]
        packs: dict[str, ComponentResearchEvidencePack] = {}
        jobs: list[ComponentResearchGenerationJob] = []
        remaining = self._budget_remaining(feature_first=feature_first)
        reserved_preview = {"components": 0, "model_calls": 0, "input_tokens": 0, "output_tokens": 0}
        skipped_reusable = 0
        skipped_budget = 0

        for priority, symbol in enumerate(requested, start=1):
            binding = binding_by_symbol[symbol]
            pack, context = self.evidence_builder.build(
                component_symbol=symbol,
                security_name=binding.component_name,
                analysis_as_of=resolution.analysis_as_of,
                selection_id=resolution.selection_id,
                resolution_id=resolution.resolution_id,
            )
            packs[pack.evidence_pack_id] = pack
            self._contexts[pack.evidence_pack_id] = context
            blocked: list[str] = []
            status = "planned"
            if binding.digest_status in {"reusable", "partial_reusable"}:
                blocked.append(f"digest_status_not_eligible:{binding.digest_status}")
                status = "skipped"
                skipped_reusable += 1
            elif binding.digest_status not in self.policy.eligible_statuses:
                blocked.append(f"digest_status_not_eligible:{binding.digest_status}")
                status = "skipped"
            if pack.quality != "complete":
                blocked.append(f"evidence_pack_quality:{pack.quality}")
                status = "blocked" if status == "planned" else status
            if pack.conflict_ids and binding.digest_status != "conflicted":
                blocked.append("evidence_pack_has_unresolved_conflicts")
                status = "blocked" if status == "planned" else status
            if _market_suffix(symbol) not in self.policy.allowed_security_markets:
                blocked.append("security_market_not_allowed")
                status = "blocked" if status == "planned" else status
            pilot_scope = etf_symbol == PILOT_ETF_SYMBOL and symbol in PILOT_COMPONENT_SYMBOLS
            if not pilot_scope:
                blocked.append("outside_p4b2_v1_pilot_scope")
                status = "blocked" if status == "planned" else status

            request_payload = self._model_payload(pack, context)
            estimated_input = conservative_token_upper_bound(request_payload)
            estimated_output = self.policy.max_output_tokens_per_component
            if estimated_input > self.policy.max_input_tokens_per_component:
                blocked.append("component_input_token_budget_exceeded")
                status = "blocked" if status == "planned" else status
            if status == "planned":
                would_use = {
                    "components": reserved_preview["components"] + 1,
                    "model_calls": reserved_preview["model_calls"] + 1,
                    "input_tokens": reserved_preview["input_tokens"] + estimated_input,
                    "output_tokens": reserved_preview["output_tokens"] + estimated_output,
                }
                enforced_budget_keys = (
                    ("input_tokens",)
                    if feature_first
                    else tuple(remaining)
                )
                if any(
                    would_use[key] > remaining[key] for key in enforced_budget_keys
                ):
                    blocked.append("dry_run_daily_budget_exceeded")
                    status = "blocked"
                    skipped_budget += 1
                else:
                    reserved_preview = would_use

            idempotency_key = stable_fingerprint(
                "p4b2idempotency",
                {
                    "component_symbol": symbol,
                    "selection_id": resolution.selection_id,
                    "resolution_id": resolution.resolution_id,
                    "evidence_pack_fingerprint": pack.input_fingerprint,
                    "analysis_as_of": resolution.analysis_as_of,
                    "prompt_version": COMPONENT_RESEARCH_PROMPT_VERSION,
                    "model_id": self.model_id,
                    "policy_version": self.policy.policy_version,
                    "authorization_contract": (
                        authorization_contract
                    ),
                },
            )
            job_id = stable_fingerprint("p4b2job", idempotency_key)
            jobs.append(
                ComponentResearchGenerationJob(
                    job_id=job_id,
                    idempotency_key=idempotency_key,
                    etf_symbol=etf_symbol,
                    selection_id=resolution.selection_id,
                    resolution_id=resolution.resolution_id,
                    component_symbol=symbol,
                    component_name=binding.component_name,
                    digest_status_before=binding.digest_status,
                    priority=priority,
                    depth="bounded",
                    evidence_pack_id=pack.evidence_pack_id,
                    evidence_pack_fingerprint=pack.input_fingerprint,
                    policy_version=self.policy.policy_version,
                    prompt_version=COMPONENT_RESEARCH_PROMPT_VERSION,
                    model_id=self.model_id,
                    selection_data_as_of=binding.selection_data_as_of,
                    analysis_as_of=resolution.analysis_as_of,
                    status=status,  # type: ignore[arg-type]
                    blocked_reasons=sorted(set(blocked)),
                    estimated_input_tokens=estimated_input if status == "planned" else 0,
                    estimated_output_tokens=estimated_output if status == "planned" else 0,
                    actual_input_tokens=0,
                    actual_output_tokens=0,
                    model_calls=0,
                    created_at=created_at,
                )
            )

        scope = list(auth_value.component_symbols) if authorized and auth_value else []
        plan_input = {
            "etf_symbol": etf_symbol,
            "selection_id": resolution.selection_id,
            "resolution_id": resolution.resolution_id,
            "knowledge_fingerprint": resolution.knowledge_fingerprint,
            "analysis_as_of": resolution.analysis_as_of,
            "dry_run": dry_run,
            "authorized": authorized,
            "authorization_scope": scope,
            "requested_components": requested,
            "evidence_fingerprints": [job.evidence_pack_fingerprint for job in jobs],
            "policy": self.policy.to_dict(),
            "prompt_version": COMPONENT_RESEARCH_PROMPT_VERSION,
            "model_id": self.model_id,
            "authorization_contract": authorization_contract,
        }
        plan_id = stable_fingerprint("p4b2plan", plan_input)
        planned = [item for item in jobs if item.status == "planned"]
        warnings = []
        if dry_run and not authorized:
            warnings.append("dry_run_only")
        if not self.policy.enabled:
            warnings.append("generation_feature_disabled")
        if not self.policy.live_run_enabled:
            warnings.append("live_run_feature_disabled")
        warnings.extend(auth_reasons if not authorized else [])
        plan = ComponentResearchGenerationPlan(
            plan_id=plan_id,
            etf_symbol=etf_symbol,
            selection_id=resolution.selection_id,
            resolution_id=resolution.resolution_id,
            analysis_as_of=resolution.analysis_as_of,
            dry_run=dry_run,
            authorized=authorized,
            authorization_scope=scope,
            candidate_count=len(requested),
            eligible_count=sum(
                1 for symbol in requested
                if binding_by_symbol[symbol].digest_status in self.policy.eligible_statuses
            ),
            planned_count=len(planned),
            skipped_reusable_count=skipped_reusable,
            skipped_budget_count=skipped_budget,
            blocked_count=sum(item.status == "blocked" for item in jobs),
            estimated_model_calls=len(planned),
            estimated_input_tokens=sum(item.estimated_input_tokens for item in planned),
            estimated_output_tokens=sum(item.estimated_output_tokens for item in planned),
            budget_remaining=remaining,
            jobs=jobs,
            warnings=sorted(set(warnings)),
            knowledge_fingerprint=resolution.knowledge_fingerprint,
            policy_version=self.policy.policy_version,
            created_at=created_at,
            expires_at=expires_at,
        )
        self._ephemeral_plans[plan_id] = plan
        for job in jobs:
            self._ephemeral_jobs[job.job_id] = job
        self._ephemeral_packs.update(packs)
        if persist and self.store.has_schema():
            plan = self.store.save_plan(plan, packs)
            self.store.audit(
                "plan_created",
                status="dry_run" if dry_run else "authorized",
                plan_id=plan.plan_id,
                metadata={
                    "authorized": authorized,
                    "candidate_count": plan.candidate_count,
                    "planned_count": plan.planned_count,
                    "estimated_model_calls": plan.estimated_model_calls,
                },
            )
        return plan

    def get_plan(self, plan_id: str) -> ComponentResearchGenerationPlan | None:
        return self.store.get_plan(plan_id) or self._ephemeral_plans.get(plan_id)

    def get_job(self, job_id: str) -> ComponentResearchGenerationJob | None:
        return self.store.get_job(job_id) or self._ephemeral_jobs.get(job_id)

    def get_evidence_pack(self, pack_id: str) -> ComponentResearchEvidencePack | None:
        return self.store.get_evidence_pack(pack_id) or self._ephemeral_packs.get(pack_id)

    @staticmethod
    def _model_payload(
        pack: ComponentResearchEvidencePack, context: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "task": "bounded_component_research",
            "evidence_pack": pack.to_dict(),
            "allowlisted_context": dict(context),
            "output_schema": {
                "component_symbol": "exact input symbol",
                "analysis_as_of": "ISO timestamp not later than input cutoff",
                "research_data_as_of": "ISO timestamp not later than input cutoff",
                "business_exposure_summary": "claim object",
                "earnings_trend_summary": "claim object",
                "catalyst_claims": "claim object array",
                "risk_claims": "claim object array",
                "material_event_claims": "claim object array",
                "valuation_claims": "claim object array; empty unless verified",
                "holder_governance_claims": "claim object array",
                "invalidation_conditions": "string array",
                "coverage_dimensions": "string array",
                "missing_dimensions": "string array",
                "warnings": "string array",
                "claim_object": {
                    "text": "string",
                    "dimension": "allowed dimension",
                    "stance": "supportive|neutral|adverse|mixed",
                    "confidence": "low|medium|high",
                    "evidence_ids": "non-empty allowlisted ID array",
                    "fact_ids": "allowlisted ID array",
                    "valid_until": "optional ISO timestamp",
                    "invalidation_conditions": "string array",
                },
            },
        }

    @staticmethod
    def _messages(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "role": "system",
                "content": (
                    "你是受控 ETF 成分研究生成器。只能使用 user 中 allowlisted_context 的内容；"
                    "不得搜索、猜测、补造事实、价格、财务数字或来源。所有关键 Claim 必须引用"
                    " allowlisted Evidence ID。只返回严格 JSON，不返回 Markdown。"
                ),
            },
            {"role": "user", "content": _canonical_json(payload)},
        ]

    @staticmethod
    def _extract_json(raw: str) -> dict[str, Any]:
        text = str(raw or "").strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I)
        candidate = fenced.group(1) if fenced else text
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start : end + 1]
        value = json.loads(candidate)
        if not isinstance(value, dict):
            raise ValueError("component research output must be a JSON object")
        return value

    def validate_output(
        self,
        raw: Mapping[str, Any],
        *,
        job: ComponentResearchGenerationJob,
        pack: ComponentResearchEvidencePack,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        value = dict(raw)
        if normalize_component_symbol(str(value.get("component_symbol") or "")) != job.component_symbol:
            raise ValueError("output component_symbol does not match the exact job symbol")
        analysis = _parse_time(value.get("analysis_as_of"))
        research_as_of = _parse_time(value.get("research_data_as_of"))
        cutoff = _parse_time(job.analysis_as_of)
        if analysis is None or research_as_of is None or cutoff is None:
            raise ValueError("output cutoff timestamps are required")
        if analysis > cutoff or research_as_of > cutoff:
            raise ValueError("future output timestamps are not allowed")
        if value.get("analysis_as_of") != job.analysis_as_of:
            raise ValueError("output analysis_as_of must equal the frozen job cutoff")

        field_dimensions = {
            "business_exposure_summary": "business_exposure",
            "earnings_trend_summary": "earnings_trend",
            "catalyst_claims": "catalysts",
            "risk_claims": "risks",
            "material_event_claims": "material_events",
            "valuation_claims": "valuation",
            "holder_governance_claims": "holder_governance",
        }
        claims: list[dict[str, Any]] = []
        allowed_evidence = set(pack.evidence_ids)
        allowed_facts = set(pack.fact_ids)
        for field, expected_dimension in field_dimensions.items():
            raw_items = value.get(field)
            items = [raw_items] if field.endswith("_summary") else list(raw_items or [])
            if field.endswith("_summary") and not isinstance(raw_items, dict):
                raise ValueError(f"{field} must be a sourced claim object")
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError(f"{field} contains a non-object claim")
                claim = dict(item)
                if str(claim.get("dimension") or "") != expected_dimension:
                    raise ValueError(f"{field} claim dimension mismatch")
                if not str(claim.get("text") or "").strip():
                    raise ValueError("claim text is required")
                evidence_ids = [str(entry) for entry in claim.get("evidence_ids") or []]
                fact_ids = [str(entry) for entry in claim.get("fact_ids") or []]
                if not evidence_ids or not set(evidence_ids).issubset(allowed_evidence):
                    raise ValueError("every key claim requires allowlisted Evidence IDs")
                if not set(fact_ids).issubset(allowed_facts):
                    raise ValueError("claim contains a non-allowlisted Fact ID")
                if expected_dimension == "valuation" and pack.market_data_status != "verified":
                    raise ValueError("valuation claims require a verified market snapshot")
                claim["evidence_ids"] = sorted(set(evidence_ids))
                claim["fact_ids"] = sorted(set(fact_ids))
                claim["invalidation_conditions"] = [
                    str(entry) for entry in claim.get("invalidation_conditions") or []
                ]
                claims.append(claim)
        dimensions = {str(item["dimension"]) for item in claims}
        if not _CORE_EVIDENCE_DIMENSIONS.issubset(dimensions):
            raise ValueError("output is missing a core sourced claim dimension")
        if not dimensions.issubset(set(pack.coverage_dimensions)):
            raise ValueError("output claims exceed frozen Evidence Pack coverage")
        value["coverage_dimensions"] = sorted(dimensions)
        value["missing_dimensions"] = [
            item
            for item in (
                "business_exposure",
                "earnings_trend",
                "catalysts",
                "risks",
                "material_events",
                "valuation",
                "holder_governance",
            )
            if item not in dimensions
        ]
        value["warnings"] = [str(item) for item in value.get("warnings") or []]
        return value, claims

    def _publish_transaction(
        self,
        *,
        job: ComponentResearchGenerationJob,
        pack: ComponentResearchEvidencePack,
        output: Mapping[str, Any],
        claims: list[dict[str, Any]],
        actual_input_tokens: int,
        actual_output_tokens: int,
        fail_after_claims: bool = False,
    ) -> ComponentResearchPublishResult:
        published_at = _normalized_time(self.now_provider(), field_name="published_at")
        report_id = stable_fingerprint("componentreport", job.idempotency_key)
        publish_id = stable_fingerprint("p4b2publish", [job.job_id, report_id])
        claim_rows: list[dict[str, Any]] = []
        for item in claims:
            claim_id = stable_fingerprint(
                "claim",
                [
                    report_id,
                    item["dimension"],
                    item["text"],
                    item["evidence_ids"],
                    item.get("fact_ids") or [],
                ],
            )
            claim_rows.append({**item, "claim_id": claim_id})
        evidence_ids = sorted({entry for item in claim_rows for entry in item["evidence_ids"]})
        fact_ids = sorted({entry for item in claim_rows for entry in item.get("fact_ids") or []})
        coverage = set(str(item) for item in output.get("coverage_dimensions") or [])
        coverage_status = "complete" if len(coverage) == 7 else "partial"
        quality_status = "passed" if coverage_status == "complete" else "passed_with_gaps"

        with self.knowledge.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if evidence_ids:
                rows = conn.execute(
                    f"SELECT evidence_id FROM evidence_records WHERE evidence_id IN ({','.join('?' for _ in evidence_ids)})",
                    evidence_ids,
                ).fetchall()
                if {str(row[0]) for row in rows} != set(evidence_ids):
                    conn.rollback()
                    raise ValueError("publish Evidence lineage changed or is incomplete")
            if fact_ids:
                rows = conn.execute(
                    f"SELECT fact_id FROM fact_records WHERE fact_id IN ({','.join('?' for _ in fact_ids)})",
                    fact_ids,
                ).fetchall()
                if {str(row[0]) for row in rows} != set(fact_ids):
                    conn.rollback()
                    raise ValueError("publish Fact lineage changed or is incomplete")
            knowledge_link = {
                "component_research_profile": COMPONENT_RESEARCH_PROFILE_ID,
                "evidence_pack_id": pack.evidence_pack_id,
                "prompt_version": job.prompt_version,
                "model_id": job.model_id,
                "input_tokens": actual_input_tokens,
                "output_tokens": actual_output_tokens,
                "selection_id": job.selection_id,
                "resolution_id": job.resolution_id,
                "generated_output": dict(output),
            }
            conn.execute(
                """INSERT INTO report_catalog_entries(
                       report_id,family_id,report_kind,subject_type,subject_key,symbol,security_name,
                       status,report_quality_status,coverage_status,generated_at,data_as_of,
                       source_type,source_id,source_revision,knowledge_link_json,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(report_id) DO UPDATE SET
                       report_quality_status=excluded.report_quality_status,
                       coverage_status=excluded.coverage_status,
                       knowledge_link_json=excluded.knowledge_link_json,
                       updated_at=excluded.updated_at""",
                (
                    report_id,
                    report_id,
                    "component_research",
                    "symbol",
                    job.component_symbol,
                    job.component_symbol,
                    job.component_name,
                    "published",
                    quality_status,
                    coverage_status,
                    published_at,
                    str(output["research_data_as_of"]),
                    "p4b2_component_research",
                    job.job_id,
                    1,
                    _canonical_json(knowledge_link),
                    published_at,
                    published_at,
                ),
            )
            for item in claim_rows:
                conn.execute(
                    """INSERT INTO claim_records(
                           claim_id,origin_type,origin_id,section_id,claim_type,text,
                           fact_ids_json,evidence_ids_json,claim_status,superseded_by,created_at
                       ) VALUES (?,'report',?,?,?,?,?,?,? ,NULL,?)
                       ON CONFLICT(claim_id) DO UPDATE SET claim_status=excluded.claim_status""",
                    (
                        item["claim_id"],
                        report_id,
                        item["dimension"],
                        "inference",
                        item["text"],
                        _canonical_json(item.get("fact_ids") or []),
                        _canonical_json(item["evidence_ids"]),
                        "prior_claim",
                        published_at,
                    ),
                )
                conn.execute("DELETE FROM claim_records_fts WHERE claim_id=?", (item["claim_id"],))
                conn.execute(
                    "INSERT INTO claim_records_fts(claim_id,origin_id,search_text) VALUES (?,?,?)",
                    (item["claim_id"], report_id, f"{job.component_symbol} {item['text']}"),
                )
            if fail_after_claims:
                conn.rollback()
                raise RuntimeError("injected publish failure")
            conn.execute(
                """INSERT INTO report_knowledge_links(
                       report_id,revision,evidence_ids_json,fact_ids_json,claim_ids_json,coverage_snapshot_id
                   ) VALUES (?,?,?,?,?,NULL)
                   ON CONFLICT(report_id,revision) DO UPDATE SET
                       evidence_ids_json=excluded.evidence_ids_json,
                       fact_ids_json=excluded.fact_ids_json,
                       claim_ids_json=excluded.claim_ids_json""",
                (
                    report_id,
                    1,
                    _canonical_json(evidence_ids),
                    _canonical_json(fact_ids),
                    _canonical_json([item["claim_id"] for item in claim_rows]),
                ),
            )
            conn.commit()

        return ComponentResearchPublishResult(
            publish_id=publish_id,
            job_id=job.job_id,
            component_symbol=job.component_symbol,
            report_id=report_id,
            claim_ids=[item["claim_id"] for item in claim_rows],
            fact_ids=fact_ids,
            evidence_ids=evidence_ids,
            quality_status=quality_status,  # type: ignore[arg-type]
            coverage_status=coverage_status,  # type: ignore[arg-type]
            published_at=published_at,
            p4b1_resolution_id_after=None,
            p4b1_digest_id_after=None,
            p4b1_digest_status_after=None,
            warnings=[],
        )

    @staticmethod
    def _selection_from_resolution(
        resolution: ComponentDigestResolution,
        *,
        allowed_symbols: set[str] | None = None,
    ) -> ETFComponentSelection:
        permitted = (
            {normalize_component_symbol(item) for item in allowed_symbols}
            if allowed_symbols is not None
            else None
        )
        bindings = [
            item for item in resolution.bindings
            if permitted is None or item.component_symbol in permitted
        ]
        selected = [
            ETFSelectedComponent(
                symbol=item.component_symbol,
                name=item.component_name,
                weight=item.component_weight,
                score=item.selection_score,
                marginal_explanation_gain=item.marginal_explanation_gain,
                forced=item.forced,
                reasons=list(item.selection_reasons),
                price_contribution=item.price_contribution,
                earnings_contribution=item.earnings_contribution,
            )
            for item in sorted(bindings, key=lambda value: value.selected_rank)
        ]
        concentration = ETFConcentrationMetrics(
            expected_component_count=len(selected),
            observed_component_count=len(selected),
            observed_weight_coverage=sum(item.weight for item in selected),
            top1_weight=max((item.weight for item in selected), default=0.0),
            top3_weight=sum(sorted((item.weight for item in selected), reverse=True)[:3]),
            top5_weight=sum(sorted((item.weight for item in selected), reverse=True)[:5]),
            top10_weight=sum(sorted((item.weight for item in selected), reverse=True)[:10]),
            hhi_lower_bound=sum(item.weight * item.weight for item in selected),
            hhi_upper_bound=sum(item.weight * item.weight for item in selected),
            concentration_class="focused",
            effective_component_count_lower_bound=float(len(selected)),
            min_penetration_count=0,
            max_penetration_count=5,
        )
        return ETFComponentSelection(
            selection_id=resolution.selection_id,
            etf_symbol=resolution.etf_symbol,
            input_fingerprint=stable_fingerprint(
                "p4b2selectionreconstruction",
                [
                    resolution.selection_id,
                    [item.binding_id for item in bindings],
                ],
            ),
            quality="complete",
            concentration=concentration,
            selected=selected,
            selected_weight_coverage=sum(item.weight for item in selected),
            explanation_coverage=sum(item.marginal_explanation_gain for item in selected),
            stop_reason="p4b2 deterministic re-resolution",
            warnings=["selection_reconstructed_from_persisted_p4b1_bindings"],
            created_at=min(
                (item.created_at for item in bindings), default=resolution.analysis_as_of
            ),
        )

    def execute_job(
        self,
        job_id: str,
        *,
        authorization: Mapping[str, Any],
    ) -> ComponentResearchPublishResult:
        auth_value, authorized, auth_reasons = self._authorization(authorization)
        feature_first = _feature_first_authorized(auth_value, authorized)
        if not self.policy.enabled:
            raise PermissionError("component research generation is disabled")
        if not self.policy.live_run_enabled:
            raise PermissionError("component research live run is disabled")
        if not authorized:
            raise PermissionError("exact pilot authorization required: " + ",".join(auth_reasons))
        job = self.get_job(job_id)
        if job is None:
            raise KeyError("generation job not found")
        persisted_plan_id = self.store.plan_id_for_job(job_id)
        plan = self.get_plan(persisted_plan_id) if persisted_plan_id else None
        if plan is None:
            plan = next(
                (value for value in self._ephemeral_plans.values() if job_id in {item.job_id for item in value.jobs}),
                None,
            )
        if plan is None or plan.dry_run or not plan.authorized:
            raise PermissionError("only a non-dry, authorized plan can execute")
        current_time = _parse_time(self.now_provider()) or datetime.now(timezone.utc)
        if (_parse_time(plan.expires_at) or datetime.min.replace(tzinfo=timezone.utc)) < current_time:
            raise RuntimeError("generation plan expired")
        if job.status != "planned":
            cached = self.store.latest_publish(job.component_symbol)
            if job.status == "published" and cached is not None:
                return cached
            raise RuntimeError(f"job is not executable from status {job.status}")

        pack = self.get_evidence_pack(job.evidence_pack_id)
        if pack is None:
            raise RuntimeError("frozen Evidence Pack not found")
        rebuilt, context = self.evidence_builder.build(
            component_symbol=job.component_symbol,
            security_name=job.component_name,
            analysis_as_of=job.analysis_as_of,
            selection_id=job.selection_id,
            resolution_id=job.resolution_id,
        )
        if rebuilt.input_fingerprint != job.evidence_pack_fingerprint:
            raise RuntimeError("Evidence Pack fingerprint changed; create a new plan")
        if rebuilt.quality != "complete":
            raise RuntimeError("Evidence Pack no longer passes the complete gate")

        digest_service = self._require_digest_service()
        resolution = self._resolution_by_id(job.resolution_id)
        if resolution is None:
            raise RuntimeError("P4B1 Resolution is missing")
        selection = self._selection_from_resolution(
            resolution,
            allowed_symbols=set(plan.authorization_scope),
        )
        refreshed = digest_service.resolve_selection(
            selection,
            job.analysis_as_of,
            selection_data_as_of=job.selection_data_as_of,
        )
        current_binding = next(
            (item for item in refreshed.bindings if item.component_symbol == job.component_symbol),
            None,
        )
        if current_binding is None or current_binding.digest_status not in self.policy.eligible_statuses:
            skipped = replace(
                job,
                status="skipped",
                blocked_reasons=["p4b1_status_no_longer_eligible"],
                finished_at=self.now_provider(),
            )
            self.store.update_job(skipped)
            raise RuntimeError("P4B1 status is no longer eligible")

        with self._flight_guard:
            flight = self._flights.setdefault(job.component_symbol, threading.Lock())
        with flight:
            reserved, reason, cached_publish = self.store.reserve_budget(
                job,
                self.policy,
                feature_first=feature_first,
            )
            if cached_publish is not None:
                return cached_publish
            if not reserved:
                raise RuntimeError(reason or "budget reservation failed")
            running = replace(job, status="running", started_at=self.now_provider())
            self.store.update_job(running)
            payload = self._model_payload(pack, context)
            messages = self._messages(payload)
            actual_input = 0
            actual_output = 0
            try:
                response = self.model_runner(
                    model_id=job.model_id,
                    messages=messages,
                    max_output_tokens=self.policy.max_output_tokens_per_component,
                )
                usage = dict(response.get("usage") or {})
                if "input_tokens" not in usage or "output_tokens" not in usage:
                    raise RuntimeError("provider did not report actual token usage")
                actual_input = int(usage["input_tokens"])
                actual_output = int(usage["output_tokens"])
                if actual_input > self.policy.max_input_tokens_per_component:
                    raise RuntimeError("actual component input token budget exceeded")
                if (
                    not feature_first
                    and actual_output > self.policy.max_output_tokens_per_component
                ):
                    raise RuntimeError("actual component output token budget exceeded")
                raw_output = self._extract_json(str(response.get("content") or ""))
                output, claims = self.validate_output(raw_output, job=job, pack=pack)
                result = self._publish_transaction(
                    job=job,
                    pack=pack,
                    output=output,
                    claims=claims,
                    actual_input_tokens=actual_input,
                    actual_output_tokens=actual_output,
                )
                after_cutoff = max(
                    _parse_time(job.analysis_as_of), _parse_time(result.published_at)
                )
                assert after_cutoff is not None
                after = digest_service.resolve_selection(
                    selection,
                    after_cutoff.isoformat(),
                    selection_data_as_of=job.selection_data_as_of,
                )
                after_binding = next(
                    item for item in after.bindings if item.component_symbol == job.component_symbol
                )
                result = replace(
                    result,
                    p4b1_resolution_id_after=after.resolution_id,
                    p4b1_digest_id_after=after_binding.digest_id,
                    p4b1_digest_status_after=after_binding.digest_status,
                )
                self.store.save_publish_result(result)
                published_job = replace(
                    running,
                    status="published",
                    actual_input_tokens=actual_input,
                    actual_output_tokens=actual_output,
                    model_calls=1,
                    finished_at=result.published_at,
                    publish_id=result.publish_id,
                )
                self.store.update_job(published_job)
                self.store.settle_budget(
                    job.job_id,
                    actual_input_tokens=actual_input,
                    actual_output_tokens=actual_output,
                )
                self.store.audit(
                    "publish_complete",
                    status="published",
                    plan_id=plan.plan_id,
                    job_id=job.job_id,
                    component_symbol=job.component_symbol,
                    metadata=result.to_dict(),
                )
                return result
            except Exception as exc:
                failed = replace(
                    running,
                    status="failed",
                    blocked_reasons=[str(exc)],
                    actual_input_tokens=actual_input,
                    actual_output_tokens=actual_output,
                    model_calls=1,
                    finished_at=self.now_provider(),
                )
                self.store.update_job(failed)
                self.store.settle_budget(
                    job.job_id,
                    actual_input_tokens=actual_input,
                    actual_output_tokens=actual_output,
                )
                self.store.audit(
                    "model_or_publish_failed",
                    status="failed",
                    plan_id=plan.plan_id,
                    job_id=job.job_id,
                    component_symbol=job.component_symbol,
                    metadata={"error": str(exc), "auto_repairs": 0},
                )
                raise

    def execute_plan(
        self,
        plan_id: str,
        *,
        authorization: Mapping[str, Any],
    ) -> list[ComponentResearchPublishResult]:
        plan = self.get_plan(plan_id)
        if plan is None:
            raise KeyError("generation plan not found")
        return [
            self.execute_job(job.job_id, authorization=authorization)
            for job in sorted(plan.jobs, key=lambda item: item.priority)
            if job.status in {"planned", "published"}
        ]

    def cancel_job(self, job_id: str) -> ComponentResearchGenerationJob:
        job = self.get_job(job_id)
        if job is None:
            raise KeyError("generation job not found")
        if job.status not in {"planned", "blocked", "approved"}:
            raise RuntimeError("only a not-started job can be cancelled")
        cancelled = replace(job, status="cancelled", finished_at=self.now_provider())
        if self.store.has_schema():
            self.store.update_job(cancelled)
        self._ephemeral_jobs[job_id] = cancelled
        return cancelled


def prepare_component_research_live_database(
    database_path: Path,
    *,
    authorization: Mapping[str, Any],
    backup_dir: Path | None = None,
) -> dict[str, Any]:
    """Back up first, then idempotently initialize P4B1/P4B2 after authorization."""

    auth = ComponentResearchAuthorization.from_value(authorization)
    valid, reasons = validate_pilot_authorization(auth)
    if not valid:
        raise PermissionError("exact pilot authorization required: " + ",".join(reasons))
    source_path = database_path.expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    target_dir = (backup_dir or source_path.parent / "backups").resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = target_dir / f"{source_path.stem}.pre-p4b2.{stamp}.sqlite3"
    writer = sqlite3.connect(source_path, timeout=2)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.rollback()
    finally:
        writer.close()
    source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    digest_store = ComponentResearchDigestStore(path=source_path)
    generation_store = ComponentResearchGenerationStore(path=source_path, auto_initialize=True)
    if not generation_store.has_schema() or not digest_store.path.exists():
        raise RuntimeError("P4B1/P4B2 migration verification failed")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return {
        "backup_path": str(target),
        "backup_size": target.stat().st_size,
        "backup_sha256": digest,
        "p4b1_initialized": True,
        "p4b2_initialized": True,
    }


def inspect_component_research_preflight(
    *,
    workspace_path: Path,
    runtime_database_path: Path,
    plan: ComponentResearchGenerationPlan,
    policy: ComponentResearchGenerationPolicy,
    authorization: Mapping[str, Any] | None = None,
    health_url: str = "http://127.0.0.1:8899/health",
) -> ComponentResearchPreflightResult:
    """Perform read-only workspace, service, SQLite, freshness, auth, and budget checks."""

    changes: list[str] = []
    try:
        completed = subprocess.run(
            ["git", "status", "--short"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        changes = [line for line in completed.stdout.splitlines() if line]
    except (OSError, subprocess.SubprocessError):
        changes = ["workspace_status_unavailable"]
    healthy = False
    service_detail = "unavailable"
    try:
        with urllib.request.urlopen(health_url, timeout=5) as response:  # noqa: S310 - fixed local URL
            body = response.read(4096).decode("utf-8", errors="replace")
            healthy = response.status == 200
            service_detail = body
    except (OSError, urllib.error.URLError) as exc:
        service_detail = str(exc)

    db_path = runtime_database_path.expanduser().resolve()
    stat = db_path.stat()
    tables: dict[str, int] = {}
    names: set[str] = set()
    uri = f"file:{db_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        names = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        for name in sorted(names):
            if not re.fullmatch(r"[A-Za-z0-9_]+", name):
                continue
            try:
                tables[name] = int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            except sqlite3.DatabaseError:
                tables[name] = -1
        selection_current = False
        if "etf_module_cache" in names:
            rows = conn.execute(
                """SELECT result_json FROM etf_module_cache
                   WHERE symbol=? AND module_id='holding_penetration' ORDER BY created_at DESC""",
                (plan.etf_symbol,),
            ).fetchall()
            selection_current = any(
                str((_loads(row[0], {}).get("selection") or {}).get("selection_id") or "")
                == plan.selection_id
                for row in rows
            )
        resolution_current = False
        if "component_digest_resolutions" in names:
            resolution_current = conn.execute(
                "SELECT 1 FROM component_digest_resolutions WHERE resolution_id=?",
                (plan.resolution_id,),
            ).fetchone() is not None

    auth = ComponentResearchAuthorization.from_value(authorization)
    authorized, auth_reasons = validate_pilot_authorization(auth)
    feature_first = _feature_first_authorized(auth, authorized)
    store = ComponentResearchGenerationStore(path=db_path)
    used = store.budget_usage()
    remaining = {
        "components": (
            -1 if feature_first else max(0, policy.max_components_per_day - used["components"])
        ),
        "model_calls": (
            -1 if feature_first else max(0, policy.max_model_calls_per_day - used["model_calls"])
        ),
        "input_tokens": max(0, policy.max_input_tokens_per_day - used["input_tokens"]),
        "output_tokens": (
            -1 if feature_first else max(0, policy.max_output_tokens_per_day - used["output_tokens"])
        ),
    }
    planned = {
        "components": plan.planned_count,
        "model_calls": plan.estimated_model_calls,
        "input_tokens": plan.estimated_input_tokens,
        "output_tokens": plan.estimated_output_tokens,
    }
    budget_sufficient = all(
        planned[key] <= remaining[key]
        for key in (("input_tokens",) if feature_first else tuple(planned))
    )
    blocked = []
    if not healthy:
        blocked.append("service_unhealthy")
    if not selection_current:
        blocked.append("selection_not_current_in_runtime_cache")
    if not resolution_current:
        blocked.append("resolution_not_initialized_in_runtime_database")
    if not _P4B1_TABLES.issubset(names):
        blocked.append("p4b1_schema_not_initialized")
    if not _GENERATION_TABLES.issubset(names):
        blocked.append("p4b2_schema_not_initialized")
    if not authorized:
        blocked.extend(auth_reasons)
    if not budget_sufficient:
        blocked.append("budget_insufficient")
    preflight_id = stable_fingerprint(
        "p4b2preflight",
        {
            "plan_id": plan.plan_id,
            "database_size": stat.st_size,
            "database_mtime_ns": stat.st_mtime_ns,
            "tables": tables,
            "authorized": authorized,
            "planned": planned,
            "used": used,
        },
    )
    return ComponentResearchPreflightResult(
        preflight_id=preflight_id,
        checked_at=utc_now(),
        healthy_service=healthy,
        service_detail=service_detail,
        workspace_dirty=bool(changes),
        workspace_changes=changes,
        runtime_database_path=str(db_path),
        runtime_database_size=stat.st_size,
        runtime_database_mtime_ns=stat.st_mtime_ns,
        runtime_tables=tables,
        p4b1_initialized=_P4B1_TABLES.issubset(names),
        p4b2_initialized=_GENERATION_TABLES.issubset(names),
        selection_current=selection_current,
        resolution_current=resolution_current,
        authorized=authorized,
        authorization_scope=list(auth.component_symbols) if authorized and auth else [],
        budget_used=used,
        budget_remaining=remaining,
        planned_budget=planned,
        budget_sufficient=budget_sufficient,
        dry_run_only=not authorized,
        blocked_reasons=sorted(set(blocked)),
    )


_shared_service: ComponentResearchGenerationService | None = None
_shared_lock = threading.Lock()


def get_component_research_generation_service() -> ComponentResearchGenerationService:
    global _shared_service
    if _shared_service is None:
        with _shared_lock:
            if _shared_service is None:
                _shared_service = ComponentResearchGenerationService()
    return _shared_service
