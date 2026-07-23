"""Assemble structured, reusable report knowledge for weekly-review v2."""

from __future__ import annotations

import json
import hashlib
from datetime import date, datetime, time, timezone
from typing import Any

from src.portfolio.instruments import infer_portfolio_instrument_type
from src.reports.data_gaps import gap_codes, make_gap_detail, normalize_gap_details


_ETF_SCOPE_METRICS = {
    "product_profile": {
        "fund_short_name", "fund_full_name", "manager", "custodian",
        "tracked_index_code", "tracked_index_name", "management_fee_rate",
        "custody_fee_rate",
    },
    "tracking_index": {
        "tracked_index_code", "tracked_index_name", "index_code", "index_name",
    },
    "index_relative_strength": {
        "index_relative_strength_1w", "fund_index_return_gap_1w",
    },
    "fund_shares": {
        "etf_fund_units", "fund_units", "published_fund_units",
        "etf_fund_units_change_1d",
    },
    "premium_discount": {"premium_discount_rate"},
    "nav_reference": {"unit_nav", "iopv", "iopv_premium_discount_rate"},
    "official_tracking_quality": {
        "tracking_error", "tracking_difference", "tracking_volatility_difference",
        "daily_tracking_deviation_absolute_limit", "annual_tracking_error_limit",
    },
    "market_tracking_deviation": {
        "market_tracking_deviation_20d", "market_tracking_deviation_60d",
        "market_return_gap_20d", "market_return_gap_60d",
    },
    "component_exposure": {
        "etf_component_weight", "etf_observed_weight_coverage",
        "etf_selected_weight_coverage", "etf_explanation_coverage",
    },
    "component_research": {
        "etf_component_research_coverage",
        "etf_component_fully_supported_coverage",
    },
}

_ETF_SCOPE_GAP_CODES = {
    "product_profile": "etf_product_profile_scope_unavailable",
    "tracking_index": "etf_tracking_index_scope_unavailable",
    "index_relative_strength": "etf_index_relative_strength_scope_unavailable",
    "fund_shares": "etf_share_scope_unavailable",
    "premium_discount": "etf_premium_discount_scope_unavailable",
    "nav_reference": "etf_nav_reference_scope_unavailable",
    "official_tracking_quality": "etf_tracking_error_scope_unavailable",
    "market_tracking_deviation": "etf_market_tracking_deviation_scope_unavailable",
    "component_exposure": "etf_component_exposure_scope_unavailable",
    "component_research": "etf_component_research_scope_unavailable",
}


