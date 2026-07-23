"""Canonical data-gap registry shared by report producers and renderers.

Machine outputs keep stable reason codes.  Human artifacts obtain Chinese
labels from this registry.  A gap is never inferred from arbitrary Agent prose.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal


GapImpact = Literal["blocking", "confidence_only", "disclosure_only"]


@dataclass(frozen=True)
class GapDefinition:
    reason_code: str
    label_zh: str
    scope: str
    impact: GapImpact = "confidence_only"
    instrument_types: frozenset[str] = frozenset({"company_equity", "etf"})


def _gap(
    reason_code: str,
    label_zh: str,
    scope: str,
    *,
    impact: GapImpact = "confidence_only",
    instrument_types: Iterable[str] = ("company_equity", "etf"),
) -> GapDefinition:
    return GapDefinition(
        reason_code=reason_code,
        label_zh=label_zh,
        scope=scope,
        impact=impact,
        instrument_types=frozenset(instrument_types),
    )


_ETF = ("etf",)

GAP_DEFINITIONS: dict[str, GapDefinition] = {
    item.reason_code: item
    for item in (
        _gap("daily_history_lt_20", "已完成日线不足二十个交易日", "market_history", impact="blocking"),
        _gap("current_week_incomplete", "本周交易数据尚不完整", "market_history", impact="blocking"),
        _gap("previous_week_unavailable", "缺少上一交易周的数据", "market_history"),
        _gap("market_data_conflict", "市场行情存在尚未解决的来源冲突", "market_data", impact="blocking"),
        _gap("market_data_unavailable", "市场行情数据不可用", "market_data", impact="blocking"),
        _gap("market_data_single_source_unapproved", "日线行情当前仅有一个独立来源且尚未授权使用", "market_data"),
        _gap("short_trading_week", "本周交易日较少，覆盖状态已相应降低", "market_history"),
        _gap("analysis_method_scope_incomplete", "部分分析方法缺少足够历史数据", "analysis_method"),
        _gap("agent_method_analysis_unavailable", "分析智能体未完成，当前保留确定性分析结果", "agent_analysis"),
        _gap("report_catalog_context_unavailable", "统一报告目录上下文不可用", "report_catalog"),
        _gap("reusable_report_claims_unavailable", "没有达到复用等级的历史报告结论", "report_claims"),
        _gap("etf_product_profile_scope_unavailable", "缺少基金产品档案数据", "product_profile", instrument_types=_ETF),
        _gap("etf_tracking_index_scope_unavailable", "缺少基金所跟踪指数的数据", "tracking_index", instrument_types=_ETF),
        _gap("etf_index_relative_strength_scope_unavailable", "缺少基金相对跟踪指数的强弱数据", "index_relative_strength", instrument_types=_ETF),
        _gap("etf_share_premium_tracking_error_scope_unavailable", "缺少基金份额、折溢价和跟踪质量数据", "legacy_etf_context", instrument_types=_ETF),
        _gap("etf_share_scope_unavailable", "缺少基金份额数据", "fund_shares", instrument_types=_ETF),
        _gap("etf_premium_discount_scope_unavailable", "缺少基金折溢价数据", "premium_discount", instrument_types=_ETF),
        _gap("etf_nav_reference_scope_unavailable", "缺少基金净值或盘中参考净值数据", "nav_reference", impact="disclosure_only", instrument_types=_ETF),
        _gap("etf_tracking_error_scope_unavailable", "缺少基金官方跟踪误差或跟踪偏离度", "official_tracking_quality", instrument_types=_ETF),
        _gap("etf_market_tracking_deviation_scope_unavailable", "缺少可复算的市场价格跟踪偏离数据", "market_tracking_deviation", instrument_types=_ETF),
        _gap("etf_component_exposure_scope_unavailable", "缺少基金成分与行业暴露数据", "component_exposure", instrument_types=_ETF),
        _gap("etf_component_research_scope_unavailable", "缺少基金成分公司研究数据", "component_research", instrument_types=_ETF),
        _gap("etf_component_research_scope_partial", "基金成分公司研究尚未完全覆盖", "component_research", instrument_types=_ETF),
    )
}

DATA_GAP_LABELS = {
    code: definition.label_zh for code, definition in GAP_DEFINITIONS.items()
}


def data_gap_registry_payload(
    instrument_type: str | None = None,
) -> list[dict[str, Any]]:
    """Return the stable public registry for APIs, UIs, and other producers."""

    kind = str(instrument_type or "").strip() or None
    if kind is not None and kind not in {"company_equity", "etf"}:
        raise ValueError(f"unsupported instrument_type: {kind}")
    return [
        {
            "reason_code": definition.reason_code,
            "label_zh": definition.label_zh,
            "scope": definition.scope,
            "impact": definition.impact,
            "instrument_types": sorted(definition.instrument_types),
        }
        for definition in GAP_DEFINITIONS.values()
        if kind is None or kind in definition.instrument_types
    ]


def gap_definition(reason_code: str) -> GapDefinition:
    """Return a registered definition or fail closed for formal reports."""

    code = str(reason_code or "").strip()
    try:
        return GAP_DEFINITIONS[code]
    except KeyError as exc:
        raise ValueError(f"unregistered data gap reason_code: {code or '<empty>'}") from exc


def make_gap_detail(
    reason_code: str,
    *,
    source: str,
    instrument_type: str,
    availability: str | None = None,
    missing_items: Iterable[Any] = (),
    data_as_of: str | None = None,
    impact: GapImpact | None = None,
) -> dict[str, Any] | None:
    """Build one typed gap and discard scopes not applicable to the asset."""

    definition = gap_definition(reason_code)
    kind = str(instrument_type or "").strip()
    if kind not in definition.instrument_types:
        return None
    resolved_impact = impact or definition.impact
    if resolved_impact not in {"blocking", "confidence_only", "disclosure_only"}:
        raise ValueError(f"invalid data gap impact: {resolved_impact}")
    return {
        "reason_code": definition.reason_code,
        "scope": definition.scope,
        "availability": str(availability or "missing"),
        "impact": resolved_impact,
        "source": str(source or "unknown")[:120],
        "missing_items": list(dict.fromkeys(
            str(item).strip()[:300] for item in missing_items if str(item).strip()
        )),
        "data_as_of": str(data_as_of) if data_as_of else None,
    }


def normalize_gap_details(
    values: Iterable[dict[str, Any] | None],
    *,
    instrument_type: str,
) -> list[dict[str, Any]]:
    """Validate, applicability-filter, and deterministically deduplicate gaps."""

    merged: dict[str, dict[str, Any]] = {}
    for raw in values:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("reason_code") or "").strip()
        definition = gap_definition(code)
        if instrument_type not in definition.instrument_types:
            continue
        detail = make_gap_detail(
            code,
            source=str(raw.get("source") or "unknown"),
            instrument_type=instrument_type,
            availability=str(raw.get("availability") or "missing"),
            missing_items=raw.get("missing_items") or [],
            data_as_of=str(raw.get("data_as_of") or "") or None,
            impact=str(raw.get("impact") or definition.impact),  # type: ignore[arg-type]
        )
        if detail is None:
            continue
        current = merged.get(code)
        if current is None:
            detail["sources"] = [detail["source"]]
            merged[code] = detail
            continue
        current["sources"] = list(dict.fromkeys([
            *(current.get("sources") or [current.get("source")]),
            detail["source"],
        ]))
        current["missing_items"] = list(dict.fromkeys([
            *(current.get("missing_items") or []),
            *(detail.get("missing_items") or []),
        ]))
        if current.get("data_as_of") is None and detail.get("data_as_of"):
            current["data_as_of"] = detail["data_as_of"]
    return list(merged.values())


def gap_codes(values: Iterable[dict[str, Any]]) -> list[str]:
    return list(dict.fromkeys(
        str(item.get("reason_code") or "").strip()
        for item in values
        if isinstance(item, dict) and str(item.get("reason_code") or "").strip()
    ))


def quality_affecting_gaps(values: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item for item in values
        if isinstance(item, dict)
        and str(item.get("impact") or "confidence_only") != "disclosure_only"
    ]
