"""Evidence-backed monitor draft generation.

The first production-safe planner is deliberately constrained: it converts
verified raw market evidence into a strict, editable plan. A structured LLM
planner can replace the strategy port later without changing persistence or
runtime contracts.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from src.market_cache import get_market_refresh_service
from src.portfolio.analysis_methods import (
    ACTION_READY_MIN_BARS,
    METHOD_REGISTRY_VERSION,
    WATCH_ONLY_MIN_BARS,
    build_market_analysis_snapshot,
    method_release_status,
)
from src.portfolio.state import normalize_symbol

from .models import DEFAULT_PRICE_VOLUME_POLICY, validate_plan


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _rounded(value: float) -> float:
    return round(value, 3 if value < 20 else 2)


def _bar_date(row: dict[str, Any]) -> str:
    return str(row.get("session_date") or row.get("bar_time") or "")[:10] or "日期未知"


def _calculation_basis(
    *,
    method: str,
    method_label: str,
    formula: str,
    summary: str,
    recommended_value: float,
    references: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "method": method,
        "method_label": method_label,
        "formula": formula,
        "summary": summary,
        "recommended_value": _rounded(recommended_value),
        "references": references,
    }


class MonitoringPlanner:
    """Build a reviewable plan without inventing unavailable evidence."""

    model_id = METHOD_REGISTRY_VERSION

    def __init__(self, market_service: Any | None = None) -> None:
        self.market_service = market_service or get_market_refresh_service()

    def _actionable_quote(
        self,
        symbol: str,
        *,
        allow_single_source: bool = False,
    ) -> dict[str, Any] | None:
        """Return the freshest verified quote usable for draft planning.

        A one-minute provider can be a few minutes behind while the immediately
        preceding five-minute bar is still fresh and independently verified.
        The portfolio quote remains honest about the stale forming bar; the
        planner falls back only to a recent quorum bar, never to single-source
        data.
        """
        quote = self.market_service.store.quote(symbol)
        if quote and quote.get("status") == "verified":
            return quote

        now = datetime.now(timezone.utc)
        candidates: list[dict[str, Any]] = []
        single_source_candidates: list[dict[str, Any]] = []
        for interval, maximum_age in (("1m", timedelta(minutes=3)), ("5m", timedelta(minutes=10))):
            bars = self.market_service.store.query_bars(
                symbol=symbol,
                interval=interval,
                adjustment="raw",
                view="consensus",
                limit=10,
            )
            for row in reversed(bars):
                status = str(row.get("status") or "")
                if (
                    status not in ({"verified", "single_source"} if allow_single_source else {"verified"})
                    or row.get("close") is None
                ):
                    continue
                try:
                    bar_time = datetime.fromisoformat(
                        str(row["bar_time"]).replace("Z", "+00:00")
                    ).astimezone(timezone.utc)
                except (KeyError, TypeError, ValueError):
                    continue
                age = now - bar_time
                if timedelta(0) <= age <= maximum_age:
                    candidate = {
                            "symbol": symbol,
                            "interval": interval,
                            "bar_time": row["bar_time"],
                            "session_date": row.get("session_date"),
                            "adjustment": "raw",
                            "last_price": row["close"],
                            "volume": row.get("volume"),
                            "amount": row.get("amount"),
                            "vwap": row.get("vwap"),
                            "status": status,
                            "price_spread_pct": row.get("price_spread_pct"),
                            "source_count": row.get("source_count"),
                            "sources": list(row.get("sources") or []),
                            "verified_at": row.get("verified_at"),
                            "batch_id": row.get("batch_id"),
                        }
                    if status == "verified":
                        candidates.append(candidate)
                    else:
                        single_source_candidates.append(candidate)
                    break
        if candidates:
            return max(candidates, key=lambda item: str(item["bar_time"]))
        if allow_single_source and single_source_candidates:
            return max(single_source_candidates, key=lambda item: str(item["bar_time"]))
        if allow_single_source and quote and quote.get("status") == "single_source":
            return quote
        return quote

    def build(
        self,
        holding: dict[str, Any],
        *,
        allow_single_source: bool = False,
    ) -> tuple[dict[str, Any] | None, dict[str, Any], list[str]]:
        """Build a continuity-safe structural plan from deterministic levels."""

        symbol = normalize_symbol(str(holding.get("symbol") or holding.get("code") or "")).upper()
        quote = self._actionable_quote(symbol, allow_single_source=allow_single_source)
        blocked: list[str] = []
        accepted_statuses = {"verified", "single_source"} if allow_single_source else {"verified"}
        if not quote:
            blocked.append("verified_quote_missing")
        elif quote.get("status") not in accepted_statuses:
            blocked.append(f"quote_not_actionable:{quote.get('status') or 'unknown'}")
        elif quote.get("adjustment") != "raw":
            blocked.append("raw_price_basis_unavailable")
        elif not quote.get("sources"):
            blocked.append("quote_provenance_missing")
        try:
            last_price = float((quote or {}).get("last_price"))
            if last_price <= 0:
                raise ValueError
        except (TypeError, ValueError):
            last_price = 0.0
            blocked.append("verified_price_missing")

        daily = self.market_service.store.query_bars(
            symbol=symbol,
            interval="1D",
            adjustment="raw",
            view="consensus",
            limit=320,
        )
        verified_daily = [
            row
            for row in daily
            if row.get("status") in accepted_statuses and row.get("close") is not None
        ]
        intraday = self.market_service.store.query_bars(
            symbol=symbol,
            interval="5m",
            adjustment="raw",
            view="consensus",
            limit=2500,
        )
        verified_intraday = [
            row
            for row in intraday
            if row.get("status") in accepted_statuses
            and row.get("close") is not None
            and str(row.get("volume_status") or row.get("status") or "") in accepted_statuses
        ]
        if hasattr(self.market_service, "derive_adjustment_factor_candidates"):
            self.market_service.derive_adjustment_factor_candidates(
                symbol,
                raw_bars=verified_daily,
            )
        factor_rows = (
            self.market_service.store.list_adjustment_factors(symbol)
            if hasattr(self.market_service.store, "list_adjustment_factors")
            else []
        )
        instrument_type = (
            "etf"
            if symbol[:2] in {"15", "16", "50", "51", "52", "56", "58"}
            else "company_equity"
        )
        snapshot = build_market_analysis_snapshot(
            verified_daily,
            symbol=symbol,
            instrument_type=instrument_type,
            adjustment="raw",
            factor_rows=factor_rows,
            intraday_bars=verified_intraday,
        )
        continuity = dict(snapshot.get("continuity") or {})
        data_mode = "single_source" if (quote or {}).get("status") == "single_source" else "verified"
        volume_status = str((quote or {}).get("volume_status") or "unavailable")
        if volume_status == "unavailable" and verified_daily:
            volume_status = str(verified_daily[-1].get("volume_status") or "unavailable")
        evidence = {
            "symbol": symbol,
            "holding": {
                "name": holding.get("name"),
                "quantity": holding.get("quantity"),
                "cost_price": holding.get("cost_price"),
                "updated_at": holding.get("updated_at"),
            },
            "quote": quote,
            "daily_bar_count": len(verified_daily),
            "usable_daily_bar_count": int(snapshot.get("bar_count") or 0),
            "daily_tail_hash": _hash(verified_daily[-60:]),
            "intraday_tail_hash": _hash(
                [
                    (
                        row.get("bar_time"),
                        row.get("open"),
                        row.get("high"),
                        row.get("low"),
                        row.get("close"),
                        row.get("volume"),
                        row.get("amount"),
                        row.get("volume_status"),
                    )
                    for row in verified_intraday[-120:]
                ]
            ),
            "volume_signature": _hash(
                [
                    (
                        row.get("bar_time"),
                        row.get("volume_status"),
                        row.get("volume_source_count"),
                        row.get("volume_sources"),
                    )
                    for row in verified_daily[-20:]
                ]
            ),
            "adjustment_factor_revision": _hash(factor_rows),
            "method_registry_version": METHOD_REGISTRY_VERSION,
            "level_snapshot_id": snapshot.get("level_snapshot_id"),
            "level_snapshot": snapshot,
            "continuity": continuity,
            "data_as_of": (quote or {}).get("bar_time"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "planner_mode": "multi_method_level_evidence",
            "threshold_method": "multi_method_level_evidence",
            "selection_mode": "deterministic_fallback",
            "data_mode": data_mode,
            "volume_gate": {
                "status": "ready" if volume_status in accepted_statuses else "pending_evidence",
                "source_status": volume_status,
                "blocks_price_observation": False,
                "blocks_confirmation": volume_status not in accepted_statuses,
            },
        }
        release_gate = method_release_status(METHOD_REGISTRY_VERSION)
        evidence["method_release_gate"] = release_gate
        if not release_gate.get("eligible_for_automatic_release"):
            blocked.append("no_qualified_level")
        if data_mode == "single_source":
            evidence["single_source_consent"] = {
                "granted": bool(allow_single_source),
                "granted_at": datetime.now(timezone.utc).isoformat(),
            }
        if int(snapshot.get("bar_count") or 0) < WATCH_ONLY_MIN_BARS:
            blocked.extend(str(item) for item in snapshot.get("data_gap_codes") or [])
            if "insufficient_post_event_history" not in blocked:
                blocked.append("insufficient_post_event_history")
        if blocked:
            return None, evidence, list(dict.fromkeys(blocked))

        primary = dict(snapshot.get("primary_levels") or {})
        ladder = dict(snapshot.get("level_ladder") or {})

        def preferred_level(side: str, roles: set[str]) -> dict[str, Any] | None:
            values = [
                item
                for item in list(ladder.get(side) or [])
                if isinstance(item, dict) and str(item.get("role") or "") in roles
            ]
            if not values:
                values = [
                    item
                    for item in list(snapshot.get("level_candidates") or [])
                    if isinstance(item, dict)
                    and item.get("level_type") == side
                    and str(item.get("role") or "") in roles
                ]
            if not values:
                return None
            values.sort(
                key=lambda item: (
                    item.get("automation_status") == "action_ready",
                    float(item.get("rank_score") or 0.0),
                ),
                reverse=True,
            )
            return dict(values[0])

        support = preferred_level("support", {"S1"}) or preferred_level(
            "support", {"S2"}
        ) or primary.get("support")
        resistance = preferred_level("resistance", {"R1"}) or preferred_level(
            "resistance", {"R2"}
        ) or primary.get("resistance")
        if not isinstance(support, dict) or not isinstance(resistance, dict):
            return None, evidence, ["no_qualified_level"]

        def calculation_basis(candidate: dict[str, Any], value: float, label: str) -> dict[str, Any]:
            references = []
            for item in list((candidate.get("calculation_basis") or {}).get("references") or [])[:8]:
                references.append(
                    {
                        "label": str(item.get("kind") or "结构证据")[:80],
                        "value": float(item.get("value")),
                        "date": str(item.get("date") or "")[:40],
                    }
                )
            return _calculation_basis(
                method="multi_method_level_evidence",
                method_label=label,
                formula="多周期边界 + 已确认摆动点 + ATR聚类 + 触达反应与量能证据评分",
                summary=(
                    f"区间 {candidate['lower']}–{candidate['upper']}，评分 {candidate['score']}，"
                    f"置信度 {candidate['confidence']}，方法 {', '.join(candidate.get('method_ids') or [])}。"
                ),
                recommended_value=value,
                references=references,
            )

        support_invalidation = float((support.get("invalidation") or {}).get("value"))
        resistance_invalidation = float((resistance.get("invalidation") or {}).get("value"))
        rules: list[dict[str, Any]] = []
        scenarios: list[dict[str, Any]] = []

        def append_scenario(
            *,
            client_rule_id: str,
            kind: str,
            severity: str,
            intent: str,
            level: int,
            candidate: dict[str, Any],
            label: str,
            rationale: str,
            confirmation_count: int,
            action: str,
            entry_conditions: list[dict[str, Any]],
            confirmation_conditions: list[dict[str, Any]],
            threshold: float | None = None,
            zone: tuple[float, float] | None = None,
            automation_status: str | None = None,
        ) -> None:
            if zone is not None:
                lower, upper = zone
                parameters = {
                    "lower": _rounded(lower),
                    "upper": _rounded(upper),
                    "interval": "5m",
                    "adjustment": "raw",
                    "confirmation_count": confirmation_count,
                    "cooldown_minutes": 120,
                    "clear_hysteresis_bps": 30,
                }
                trigger = {
                    "kind": kind,
                    "lower": _rounded(lower),
                    "upper": _rounded(upper),
                    "interval": "5m",
                    "confirmation_count": confirmation_count,
                }
                original_level = {
                    "kind": "zone",
                    "lower": _rounded(lower),
                    "upper": _rounded(upper),
                    "unit": "CNY",
                    "adjustment": "raw",
                    "source_text": label,
                }
                recommended_value = (lower + upper) / 2
            else:
                assert threshold is not None
                parameters = {
                    "threshold": _rounded(threshold),
                    "interval": "5m",
                    "adjustment": "raw",
                    "confirmation_count": confirmation_count,
                    "cooldown_minutes": 120,
                    "clear_hysteresis_bps": 30,
                }
                trigger = {
                    "kind": kind,
                    "threshold": _rounded(threshold),
                    "interval": "5m",
                    "confirmation_count": confirmation_count,
                }
                original_level = {
                    "kind": "price",
                    "value": _rounded(threshold),
                    "unit": "CNY",
                    "adjustment": "raw",
                    "source_text": label,
                }
                recommended_value = threshold
            basis = calculation_basis(candidate, recommended_value, label)
            rules.append(
                {
                    "client_rule_id": client_rule_id,
                    "kind": kind,
                    "severity": severity,
                    "enabled": True,
                    "target_intent": intent,
                    "target_level": level,
                    "alert_cue": "none",
                    "parameters": parameters,
                    "valid_until": rule_valid_until,
                    "rationale": rationale,
                    "calculation_basis": basis,
                }
            )
            source_conditions = []
            for condition in [*entry_conditions, *confirmation_conditions]:
                source_conditions.append(
                    {
                        "condition_id": condition["source_condition_id"],
                        "source_text": str(condition.pop("source_text")),
                        "role": "required",
                        "coverage_status": "mapped",
                        "reason": "",
                        "evidence_refs": [str(candidate.get("candidate_id") or client_rule_id)],
                    }
                )
            scenario_status = automation_status or str(
                candidate.get("automation_status") or "watch_only"
            )
            scenarios.append(
                {
                    "scenario_id": client_rule_id,
                    "client_rule_id": client_rule_id,
                    "label": label,
                    "intent": intent,
                    "evidence_refs": [str(candidate.get("candidate_id") or client_rule_id)],
                    "original_level": original_level,
                    "trigger": trigger,
                    "approach_policy": {
                        "distance_bps": 100,
                        "source": "atr20_default",
                        "check_interval": "1m",
                    },
                    "volume_confirmation": {
                        "metric": "same_bucket_5m_volume_ratio",
                        "comparator": "gte",
                        "threshold": 1.2,
                        "min_samples": 10,
                        "mode": "classify_only",
                        "unit": "ratio",
                    },
                    "resolution_policy": {
                        "rejection_hysteresis_bps": 30,
                        "max_observation_bars": 12 if any(
                            str(item.get("interval")) == "1d"
                            for item in confirmation_conditions
                        ) else 6,
                        "close_action": "unresolved",
                    },
                    "rationale": rationale,
                    "source_conditions": source_conditions,
                    "entry_conditions": {"operator": "all", "conditions": entry_conditions},
                    "confirmation_conditions": {
                        "operator": "all",
                        "conditions": confirmation_conditions,
                    },
                    "invalidation_conditions": {"operator": "all", "conditions": []},
                    "sequence_policy": {
                        "enabled": bool(confirmation_conditions),
                        "max_wait_bars": 12 if any(
                            str(item.get("interval")) == "1d"
                            for item in confirmation_conditions
                        ) else 6,
                        "reset_on_invalidation": True,
                    },
                    "action_template": {
                        "action": action,
                        "sizing": {
                            "kind": "default_policy",
                            "source": "requires_user_risk_preferences",
                        },
                        "confidence_floor": "high" if scenario_status == "action_ready" else "low",
                    },
                    "automation_status": scenario_status,
                }
            )

        def condition(
            condition_id: str,
            source_text: str,
            kind: str,
            operator: str,
            *,
            interval: str,
            **values: Any,
        ) -> dict[str, Any]:
            return {
                "condition_id": condition_id,
                "source_condition_id": f"source-{condition_id}",
                "source_text": source_text,
                "kind": kind,
                "operator": operator,
                "interval": interval,
                "consecutive": int(values.pop("consecutive", 1)),
                "lookback_bars": int(values.pop("lookback_bars", 1)),
                "freshness_seconds": int(
                    values.pop("freshness_seconds", 172800 if interval == "1d" else 900)
                ),
                **values,
            }

        now = datetime.now(timezone.utc)
        rule_valid_until = (now + timedelta(days=45)).isoformat()
        hard_valid_until = (now + timedelta(days=90)).isoformat()
        support_is_actionable_s1 = bool(
            support.get("role") == "S1" and support.get("automation_status") == "action_ready"
        )
        append_scenario(
            client_rule_id="support-zone-test",
            kind="price_zone_enter",
            zone=(float(support["lower"]), float(support["upper"])),
            severity="info",
            intent="add_position",
            level=1,
            candidate=support,
            label=f"{support.get('role') or 'T0'} 支撑测试",
            rationale="进入支撑区只表示开始测试；收复上沿并通过完成K线和量能确认后，才允许生成加仓草稿。",
            confirmation_count=1,
            action="add" if support_is_actionable_s1 else "observe",
            automation_status="action_ready" if support_is_actionable_s1 else "watch_only",
            entry_conditions=[
                condition(
                    "support-zone-entry",
                    "已完成5分钟K线进入支撑区",
                    "price_zone",
                    "between",
                    interval="5m",
                    lower=_rounded(float(support["lower"])),
                    upper=_rounded(float(support["upper"])),
                )
            ],
            confirmation_conditions=[
                condition(
                    "support-reclaim",
                    "进入支撑区后重新收于区间上沿之上",
                    "price_reclaim",
                    "gte",
                    interval="5m",
                    value=_rounded(float(support["upper"])),
                    direction="above",
                    lookback_bars=6,
                ),
                condition(
                    "support-volume",
                    "同时间桶成交量或成交额比不低于1.2",
                    "volume_ratio",
                    "gte",
                    interval="5m",
                    value=1.2,
                    metric="confirmation_ratio",
                ),
            ],
        )
        append_scenario(
            client_rule_id="resistance-breakout-confirmation",
            kind="price_cross_above",
            threshold=float(resistance["upper"]),
            severity="warning",
            intent="breakout",
            level=2,
            candidate=resistance,
            label=f"{resistance.get('role') or 'R1'} 趋势突破确认",
            rationale="阻力上沿越过只是早期事实；连续两根已完成5分钟K线站上并通过量能门禁后，才确认有效突破并重算点位。",
            confirmation_count=2,
            action="observe",
            entry_conditions=[
                condition(
                    "breakout-entry",
                    "5分钟价格越过阻力区上沿",
                    "price_compare",
                    "gte",
                    interval="5m",
                    value=_rounded(float(resistance["upper"])),
                )
            ],
            confirmation_conditions=[
                condition(
                    "breakout-closes",
                    "连续两根已完成5分钟K线收于阻力区上沿之上",
                    "price_compare",
                    "gte",
                    interval="5m",
                    value=_rounded(float(resistance["upper"])),
                    consecutive=2,
                    lookback_bars=2,
                ),
                condition(
                    "breakout-volume",
                    "同时间桶成交量或成交额比不低于1.2",
                    "volume_ratio",
                    "gte",
                    interval="5m",
                    value=1.2,
                    metric="confirmation_ratio",
                ),
            ],
        )
        append_scenario(
            client_rule_id="support-invalidation",
            kind="price_cross_below",
            threshold=support_invalidation,
            severity="critical",
            intent="stop_loss",
            level=2,
            candidate=support,
            label=f"{support.get('role') or 'S1'} 结构风险防线",
            rationale="分钟级跌破只进入待确认并暂停新增买入；只有日线收盘跌破且量能门禁通过，才确认结构失效并允许生成减仓草稿。",
            confirmation_count=1,
            action="reduce",
            entry_conditions=[
                condition(
                    "risk-early-break",
                    "已完成5分钟K线跌破结构风险防线",
                    "price_compare",
                    "lte",
                    interval="5m",
                    value=_rounded(support_invalidation),
                )
            ],
            confirmation_conditions=[
                condition(
                    "risk-daily-close",
                    "日线收盘确认跌破结构风险防线",
                    "price_compare",
                    "lte",
                    interval="1d",
                    value=_rounded(support_invalidation),
                ),
                condition(
                    "risk-daily-volume",
                    "日线成交量相对前20日不低于1.2倍",
                    "rolling_volume_ratio",
                    "gte",
                    interval="1d",
                    value=1.2,
                    metric="volume",
                    lookback_bars=20,
                ),
            ],
        )
        resistance_is_actionable_r1 = bool(
            resistance.get("role") == "R1"
            and resistance.get("automation_status") == "action_ready"
        )
        append_scenario(
            client_rule_id="resistance-zone-test",
            kind="price_zone_enter",
            zone=(float(resistance["lower"]), float(resistance["upper"])),
            severity="info",
            intent="take_profit",
            level=1,
            candidate=resistance,
            label=f"{resistance.get('role') or 'R1'} 阻力测试",
            rationale="进入阻力区不是机械止盈；已完成K线转弱且量能确认后，才允许生成减仓草稿。",
            confirmation_count=1,
            action="reduce" if resistance_is_actionable_r1 else "observe",
            automation_status="action_ready" if resistance_is_actionable_r1 else "watch_only",
            entry_conditions=[
                condition(
                    "resistance-zone-entry",
                    "已完成5分钟K线进入阻力区",
                    "price_zone",
                    "between",
                    interval="5m",
                    lower=_rounded(float(resistance["lower"])),
                    upper=_rounded(float(resistance["upper"])),
                )
            ],
            confirmation_conditions=[
                condition(
                    "resistance-bearish",
                    "阻力测试后出现已完成5分钟阴线",
                    "bar_direction",
                    "equals",
                    interval="5m",
                    direction="bearish",
                ),
                condition(
                    "resistance-volume",
                    "同时间桶成交量或成交额比不低于1.2",
                    "volume_ratio",
                    "gte",
                    interval="5m",
                    value=1.2,
                    metric="confirmation_ratio",
                ),
            ],
        )
        evidence["rule_automation_status"] = {
            "resistance-breakout-confirmation": resistance.get("automation_status", "watch_only"),
            "resistance-zone-test": "action_ready" if resistance_is_actionable_r1 else "watch_only",
            "support-zone-test": "action_ready" if support_is_actionable_s1 else "watch_only",
            "support-invalidation": support.get("automation_status", "watch_only"),
        }
        evidence["target_ladder"] = {
            side: [
                {
                    "candidate_id": item.get("candidate_id"),
                    "role": item.get("role"),
                    "zone": [item.get("lower"), item.get("upper")],
                    "invalidation": (item.get("invalidation") or {}).get("value"),
                    "score": item.get("score"),
                    "confidence": item.get("confidence"),
                    "automation_status": item.get("automation_status"),
                    "method_families": item.get("method_families") or [],
                    "noise_gate": item.get("noise_gate") or {},
                }
                for item in list(ladder.get(side) or [])[:3]
            ]
            for side in ("support", "resistance")
        }
        price_volume_policy = {
            **DEFAULT_PRICE_VOLUME_POLICY,
            "baseline_sessions": 20,
            "min_samples": 10,
            "expansion_ratio": 1.2,
        }
        plan = {
            "schema_version": 5,
            "symbol": symbol,
            "data_mode": data_mode,
            "summary": "多层结构点位按观察、确认和失效原始周期分离；系统可生成非交易型建议，但不执行交易。",
            "quote_tier": "normal",
            "near_trigger_tier": "active",
            "near_trigger_distance_bps": 100,
            "price_volume_policy": price_volume_policy,
            "analysis_ref": {
                "snapshot_id": str(snapshot.get("level_snapshot_id") or _hash(snapshot))[:80],
                "report_ref": f"market-analysis://{symbol}/{snapshot.get('level_snapshot_id')}",
                "report_type": "monitor_research",
                "title": f"{symbol} 多方法结构监控快照",
                "revision": 1,
                "body_sha256": _hash(snapshot),
                "quality_status": "ready",
                "generated_at": now.isoformat(),
                "data_as_of": now.isoformat(),
            },
            "watch_scenarios": scenarios,
            "market_rules": rules,
            "news_topics": [],
            "fundamental_monitor": {
                "enabled": False,
                "capability_status": "unavailable_until_document_evidence_is_calibrated",
            },
            "hard_valid_until": hard_valid_until,
            "evidence_notes": [
                "所有价格均由确定性结构引擎计算，AI不能新增价格。",
                "进入区间属于观察事件；突破、跌破和反弹须通过已完成K线与量能门禁。",
                "成交量冲突只暂停确认，不关闭价格观察档案。",
            ],
            "automation_policy": {
                "activation_mode": "autonomous",
                "activated_by": "autopilot",
                "evidence_fingerprint": _hash(
                    {
                        "level_snapshot_id": snapshot.get("level_snapshot_id"),
                        "daily_tail_hash": evidence.get("daily_tail_hash"),
                        "intraday_tail_hash": evidence.get("intraday_tail_hash"),
                        "volume_signature": evidence.get("volume_signature"),
                    }
                ),
                "trade_execution": "forbidden",
                "trigger_type": "multi_method_level_monitor",
            },
        }
        return validate_plan(plan, expected_symbol=symbol), evidence, []

    def _build_legacy(
        self,
        holding: dict[str, Any],
        *,
        allow_single_source: bool = False,
    ) -> tuple[dict[str, Any] | None, dict[str, Any], list[str]]:
        symbol = normalize_symbol(str(holding.get("symbol") or holding.get("code") or "")).upper()
        quote = self._actionable_quote(symbol, allow_single_source=allow_single_source)
        blocked: list[str] = []
        if not quote:
            blocked.append("verified_quote_missing")
        elif quote.get("status") not in (
            {"verified", "single_source"} if allow_single_source else {"verified"}
        ):
            blocked.append(f"quote_not_actionable:{quote.get('status') or 'unknown'}")
        elif quote.get("adjustment") != "raw":
            blocked.append("raw_price_basis_unavailable")
        elif not quote.get("sources"):
            blocked.append("quote_provenance_missing")
        try:
            last_price = float((quote or {}).get("last_price"))
            if last_price <= 0:
                raise ValueError
        except (TypeError, ValueError):
            last_price = 0.0
            if "verified_quote_missing" not in blocked:
                blocked.append("verified_price_missing")

        daily = self.market_service.store.query_bars(
            symbol=symbol, interval="1D", adjustment="raw", view="consensus", limit=30
        )
        accepted_daily_statuses = (
            {"verified", "single_source"} if allow_single_source else {"verified"}
        )
        verified_daily = [
            row for row in daily
            if row.get("status") in accepted_daily_statuses and row.get("close") is not None
        ]
        data_mode = "single_source" if (quote or {}).get("status") == "single_source" else "verified"
        evidence = {
            "symbol": symbol,
            "holding": {
                "name": holding.get("name"),
                "quantity": holding.get("quantity"),
                "cost_price": holding.get("cost_price"),
                "updated_at": holding.get("updated_at"),
            },
            "quote": quote,
            "daily_bar_count": len(verified_daily),
            "daily_tail_hash": _hash(verified_daily[-30:]),
            "data_as_of": (quote or {}).get("bar_time"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "planner_mode": "evidence_policy",
            "data_mode": data_mode,
        }
        if data_mode == "single_source":
            evidence["single_source_consent"] = {
                "granted": bool(allow_single_source),
                "granted_at": datetime.now(timezone.utc).isoformat(),
            }
        if blocked:
            return None, evidence, blocked

        price_window = verified_daily[-20:]
        closes = [float(row["close"]) for row in price_window]
        highs = [float(row.get("high") or row["close"]) for row in price_window]
        lows = [float(row.get("low") or row["close"]) for row in price_window]
        if len(closes) >= 10:
            upper_index = max(range(len(highs)), key=highs.__getitem__)
            lower_index = min(range(len(lows)), key=lows.__getitem__)
            range_upper = highs[upper_index]
            range_lower = lows[lower_index]
            range_start = _bar_date(price_window[0])
            range_end = _bar_date(price_window[-1])
            upper_date = _bar_date(price_window[upper_index])
            lower_date = _bar_date(price_window[lower_index])
            returns = [abs(closes[index] / closes[index - 1] - 1) for index in range(1, len(closes)) if closes[index - 1]]
            noise = statistics.median(returns) if returns else 0.02
            buffer = max(0.005, min(noise * 0.5, 0.03))
            buffered_above = last_price * (1 + buffer)
            buffered_below = last_price * (1 - buffer)
            above = max(buffered_above, range_upper)
            below = min(buffered_below, range_lower)
            evidence["threshold_method"] = "20_session_range_with_noise_buffer"
            take_profit_basis = _calculation_basis(
                method="range_upper_with_noise_buffer",
                method_label="近20日震荡区间上沿 + 波动缓冲",
                formula="max(近20日最高价, 最新价 × (1 + 日波动缓冲))",
                summary=(
                    f"统计 {range_start} 至 {range_end} 的 {len(price_window)} 个交易日："
                    f"震荡区间上沿为 {_rounded(range_upper)}（{upper_date} 高点），"
                    f"最新价 {_rounded(last_price)} 按 {buffer * 100:.2f}% 日波动缓冲计算为 {_rounded(buffered_above)}；"
                    f"两者取较高值，得到 {_rounded(above)}。"
                ),
                recommended_value=above,
                references=[
                    {"label": "震荡区间上沿", "value": _rounded(range_upper), "date": upper_date},
                    {"label": "最新价", "value": _rounded(last_price)},
                    {"label": "日波动缓冲（%）", "value": round(buffer * 100, 4)},
                ],
            )
            stop_loss_basis = _calculation_basis(
                method="range_lower_with_noise_buffer",
                method_label="近20日震荡区间下沿 + 波动缓冲",
                formula="min(近20日最低价, 最新价 × (1 - 日波动缓冲))",
                summary=(
                    f"统计 {range_start} 至 {range_end} 的 {len(price_window)} 个交易日："
                    f"震荡区间下沿为 {_rounded(range_lower)}（{lower_date} 低点），"
                    f"最新价 {_rounded(last_price)} 按 {buffer * 100:.2f}% 日波动缓冲计算为 {_rounded(buffered_below)}；"
                    f"两者取较低值，得到 {_rounded(below)}。"
                ),
                recommended_value=below,
                references=[
                    {"label": "震荡区间下沿", "value": _rounded(range_lower), "date": lower_date},
                    {"label": "最新价", "value": _rounded(last_price)},
                    {"label": "日波动缓冲（%）", "value": round(buffer * 100, 4)},
                ],
            )
        else:
            above = last_price * 1.05
            below = last_price * 0.95
            evidence["threshold_method"] = "verified_quote_five_percent_review_band"
            evidence["limitations"] = ["insufficient_verified_daily_history"]
            take_profit_basis = _calculation_basis(
                method="verified_quote_percentage_band",
                method_label="已校核现价上方 5% 观察带",
                formula="最新价 × 1.05",
                summary=f"可用日线不足 10 根，暂以已校核最新价 {_rounded(last_price)} 上方 5% 计算，得到 {_rounded(above)}。",
                recommended_value=above,
                references=[{"label": "已校核最新价", "value": _rounded(last_price)}],
            )
            stop_loss_basis = _calculation_basis(
                method="verified_quote_percentage_band",
                method_label="已校核现价下方 5% 观察带",
                formula="最新价 × 0.95",
                summary=f"可用日线不足 10 根，暂以已校核最新价 {_rounded(last_price)} 下方 5% 计算，得到 {_rounded(below)}。",
                recommended_value=below,
                references=[{"label": "已校核最新价", "value": _rounded(last_price)}],
            )

        downside_step = max(last_price - below, last_price * 0.005)
        upside_step = max(above - last_price, last_price * 0.005)
        add_position_watch = last_price - downside_step * 0.5
        second_above = above + upside_step
        add_position_basis = _calculation_basis(
            method="current_to_stop_midpoint",
            method_label="现价与止损防线中点",
            formula="(最新价 + L2 止损点) ÷ 2",
            summary=(
                f"取已校核最新价 {_rounded(last_price)} 与 L2 止损防线 {_rounded(below)} 的中点，"
                f"得到 {_rounded(add_position_watch)}；这里只提醒重新评估，不代表自动买入。"
            ),
            recommended_value=add_position_watch,
            references=[
                {"label": "已校核最新价", "value": _rounded(last_price)},
                {"label": "L2 止损点", "value": _rounded(below)},
            ],
        )
        second_take_profit_basis = _calculation_basis(
            method="symmetric_target_extension",
            method_label="第一止盈目标等距延展",
            formula="L1 止盈点 + (L1 止盈点 - 最新价)",
            summary=(
                f"L1 止盈点 {_rounded(above)} 距最新价 {_rounded(last_price)} 为 {_rounded(upside_step)}，"
                f"向上等距延展后得到 L2 止盈点 {_rounded(second_above)}。"
            ),
            recommended_value=second_above,
            references=[
                {"label": "已校核最新价", "value": _rounded(last_price)},
                {"label": "L1 止盈点", "value": _rounded(above)},
                {"label": "延展距离", "value": _rounded(upside_step)},
            ],
        )
        evidence["target_ladder"] = {
            "downside": [
                {"intent": "add_position", "level": 1, "threshold": _rounded(add_position_watch)},
                {"intent": "stop_loss", "level": 2, "threshold": _rounded(below)},
            ],
            "upside": [
                {"intent": "take_profit", "level": 1, "threshold": _rounded(above)},
                {"intent": "take_profit", "level": 2, "threshold": _rounded(second_above)},
            ],
        }

        now = datetime.now(timezone.utc)
        # Activation requires enough review runway to avoid a freshly approved
        # plan expiring almost immediately.  Keep rule validity comfortably
        # inside the 30-day lower bound and the plan's 90-day hard stop.
        rule_valid_until = (now + timedelta(days=45)).isoformat()
        hard_valid_until = (now + timedelta(days=90)).isoformat()
        plan = {
            "schema_version": 3,
            "symbol": symbol,
            "data_mode": data_mode,
            "summary": "基于已校核原始行情生成四级价格目标草案：加仓观察、止损与两级止盈。阈值须由用户审核；系统只提醒，不执行交易。",
            "quote_tier": "normal",
            "near_trigger_tier": "active",
            "near_trigger_distance_bps": 100,
            "price_volume_policy": dict(DEFAULT_PRICE_VOLUME_POLICY),
            "market_rules": [
                {
                    "client_rule_id": "take-profit-level-1",
                    "kind": "price_cross_above",
                    "severity": "warning",
                    "enabled": True,
                    "target_intent": "take_profit",
                    "target_level": 1,
                    "parameters": {
                        "threshold": _rounded(above), "interval": "5m", "adjustment": "raw",
                        "confirmation_count": 2, "cooldown_minutes": 120, "clear_hysteresis_bps": 30,
                    },
                    "valid_until": rule_valid_until,
                    "rationale": "第一止盈观察点：价格持续突破近期已校核区间时提醒复核。",
                    "calculation_basis": take_profit_basis,
                },
                {
                    "client_rule_id": "take-profit-level-2",
                    "kind": "price_cross_above",
                    "severity": "warning",
                    "enabled": True,
                    "target_intent": "take_profit",
                    "target_level": 2,
                    "parameters": {
                        "threshold": _rounded(second_above), "interval": "5m", "adjustment": "raw",
                        "confirmation_count": 2, "cooldown_minutes": 120, "clear_hysteresis_bps": 30,
                    },
                    "valid_until": rule_valid_until,
                    "rationale": "第二止盈观察点：以第一目标到现价的风险距离向上延展，只作复核提醒。",
                    "calculation_basis": second_take_profit_basis,
                },
                {
                    "client_rule_id": "add-position-watch-level-1",
                    "kind": "price_cross_below",
                    "severity": "info",
                    "enabled": True,
                    "target_intent": "add_position",
                    "target_level": 1,
                    "parameters": {
                        "threshold": _rounded(add_position_watch), "interval": "5m", "adjustment": "raw",
                        "confirmation_count": 2, "cooldown_minutes": 120, "clear_hysteresis_bps": 30,
                    },
                    "valid_until": rule_valid_until,
                    "rationale": "加仓观察点：位于现价与止损防线之间，只提醒重新评估，不代表自动买入。",
                    "calculation_basis": add_position_basis,
                },
                {
                    "client_rule_id": "stop-loss-level-2",
                    "kind": "price_cross_below",
                    "severity": "critical",
                    "enabled": True,
                    "target_intent": "stop_loss",
                    "target_level": 2,
                    "parameters": {
                        "threshold": _rounded(below), "interval": "5m", "adjustment": "raw",
                        "confirmation_count": 2, "cooldown_minutes": 120, "clear_hysteresis_bps": 30,
                    },
                    "valid_until": rule_valid_until,
                    "rationale": "止损观察点：价格持续跌破近期已校核区间时发出高优先级复核提醒。",
                    "calculation_basis": stop_loss_basis,
                },
            ],
            "news_topics": [],
            "fundamental_monitor": {
                "enabled": False,
                "capability_status": "unavailable_until_document_evidence_is_calibrated",
            },
            "hard_valid_until": hard_valid_until,
            "evidence_notes": [
                "阈值来自可追溯行情，不是模型自由生成。",
                "加仓点仅为回撤复核位置，止盈与止损点均为提醒目标，不构成自动交易指令。",
                "新闻和基本面通道在数据契约完成校准前保持关闭。",
            ],
        }
        if data_mode == "single_source":
            plan["summary"] = "基于用户明确同意的单源原始行情生成观察草案；数据可能不准确，系统只提醒，不执行交易。"
            plan["evidence_notes"].append("当前仅有单一行情来源，可能不准确；恢复双源后系统会优先使用已校验数据。")
        return validate_plan(plan, expected_symbol=symbol), evidence, []