def _cutoff(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if len(raw) == 10:
            return datetime.combine(date.fromisoformat(raw), time.max, tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _not_after(value: Any, cutoff: datetime | None) -> bool:
    if cutoff is None:
        return True
    parsed = _cutoff(str(value or ""))
    return parsed is None or parsed <= cutoff


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


class WeeklyContextAssembler:
    """Read the catalog and Fact ledger without parsing report Markdown."""

    def __init__(self, library: Any | None = None) -> None:
        self.library = library

    def _library(self) -> Any | None:
        if self.library is not None:
            return self.library
        try:
            from src.reports.catalog import (
                get_report_library_service,
                report_library_enabled,
            )

            return get_report_library_service() if report_library_enabled() else None
        except Exception:
            return None

    @staticmethod
    def _reusable_claims(candidate: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(candidate, dict):
            return {
                "summary": None,
                "risks": [],
                "pending_items": [],
                "pending_verification": [],
            }

        def reusable(value: Any) -> dict[str, Any] | None:
            if not isinstance(value, dict) or value.get("reusable") is not True:
                return None
            return {
                key: value.get(key)
                for key in (
                    "claim_id", "section_id", "text", "support_status",
                    "fact_ids", "evidence_ids", "data_as_of", "valid_until",
                )
            }

        raw_claims = [
            candidate.get("summary"),
            *(candidate.get("risks") or []),
            *(candidate.get("pending_items") or []),
        ]
        return {
            "summary": reusable(candidate.get("summary")),
            "risks": [
                item for item in (reusable(value) for value in candidate.get("risks") or [])
                if item
            ],
            "pending_items": [
                item
                for item in (
                    reusable(value) for value in candidate.get("pending_items") or []
                )
                if item
            ],
            "pending_verification": [
                {
                    key: value.get(key)
                    for key in (
                        "claim_id", "section_id", "text", "support_status",
                        "support_reason", "fact_ids", "evidence_ids",
                        "data_as_of", "valid_until",
                    )
                }
                for value in raw_claims
                if isinstance(value, dict)
                and value.get("reusable") is not True
                and value.get("support_status") in {"weak", "conflicted"}
            ],
        }

    @staticmethod
    def _facts(
        library: Any,
        report_ids: list[str],
        *,
        symbol: str | None = None,
        reusable_metrics: set[str] | None = None,
        cutoff: datetime | None = None,
    ) -> list[dict[str, Any]]:
        fact_ids: list[str] = []
        for report_id in report_ids:
            report = library.get_report(report_id)
            if not isinstance(report, dict):
                continue
            fact_ids.extend(
                str(value)
                for value in (report.get("knowledge_link") or {}).get("fact_ids") or []
                if str(value)
            )
        fact_ids = list(dict.fromkeys(fact_ids))
        linked_fact_ids = set(fact_ids)
        knowledge = getattr(library, "knowledge", None)
        if knowledge is None or not hasattr(knowledge, "connect"):
            return []
        with knowledge.connect() as conn:
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(fact_records)").fetchall()
            }
            clauses: list[str] = []
            params: list[Any] = []
            if fact_ids:
                clauses.append(
                    f"fact_id IN ({','.join('?' for _ in fact_ids)})"
                )
                params.extend(fact_ids)
            metrics = sorted(str(item) for item in reusable_metrics or set() if str(item))
            if symbol and metrics and {"symbol", "metric"} <= columns:
                clauses.append(
                    f"(symbol=? AND metric IN ({','.join('?' for _ in metrics)}))"
                )
                params.extend([str(symbol).upper(), *metrics])
            if not clauses:
                return []
            rows = conn.execute(
                f"SELECT * FROM fact_records WHERE {' OR '.join(clauses)}",
                params,
            ).fetchall()
            archive_tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        facts: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if item.get("superseded_by") or not _not_after(item.get("period"), cutoff):
                continue
            item["evidence_ids"] = _loads(item.pop("evidence_ids_json", "[]"), [])
            item["input_fact_ids"] = _loads(item.pop("input_fact_ids_json", "[]"), [])
            if (
                item.get("fact_id") not in linked_fact_ids
                and cutoff is not None
                and {"evidence_records", "source_documents"} <= archive_tables
            ):
                evidence_ids = [str(value) for value in item["evidence_ids"] if str(value)]
                if not evidence_ids:
                    continue
                with knowledge.connect() as evidence_conn:
                    publication_rows = evidence_conn.execute(
                        f"SELECT d.published_at FROM evidence_records e "
                        f"JOIN source_documents d USING(document_ref) "
                        f"WHERE e.evidence_id IN ({','.join('?' for _ in evidence_ids)})",
                        evidence_ids,
                    ).fetchall()
                publications = [
                    _cutoff(str(value[0] or "")) for value in publication_rows
                ]
                if not any(
                    published is not None and published <= cutoff
                    for published in publications
                ):
                    continue
            facts.append(item)
        facts.sort(
            key=lambda item: (str(item.get("period") or ""), str(item.get("created_at") or "")),
            reverse=True,
        )
        return facts

    @staticmethod
    def _metric_scope(facts: list[dict[str, Any]], metrics: set[str]) -> dict[str, Any]:
        rows = [item for item in facts if str(item.get("metric") or "") in metrics]
        return {
            "availability": "complete" if rows else "missing",
            "facts": rows,
            "fact_ids": [str(item.get("fact_id")) for item in rows if item.get("fact_id")],
            "evidence_ids": list(dict.fromkeys(
                str(value)
                for item in rows
                for value in item.get("evidence_ids") or []
                if str(value)
            )),
            "data_as_of": max((str(item.get("period") or "") for item in rows), default=None),
        }

    def assemble(
        self,
        symbol: str,
        *,
        week_end: str | None = None,
        instrument_type: str | None = None,
    ) -> dict[str, Any]:
        normalized = str(symbol or "").strip().upper()
        resolved_type = infer_portfolio_instrument_type(
            normalized,
            explicit=instrument_type,
        )
        cutoff = _cutoff(week_end)
        library = self._library()
        if library is None:
            missing_scopes = {
                key: {
                    "availability": "missing",
                    "facts": [],
                    "fact_ids": [],
                    "evidence_ids": [],
                    "data_as_of": None,
                }
                for key in _ETF_SCOPE_METRICS
                if resolved_type == "etf"
            }
            gap_details = [
                make_gap_detail(
                    "report_catalog_context_unavailable",
                    source="weekly_context",
                    instrument_type=resolved_type,
                ),
                make_gap_detail(
                    "reusable_report_claims_unavailable",
                    source="weekly_context",
                    instrument_type=resolved_type,
                    missing_items=["report_catalog_unavailable"],
                ),
            ]
            if resolved_type == "etf":
                gap_details.extend(
                    make_gap_detail(
                        gap_code,
                        source="weekly_context",
                        instrument_type=resolved_type,
                    )
                    for gap_code in _ETF_SCOPE_GAP_CODES.values()
                )
            normalized_gaps = normalize_gap_details(
                gap_details,
                instrument_type=resolved_type,
            )
            return {
                "schema_version": 2,
                "symbol": normalized,
                "instrument_type": resolved_type,
                "assembled_at": datetime.now(timezone.utc).isoformat(),
                "catalog_available": False,
                "current_reports": {},
                "structured_claims": {},
                "scopes": missing_scopes,
                "data_gap_details": normalized_gaps,
                "data_gaps": gap_codes(normalized_gaps),
                "source_report_ids": [],
                "context_fingerprint": hashlib.sha256(
                    f"{normalized}|{week_end or ''}|unavailable".encode("utf-8")
                ).hexdigest(),
                "source_manifest": {
                    "report_ids": [], "fact_ids": [], "evidence_ids": [],
                },
            }
        subject = library.subject(
            normalized,
            limit=50,
            include_timeline=True,
            history_mode="full",
        )
        current_candidates: dict[str, dict[str, Any]] = {}
        excluded_items: list[dict[str, Any]] = []
        for horizon in ("daily", "weekly", "structural"):
            eligible: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for report in subject.get("timeline") or []:
                if not isinstance(report, dict):
                    continue
                viewpoint = next(
                    (
                        item for item in report.get("viewpoints") or []
                        if isinstance(item, dict) and item.get("horizon") == horizon
                    ),
                    None,
                )
                if viewpoint is None:
                    continue
                exclusion_reason = None
                if report.get("status") != "published":
                    exclusion_reason = "report_not_published"
                elif report.get("report_quality_status") == "failed_validation":
                    exclusion_reason = "report_failed_validation"
                elif not _not_after(report.get("data_as_of"), cutoff):
                    exclusion_reason = "future_report_data"
                if exclusion_reason:
                    excluded_items.append({
                        "report_id": report.get("report_id"),
                        "horizon": horizon,
                        "reason": exclusion_reason,
                    })
                    continue
                valid_until = _cutoff(str(viewpoint.get("valid_until") or ""))
                if cutoff is not None and valid_until is not None and valid_until < cutoff:
                    excluded_items.append({
                        "report_id": report.get("report_id"),
                        "horizon": horizon,
                        "reason": "report_viewpoint_expired",
                    })
                    continue
                eligible.append((report, viewpoint))
            eligible.sort(
                key=lambda item: (
                    str(item[0].get("data_as_of") or ""),
                    str(item[0].get("generated_at") or ""),
                    int(item[0].get("source_revision") or 1),
                ),
                reverse=True,
            )
            latest = eligible[0] if eligible else None
            latest_complete = next(
                (
                    item for item in eligible
                    if item[0].get("report_quality_status") == "passed"
                    and item[0].get("coverage_status") == "complete"
                ),
                None,
            )
            current_candidates[horizon] = {
                "latest": library._candidate_payload(*latest) if latest else None,
                "latest_complete": (
                    library._candidate_payload(*latest_complete)
                    if latest_complete else None
                ),
            }
        candidates = {
            horizon: values.get("latest")
            for horizon, values in current_candidates.items()
            if values.get("latest")
        }
        report_ids = list(dict.fromkeys(
            str(candidate.get("report_id"))
            for values in current_candidates.values()
            for candidate in (values.get("latest"), values.get("latest_complete"))
            if isinstance(candidate, dict) and candidate.get("report_id")
        ))
        facts = self._facts(
            library,
            report_ids,
            symbol=normalized if resolved_type == "etf" else None,
            reusable_metrics={
                metric
                for metrics in _ETF_SCOPE_METRICS.values()
                for metric in metrics
            } if resolved_type == "etf" else None,
            cutoff=cutoff,
        )
        scopes = (
            {
                key: self._metric_scope(facts, metrics)
                for key, metrics in _ETF_SCOPE_METRICS.items()
            }
            if resolved_type == "etf"
            else {}
        )
        structural_report = (
            library.get_report((candidates.get("structural") or {}).get("report_id"))
            if candidates.get("structural") else None
        )
        penetration = dict(
            (structural_report or {}).get("knowledge_link", {}).get("etf_penetration") or {}
        )
        if penetration and resolved_type == "etf":
            if any(
                penetration.get(key) is not None
                for key in ("selected_count", "selected_weight_coverage", "explanation_coverage")
            ):
                scopes["component_exposure"]["availability"] = "complete"
            if any(
                penetration.get(key) is not None
                for key in ("research_coverage", "fully_supported_coverage", "reusable_count")
            ):
                research_coverage = penetration.get("research_coverage")
                unresolved_count = sum(
                    int(penetration.get(key) or 0)
                    for key in ("stale_count", "missing_count", "conflicted_count")
                )
                scopes["component_research"]["availability"] = (
                    "complete"
                    if unresolved_count == 0
                    and isinstance(research_coverage, (int, float))
                    and float(research_coverage) >= 0.999999
                    else "partial"
                )
            scopes["component_exposure"]["penetration"] = penetration
            scopes["component_research"]["penetration"] = penetration
        if resolved_type == "etf":
            official = dict(scopes.get("official_tracking_quality") or {})
            scopes["tracking_error"] = {
                **official,
                "legacy": True,
                "scope_alias": "official_tracking_quality",
            }
        structured_claims = {
            horizon: self._reusable_claims(candidate)
            for horizon, candidate in candidates.items()
        }
        reusable_summary = any(
            group.get("summary") for group in structured_claims.values()
            if isinstance(group, dict)
        )
        gap_details: list[dict[str, Any] | None] = []
        if resolved_type == "etf":
            for scope_id, gap_code in _ETF_SCOPE_GAP_CODES.items():
                scope = scopes.get(scope_id) or {}
                availability = str(scope.get("availability") or "missing")
                if availability == "missing":
                    gap_details.append(
                        make_gap_detail(
                            gap_code,
                            source="weekly_context",
                            instrument_type=resolved_type,
                            availability=availability,
                            data_as_of=scope.get("data_as_of"),
                        )
                    )
                elif scope_id == "component_research" and availability == "partial":
                    penetration_state = scope.get("penetration") or {}
                    gap_details.append(
                        make_gap_detail(
                            "etf_component_research_scope_partial",
                            source="weekly_context",
                            instrument_type=resolved_type,
                            availability="partial",
                            missing_items=[
                                key
                                for key in ("stale_count", "missing_count", "conflicted_count")
                                if int(penetration_state.get(key) or 0) > 0
                            ],
                            data_as_of=scope.get("data_as_of"),
                        )
                    )
        if not reusable_summary:
            gap_details.append(
                make_gap_detail(
                    "reusable_report_claims_unavailable",
                    source="weekly_context",
                    instrument_type=resolved_type,
                    missing_items=[
                        str(item.get("reason") or "")
                        for item in excluded_items
                        if str(item.get("reason") or "")
                    ] or ["no_reusable_summary"],
                )
            )
        normalized_gaps = normalize_gap_details(
            gap_details,
            instrument_type=resolved_type,
        )
        fact_ids = list(dict.fromkeys(
            str(item.get("fact_id")) for item in facts if item.get("fact_id")
        ))
        evidence_ids = list(dict.fromkeys(
            str(value)
            for item in facts for value in item.get("evidence_ids") or [] if str(value)
        ))
        fingerprint_payload = {
            "schema_version": 2,
            "symbol": normalized,
            "instrument_type": resolved_type,
            "week_end": week_end,
            "report_ids": report_ids,
            "fact_ids": fact_ids,
            "claim_ids": sorted({
                str(claim.get("claim_id"))
                for group in structured_claims.values() if isinstance(group, dict)
                for claim in [group.get("summary"), *(group.get("risks") or [])]
                if isinstance(claim, dict) and claim.get("claim_id")
            }),
        }
        structural_name = str(
            (candidates.get("structural") or {}).get("security_name") or ""
        ).strip()
        official_product_name = next(
            (
                str(item.get("value") or "").strip()
                for item in scopes.get("product_profile", {}).get("facts") or []
                if str(item.get("metric") or "") == "fund_short_name"
                and str(item.get("value") or "").strip()
            ),
            "",
        )
        return {
            "schema_version": 2,
            "symbol": normalized,
            "instrument_type": resolved_type,
            # Structural identity and the official ETF product profile outrank
            # stale portfolio aliases and previously generated weekly titles.
            "security_name": (
                official_product_name
                or structural_name
                or subject.get("security_name")
            ),
            "assembled_at": datetime.now(timezone.utc).isoformat(),
            "catalog_available": True,
            "cutoff": week_end,
            "current_reports": candidates,
            "current_report_candidates": current_candidates,
            "structured_claims": structured_claims,
            "scopes": scopes,
            "data_gap_details": normalized_gaps,
            "data_gaps": gap_codes(normalized_gaps),
            "source_report_ids": report_ids,
            "excluded_items": list({
                (str(item.get("report_id") or ""), str(item.get("horizon") or ""), str(item.get("reason") or "")):
                item for item in excluded_items
            }.values()),
            "reuse_exclusions": list({
                (str(item.get("report_id") or ""), str(item.get("horizon") or ""), str(item.get("reason") or "")):
                item for item in excluded_items
            }.values()),
            "context_fingerprint": hashlib.sha256(
                json.dumps(
                    fingerprint_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "source_manifest": {
                "report_ids": report_ids,
                "fact_ids": fact_ids,
                "evidence_ids": evidence_ids,
            },
        }
