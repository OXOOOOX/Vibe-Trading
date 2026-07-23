"""Unified report catalog built on top of the research knowledge database."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import threading
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from src.research.knowledge import ResearchKnowledgeStore, get_research_knowledge_store

from .contracts import (
    ReportArtifactLink,
    ReportEnvelope,
    ReportRelation,
    ReportViewpoint,
)


def _report_reference_code(value: dict[str, Any]) -> str:
    """Stable, human-searchable code for internal report citations."""

    symbol = re.sub(r"[^A-Z0-9]", "", str(value.get("symbol") or "UNKNOWN").upper())
    profile = str(value.get("profile") or "")
    kind = str(value.get("report_kind") or "")
    instrument = (
        "ETF" if profile == "etf_deep_research"
        else "IDX" if profile == "index_deep_research"
        else "EQ" if profile == "equity_deep_research"
        else {
            "component_research": "CR",
            "daily_holding": "DY",
            "daily_portfolio": "PF",
            "weekly_review": "WK",
            "monitor_research": "MR",
            "deep_research": "DR",
        }.get(kind, "IR")
    )
    date_value = re.sub(
        r"\D", "", str(value.get("report_date") or value.get("generated_at") or "")
    )[:8] or "UNDATED"
    revision = int(value.get("revision") or value.get("source_revision") or 1)
    suffix = hashlib.sha256(str(value.get("report_id") or "").encode("utf-8")).hexdigest()[:6].upper()
    return f"VT-{instrument}-{symbol}-{date_value}-R{revision:02d}-{suffix}"


logger = logging.getLogger(__name__)
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_HORIZONS = ("intraday", "daily", "weekly", "structural")
_REPORT_KINDS = {
    "deep_research",
    "daily_holding",
    "daily_portfolio",
    "weekly_review",
    "monitor_research",
    "component_research",
}
_QUALITY = {"passed", "passed_with_gaps", "failed_validation"}
_COVERAGE = {"complete", "partial", "insufficient", "unknown"}
_STATUS = {"published", "diagnostic", "archived"}
_STANCE = {"bullish", "neutral", "bearish", "mixed", "unknown"}
_ACTION = {"observe", "add", "reduce", "exit", "not_applicable"}
_CONFIDENCE = {"low", "medium", "high", "unknown"}


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", "off"}


def report_library_enabled() -> bool:
    return _flag("VIBE_TRADING_REPORT_LIBRARY_ENABLED")


def report_viewpoint_ai_enabled() -> bool:
    return _flag("VIBE_TRADING_REPORT_VIEWPOINT_AI_ENABLED")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, *parts: Any) -> str:
    payload = json.dumps(
        parts,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return f"{prefix}_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def _parse_time(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(raw[:10]), time(15, 0), _SHANGHAI)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_SHANGHAI)
    return parsed.astimezone(timezone.utc)


def _aware_iso(value: Any, *, fallback: Any = None) -> str:
    parsed = _parse_time(value) or _parse_time(fallback)
    if parsed is None:
        raise ValueError("a timezone-aware generated_at/data_as_of value is required")
    return parsed.isoformat()


def _loads(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _strings(value: Any) -> list[str]:
    return [str(item) for item in (value or []) if str(item or "").strip()]


def _report_period(value: dict[str, Any], *, data_as_of: str) -> dict[str, str | None]:
    """Return a display period without changing the meaning of data_as_of."""

    raw = value.get("report_period")
    if isinstance(raw, dict):
        start_date = str(raw.get("start_date") or "").strip()[:10] or None
        end_date = str(raw.get("end_date") or "").strip()[:10] or None
        label = str(raw.get("label") or "").strip() or None
        if start_date or end_date or label:
            return {"start_date": start_date, "end_date": end_date, "label": label}
    report_date = str(value.get("report_date") or "").strip()[:10]
    if report_date:
        return {"start_date": report_date, "end_date": report_date, "label": report_date}
    day = str(data_as_of or "")[:10] or None
    return {"start_date": day, "end_date": day, "label": day}


def _next_business_day_end(value: str) -> str | None:
    parsed = _parse_time(value)
    if parsed is None:
        return None
    local = parsed.astimezone(_SHANGHAI)
    next_day = local.date() + timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += timedelta(days=1)
    return datetime.combine(next_day, time(23, 59, 59), _SHANGHAI).astimezone(
        timezone.utc
    ).isoformat()


def _markdown_summary(body: str, *, maximum: int = 1200) -> str:
    paragraphs = []
    buffer: list[str] = []
    for line in str(body or "").splitlines():
        if line.lstrip().startswith("#") or line.lstrip().startswith("|"):
            continue
        if not line.strip():
            if buffer:
                paragraphs.append(" ".join(buffer).strip())
                buffer.clear()
            continue
        if not line.lstrip().startswith(("-", ">", "```")):
            buffer.append(line.strip())
    if buffer:
        paragraphs.append(" ".join(buffer).strip())
    summary = next((item for item in paragraphs if len(item) >= 12), "")
    return summary[:maximum]


def _infer_stance_action(text: str, action: str | None = None) -> tuple[str, str]:
    normalized_action = str(action or "").strip().lower()
    if normalized_action in _ACTION:
        stance = {
            "add": "bullish",
            "reduce": "bearish",
            "exit": "bearish",
            "observe": "neutral",
            "not_applicable": "unknown",
        }[normalized_action]
        return stance, normalized_action
    lowered = str(text or "").casefold()
    if any(token in lowered for token in ("清仓", "退出", "exit")):
        return "bearish", "exit"
    if any(token in lowered for token in ("减仓", "降低仓位", "reduce")):
        return "bearish", "reduce"
    if any(token in lowered for token in ("加仓", "增加仓位", "add")):
        return "bullish", "add"
    bullish = any(token in lowered for token in ("偏多", "看多", "上行", "积极"))
    bearish = any(token in lowered for token in ("偏空", "看空", "下行", "谨慎"))
    if bullish and bearish:
        return "mixed", "not_applicable"
    if bullish:
        return "bullish", "not_applicable"
    if bearish:
        return "bearish", "not_applicable"
    if any(token in lowered for token in ("观察", "中性", "observe", "neutral")):
        return "neutral", "observe"
    return "unknown", "not_applicable"


def _claim(
    report_id: str,
    section_id: str,
    ordinal: int,
    text: Any,
    *,
    claim_type: str = "opinion",
) -> dict[str, Any] | None:
    content = str(text or "").strip()
    if not content:
        return None
    return {
        "claim_id": _stable_id("claim", report_id, section_id, ordinal, content),
        "section_id": section_id,
        "claim_type": claim_type,
        "text": content[:4000],
        "fact_ids": [],
        "evidence_ids": [],
    }


class ChatReportComparisonSummarizer:
    """Optional bounded LLM explanation over structured deltas and Claim excerpts."""

    prompt_version = "report-viewpoint-delta-v1"

    def __init__(self, client_factory: Callable[[], Any] | None = None) -> None:
        self.client_factory = client_factory

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
            raise ValueError("comparison summary must be a JSON object")
        return value

    def summarize(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.client_factory is None:
            from src.providers.chat import ChatLLM

            client = ChatLLM()
        else:
            client = self.client_factory()
        messages = [
            {
                "role": "system",
                "content": (
                    "你只解释给定报告观点的变化，不生成新的事实、价格、交易点位或统一结论。"
                    "只能引用输入 allowlisted_claims 中存在的 report_id、claim_id、section_id。"
                    "返回严格 JSON：{summary:string, items:[{text:string,citations:[{report_id,claim_id,section_id}]}]}。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            },
        ]
        raw = str(client.chat(messages, timeout=120).content or "")
        return self._extract_json(raw)


class ReportLibraryService:
    """Searchable report metadata and viewpoint comparisons over knowledge IDs."""

    def __init__(
        self,
        knowledge_store: ResearchKnowledgeStore | None = None,
        *,
        summarizer: ChatReportComparisonSummarizer | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.knowledge = knowledge_store or get_research_knowledge_store()
        self.summarizer = summarizer
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._ensure_epoch()

    def _ensure_epoch(self) -> None:
        now = _utc_now()
        with self.knowledge.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO report_library_meta(key,value,updated_at) VALUES ('catalog_epoch', ?, ?)",
                (now, now),
            )

    def catalog_epoch(self) -> str:
        with self.knowledge.connect() as conn:
            row = conn.execute(
                "SELECT value FROM report_library_meta WHERE key='catalog_epoch'"
            ).fetchone()
        return str(row["value"] if row else "")

    def record_index_failure(
        self,
        *,
        source_type: str,
        source_id: str,
        error: str,
    ) -> None:
        """Persist a compact operational signal without touching report content."""

        now = _utc_now()
        with self.knowledge.connect() as conn:
            row = conn.execute(
                "SELECT value FROM report_library_meta WHERE key='index_failure_count'"
            ).fetchone()
            try:
                count = int(row["value"] if row else 0) + 1
            except (TypeError, ValueError):
                count = 1
            payload = json.dumps(
                {
                    "source_type": source_type,
                    "source_id": source_id,
                    "error": str(error)[:1000],
                    "failed_at": now,
                },
                ensure_ascii=False,
            )
            conn.execute(
                """INSERT INTO report_library_meta(key,value,updated_at) VALUES ('index_failure_count',?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                (str(count), now),
            )
            conn.execute(
                """INSERT INTO report_library_meta(key,value,updated_at) VALUES ('last_index_failure',?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                (payload, now),
            )

    def status(self) -> dict[str, Any]:
        """Return catalog coverage counters suitable for health and rollout checks."""

        with self.knowledge.connect() as conn:
            summary = conn.execute(
                """SELECT COUNT(*) AS report_count,
                          COUNT(DISTINCT subject_key) AS subject_count,
                          SUM(CASE WHEN status='published' THEN 1 ELSE 0 END) AS published_count,
                          SUM(CASE WHEN status='diagnostic' THEN 1 ELSE 0 END) AS diagnostic_count
                   FROM report_catalog_entries"""
            ).fetchone()
            kinds = conn.execute(
                "SELECT report_kind,COUNT(*) AS count FROM report_catalog_entries GROUP BY report_kind"
            ).fetchall()
            meta_rows = conn.execute(
                "SELECT key,value FROM report_library_meta WHERE key IN ('index_failure_count','last_index_failure')"
            ).fetchall()
            source_summary = conn.execute(
                """SELECT
                       (SELECT COUNT(*) FROM source_documents) AS document_count,
                       (SELECT COUNT(*) FROM source_observations) AS observation_count,
                       (SELECT COUNT(*) FROM report_source_links) AS report_source_link_count,
                       (SELECT COUNT(*) FROM research_note_subjects) AS research_note_count,
                       (SELECT COUNT(*) FROM source_observations
                        WHERE verification_status='official_primary') AS official_primary_count"""
            ).fetchone()
        meta = {str(row["key"]): row["value"] for row in meta_rows}
        return {
            "enabled": report_library_enabled(),
            "catalog_epoch": self.catalog_epoch(),
            "report_count": int(summary["report_count"] or 0),
            "subject_count": int(summary["subject_count"] or 0),
            "published_count": int(summary["published_count"] or 0),
            "diagnostic_count": int(summary["diagnostic_count"] or 0),
            "by_report_kind": {
                str(row["report_kind"]): int(row["count"] or 0) for row in kinds
            },
            "index_failure_count": int(meta.get("index_failure_count") or 0),
            "last_index_failure": _loads(meta.get("last_index_failure"), None),
            "source_archive": {
                "document_count": int(source_summary["document_count"] or 0),
                "observation_count": int(source_summary["observation_count"] or 0),
                "report_source_link_count": int(source_summary["report_source_link_count"] or 0),
                "research_note_count": int(source_summary["research_note_count"] or 0),
                "official_primary_count": int(source_summary["official_primary_count"] or 0),
                **self.knowledge.source_archive_integrity(),
            },
        }

    def _knowledge_link(self, report_id: str, revision: int | None = None) -> dict[str, Any]:
        sql = "SELECT * FROM report_knowledge_links WHERE report_id=?"
        params: list[Any] = [report_id]
        if revision is not None:
            sql += " AND revision=?"
            params.append(int(revision))
        sql += " ORDER BY revision DESC LIMIT 1"
        with self.knowledge.connect() as conn:
            row = conn.execute(sql, params).fetchone()
        if not row:
            return {}
        claim_support = _loads(row["claim_support_json"], {})
        support_by_claim = {
            str(item.get("claim_id")): dict(item)
            for item in claim_support.get("claims") or []
            if isinstance(item, dict) and item.get("claim_id")
        } if isinstance(claim_support, dict) else {}
        return {
            "coverage_snapshot_id": row["coverage_snapshot_id"],
            "evidence_ids": _loads(row["evidence_ids_json"], []),
            "fact_ids": _loads(row["fact_ids_json"], []),
            "claim_ids": _loads(row["claim_ids_json"], []),
            "claim_support": claim_support,
            "claim_support_by_claim": support_by_claim,
        }

    def _claim_rows(
        self,
        claim_ids: Iterable[str],
        support_by_claim: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        ordered = _strings(claim_ids)
        if not ordered:
            return []
        with self.knowledge.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM claim_records WHERE claim_id IN ({','.join('?' for _ in ordered)})",
                ordered,
            ).fetchall()
        by_id: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            item["fact_ids"] = _loads(item.pop("fact_ids_json", "[]"), [])
            item["evidence_ids"] = _loads(item.pop("evidence_ids_json", "[]"), [])
            support = dict((support_by_claim or {}).get(str(row["claim_id"])) or {})
            item["support_status"] = support.get("support_status", "unclassified")
            item["support_reason"] = support.get("support_reason", "legacy_unclassified")
            item["reusable"] = bool(support.get("reusable", False))
            by_id[str(row["claim_id"])] = item
        return [by_id[item] for item in ordered if item in by_id]

    @staticmethod
    def _claim_groups(rows: list[dict[str, Any]]) -> dict[str, Any]:
        summary: str | None = None
        reasons: list[str] = []
        risks: list[str] = []
        conditions: list[str] = []
        invalidations: list[str] = []
        for row in rows:
            claim_id = str(row.get("claim_id") or "")
            section = str(row.get("section_id") or "").casefold()
            if summary is None and any(token in section for token in ("executive", "summary", "conclusion")):
                summary = claim_id
            if any(token in section for token in ("counter", "risk")):
                risks.append(claim_id)
            elif any(token in section for token in ("invalidation", "失效")):
                invalidations.append(claim_id)
            elif any(
                token in section
                for token in (
                    "watch",
                    "condition",
                    "daily_level",
                    "daily_trigger",
                    "daily_confirmation",
                    "daily_volume_confirmation",
                    "daily_action",
                    "daily_calculation_basis",
                    "daily_interpretation",
                    "weekly_level",
                    "weekly_trigger",
                    "weekly_confirmation",
                    "weekly_volume_condition",
                    "weekly_action",
                    "weekly_previous_outcome",
                    "weekly_change_reason",
                )
            ):
                conditions.append(claim_id)
            else:
                reasons.append(claim_id)
        if summary is None and rows:
            summary = str(rows[0].get("claim_id") or "") or None
        return {
            "summary": summary,
            "reasons": [item for item in reasons if item != summary],
            "risks": risks,
            "conditions": conditions,
            "invalidations": invalidations,
        }

    @staticmethod
    def _artifact_link(raw: dict[str, Any], *, source_locator: str, revision: int) -> ReportArtifactLink:
        path = Path(str(raw.get("path") or ""))
        sha256 = str(raw.get("sha256") or "") or None
        if sha256 is None and path.is_file():
            try:
                sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                sha256 = None
        media_type = str(
            raw.get("media_type")
            or raw.get("artifact_type")
            or "application/octet-stream"
        )
        materialization_status = raw.get("materialization_status")
        if not materialization_status and media_type == "application/pdf":
            materialization_status = (
                "materialized" if bool(raw.get("materialized")) or path.is_file()
                else "generatable"
            )
        return ReportArtifactLink(
            artifact_id=str(raw.get("artifact_id") or raw.get("kind") or "artifact"),
            artifact_role=str(raw.get("artifact_role") or raw.get("kind") or "report"),
            filename=str(raw.get("filename") or path.name or "artifact"),
            media_type=media_type,
            source_locator=source_locator,
            sha256=sha256,
            available=bool(raw.get("available", True)) and not bool(raw.get("expired")),
            revision=int(raw.get("revision") or revision or 1),
            materialization_status=materialization_status,
            materialization_error=(
                str(raw.get("materialization_error"))[:500]
                if raw.get("materialization_error") else None
            ),
        )

    def register_report(self, envelope: ReportEnvelope | dict[str, Any]) -> dict[str, Any]:
        value = envelope.to_dict() if isinstance(envelope, ReportEnvelope) else dict(envelope)
        report_id = str(value.get("report_id") or "").strip()
        if not report_id:
            raise ValueError("report_id is required")
        if str(value.get("report_kind")) not in _REPORT_KINDS:
            raise ValueError("unsupported report_kind")
        if str(value.get("subject_type")) not in {"symbol", "portfolio"}:
            raise ValueError("unsupported subject_type")
        if str(value.get("status")) not in _STATUS:
            raise ValueError("unsupported report status")
        if str(value.get("report_quality_status")) not in _QUALITY:
            raise ValueError("unsupported report quality")
        if str(value.get("coverage_status")) not in _COVERAGE:
            raise ValueError("unsupported coverage status")
        generated_at = _aware_iso(value.get("generated_at"))
        data_as_of = _aware_iso(value.get("data_as_of"), fallback=generated_at)
        source_revision = int(value.get("source_revision") or 1)
        knowledge_link = dict(value.get("knowledge_link") or {})
        knowledge_link.setdefault(
            "report_period",
            _report_period(value, data_as_of=data_as_of),
        )
        knowledge_link.setdefault(
            "internal_reference_code",
            _report_reference_code({
                **value,
                "generated_at": generated_at,
                "source_revision": source_revision,
            }),
        )
        value["knowledge_link"] = knowledge_link
        now = _utc_now()
        viewpoints = [
            item.to_dict() if isinstance(item, ReportViewpoint) else dict(item)
            for item in value.get("viewpoints") or []
        ]
        artifacts = [
            item.to_dict() if isinstance(item, ReportArtifactLink) else dict(item)
            for item in value.get("artifacts") or []
        ]
        relations = [
            item.to_dict() if isinstance(item, ReportRelation) else dict(item)
            for item in value.get("relations") or []
        ]
        seen_horizons: set[str] = set()
        for viewpoint in viewpoints:
            horizon = str(viewpoint.get("horizon") or "")
            if horizon not in _HORIZONS or horizon in seen_horizons:
                raise ValueError("each report must have at most one valid viewpoint per horizon")
            if str(viewpoint.get("stance") or "unknown") not in _STANCE:
                raise ValueError("unsupported viewpoint stance")
            if str(viewpoint.get("action") or "not_applicable") not in _ACTION:
                raise ValueError("unsupported viewpoint action")
            if str(viewpoint.get("confidence") or "unknown") not in _CONFIDENCE:
                raise ValueError("unsupported viewpoint confidence")
            seen_horizons.add(horizon)

        with self._lock, self.knowledge.connect() as conn:
            existing_source = conn.execute(
                "SELECT report_id FROM report_catalog_entries WHERE source_type=? AND source_id=? AND source_revision=?",
                (
                    str(value.get("source_type") or ""),
                    str(value.get("source_id") or ""),
                    source_revision,
                ),
            ).fetchone()
            if existing_source and str(existing_source["report_id"]) != report_id:
                return self.get_report(str(existing_source["report_id"])) or {}
            conn.execute(
                """INSERT INTO report_catalog_entries(
                   report_id,family_id,report_kind,subject_type,subject_key,symbol,security_name,
                   status,report_quality_status,coverage_status,generated_at,data_as_of,
                   source_type,source_id,source_revision,knowledge_link_json,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(report_id) DO UPDATE SET
                   status=excluded.status,report_quality_status=excluded.report_quality_status,
                   coverage_status=excluded.coverage_status,knowledge_link_json=excluded.knowledge_link_json,
                   updated_at=excluded.updated_at""",
                (
                    report_id,
                    str(value.get("family_id") or report_id),
                    str(value["report_kind"]),
                    str(value["subject_type"]),
                    str(value.get("subject_key") or value.get("symbol") or "portfolio:default"),
                    str(value.get("symbol") or "") or None,
                    str(value.get("security_name") or ""),
                    str(value["status"]),
                    str(value["report_quality_status"]),
                    str(value["coverage_status"]),
                    generated_at,
                    data_as_of,
                    str(value.get("source_type") or "unknown"),
                    str(value.get("source_id") or report_id),
                    source_revision,
                    json.dumps(knowledge_link, ensure_ascii=False),
                    str(value.get("created_at") or now),
                    now,
                ),
            )
            conn.execute("DELETE FROM report_viewpoints WHERE report_id=?", (report_id,))
            for item in viewpoints:
                horizon = str(item["horizon"])
                viewpoint_id = str(item.get("viewpoint_id") or _stable_id("viewpoint", report_id, horizon))
                conn.execute(
                    """INSERT INTO report_viewpoints(
                       viewpoint_id,report_id,horizon,stance,action,confidence,summary_claim_id,
                       reason_claim_ids_json,risk_claim_ids_json,condition_claim_ids_json,
                       invalidation_claim_ids_json,valid_from,valid_until,created_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        viewpoint_id,
                        report_id,
                        horizon,
                        str(item.get("stance") or "unknown"),
                        str(item.get("action") or "not_applicable"),
                        str(item.get("confidence") or "unknown"),
                        item.get("summary_claim_id"),
                        json.dumps(_strings(item.get("reason_claim_ids"))),
                        json.dumps(_strings(item.get("risk_claim_ids"))),
                        json.dumps(_strings(item.get("condition_claim_ids"))),
                        json.dumps(_strings(item.get("invalidation_claim_ids"))),
                        item.get("valid_from"),
                        item.get("valid_until"),
                        str(item.get("created_at") or now),
                    ),
                )
            conn.execute("DELETE FROM report_artifact_links WHERE report_id=?", (report_id,))
            for item in artifacts:
                conn.execute(
                    """INSERT INTO report_artifact_links(
                       report_id,artifact_id,artifact_role,filename,media_type,source_locator,
                       sha256,available,revision,materialization_status,materialization_error
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        report_id,
                        str(item.get("artifact_id") or "artifact"),
                        str(item.get("artifact_role") or "report"),
                        str(item.get("filename") or "artifact"),
                        str(item.get("media_type") or "application/octet-stream"),
                        str(item.get("source_locator") or ""),
                        item.get("sha256"),
                        1 if item.get("available", True) else 0,
                        int(item.get("revision") or source_revision),
                        item.get("materialization_status"),
                        item.get("materialization_error"),
                    ),
                )
            conn.execute("DELETE FROM report_relations WHERE from_report_id=?", (report_id,))
            for item in relations:
                target = str(item.get("to_report_id") or "")
                relation_type = str(item.get("relation_type") or "")
                if not target or relation_type not in {"revision_of", "supersedes"}:
                    continue
                relation_id = str(
                    item.get("relation_id")
                    or _stable_id("relation", report_id, target, relation_type, item.get("horizon"))
                )
                conn.execute(
                    """INSERT INTO report_relations(
                       relation_id,from_report_id,to_report_id,relation_type,horizon,created_at
                       ) VALUES (?,?,?,?,?,?)""",
                    (
                        relation_id,
                        report_id,
                        target,
                        relation_type,
                        item.get("horizon"),
                        str(item.get("created_at") or now),
                    ),
                )
        return self.get_report(report_id) or {}

    def register_deep_report(self, record: Any) -> dict[str, Any] | None:
        value = record.to_dict() if hasattr(record, "to_dict") else dict(record)
        if str(value.get("status") or "") != "completed":
            return None
        report_id = str(value.get("report_id") or "")
        revision = int(value.get("revision") or 1)
        quality = str(value.get("quality_status") or "failed_validation")
        knowledge_link = self._knowledge_link(report_id, revision)
        knowledge_link["internal_reference_code"] = _report_reference_code(value)
        report_date = str(value.get("report_date") or value.get("data_as_of") or "")[:10]
        knowledge_link["report_period"] = {
            "start_date": report_date or None,
            "end_date": report_date or None,
            "label": report_date or None,
        }
        knowledge_link["profile"] = value.get("profile")
        knowledge_link["instrument_type"] = (
            value.get("instrument_type")
            or ("etf" if value.get("profile") == "etf_deep_research" else "company_equity")
        )
        knowledge_link["subject_identity"] = {
            "security_name_source": value.get("security_name_source"),
            "security_name_aliases": list(value.get("security_name_aliases") or []),
            "identity_snapshot_id": value.get("identity_snapshot_id"),
        }
        if str(value.get("profile") or "") == "etf_deep_research":
            etf_readiness = dict(value.get("etf_readiness") or {})
            knowledge_link["etf_readiness"] = etf_readiness
            knowledge_link["pipeline_checks"] = dict(value.get("pipeline_checks") or {})
            knowledge_link["report_sections"] = dict(value.get("report_sections") or {})
            modules = dict(value.get("analysis_modules") or {})
            selection_module = dict(modules.get("holding_penetration") or {})
            component_module = dict(modules.get("component_research") or {})
            def effective_details(module: dict[str, Any]) -> dict[str, Any]:
                merged: dict[str, Any] = {}
                for result_key in ("deterministic_result", "narrative_result"):
                    result = dict(module.get(result_key) or {})
                    merged.update(dict(result.get("details") or {}))
                merged.update(dict(module.get("details") or {}))
                return merged

            selection_details = effective_details(selection_module)
            component_details = effective_details(component_module)
            def module_value(module: dict[str, Any], details: dict[str, Any], key: str) -> Any:
                return module.get(key) if module.get(key) is not None else details.get(key)
            knowledge_link["etf_penetration"] = {
                "selection_status": selection_module.get("status"),
                "selection_availability": selection_module.get("availability"),
                "selection_validation": selection_module.get("validation"),
                "component_research_status": component_module.get("status"),
                "component_research_availability": component_module.get("availability"),
                "component_research_validation": component_module.get("validation"),
                "selection_id": module_value(selection_module, selection_details, "selection_id"),
                "universe_snapshot_id": module_value(
                    selection_module, selection_details, "universe_snapshot_id"
                ),
                "selected_count": module_value(
                    selection_module, selection_details, "selected_count"
                ),
                "selected_weight_coverage": (
                    module_value(
                        selection_module, selection_details, "selected_weight_coverage"
                    )
                    if module_value(
                        selection_module, selection_details, "selected_weight_coverage"
                    ) is not None
                    else selection_module.get("coverage")
                ),
                "explanation_coverage": module_value(
                    selection_module, selection_details, "explanation_coverage"
                ),
                "research_coverage": module_value(
                    component_module, component_details, "research_coverage"
                ),
                "fully_supported_coverage": module_value(
                    component_module, component_details, "fully_supported_coverage"
                ),
                "reusable_count": module_value(
                    component_module, component_details, "reusable_count"
                ),
                "partial_reusable_count": module_value(
                    component_module, component_details, "partial_reusable_count"
                ),
                "missing_count": module_value(
                    component_module, component_details, "missing_count"
                ),
                "stale_count": module_value(
                    component_module, component_details, "stale_count"
                ),
                "conflicted_count": module_value(
                    component_module, component_details, "conflicted_count"
                ),
                "selected_components": (
                    selection_module.get("selected_components")
                    or selection_details.get("selected_components")
                    or []
                ),
            }
        support_by_claim = dict(knowledge_link.get("claim_support_by_claim") or {})
        claim_rows = self._claim_rows(
            knowledge_link.get("claim_ids") or [],
            support_by_claim,
        )
        reusable_rows = [item for item in claim_rows if item.get("reusable")]
        # Legacy reports remain discoverable, but their claims stay explicitly
        # unclassified until a backfill produces a frozen support audit.
        grouped_rows = reusable_rows if support_by_claim else claim_rows
        groups = self._claim_groups(grouped_rows)
        summary_row = next(
            (item for item in claim_rows if item.get("claim_id") == groups["summary"]),
            {},
        )
        stance, action = _infer_stance_action(str(summary_row.get("text") or ""))
        parent_id = str(value.get("parent_report_id") or "") or None
        parent = self.get_report(parent_id) if parent_id else None
        same_cutoff = bool(
            parent
            and _parse_time(parent.get("data_as_of")) == _parse_time(value.get("data_as_of"))
            and str(value.get("revision_mode") or "") != "full_refresh"
        )
        relations: list[ReportRelation] = []
        family_id = report_id
        if parent_id:
            relation_type = "revision_of" if same_cutoff else "supersedes"
            relations.append(
                ReportRelation(
                    relation_id=_stable_id("relation", report_id, parent_id, relation_type),
                    from_report_id=report_id,
                    to_report_id=parent_id,
                    relation_type=relation_type,  # type: ignore[arg-type]
                    horizon="structural",
                )
            )
            if parent:
                family_id = str(parent.get("family_id") or parent_id)
        artifacts = [
            self._artifact_link(
                dict(item),
                source_locator=f"deep-report:{report_id}:{item.get('artifact_id')}",
                revision=revision,
            )
            for item in value.get("artifacts") or []
            if isinstance(item, dict)
        ]
        raw_monitoring_artifact = next(
            (
                dict(item)
                for item in value.get("artifacts") or []
                if isinstance(item, dict) and item.get("artifact_id") == "monitoring_bundle"
            ),
            None,
        )
        if raw_monitoring_artifact is not None:
            try:
                monitoring_bundle = json.loads(
                    Path(str(raw_monitoring_artifact.get("path") or "")).read_text(
                        encoding="utf-8"
                    )
                )
            except (OSError, ValueError, json.JSONDecodeError):
                monitoring_bundle = None
            if isinstance(monitoring_bundle, dict):
                knowledge_link.update({
                    "monitoring_bundle_artifact_id": "monitoring_bundle",
                    "monitoring_bundle_source_locator": (
                        f"deep-report:{report_id}:monitoring_bundle"
                    ),
                    "monitoring_bundle_status": monitoring_bundle.get("monitoring_status"),
                    "monitoring_candidate_count": len(
                        monitoring_bundle.get("candidates") or []
                    ),
                    "monitoring_schema_version": monitoring_bundle.get("schema_version"),
                })
        viewpoint = ReportViewpoint(
            viewpoint_id=_stable_id("viewpoint", report_id, "structural"),
            report_id=report_id,
            horizon="structural",
            stance=stance,  # type: ignore[arg-type]
            action=action,  # type: ignore[arg-type]
            confidence="high" if quality == "passed" else "medium" if quality == "passed_with_gaps" else "low",
            summary_claim_id=groups["summary"],
            reason_claim_ids=groups["reasons"],
            risk_claim_ids=groups["risks"],
            condition_claim_ids=groups["conditions"],
            invalidation_claim_ids=groups["invalidations"],
            valid_from=_aware_iso(value.get("data_as_of"), fallback=value.get("updated_at")),
        )
        envelope = ReportEnvelope(
            report_id=report_id,
            family_id=family_id,
            report_kind="deep_research",
            subject_type="symbol",
            subject_key=str(value.get("symbol") or "").upper(),
            symbol=str(value.get("symbol") or "").upper() or None,
            security_name=str(value.get("security_name") or ""),
            status="diagnostic" if quality == "failed_validation" else "published",
            report_quality_status=quality,  # type: ignore[arg-type]
            coverage_status=(
                "insufficient"
                if quality == "failed_validation"
                or str(dict(value.get("etf_readiness") or {}).get("status") or "")
                == "not_publishable"
                else "complete"
                if str(dict(value.get("etf_readiness") or {}).get("status") or "")
                == "penetration_ready"
                or (
                    str(value.get("profile") or "") != "etf_deep_research"
                    and quality == "passed"
                )
                else "partial"
            ),
            generated_at=_aware_iso(value.get("created_at"), fallback=value.get("updated_at")),
            data_as_of=_aware_iso(value.get("data_as_of"), fallback=value.get("updated_at")),
            source_type="deep_report",
            source_id=report_id,
            source_revision=revision,
            knowledge_link=knowledge_link,
            viewpoints=[viewpoint],
            artifacts=artifacts,
            relations=relations,
        )
        return self.register_report(envelope)

    def _latest_report_id(self, subject_key: str, horizon: str) -> str | None:
        with self.knowledge.connect() as conn:
            row = conn.execute(
                """SELECT e.report_id FROM report_catalog_entries e
                   JOIN report_viewpoints v ON v.report_id=e.report_id
                   WHERE e.subject_key=? AND v.horizon=? AND e.status='published'
                   ORDER BY e.data_as_of DESC,e.generated_at DESC,e.source_revision DESC LIMIT 1""",
                (subject_key, horizon),
            ).fetchone()
        return str(row["report_id"]) if row else None

    def _link_generated_claims(
        self,
        *,
        report_id: str,
        revision: int,
        symbol: str,
        quality_status: str,
        claims: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.knowledge.link_report(
            report_id=report_id,
            revision=revision,
            symbol=symbol,
            quality_status=quality_status,
            evidence=[],
            facts=[],
            claims=claims,
            coverage_snapshot_id=None,
            base_report_id=None,
        )
        return self._knowledge_link(report_id, revision)

    def register_daily_run(
        self,
        record: dict[str, Any],
        aggregate: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if str(record.get("status") or "") not in {"completed", "completed_with_warnings"}:
            return []
        run_id = str(record.get("run_id") or "")
        revision = int(record.get("artifact_revision") or record.get("revision") or 1)
        generated_at = _aware_iso(record.get("completed_at"), fallback=record.get("created_at"))
        target_report_date = str(record.get("market_date") or "")[:10]
        warnings = list(record.get("warnings") or [])
        quality = "passed_with_gaps" if warnings else "passed"
        coverage = "partial" if warnings else "complete"
        artifacts = [dict(item) for item in record.get("artifacts") or [] if isinstance(item, dict)]
        results: list[dict[str, Any]] = []
        for raw_brief in aggregate.get("briefs") or []:
            brief = dict(raw_brief)
            symbol = str(brief.get("symbol") or "").upper()
            if not symbol:
                continue
            data_as_of = _aware_iso(brief.get("data_as_of"), fallback=generated_at)
            report_id = _stable_id("daily", run_id, symbol, revision)
            claims: list[dict[str, Any]] = []
            summary = _claim(report_id, "daily_summary", 0, brief.get("summary"))
            if summary:
                claims.append(summary)
            for section, values in (
                ("daily_reason", brief.get("reasons") or []),
                ("daily_risk", brief.get("risks") or []),
                ("daily_condition", brief.get("watch_points") or []),
            ):
                for index, text in enumerate(values):
                    item = _claim(report_id, section, index, text, claim_type="inference")
                    if item:
                        claims.append(item)
            for index, condition in enumerate(brief.get("condition_orders") or []):
                item = _claim(
                    report_id,
                    "daily_condition",
                    100 + index,
                    f"{condition.get('trigger') or ''} → {condition.get('response') or ''}",
                )
                if item:
                    claims.append(item)
                invalidation = _claim(
                    report_id,
                    "daily_invalidation",
                    index,
                    condition.get("invalidation"),
                )
                if invalidation:
                    claims.append(invalidation)
            for raw_claim in brief.get("monitoring_claims") or []:
                if not isinstance(raw_claim, dict):
                    continue
                claim_id = str(raw_claim.get("claim_id") or "").strip()
                section_id = str(raw_claim.get("section_id") or "").strip()
                text = str(raw_claim.get("text") or "").strip()
                if not claim_id or not section_id or not text:
                    continue
                claims.append(
                    {
                        "claim_id": claim_id,
                        "section_id": section_id,
                        "claim_type": str(raw_claim.get("claim_type") or "inference"),
                        "text": text[:4000],
                        "fact_ids": _strings(raw_claim.get("fact_ids")),
                        "evidence_ids": _strings(raw_claim.get("evidence_ids")),
                    }
                )
            knowledge_link = self._link_generated_claims(
                report_id=report_id,
                revision=revision,
                symbol=symbol,
                quality_status=quality,
                claims=claims,
            )
            knowledge_link["report_period"] = {
                "start_date": target_report_date or data_as_of[:10],
                "end_date": target_report_date or data_as_of[:10],
                "label": target_report_date or data_as_of[:10],
            }
            monitoring_bundle = brief.get("monitoring_bundle")
            monitoring_artifact = next(
                (
                    item
                    for item in artifacts
                    if str(item.get("symbol") or "").upper() == symbol
                    and str(item.get("kind") or "") == "holding_daily_json"
                ),
                None,
            )
            if isinstance(monitoring_bundle, dict) and isinstance(monitoring_artifact, dict):
                knowledge_link.update(
                    monitoring_bundle_artifact_id=monitoring_artifact.get("artifact_id"),
                    monitoring_bundle_source_locator=(
                        f"daily-run:{run_id}:{monitoring_artifact.get('artifact_id')}"
                    ),
                    monitoring_bundle_status=monitoring_bundle.get("monitoring_status"),
                    monitoring_candidate_count=len(monitoring_bundle.get("candidates") or []),
                    monitoring_schema_version=monitoring_bundle.get("schema_version"),
                )
            groups = self._claim_groups(self._claim_rows(knowledge_link.get("claim_ids") or []))
            stance, action = _infer_stance_action(
                str(brief.get("summary") or ""),
                str(brief.get("action") or "observe"),
            )
            previous = self._latest_report_id(symbol, "daily")
            relations = (
                [
                    ReportRelation(
                        relation_id=_stable_id("relation", report_id, previous, "supersedes", "daily"),
                        from_report_id=report_id,
                        to_report_id=previous,
                        relation_type="supersedes",
                        horizon="daily",
                    )
                ]
                if previous and previous != report_id
                else []
            )
            linked_artifacts = [
                self._artifact_link(
                    item,
                    source_locator=f"daily-run:{run_id}:{item.get('artifact_id')}",
                    revision=revision,
                )
                for item in artifacts
                if str(item.get("symbol") or "").upper() == symbol
                and str(item.get("kind") or "").startswith("holding_daily_")
            ]
            security_name = next(
                (
                    str(item.get("security_name") or "")
                    for item in artifacts
                    if str(item.get("symbol") or "").upper() == symbol
                    and item.get("security_name")
                ),
                symbol,
            )
            envelope = ReportEnvelope(
                report_id=report_id,
                family_id=report_id,
                report_kind="daily_holding",
                subject_type="symbol",
                subject_key=symbol,
                symbol=symbol,
                security_name=security_name,
                status="published",
                report_quality_status=quality,  # type: ignore[arg-type]
                coverage_status=coverage,  # type: ignore[arg-type]
                generated_at=generated_at,
                data_as_of=data_as_of,
                source_type="daily_run_holding",
                source_id=f"{run_id}:{symbol}",
                source_revision=revision,
                knowledge_link=knowledge_link,
                viewpoints=[
                    ReportViewpoint(
                        viewpoint_id=_stable_id("viewpoint", report_id, "daily"),
                        report_id=report_id,
                        horizon="daily",
                        stance=stance,  # type: ignore[arg-type]
                        action=action,  # type: ignore[arg-type]
                        confidence=str(brief.get("confidence") or "unknown"),  # type: ignore[arg-type]
                        summary_claim_id=groups["summary"],
                        reason_claim_ids=groups["reasons"],
                        risk_claim_ids=groups["risks"],
                        condition_claim_ids=groups["conditions"],
                        invalidation_claim_ids=groups["invalidations"],
                        valid_from=data_as_of,
                        valid_until=_next_business_day_end(data_as_of),
                    )
                ],
                artifacts=linked_artifacts,
                relations=relations,
            )
            results.append(self.register_report(envelope))

        master_id = _stable_id("portfolio_report", run_id, revision)
        brief_cutoffs = [
            _parse_time(item.get("data_as_of"))
            for item in aggregate.get("briefs") or []
            if isinstance(item, dict) and item.get("data_as_of")
        ]
        master_cutoff = max((item for item in brief_cutoffs if item is not None), default=None)
        master_data_as_of = (master_cutoff or _parse_time(generated_at) or datetime.now(timezone.utc)).isoformat()
        master_claims: list[dict[str, Any]] = []
        counts = dict(aggregate.get("counts") or {})
        master_summary = _claim(
            master_id,
            "portfolio_summary",
            0,
            "组合晨会结论：" + "、".join(f"{key} {value}" for key, value in sorted(counts.items())),
        )
        if master_summary:
            master_claims.append(master_summary)
        for index, warning in enumerate(aggregate.get("warnings") or []):
            item = _claim(master_id, "portfolio_risk", index, warning)
            if item:
                master_claims.append(item)
        master_link = self._link_generated_claims(
            report_id=master_id,
            revision=revision,
            symbol="",
            quality_status=quality,
            claims=master_claims,
        )
        master_link["report_period"] = {
            "start_date": target_report_date or master_data_as_of[:10],
            "end_date": target_report_date or master_data_as_of[:10],
            "label": target_report_date or master_data_as_of[:10],
        }
        master_groups = self._claim_groups(self._claim_rows(master_link.get("claim_ids") or []))
        previous_master = self._latest_report_id("portfolio:default", "daily")
        master_relations = (
            [
                ReportRelation(
                    relation_id=_stable_id("relation", master_id, previous_master, "supersedes", "daily"),
                    from_report_id=master_id,
                    to_report_id=previous_master,
                    relation_type="supersedes",
                    horizon="daily",
                )
            ]
            if previous_master and previous_master != master_id
            else []
        )
        master_artifacts = [
            self._artifact_link(
                item,
                source_locator=f"daily-run:{run_id}:{item.get('artifact_id')}",
                revision=revision,
            )
            for item in artifacts
            if str(item.get("kind") or "") in {"master_pdf", "master_markdown", "portfolio_decision_json"}
        ]
        results.append(
            self.register_report(
                ReportEnvelope(
                    report_id=master_id,
                    family_id=master_id,
                    report_kind="daily_portfolio",
                    subject_type="portfolio",
                    subject_key="portfolio:default",
                    security_name="组合晨会",
                    status="published",
                    report_quality_status=quality,  # type: ignore[arg-type]
                    coverage_status=coverage,  # type: ignore[arg-type]
                    generated_at=generated_at,
                    data_as_of=master_data_as_of,
                    source_type="daily_run_master",
                    source_id=run_id,
                    source_revision=revision,
                    knowledge_link=master_link,
                    viewpoints=[
                        ReportViewpoint(
                            viewpoint_id=_stable_id("viewpoint", master_id, "daily"),
                            report_id=master_id,
                            horizon="daily",
                            stance="mixed",
                            action="not_applicable",
                            confidence="medium" if warnings else "high",
                            summary_claim_id=master_groups["summary"],
                            risk_claim_ids=master_groups["risks"],
                            valid_from=master_data_as_of,
                            valid_until=_next_business_day_end(master_data_as_of),
                        )
                    ],
                    artifacts=master_artifacts,
                    relations=master_relations,
                )
            )
        )
        return results

    def register_weekly_run(
        self,
        record: dict[str, Any],
        brief: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Idempotently publish one formal weekly JSON as the weekly viewpoint."""

        if str(record.get("status") or "") not in {"completed", "completed_with_warnings"}:
            return None
        quality = str(brief.get("quality_status") or "failed_validation")
        if quality == "failed_validation":
            return None
        symbol = str(brief.get("symbol") or record.get("symbol") or "").upper()
        if not symbol:
            raise ValueError("weekly report symbol is required")
        run_id = str(record.get("run_id") or brief.get("run_id") or "")
        revision = int(record.get("revision") or brief.get("revision") or 1)
        report_id = str(brief.get("report_id") or _stable_id("weekly", run_id, symbol, revision))
        claims = [
            dict(item)
            for item in brief.get("monitoring_claims") or []
            if isinstance(item, dict)
            and item.get("claim_id")
            and item.get("section_id")
            and item.get("text")
        ]
        knowledge_link = self._link_generated_claims(
            report_id=report_id,
            revision=revision,
            symbol=symbol,
            quality_status=quality,
            claims=claims,
        )
        week_end = str(record.get("week_end") or brief.get("week_end") or brief.get("data_as_of") or "")[:10]
        week_start = str(brief.get("week_start") or "")[:10] or None
        knowledge_link["report_period"] = {
            "start_date": week_start or week_end or None,
            "end_date": week_end or None,
            "label": f"{week_start} 至 {week_end}" if week_start and week_end else week_end or None,
        }
        artifacts = [
            dict(item) for item in record.get("artifacts") or [] if isinstance(item, dict)
        ]
        json_artifact = next(
            (item for item in artifacts if item.get("kind") == "weekly_review_json"),
            None,
        )
        bundle = brief.get("monitoring_bundle")
        if isinstance(bundle, dict) and isinstance(json_artifact, dict):
            knowledge_link.update(
                monitoring_bundle_artifact_id=json_artifact.get("artifact_id"),
                monitoring_bundle_source_locator=(
                    f"weekly-run:{run_id}:{json_artifact.get('artifact_id')}"
                ),
                monitoring_bundle_status=bundle.get("monitoring_status"),
                monitoring_candidate_count=len(bundle.get("candidates") or []),
                monitoring_schema_version=bundle.get("schema_version"),
                weekly_review_due_at=brief.get("review_due_at"),
                weekly_source_valid_until=brief.get("source_valid_until"),
                weekly_previous_validation_count=len(
                    brief.get("previous_week_validation") or []
                ),
                weekly_scenario_change_count=len(brief.get("scenario_changes") or []),
            )
        groups = self._claim_groups(self._claim_rows(knowledge_link.get("claim_ids") or []))
        direction = str((brief.get("weekly_view") or {}).get("trend_direction") or "")
        stance = "bullish" if direction == "向上" else "bearish" if direction == "向下" else "neutral"
        previous = self._latest_report_id(symbol, "weekly")
        relations = (
            [
                ReportRelation(
                    relation_id=_stable_id(
                        "relation", report_id, previous, "supersedes", "weekly"
                    ),
                    from_report_id=report_id,
                    to_report_id=previous,
                    relation_type="supersedes",
                    horizon="weekly",
                )
            ]
            if previous and previous != report_id
            else []
        )
        linked_artifacts = [
            self._artifact_link(
                item,
                source_locator=f"weekly-run:{run_id}:{item.get('artifact_id')}",
                revision=revision,
            )
            for item in artifacts
            if str(item.get("kind") or "").startswith("weekly_review_")
        ]
        return self.register_report(
            ReportEnvelope(
                report_id=report_id,
                family_id=report_id,
                report_kind="weekly_review",
                subject_type="symbol",
                subject_key=symbol,
                symbol=symbol,
                security_name=str(brief.get("security_name") or symbol),
                status="published",
                report_quality_status=quality,  # type: ignore[arg-type]
                coverage_status=str(brief.get("coverage_status") or "partial"),  # type: ignore[arg-type]
                generated_at=_aware_iso(brief.get("generated_at")),
                data_as_of=_aware_iso(brief.get("data_as_of")),
                source_type="weekly_run",
                source_id=f"{run_id}:{symbol}",
                source_revision=revision,
                knowledge_link=knowledge_link,
                viewpoints=[
                    ReportViewpoint(
                        viewpoint_id=_stable_id("viewpoint", report_id, "weekly"),
                        report_id=report_id,
                        horizon="weekly",
                        stance=stance,  # type: ignore[arg-type]
                        action="observe",
                        confidence=str(brief.get("confidence") or "medium"),  # type: ignore[arg-type]
                        summary_claim_id=brief.get("summary_claim_id") or groups["summary"],
                        reason_claim_ids=_strings(brief.get("reason_claim_ids")) or groups["reasons"],
                        risk_claim_ids=_strings(brief.get("risk_claim_ids")) or groups["risks"],
                        condition_claim_ids=groups["conditions"],
                        invalidation_claim_ids=groups["invalidations"],
                        valid_from=str(brief.get("valid_from") or "") or None,
                        valid_until=str(brief.get("valid_until") or "") or None,
                    )
                ],
                artifacts=linked_artifacts,
                relations=relations,
            )
        )

    def register_monitor_research(
        self,
        candidate: dict[str, Any],
        *,
        snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        symbol = str(candidate.get("symbol") or "").upper()
        report_ref = str(candidate.get("report_ref") or "")
        revision = int(candidate.get("revision") or 1)
        report_id = _stable_id("monitor_report", report_ref, revision)
        summary_text = _markdown_summary(str(candidate.get("body") or "")) or str(
            candidate.get("title") or "监控研究"
        )
        claims = [
            item
            for item in [_claim(report_id, "monitor_summary", 0, summary_text)]
            if item
        ]
        knowledge_link = self._link_generated_claims(
            report_id=report_id,
            revision=revision,
            symbol=symbol,
            quality_status="passed",
            claims=claims,
        )
        groups = self._claim_groups(self._claim_rows(knowledge_link.get("claim_ids") or []))
        stance, action = _infer_stance_action(summary_text)
        previous = self._latest_report_id(symbol, "structural")
        relations = (
            [
                ReportRelation(
                    relation_id=_stable_id("relation", report_id, previous, "supersedes", "structural"),
                    from_report_id=report_id,
                    to_report_id=previous,
                    relation_type="supersedes",
                    horizon="structural",
                )
            ]
            if previous and previous != report_id
            else []
        )
        body_hash = hashlib.sha256(str(candidate.get("body") or "").encode("utf-8")).hexdigest()
        artifact = ReportArtifactLink(
            artifact_id="snapshot",
            artifact_role="monitor_research_snapshot",
            filename=f"{report_id}.md",
            media_type="text/markdown",
            source_locator=f"monitor-snapshot:{snapshot_id or report_ref}",
            sha256=body_hash,
            available=False,
            revision=revision,
        )
        return self.register_report(
            ReportEnvelope(
                report_id=report_id,
                family_id=report_id,
                report_kind="monitor_research",
                subject_type="symbol",
                subject_key=symbol,
                symbol=symbol,
                security_name=str(candidate.get("title") or symbol),
                status="published",
                report_quality_status="passed",
                coverage_status="complete",
                generated_at=_aware_iso(candidate.get("generated_at")),
                data_as_of=_aware_iso(candidate.get("data_as_of"), fallback=candidate.get("generated_at")),
                source_type="monitor_research",
                source_id=report_ref,
                source_revision=revision,
                knowledge_link=knowledge_link,
                viewpoints=[
                    ReportViewpoint(
                        viewpoint_id=_stable_id("viewpoint", report_id, "structural"),
                        report_id=report_id,
                        horizon="structural",
                        stance=stance,  # type: ignore[arg-type]
                        action=action,  # type: ignore[arg-type]
                        confidence="medium",
                        summary_claim_id=groups["summary"],
                        valid_from=_aware_iso(candidate.get("data_as_of"), fallback=candidate.get("generated_at")),
                    )
                ],
                artifacts=[artifact],
                relations=relations,
            )
        )

    @staticmethod
    def _decode_viewpoint(row: Any) -> dict[str, Any]:
        value = dict(row)
        for key in (
            "reason_claim_ids_json",
            "risk_claim_ids_json",
            "condition_claim_ids_json",
            "invalidation_claim_ids_json",
        ):
            value[key.removesuffix("_json")] = _loads(value.pop(key), [])
        return value

    @staticmethod
    def _decode_artifact(row: Any) -> dict[str, Any]:
        value = dict(row)
        value["available"] = bool(value.get("available"))
        return value

    def get_report(self, report_id: str | None) -> dict[str, Any] | None:
        if not report_id:
            return None
        with self.knowledge.connect() as conn:
            row = conn.execute(
                "SELECT * FROM report_catalog_entries WHERE report_id=?", (report_id,)
            ).fetchone()
            if not row:
                return None
            viewpoints = conn.execute(
                "SELECT * FROM report_viewpoints WHERE report_id=? ORDER BY horizon", (report_id,)
            ).fetchall()
            artifacts = conn.execute(
                "SELECT * FROM report_artifact_links WHERE report_id=? ORDER BY artifact_id", (report_id,)
            ).fetchall()
            relations = conn.execute(
                "SELECT * FROM report_relations WHERE from_report_id=? ORDER BY created_at", (report_id,)
            ).fetchall()
        value = dict(row)
        value["knowledge_link"] = _loads(value.pop("knowledge_link_json"), {})
        value["report_period"] = _report_period(
            value["knowledge_link"],
            data_as_of=str(value.get("data_as_of") or ""),
        )
        value["viewpoints"] = [self._decode_viewpoint(item) for item in viewpoints]
        value["artifacts"] = [self._decode_artifact(item) for item in artifacts]
        value["relations"] = [dict(item) for item in relations]
        return value

    def get_report_by_reference_code(self, reference_code: str) -> dict[str, Any] | None:
        normalized = str(reference_code or "").strip().upper()
        if not re.fullmatch(
            r"VT-(?:EQ|ETF|IDX|CR|DY|PF|WK|MR|DR|IR)-[A-Z0-9]+-"
            r"(?:\d{8}|UNDATED)-R\d{2}-[A-F0-9]{6}",
            normalized,
        ):
            return None
        with self.knowledge.connect() as conn:
            rows = conn.execute(
                """SELECT report_id,report_kind,subject_key,symbol,generated_at,
                          source_revision,knowledge_link_json
                   FROM report_catalog_entries
                   WHERE knowledge_link_json LIKE ? ORDER BY generated_at DESC LIMIT 10""",
                (f"%{normalized}%",),
            ).fetchall()
        for row in rows:
            link = _loads(row["knowledge_link_json"], {})
            if str(link.get("internal_reference_code") or "").upper() == normalized:
                return self.get_report(str(row["report_id"]))
        # Historical schema-v2 rows may predate the stored reference code.  The
        # code is deterministic, so resolve those rows without mutating history.
        with self.knowledge.connect() as conn:
            legacy_rows = conn.execute(
                """SELECT report_id,report_kind,subject_key,symbol,generated_at,
                          source_revision,knowledge_link_json
                   FROM report_catalog_entries ORDER BY generated_at DESC"""
            ).fetchall()
        for row in legacy_rows:
            value = dict(row)
            if _report_reference_code(value).upper() == normalized:
                return self.get_report(str(row["report_id"]))
        return None

    @staticmethod
    def _encode_cursor(generated_at: str, report_id: str) -> str:
        raw = json.dumps([generated_at, report_id], separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[str, str]:
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
            if not isinstance(value, list) or len(value) != 2:
                raise ValueError
            return str(value[0]), str(value[1])
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid report cursor") from exc

    def list_reports(
        self,
        *,
        query: str = "",
        subject_type: str | None = None,
        report_kind: str | None = None,
        horizon: str | None = None,
        status: str | None = None,
        quality: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        clauses = ["1=1"]
        params: list[Any] = []
        join = ""
        if horizon:
            if horizon not in _HORIZONS:
                raise ValueError("unsupported horizon")
            join = " JOIN report_viewpoints v ON v.report_id=e.report_id "
            clauses.append("v.horizon=?")
            params.append(horizon)
        if query.strip():
            needle = f"%{query.strip()}%"
            clauses.append("(e.symbol LIKE ? OR e.security_name LIKE ? OR e.subject_key LIKE ?)")
            params.extend([needle, needle, needle])
        if subject_type:
            clauses.append("e.subject_type=?")
            params.append(subject_type)
        if report_kind:
            clauses.append("e.report_kind=?")
            params.append(report_kind)
        if status:
            clauses.append("e.status=?")
            params.append(status)
        if quality:
            clauses.append("e.report_quality_status=?")
            params.append(quality)
        if start_at:
            clauses.append("e.generated_at>=?")
            params.append(_aware_iso(start_at))
        if end_at:
            clauses.append("e.generated_at<=?")
            params.append(_aware_iso(end_at))
        count_sql = (
            "SELECT COUNT(DISTINCT e.report_id) AS total_count FROM report_catalog_entries e"
            + join
            + " WHERE "
            + " AND ".join(clauses)
        )
        with self.knowledge.connect() as conn:
            count_row = conn.execute(count_sql, params).fetchone()
        total_count = int(count_row["total_count"] or 0) if count_row else 0
        if cursor:
            generated_at, report_id = self._decode_cursor(cursor)
            clauses.append("(e.generated_at<? OR (e.generated_at=? AND e.report_id<?))")
            params.extend([generated_at, generated_at, report_id])
        bounded = max(1, min(int(limit), 100))
        sql = (
            "SELECT DISTINCT e.report_id,e.generated_at FROM report_catalog_entries e"
            + join
            + " WHERE "
            + " AND ".join(clauses)
            + " ORDER BY e.generated_at DESC,e.report_id DESC LIMIT ?"
        )
        params.append(bounded + 1)
        with self.knowledge.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        selected = rows[:bounded]
        reports = [
            report
            for report in (self.get_report(str(row["report_id"])) for row in selected)
            if report is not None
        ]
        next_cursor = None
        if len(rows) > bounded and selected:
            last = selected[-1]
            next_cursor = self._encode_cursor(str(last["generated_at"]), str(last["report_id"]))
        return {
            "reports": reports,
            "next_cursor": next_cursor,
            "total_count": total_count,
        }

    @staticmethod
    def _encode_subject_cursor(generated_at: str, subject_key: str) -> str:
        raw = json.dumps([generated_at, subject_key], separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_subject_cursor(cursor: str) -> tuple[str, str]:
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
            if not isinstance(value, list) or len(value) != 2:
                raise ValueError
            return str(value[0]), str(value[1])
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid subject cursor") from exc

    def _report_summary_text(self, report: dict[str, Any]) -> str:
        for horizon in ("structural", "weekly", "daily", "intraday"):
            viewpoint = next(
                (item for item in report.get("viewpoints") or [] if item.get("horizon") == horizon),
                None,
            )
            if not viewpoint:
                continue
            claim = self._claim_excerpt(viewpoint.get("summary_claim_id"))
            if claim and claim.get("text"):
                return str(claim["text"])[:240]
        return ""

    def list_subjects(
        self,
        *,
        query: str = "",
        report_kind: str | None = None,
        quality: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
        limit: int = 30,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return one compact, accurately counted row per symbol dossier."""

        clauses = ["e.subject_type='symbol'"]
        params: list[Any] = []
        if query.strip():
            needle = f"%{query.strip()}%"
            clauses.append("(e.symbol LIKE ? OR e.security_name LIKE ? OR e.subject_key LIKE ?)")
            params.extend([needle, needle, needle])
        if report_kind:
            clauses.append("e.report_kind=?")
            params.append(report_kind)
        if quality:
            clauses.append("e.report_quality_status=?")
            params.append(quality)
        if start_at:
            clauses.append("e.generated_at>=?")
            params.append(_aware_iso(start_at))
        if end_at:
            clauses.append("e.generated_at<=?")
            params.append(_aware_iso(end_at))
        where = " AND ".join(clauses)
        with self.knowledge.connect() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(DISTINCT e.subject_key) AS total_count FROM report_catalog_entries e WHERE {where}",
                params,
            ).fetchone()
        page_clauses: list[str] = []
        page_params: list[Any] = []
        if cursor:
            latest_at, subject_key = self._decode_subject_cursor(cursor)
            page_clauses.append("(g.latest_generated_at<? OR (g.latest_generated_at=? AND g.subject_key>?))")
            page_params.extend([latest_at, latest_at, subject_key])
        bounded = max(1, min(int(limit), 100))
        cutoff = (self.now_provider().astimezone(timezone.utc) - timedelta(hours=24)).isoformat()
        sql = f"""
            WITH filtered AS (
                SELECT e.* FROM report_catalog_entries e WHERE {where}
            ), grouped AS (
                SELECT subject_key,
                       MAX(COALESCE(symbol,'')) AS symbol,
                       MAX(CASE WHEN security_name<>'' THEN security_name ELSE COALESCE(symbol,subject_key) END) AS security_name,
                       COUNT(*) AS report_count,
                       SUM(CASE WHEN generated_at>=? THEN 1 ELSE 0 END) AS new_report_count,
                       MAX(generated_at) AS latest_generated_at,
                       GROUP_CONCAT(DISTINCT report_kind) AS report_kinds_csv,
                       SUM(CASE WHEN report_quality_status='passed' THEN 1 ELSE 0 END) AS passed_count,
                       SUM(CASE WHEN coverage_status='complete' THEN 1 ELSE 0 END) AS complete_count
                FROM filtered GROUP BY subject_key
            )
            SELECT g.*,
                   (SELECT report_id FROM filtered f WHERE f.subject_key=g.subject_key
                    ORDER BY generated_at DESC,report_id DESC LIMIT 1) AS latest_report_id,
                   (SELECT data_as_of FROM filtered f WHERE f.subject_key=g.subject_key
                    ORDER BY generated_at DESC,report_id DESC LIMIT 1) AS latest_data_as_of,
                   (SELECT COUNT(*) FROM research_note_subjects n
                    WHERE n.subject_key=COALESCE(NULLIF(g.symbol,''),g.subject_key)) AS research_note_count,
                   (SELECT COUNT(DISTINCT n.note_claim_id) FROM research_note_subjects n
                    JOIN research_note_resolutions r ON r.note_claim_id=n.note_claim_id
                    WHERE n.subject_key=COALESCE(NULLIF(g.symbol,''),g.subject_key)
                      AND r.resolution_status='confirmed') AS confirmed_note_count,
                   (SELECT COUNT(DISTINCT o.document_ref) FROM source_observations o
                    WHERE o.subject_key=COALESCE(NULLIF(g.symbol,''),g.subject_key)
                      AND o.source_kind='broker_research') AS broker_research_count
            FROM grouped g
            {('WHERE ' + ' AND '.join(page_clauses)) if page_clauses else ''}
            ORDER BY g.latest_generated_at DESC,g.subject_key ASC LIMIT ?
        """
        query_params = [*params, cutoff, *page_params, bounded + 1]
        with self.knowledge.connect() as conn:
            rows = conn.execute(sql, query_params).fetchall()
        selected = rows[:bounded]
        subjects: list[dict[str, Any]] = []
        for row in selected:
            item = dict(row)
            latest = self.get_report(str(item.pop("latest_report_id") or ""))
            item["report_kinds"] = sorted(
                kind for kind in str(item.pop("report_kinds_csv") or "").split(",") if kind
            )
            if latest:
                item["current_viewpoint_summary"] = self._report_summary_text(latest)
                # Subject-directory responses must stay compact.  The complete report,
                # including its knowledge graph, artifacts and relations, is available
                # from the subject-report endpoint after the user expands the group.
                item["latest_report"] = {
                    key: latest.get(key)
                    for key in (
                        "report_id",
                        "report_kind",
                        "subject_key",
                        "symbol",
                        "security_name",
                        "status",
                        "report_quality_status",
                        "coverage_status",
                        "generated_at",
                        "data_as_of",
                        "report_period",
                    )
                }
            else:
                item["latest_report"] = None
                item["current_viewpoint_summary"] = ""
            item["quality_summary"] = {
                "passed": int(item.pop("passed_count") or 0),
                "complete": int(item.pop("complete_count") or 0),
            }
            subjects.append(item)
        next_cursor = None
        if len(rows) > bounded and selected:
            last = selected[-1]
            next_cursor = self._encode_subject_cursor(
                str(last["latest_generated_at"]),
                str(last["subject_key"]),
            )
        return {
            "subjects": subjects,
            "next_cursor": next_cursor,
            "total_count": int(total_row["total_count"] or 0) if total_row else 0,
        }

    def list_subject_reports(
        self,
        subject_key: str,
        *,
        limit: int = 10,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        clauses = ["e.subject_key=?"]
        params: list[Any] = [subject_key]
        with self.knowledge.connect() as conn:
            count_row = conn.execute(
                "SELECT COUNT(*) AS total_count FROM report_catalog_entries e WHERE e.subject_key=?",
                (subject_key,),
            ).fetchone()
        if cursor:
            generated_at, report_id = self._decode_cursor(cursor)
            clauses.append("(e.generated_at<? OR (e.generated_at=? AND e.report_id<?))")
            params.extend([generated_at, generated_at, report_id])
        bounded = max(1, min(int(limit), 100))
        params.append(bounded + 1)
        with self.knowledge.connect() as conn:
            rows = conn.execute(
                "SELECT e.report_id,e.generated_at FROM report_catalog_entries e WHERE "
                + " AND ".join(clauses)
                + " ORDER BY e.generated_at DESC,e.report_id DESC LIMIT ?",
                params,
            ).fetchall()
        selected = rows[:bounded]
        reports = [
            report
            for report in (self.get_report(str(row["report_id"])) for row in selected)
            if report is not None
        ]
        next_cursor = None
        if len(rows) > bounded and selected:
            last = selected[-1]
            next_cursor = self._encode_cursor(str(last["generated_at"]), str(last["report_id"]))
        return {
            "reports": reports,
            "next_cursor": next_cursor,
            "total_count": int(count_row["total_count"] or 0) if count_row else 0,
        }

    def _candidate_payload(self, report: dict[str, Any], viewpoint: dict[str, Any]) -> dict[str, Any]:
        support_by_claim = dict(
            (report.get("knowledge_link") or {}).get("claim_support_by_claim") or {}
        )
        summary = self._claim_excerpt(
            viewpoint.get("summary_claim_id"),
            support_by_claim=support_by_claim,
            data_as_of=report.get("data_as_of"),
            valid_until=viewpoint.get("valid_until"),
        )
        risks = [
            item
            for item in (
                self._claim_excerpt(
                    claim_id,
                    support_by_claim=support_by_claim,
                    data_as_of=report.get("data_as_of"),
                    valid_until=viewpoint.get("valid_until"),
                )
                for claim_id in viewpoint.get("risk_claim_ids") or []
            )
            if item
        ][:3]
        pending = [
            item
            for item in (
                self._claim_excerpt(
                    claim_id,
                    support_by_claim=support_by_claim,
                    data_as_of=report.get("data_as_of"),
                    valid_until=viewpoint.get("valid_until"),
                )
                for claim_id in [
                    *(viewpoint.get("condition_claim_ids") or []),
                    *(viewpoint.get("invalidation_claim_ids") or []),
                ]
            )
            if item
        ][:3]
        return {
            "report_id": report["report_id"],
            "report_kind": report["report_kind"],
            "symbol": report.get("symbol"),
            "security_name": report.get("security_name"),
            "data_as_of": report["data_as_of"],
            "generated_at": report["generated_at"],
            "report_quality_status": report["report_quality_status"],
            "coverage_status": report["coverage_status"],
            "viewpoint": viewpoint,
            "summary": summary,
            "risks": risks,
            "pending_items": pending,
        }

    def subject(
        self,
        subject_key: str,
        *,
        limit: int = 100,
        include_timeline: bool = True,
        history_mode: str = "current_families",
    ) -> dict[str, Any]:
        if history_mode not in {"current_families", "full"}:
            raise ValueError("history_mode must be current_families or full")
        with self.knowledge.connect() as conn:
            count_row = conn.execute(
                "SELECT COUNT(*) AS report_count,MAX(generated_at) AS latest_generated_at "
                "FROM report_catalog_entries WHERE subject_key=?",
                (subject_key,),
            ).fetchone()
            ids = conn.execute(
                """SELECT report_id FROM report_catalog_entries WHERE subject_key=?
                   ORDER BY data_as_of DESC,generated_at DESC,source_revision DESC LIMIT ?""",
                (subject_key, max(1, min(limit, 200))),
            ).fetchall()
        reports = [
            item
            for item in (self.get_report(str(row["report_id"])) for row in ids)
            if item is not None
        ]
        now = self.now_provider().astimezone(timezone.utc)
        current: dict[str, Any] = {}
        for horizon in _HORIZONS:
            candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for report in reports:
                if report["status"] != "published" or report["report_quality_status"] == "failed_validation":
                    continue
                viewpoint = next(
                    (item for item in report["viewpoints"] if item["horizon"] == horizon),
                    None,
                )
                if not viewpoint:
                    continue
                expires = _parse_time(viewpoint.get("valid_until"))
                if expires is not None and expires < now:
                    continue
                candidates.append((report, viewpoint))
            candidates.sort(
                key=lambda item: (
                    _parse_time(item[0]["data_as_of"]) or datetime.min.replace(tzinfo=timezone.utc),
                    _parse_time(item[0]["generated_at"]) or datetime.min.replace(tzinfo=timezone.utc),
                    int(item[0].get("source_revision") or 1),
                ),
                reverse=True,
            )
            latest = candidates[0] if candidates else None
            complete = next(
                (
                    item
                    for item in candidates
                    if item[0]["report_quality_status"] == "passed"
                    and item[0]["coverage_status"] == "complete"
                ),
                None,
            )
            current[horizon] = {
                "latest": self._candidate_payload(*latest) if latest else None,
                "latest_complete": self._candidate_payload(*complete) if complete else None,
            }
        first = reports[0] if reports else {}
        timeline = reports
        if include_timeline and history_mode == "current_families":
            seen_families: set[str] = set()
            timeline = []
            for report in reports:
                family_id = str(report.get("family_id") or report.get("report_id") or "")
                if family_id in seen_families:
                    continue
                seen_families.add(family_id)
                timeline.append(report)
        return {
            "subject_type": first.get("subject_type"),
            "subject_key": subject_key,
            "symbol": first.get("symbol"),
            "security_name": first.get("security_name"),
            "current": current,
            "report_count": int(count_row["report_count"] or 0) if count_row else 0,
            "latest_generated_at": count_row["latest_generated_at"] if count_row else None,
            "history_mode": history_mode,
            "timeline": timeline if include_timeline else [],
        }

    def _claim_excerpt(
        self,
        claim_id: str | None,
        *,
        support_by_claim: dict[str, Any] | None = None,
        data_as_of: str | None = None,
        valid_until: str | None = None,
    ) -> dict[str, Any] | None:
        rows = self._claim_rows(
            [claim_id] if claim_id else [],
            support_by_claim,
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "claim_id": row["claim_id"],
            "section_id": row.get("section_id"),
            "text": str(row.get("text") or "")[:1200],
            "support_status": row.get("support_status", "unclassified"),
            "support_reason": row.get("support_reason", "legacy_unclassified"),
            "reusable": bool(row.get("reusable")),
            "fact_ids": list(row.get("fact_ids") or []),
            "evidence_ids": list(row.get("evidence_ids") or []),
            "data_as_of": data_as_of,
            "valid_until": valid_until,
        }

    def _viewpoint_with_claims(self, report: dict[str, Any], horizon: str) -> dict[str, Any]:
        viewpoint = next(
            (item for item in report.get("viewpoints") or [] if item.get("horizon") == horizon),
            None,
        )
        if viewpoint is None:
            raise ValueError(f"report {report['report_id']} has no {horizon} viewpoint")
        claim_ids = [
            viewpoint.get("summary_claim_id"),
            *(viewpoint.get("reason_claim_ids") or []),
            *(viewpoint.get("risk_claim_ids") or []),
            *(viewpoint.get("condition_claim_ids") or []),
            *(viewpoint.get("invalidation_claim_ids") or []),
        ]
        support_by_claim = dict(
            (report.get("knowledge_link") or {}).get("claim_support_by_claim") or {}
        )
        rows = self._claim_rows(
            (item for item in claim_ids if item),
            support_by_claim,
        )
        excerpts = {
            str(item["claim_id"]): {
                "claim_id": item["claim_id"],
                "section_id": item.get("section_id"),
                "text": str(item.get("text") or "")[:1200],
                "support_status": item.get("support_status", "unclassified"),
                "reusable": bool(item.get("reusable")),
                "fact_ids": list(item.get("fact_ids") or []),
                "evidence_ids": list(item.get("evidence_ids") or []),
            }
            for item in rows
        }
        return {"report": report, "viewpoint": viewpoint, "claims": excerpts}

    @staticmethod
    def _opposite(base: dict[str, Any], current: dict[str, Any]) -> bool:
        stance_pair = {str(base.get("stance")), str(current.get("stance"))}
        if stance_pair == {"bullish", "bearish"}:
            return True
        positive = str(base.get("action")) == "add"
        negative = str(current.get("action")) in {"reduce", "exit"}
        reverse = str(current.get("action")) == "add" and str(base.get("action")) in {"reduce", "exit"}
        return (positive and negative) or reverse

    @staticmethod
    def _claim_texts(item: dict[str, Any], field: str) -> list[str]:
        viewpoint = item["viewpoint"]
        claims = item["claims"]
        claim_ids = viewpoint.get(field) or []
        return [str(claims.get(claim_id, {}).get("text") or "") for claim_id in claim_ids]

    def _pair_delta(self, base: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        base_report, current_report = base["report"], current["report"]
        base_view, current_view = base["viewpoint"], current["viewpoint"]
        if base_report.get("subject_key") != current_report.get("subject_key"):
            relation = "not_comparable"
        elif base_view.get("horizon") != current_view.get("horizon"):
            relation = "different_horizon"
        else:
            changes: dict[str, Any] = {}
            for field in ("stance", "action", "confidence"):
                if base_view.get(field) != current_view.get(field):
                    changes[field] = {"before": base_view.get(field), "after": current_view.get(field)}
            base_summary = str(base["claims"].get(base_view.get("summary_claim_id"), {}).get("text") or "")
            current_summary = str(current["claims"].get(current_view.get("summary_claim_id"), {}).get("text") or "")
            if base_summary != current_summary:
                changes["summary"] = {"before": base_summary, "after": current_summary}
            for field in (
                "reason_claim_ids",
                "risk_claim_ids",
                "condition_claim_ids",
                "invalidation_claim_ids",
            ):
                before = self._claim_texts(base, field)
                after = self._claim_texts(current, field)
                if before != after:
                    changes[field.removesuffix("_claim_ids")] = {"before": before, "after": after}
            relation = "diverged" if self._opposite(base_view, current_view) else "updated" if changes else "continued"
        changes = locals().get("changes", {})
        return {
            "base_report_id": base_report["report_id"],
            "current_report_id": current_report["report_id"],
            "base_viewpoint_id": base_view["viewpoint_id"],
            "current_viewpoint_id": current_view["viewpoint_id"],
            "relation": relation,
            "changes": changes,
            "research_delta": self.knowledge.delta(str(current_report["report_id"])) or {},
        }

    @staticmethod
    def _validate_ai_summary(
        summary: dict[str, Any], allowed: set[tuple[str, str, str | None]]
    ) -> dict[str, Any]:
        if not isinstance(summary.get("summary"), str) or not isinstance(summary.get("items"), list):
            raise ValueError("invalid comparison summary shape")
        normalized_items: list[dict[str, Any]] = []
        for raw in summary["items"][:12]:
            if not isinstance(raw, dict) or not str(raw.get("text") or "").strip():
                continue
            citations: list[dict[str, Any]] = []
            for citation in raw.get("citations") or []:
                if not isinstance(citation, dict):
                    continue
                key = (
                    str(citation.get("report_id") or ""),
                    str(citation.get("claim_id") or ""),
                    citation.get("section_id"),
                )
                if key not in allowed:
                    raise ValueError("comparison summary contains a non-allowlisted citation")
                citations.append(
                    {
                        "report_id": key[0],
                        "claim_id": key[1],
                        "section_id": key[2],
                    }
                )
            if not citations:
                raise ValueError("each comparison explanation requires a citation")
            normalized_items.append({"text": str(raw["text"])[:1200], "citations": citations})
        return {"summary": str(summary["summary"])[:2000], "items": normalized_items}

    def compare(
        self,
        items: list[dict[str, str]],
        *,
        include_ai_summary: bool = False,
    ) -> dict[str, Any]:
        if not 2 <= len(items) <= 4:
            raise ValueError("comparison requires between 2 and 4 reports")
        selected: list[dict[str, Any]] = []
        for item in items:
            report = self.get_report(str(item.get("report_id") or ""))
            if report is None:
                raise ValueError(f"report not found: {item.get('report_id')}")
            horizon = str(item.get("horizon") or "")
            if horizon not in _HORIZONS:
                raise ValueError("comparison horizon is required")
            selected.append(self._viewpoint_with_claims(report, horizon))
        deltas = [self._pair_delta(selected[0], item) for item in selected[1:]]
        public_selected = [
            {
                "report_id": item["report"]["report_id"],
                "report_kind": item["report"]["report_kind"],
                "subject_key": item["report"]["subject_key"],
                "symbol": item["report"].get("symbol"),
                "security_name": item["report"].get("security_name"),
                "data_as_of": item["report"]["data_as_of"],
                "generated_at": item["report"]["generated_at"],
                "viewpoint": item["viewpoint"],
                "claims": list(item["claims"].values()),
            }
            for item in selected
        ]
        result: dict[str, Any] = {
            "selected": public_selected,
            "deltas": deltas,
            "ai_summary": {"status": "not_requested"},
        }
        if not include_ai_summary:
            return result
        if not report_viewpoint_ai_enabled():
            result["ai_summary"] = {"status": "disabled"}
            return result
        summarizer = self.summarizer or ChatReportComparisonSummarizer()
        allowed = {
            (
                str(item["report"]["report_id"]),
                str(claim["claim_id"]),
                claim.get("section_id"),
            )
            for item in selected
            for claim in item["claims"].values()
        }
        ai_payload = {
            "viewpoints": public_selected,
            "deltas": deltas,
            "allowlisted_claims": [
                {"report_id": report_id, "claim_id": claim_id, "section_id": section_id}
                for report_id, claim_id, section_id in sorted(
                    allowed,
                    key=lambda item: (item[0], item[1], str(item[2] or "")),
                )
            ],
        }
        input_hash = hashlib.sha256(
            json.dumps(ai_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        with self.knowledge.connect() as conn:
            cached = conn.execute(
                "SELECT ai_summary_json FROM viewpoint_delta_cache WHERE input_hash=?",
                (input_hash,),
            ).fetchone()
        if cached and cached["ai_summary_json"]:
            result["ai_summary"] = {"status": "completed", **_loads(cached["ai_summary_json"], {})}
            return result
        try:
            generated = self._validate_ai_summary(summarizer.summarize(ai_payload), allowed)
        except Exception as exc:
            logger.warning("Report comparison AI summary failed: %s", exc)
            result["ai_summary"] = {"status": "unavailable"}
            return result
        now = _utc_now()
        comparison_id = _stable_id("comparison", input_hash)
        payload = {"selected": public_selected, "deltas": deltas}
        with self.knowledge.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO viewpoint_delta_cache(
                   comparison_id,input_hash,payload_json,ai_summary_json,ai_model,prompt_version,
                   created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    comparison_id,
                    input_hash,
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(generated, ensure_ascii=False),
                    os.getenv("LANGCHAIN_MODEL_NAME", ""),
                    summarizer.prompt_version,
                    now,
                    now,
                ),
            )
        result["ai_summary"] = {"status": "completed", **generated}
        return result

    def reconcile(
        self,
        *,
        deep_report_service: Any | None = None,
        daily_service: Any | None = None,
        weekly_service: Any | None = None,
        monitoring_service: Any | None = None,
    ) -> dict[str, Any]:
        """Idempotently repair catalog misses created after the catalog epoch."""

        epoch = self.catalog_epoch()
        epoch_time = _parse_time(epoch)
        result: dict[str, Any] = {
            "catalog_epoch": epoch,
            "deep_reports": 0,
            "daily_runs": 0,
            "weekly_runs": 0,
            "monitor_research": 0,
            "errors": [],
        }
        if deep_report_service is not None:
            for record in deep_report_service.list(limit=500):
                created = _parse_time(
                    getattr(record, "updated_at", None) or getattr(record, "created_at", None)
                )
                if epoch_time and created and created < epoch_time:
                    continue
                try:
                    if self.register_deep_report(record) is not None:
                        result["deep_reports"] += 1
                except Exception as exc:
                    self.record_index_failure(
                        source_type="deep_report",
                        source_id=str(getattr(record, "report_id", "")),
                        error=str(exc),
                    )
                    result["errors"].append(
                        {"source": "deep_report", "source_id": getattr(record, "report_id", ""), "error": str(exc)}
                    )
        if daily_service is not None:
            for record in daily_service.list_runs(limit=200):
                created = _parse_time(record.get("completed_at") or record.get("created_at"))
                if epoch_time and created and created < epoch_time:
                    continue
                if str(record.get("status") or "") not in {"completed", "completed_with_warnings"}:
                    continue
                try:
                    aggregate = daily_service.store.read_json(
                        str(record.get("run_id") or ""),
                        "outputs/aggregate.json",
                    )
                    if not isinstance(aggregate, dict):
                        raise ValueError("daily aggregate is unavailable")
                    self.register_daily_run(record, aggregate)
                    result["daily_runs"] += 1
                except Exception as exc:
                    self.record_index_failure(
                        source_type="daily_run",
                        source_id=str(record.get("run_id") or ""),
                        error=str(exc),
                    )
                    result["errors"].append(
                        {"source": "daily_run", "source_id": record.get("run_id"), "error": str(exc)}
                    )
        if weekly_service is not None:
            for record in weekly_service.list_runs(limit=500):
                created = _parse_time(record.get("completed_at") or record.get("created_at"))
                if epoch_time and created and created < epoch_time:
                    continue
                if str(record.get("status") or "") not in {"completed", "completed_with_warnings"}:
                    continue
                if str(record.get("quality_status") or "") == "failed_validation":
                    continue
                try:
                    brief = weekly_service.store.read_json(
                        str(record.get("run_id") or ""), "outputs/weekly_review.json"
                    )
                    if not isinstance(brief, dict):
                        raise ValueError("weekly review JSON is unavailable")
                    self.register_weekly_run(record, brief)
                    result["weekly_runs"] += 1
                except Exception as exc:
                    self.record_index_failure(
                        source_type="weekly_run",
                        source_id=str(record.get("run_id") or ""),
                        error=str(exc),
                    )
                    result["errors"].append(
                        {
                            "source": "weekly_run",
                            "source_id": record.get("run_id"),
                            "error": str(exc),
                        }
                    )
        if monitoring_service is not None:
            snapshots = monitoring_service.store.list_report_snapshots(
                report_type="monitor_research",
                since=epoch,
                limit=500,
            )
            for snapshot in snapshots:
                try:
                    self.register_monitor_research(
                        snapshot,
                        snapshot_id=str(snapshot.get("snapshot_id") or "") or None,
                    )
                    result["monitor_research"] += 1
                except Exception as exc:
                    self.record_index_failure(
                        source_type="monitor_research",
                        source_id=str(snapshot.get("report_ref") or ""),
                        error=str(exc),
                    )
                    result["errors"].append(
                        {
                            "source": "monitor_research",
                            "source_id": snapshot.get("report_ref"),
                            "error": str(exc),
                        }
                    )
        return result


_shared_report_library: ReportLibraryService | None = None
_shared_lock = threading.Lock()


def get_report_library_service() -> ReportLibraryService:
    global _shared_report_library
    if _shared_report_library is None:
        with _shared_lock:
            if _shared_report_library is None:
                _shared_report_library = ReportLibraryService()
    return _shared_report_library


def register_deep_report_safely(record: Any) -> None:
    if not report_library_enabled():
        return
    service: ReportLibraryService | None = None
    try:
        service = get_report_library_service()
        service.register_deep_report(record)
    except Exception as exc:
        if service is not None:
            service.record_index_failure(
                source_type="deep_report",
                source_id=str(getattr(record, "report_id", "")),
                error=str(exc),
            )
        logger.exception("Failed to index Deep Report %s", getattr(record, "report_id", "unknown"))


def register_daily_run_safely(record: dict[str, Any], aggregate: dict[str, Any]) -> None:
    if not report_library_enabled():
        return
    service: ReportLibraryService | None = None
    try:
        service = get_report_library_service()
        service.register_daily_run(record, aggregate)
    except Exception as exc:
        if service is not None:
            service.record_index_failure(
                source_type="daily_run",
                source_id=str(record.get("run_id") or ""),
                error=str(exc),
            )
        logger.exception("Failed to index Daily Run %s", record.get("run_id"))


def register_monitor_research_safely(
    candidate: dict[str, Any], *, snapshot_id: str | None = None
) -> None:
    if not report_library_enabled():
        return
    service: ReportLibraryService | None = None
    try:
        service = get_report_library_service()
        service.register_monitor_research(
            candidate,
            snapshot_id=snapshot_id,
        )
    except Exception as exc:
        if service is not None:
            service.record_index_failure(
                source_type="monitor_research",
                source_id=str(candidate.get("report_ref") or ""),
                error=str(exc),
            )
        logger.exception("Failed to index monitor research %s", candidate.get("report_ref"))
