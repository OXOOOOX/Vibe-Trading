"""Deterministic ETF report readiness and presentation projection.

Evidence quality and penetration completeness are deliberately orthogonal:
quality describes how well the available claims are supported, while readiness
describes which ETF report product the collected evidence is allowed to claim.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from typing import Any, Mapping


READINESS_VERSION = "etf-readiness-v1"
DEFAULT_THRESHOLDS = {
    "holdings_weight_coverage": 0.95,
    "mandatory_component_weight": 0.08,
    "component_research_coverage": 0.80,
    "fully_supported_etf_weight": 0.40,
}

_REPORT_SECTION_IDS = (
    "executive_summary",
    "index_and_product",
    "exposure_structure",
    "aggregate_fundamentals",
    "price_volume_structure",
    "flow_liquidity_tracking",
    "holding_penetration",
    "scenarios_watchlist",
)
_PIPELINE_IDS = (
    "report_gate",
    "identity",
    "product_profile",
    "universe",
    "market_data",
    "peer_flow",
    "component_research",
)
_REASON_LABELS = {
    "evidence_validation_failed": "证据校验未通过",
    "missing_official_index_identity": "ETF 或跟踪指数官方身份不完整",
    "product_profile_hard_gate_failed": "ETF 产品画像硬字段不完整",
    "verified_market_data_missing": "缺少已核验市场数据",
    "report_gate_failed": "正式报告发布门控未通过",
    "holdings_weight_coverage_below_threshold": "持仓权重覆盖不足",
    "mandatory_component_research_missing": "高权重成分研究尚未完成",
    "component_research_coverage_below_threshold": "成分研究覆盖不足",
    "fully_supported_etf_weight_below_threshold": "完全支持的 ETF 权重不足",
    "material_fact_conflict": "存在重大事实或成分研究冲突",
}


def _payload(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    return {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _module_value(module: dict[str, Any], key: str) -> Any:
    if module.get(key) is not None:
        return module[key]
    for result_key in ("deterministic_result", "narrative_result"):
        result = _payload(module.get(result_key))
        details = _payload(result.get("details"))
        if details.get(key) is not None:
            return details[key]
    return _payload(module.get("details")).get(key)


def _projection_status(value: Mapping[str, Any]) -> str:
    validation = str(value.get("validation") or "")
    availability = str(value.get("availability") or "")
    if validation == "failed":
        return "failed_validation"
    if availability == "missing":
        return "insufficient_evidence"
    if validation == "warning" or availability == "partial":
        return "warning"
    return "passed"


def _narrative_projection(module: dict[str, Any]) -> dict[str, Any]:
    narrative = _payload(module.get("narrative_result"))
    if not narrative:
        return dict(module)
    return {
        "status": _projection_status(narrative),
        "coverage": narrative.get("coverage"),
        "reason": narrative.get("reason_code"),
        "details": _payload(narrative.get("details")),
        "availability": narrative.get("availability"),
        "validation": narrative.get("validation"),
        "reason_code": narrative.get("reason_code"),
        "missing_items": list(narrative.get("missing_items") or []),
    }


def project_etf_module_namespaces(
    analysis_modules: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Split execution checks from reader-facing report section results."""

    modules = {str(key): _payload(value) for key, value in analysis_modules.items()}
    pipeline = {
        key: dict(modules[key])
        for key in _PIPELINE_IDS
        if key in modules
    }
    holding = modules.get("holding_penetration")
    if holding:
        deterministic = _payload(holding.get("deterministic_result"))
        pipeline["holding_selection"] = (
            {
                **dict(holding),
                "status": _projection_status(deterministic),
                "coverage": deterministic.get("coverage", holding.get("coverage")),
                "reason": deterministic.get("reason_code", holding.get("reason")),
                "details": {
                    **_payload(deterministic.get("details")),
                    **_payload(holding.get("details")),
                },
            }
            if deterministic
            else dict(holding)
        )

    sections = {
        key: _narrative_projection(modules[key])
        for key in _REPORT_SECTION_IDS
        if key in modules
    }

    def downgrade(section_id: str, pipeline_id: str, reason: str) -> None:
        source = pipeline.get(pipeline_id)
        section = sections.get(section_id)
        if not source or not section or str(source.get("status")) == "passed":
            return
        source_status = str(source.get("status") or "warning")
        section_status = str(section.get("status") or "pending")
        if section_status == "failed_validation":
            return
        section["status"] = (
            "failed_validation" if source_status == "failed_validation" else "warning"
        )
        section["reason"] = reason
        section["reason_code"] = reason
        section["coverage"] = source.get("coverage")
        source_missing = list(source.get("missing_items") or [])
        source_missing.extend(
            _payload(source.get("details")).get("missing_optional_fields") or []
        )
        section["missing_items"] = list(dict.fromkeys(map(str, source_missing)))

    downgrade("index_and_product", "product_profile", "product_profile_not_ready")
    downgrade("flow_liquidity_tracking", "peer_flow", "peer_flow_not_ready")
    downgrade("holding_penetration", "component_research", "component_research_not_ready")
    return pipeline, sections


