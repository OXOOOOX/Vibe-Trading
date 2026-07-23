"""Register opened-source evidence and extracted facts for a deep report."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable

from src.agent.tools import BaseTool


_UNSUPPORTED_FORECAST_PROVENANCE_MARKERS = (
    "内部估计",
    "内部预测",
    "内部测算",
    "自行估计",
    "自行预测",
    "自行测算",
    "管理层指引外推",
    "无一致预期",
    "没有一致预期",
    "未给出具体eps预测",
    "未给出具体盈利预测",
    "internal estimate",
    "internal forecast",
    "internal projection",
    "assistant estimate",
    "model estimate",
    "management guidance extrapolation",
    "no consensus",
    "extrapolat",
)


def _has_unsupported_forecast_provenance(*values: Any) -> bool:
    descriptor = " ".join(str(value or "") for value in values).casefold()
    return any(marker.casefold() in descriptor for marker in _UNSUPPORTED_FORECAST_PROVENANCE_MARKERS)


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]}"


class RecordReportEvidenceTool(BaseTool):
    """Create auditable IDs only from content the research agent actually opened."""

    name = "record_report_evidence"
    description = (
        "Register non-financial evidence and raw facts for the active equity deep report. "
        "Use only after opening the full source document/API payload; search snippets are "
        "rejected. Returns Evidence and Fact IDs that must be cited in the report. This "
        "tool does not validate the truth of an interpretation and must not be used to "
        "record unsupported assumptions as facts."
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "document_ref": {
                "type": "string",
                "description": "Preferred: document_ref returned by read_url/read_document.",
            },
            "chunk_refs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Exact source chunks supporting the extracted facts.",
            },
            "domain": {
                "type": "string",
                "enum": ["industry", "tam", "competition", "announcement", "market", "consensus", "fx", "other"],
            },
            "source": {"type": "string", "description": "Publisher/provider name."},
            "source_locator": {"type": "string", "description": "Opened URL, document locator, or API endpoint."},
            "source_read_status": {
                "type": "string",
                "enum": ["opened_webpage", "opened_document", "api_payload"],
            },
            "published_at": {"type": "string"},
            "coverage_count": {
                "type": "integer",
                "minimum": 1,
                "description": "Required for consensus forecasts: identifiable analyst/report coverage count.",
            },
            "forecast_kind": {
                "type": "string",
                "enum": ["consensus", "single_broker"],
                "description": "Required for consensus-domain evidence; distinguishes consensus from one broker forecast.",
            },
            "excerpt": {
                "type": "string",
                "description": "Short source-grounded excerpt or faithful source summary; at least 20 characters.",
            },
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "metric": {"type": "string"},
                        "value": {},
                        "unit": {"type": "string"},
                        "currency": {"type": "string"},
                        "period": {"type": "string"},
                        "scope": {"type": "string"},
                        "scope_key": {"type": "string"},
                    },
                    "required": ["metric", "value", "unit", "period"],
                },
            },
        },
        "required": ["symbol", "domain", "facts"],
    }
    is_readonly = True
    repeatable = True

    def __init__(
        self,
        default_session_id: str | None = None,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.default_session_id = default_session_id
        self.event_callback = event_callback

    def execute(self, **kwargs: Any) -> str:
        symbol = str(kwargs.get("symbol") or "").strip().upper()
        document_ref = str(kwargs.get("document_ref") or "").strip()
        chunk_refs = [str(item) for item in (kwargs.get("chunk_refs") or []) if str(item)]
        source = str(kwargs.get("source") or "").strip()
        locator = str(kwargs.get("source_locator") or "").strip()
        read_status = str(kwargs.get("source_read_status") or "").strip()
        published_at = str(kwargs.get("published_at") or "").strip()
        excerpt = str(kwargs.get("excerpt") or "").strip()
        domain = str(kwargs.get("domain") or "other").strip()
        coverage_count = kwargs.get("coverage_count")
        forecast_kind = str(kwargs.get("forecast_kind") or "").strip()
        raw_facts = kwargs.get("facts")
        document_text = ""
        source_strength = "D"
        independence_group = ""
        source_class = ""
        valid_until: str | None = None
        if document_ref:
            try:
                from src.research import get_research_knowledge_store, knowledge_enabled

                if not knowledge_enabled():
                    return json.dumps({"status": "error", "error": "document_ref requires the research knowledge feature"}, ensure_ascii=False)
                store = get_research_knowledge_store()
                opened = store.read_document(document_ref, chunk_refs=chunk_refs, limit=30)
                document = dict(opened["document"])
                chunks = [dict(item) for item in opened.get("chunks") or []]
                if not chunks:
                    return json.dumps({"status": "error", "error": "document_ref/chunk_refs did not resolve to source text"}, ensure_ascii=False)
                chunk_refs = [str(item["chunk_ref"]) for item in chunks]
                document_text = "\n".join(str(item.get("text") or "") for item in chunks)
                source = str(document.get("publisher") or document.get("title") or "source document")
                locator = str(document.get("canonical_url") or "")
                published_at = str(document.get("published_at") or document.get("retrieved_at") or "")
                excerpt = excerpt or document_text[:1200]
                read_status = "opened_document"
                source_class = str(document.get("source_class") or "")
                independence_group = str(document.get("independence_group") or "")
                source_strength = {
                    "regulatory_filing": "A", "company_disclosure": "A", "official_statistics": "A",
                    "industry_association": "B", "broker_research": "B", "commercial_research": "C",
                    "mainstream_media": "C", "research_session": "D",
                }.get(source_class, "D")
            except KeyError:
                return json.dumps({"status": "error", "error": "document_ref was not found"}, ensure_ascii=False)
        if not symbol or not source or not locator or not published_at:
            return json.dumps({"status": "error", "error": "provide document_ref or complete source/source_locator/published_at metadata"}, ensure_ascii=False)
        if read_status not in {"opened_webpage", "opened_document", "api_payload"}:
            return json.dumps({"status": "error", "error": "search snippets cannot be registered as evidence"}, ensure_ascii=False)
        minimum_excerpt_length = 8 if document_ref else 20
        if len(excerpt) < minimum_excerpt_length:
            return json.dumps({
                "status": "error",
                "error": f"opened source excerpt must contain at least {minimum_excerpt_length} characters",
            }, ensure_ascii=False)
        if not isinstance(raw_facts, list) or not raw_facts:
            return json.dumps({"status": "error", "error": "at least one extracted fact is required"}, ensure_ascii=False)
        if domain == "consensus":
            if not isinstance(coverage_count, int) or coverage_count < 1:
                return json.dumps({
                    "status": "error",
                    "error": "consensus evidence requires a positive integer coverage_count",
                }, ensure_ascii=False)
            if forecast_kind not in {"consensus", "single_broker"}:
                return json.dumps({
                    "status": "error",
                    "error": "consensus evidence requires forecast_kind=consensus or single_broker",
                }, ensure_ascii=False)
            if (
                (forecast_kind == "single_broker" and coverage_count != 1)
                or (forecast_kind == "consensus" and coverage_count < 2)
            ):
                return json.dumps({
                    "status": "error",
                    "error": "coverage_count must be 1 for single_broker and at least 2 for consensus",
                }, ensure_ascii=False)
            fact_provenance = [
                value
                for raw in raw_facts
                if isinstance(raw, dict)
                for value in (raw.get("metric"), raw.get("scope"))
            ]
            if _has_unsupported_forecast_provenance(
                source,
                locator,
                excerpt,
                *fact_provenance,
            ):
                return json.dumps({
                    "status": "error",
                    "error": (
                        "consensus evidence must contain published broker/analyst forecasts; "
                        "internal estimates and extrapolations are not allowed"
                    ),
                }, ensure_ascii=False)

        retrieved_at = datetime.now(timezone.utc).isoformat()
        content_hash = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
        evidence_id = _stable_id("ev", symbol, domain, source, locator, published_at, content_hash)
        evidence = {
            "evidence_id": evidence_id,
            "symbol": symbol,
            "domain": domain,
            "source": source,
            "source_locator": locator,
            "retrieved_at": retrieved_at,
            "published_at": published_at,
            "content_hash": content_hash,
            "summary": excerpt,
            "status": "recorded_from_opened_source",
            "metadata": {
                "source_read_status": read_status,
                **({
                    "coverage_count": coverage_count,
                    "forecast_kind": forecast_kind,
                } if domain == "consensus" else {}),
            },
        }
        facts: list[dict[str, Any]] = []
        for raw in raw_facts:
            if not isinstance(raw, dict):
                continue
            metric = str(raw.get("metric") or "").strip()
            unit = str(raw.get("unit") or "").strip()
            period = str(raw.get("period") or "").strip()
            if not metric or raw.get("value") is None or not unit or not period:
                continue
            if document_ref and not _fact_value_present(raw.get("value"), unit, document_text):
                return json.dumps({
                    "status": "error",
                    "error": f"fact {metric!r} cannot be replayed from the selected source chunks",
                }, ensure_ascii=False)
            scope_key = str(raw.get("scope_key") or raw.get("scope") or "")
            currency = str(raw.get("currency") or "")
            fact_id = _stable_id(
                "fact", symbol, metric, period, scope_key, unit, currency,
                raw.get("value"), evidence_id,
            )
            facts.append({
                "fact_id": fact_id,
                "symbol": symbol,
                "metric": metric,
                "value": str(raw.get("value")),
                "unit": unit,
                "period": period,
                "formula": None,
                "input_fact_ids": [],
                "evidence_ids": [evidence_id],
                "calculation_version": "source-extraction-v1",
                "validation_status": "pass",
                "statement_type": None,
                "metadata": {
                    "scope": str(raw.get("scope") or ""),
                    "scope_key": scope_key,
                    "currency": currency,
                },
            })
        if not facts:
            return json.dumps({"status": "error", "error": "no complete facts were supplied"}, ensure_ascii=False)

        evidence["metadata"].update({
            "document_ref": document_ref or None,
            "chunk_refs": chunk_refs,
            "source_class": source_class,
            "source_strength": source_strength,
            "independence_group": independence_group,
            "scope_key": str(kwargs.get("scope_key") or ""),
            "valid_until": valid_until,
        })
        bundle = {"evidence": [evidence], "facts": facts}
        if document_ref:
            try:
                from src.research import get_research_knowledge_store

                knowledge_result = get_research_knowledge_store().register_bundle(bundle)
            except Exception as exc:
                return json.dumps({"status": "error", "error": f"knowledge registration failed: {exc}"}, ensure_ascii=False)
        else:
            knowledge_result = {"status": "legacy_report_only"}
        if self.event_callback is not None:
            self.event_callback("report.external_evidence", {"bundle": bundle})
        return json.dumps({
            "status": "ok",
            "evidence_id": evidence_id,
            "fact_ids": [item["fact_id"] for item in facts],
            "document_ref": document_ref or None,
            "chunk_refs": chunk_refs,
            "conflicts": knowledge_result.get("conflicts", []),
            "wording_guard": "IDs confirm provenance registration, not the correctness of an inference.",
        }, ensure_ascii=False)


def _fact_value_present(value: Any, unit: str, text: str) -> bool:
    """Conservatively verify a numeric source fact against selected chunks."""

    raw = str(value).strip()
    normalized_text = str(text or "").replace(",", "").replace("，", "")
    if raw.replace(",", "") in normalized_text:
        return True
    try:
        number = float(raw.replace(",", ""))
    except ValueError:
        return raw.casefold() in normalized_text.casefold()
    candidates = {f"{number:g}", f"{number:.1f}", f"{number:.2f}"}
    # Allow explicit Chinese display-unit conversions while keeping the
    # original value discoverable from the source text.
    if unit in {"元", "CNY", "RMB"}:
        candidates.update({f"{number / 10_000:g}万", f"{number / 100_000_000:g}亿"})
    return any(candidate.rstrip("0").rstrip(".") in normalized_text for candidate in candidates)