def evaluate_etf_report_readiness(
    *,
    quality_status: str,
    analysis_modules: Mapping[str, Any],
    research_coverage: Mapping[str, Any] | None = None,
    thresholds: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Return one deterministic verdict for all ETF report exits."""

    limits = {**DEFAULT_THRESHOLDS, **dict(thresholds or {})}
    pipeline, sections = project_etf_module_namespaces(analysis_modules)
    universe = pipeline.get("universe", {})
    identity = pipeline.get("identity", {})
    product = pipeline.get("product_profile", {})
    market = pipeline.get("market_data", {})
    report_gate = pipeline.get("report_gate", {})
    selection = pipeline.get("holding_selection", {})
    component = pipeline.get("component_research", {})

    holdings_coverage = _number(universe.get("coverage")) or 0.0
    selected_weight = _number(_module_value(selection, "selected_weight_coverage")) or 0.0
    selected_count = int(_number(_module_value(component, "selected_count")) or 0)
    research_coverage_ratio = _number(_module_value(component, "research_coverage")) or 0.0
    fully_supported_ratio = (
        _number(_module_value(component, "fully_supported_coverage")) or 0.0
    )
    fully_supported_etf_weight = selected_weight * fully_supported_ratio
    reusable_count = int(_number(_module_value(component, "reusable_count")) or 0)
    partial_count = int(_number(_module_value(component, "partial_reusable_count")) or 0)
    conflicted_count = int(_number(_module_value(component, "conflicted_count")) or 0)
    material_conflicts = list((research_coverage or {}).get("material_conflicts") or [])

    bindings = list(_module_value(component, "selected_components") or [])
    mandatory_incomplete: list[str] = []
    for raw in bindings:
        item = _payload(raw)
        weight = _number(item.get("component_weight", item.get("weight"))) or 0.0
        status = str(item.get("digest_status") or item.get("status") or "missing")
        if weight >= limits["mandatory_component_weight"] and status != "reusable":
            mandatory_incomplete.append(
                str(item.get("component_symbol") or item.get("symbol") or "UNKNOWN")
            )

    hard_checks = {
        "evidence_validation": quality_status != "failed_validation",
        "identity": bool(identity) and str(identity.get("status")) == "passed",
        "product_hard_gate": bool(product) and str(product.get("status")) != "failed_validation",
        "market_data": bool(market) and str(market.get("status")) == "passed",
        "report_gate": not report_gate or str(report_gate.get("status")) == "passed",
        "holdings_weight_coverage": holdings_coverage >= limits["holdings_weight_coverage"],
    }
    hard_gate_passed = all(hard_checks.values())
    penetration_checks = {
        "mandatory_components": not mandatory_incomplete,
        "component_research_coverage": (
            selected_count > 0
            and research_coverage_ratio >= limits["component_research_coverage"]
        ),
        "fully_supported_etf_weight": (
            fully_supported_etf_weight >= limits["fully_supported_etf_weight"]
        ),
        "component_conflicts": conflicted_count == 0 and not material_conflicts,
    }
    has_any_research = reusable_count + partial_count > 0 or research_coverage_ratio > 0

    reason_codes: list[str] = []
    hard_reason_codes = {
        "evidence_validation": "evidence_validation_failed",
        "identity": "missing_official_index_identity",
        "product_hard_gate": "product_profile_hard_gate_failed",
        "market_data": "verified_market_data_missing",
        "report_gate": "report_gate_failed",
        "holdings_weight_coverage": "holdings_weight_coverage_below_threshold",
    }
    penetration_reason_codes = {
        "mandatory_components": "mandatory_component_research_missing",
        "component_research_coverage": "component_research_coverage_below_threshold",
        "fully_supported_etf_weight": "fully_supported_etf_weight_below_threshold",
        "component_conflicts": "material_fact_conflict",
    }
    reason_codes.extend(
        hard_reason_codes[key] for key, passed in hard_checks.items() if not passed
    )
    reason_codes.extend(
        penetration_reason_codes[key]
        for key, passed in penetration_checks.items()
        if not passed
    )

    if not hard_gate_passed:
        status = "not_publishable"
    elif all(penetration_checks.values()):
        status = "penetration_ready"
    elif has_any_research:
        status = "penetration_partial"
    else:
        status = "structure_ready"

    metrics = {
        "holdings_weight_coverage": round(holdings_coverage, 8),
        "selected_component_count": selected_count,
        "selected_weight_coverage": round(selected_weight, 8),
        "component_research_coverage": round(research_coverage_ratio, 8),
        "fully_supported_selected_coverage": round(fully_supported_ratio, 8),
        "fully_supported_etf_weight": round(fully_supported_etf_weight, 8),
        "reusable_component_count": reusable_count,
        "partial_reusable_component_count": partial_count,
        "conflicted_component_count": conflicted_count,
        "mandatory_incomplete_components": mandatory_incomplete,
    }
    fingerprint_input = {
        "version": READINESS_VERSION,
        "quality_status": quality_status,
        "thresholds": limits,
        "hard_checks": hard_checks,
        "penetration_checks": penetration_checks,
        "metrics": metrics,
        "reason_codes": reason_codes,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return {
        "version": READINESS_VERSION,
        "status": status,
        "evidence_quality": quality_status,
        "hard_gate_passed": hard_gate_passed,
        "structure_checks": hard_checks,
        "penetration_checks": penetration_checks,
        "metrics": metrics,
        "thresholds": limits,
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "missing_actions": list(dict.fromkeys(reason_codes)),
        "input_fingerprint": fingerprint,
    }


def etf_report_presentation(readiness: Mapping[str, Any] | None) -> dict[str, str]:
    status = str((readiness or {}).get("status") or "structure_ready")
    values = {
        "not_publishable": (
            "ETF研究诊断草稿",
            "ETF 研究诊断草稿",
            "报告未通过正式发布审查",
        ),
        "structure_ready": (
            "ETF结构研究",
            "ETF 结构研究",
            "ETF 结构报告已生成，尚未完成成分穿透",
        ),
        "penetration_partial": (
            "ETF穿透研究-部分覆盖",
            "ETF 穿透研究（部分覆盖）",
            "ETF 部分穿透研究已生成，尚有成分研究未完成",
        ),
        "penetration_ready": (
            "ETF穿透式深度研究",
            "ETF 穿透式深度研究",
            "ETF 穿透式深度研究已完成",
        ),
    }
    filename_label, title_label, completion = values.get(status, values["structure_ready"])
    return {
        "status": status,
        "filename_label": filename_label,
        "title_label": title_label,
        "completion_message": completion,
    }


def etf_readiness_reason_label(reason_code: str) -> str:
    return _REASON_LABELS.get(str(reason_code), str(reason_code))
