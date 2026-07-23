"""Deterministic decision briefs and local-only condition-order drafts.

This module intentionally has no dependency on live trading or broker order
submission.  It converts already calculated monitoring evidence into a compact
user decision and, when configured, a non-executable local draft.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _choice(
    choice_id: str,
    label: str,
    description: str,
    *,
    recommended: bool = False,
    draft_type: str | None = None,
) -> dict[str, Any]:
    value = {
        "choice_id": choice_id,
        "label": label,
        "description": description,
        "recommended": recommended,
    }
    if draft_type:
        value["eligible_draft_type"] = draft_type
    return value


def validate_risk_preference(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("risk preference must be an object")
    holding_period = str(payload.get("holding_period") or "").strip()
    if holding_period not in {"short_term", "swing", "long_term"}:
        raise ValueError("holding_period must be short_term, swing, or long_term")
    permission = str(payload.get("condition_order_permission") or "only_alert")
    if permission not in {"only_alert", "local_draft", "broker_export"}:
        raise ValueError("condition_order_permission is not allowed")

    normalized: dict[str, Any] = {
        "holding_period": holding_period,
        "condition_order_permission": permission,
        "confirmation_intervals": [],
    }
    intervals = payload.get("confirmation_intervals") or ["5m", "1d"]
    if not isinstance(intervals, list) or not intervals:
        raise ValueError("confirmation_intervals must be a non-empty list")
    allowed_intervals = {"5m", "30m", "1d"}
    normalized["confirmation_intervals"] = list(
        dict.fromkeys(str(item).lower() for item in intervals)
    )
    if not set(normalized["confirmation_intervals"]).issubset(allowed_intervals):
        raise ValueError("confirmation_intervals contains an unsupported interval")

    numeric_rules = {
        "max_risk_amount": (0.0, None),
        "max_risk_pct": (0.0, 1.0),
        "max_add_amount": (0.0, None),
        "max_position_amount": (0.0, None),
        "minimum_reward_risk": (0.0, 20.0),
        "max_buy_price": (0.0, None),
        "min_sell_price": (0.0, None),
        "slippage_bps": (0.0, 1000.0),
        "sellable_quantity": (0.0, None),
        "intraday_added_quantity": (0.0, None),
        "default_reduce_fraction": (0.0, 1.0),
    }
    for field, (minimum, maximum) in numeric_rules.items():
        if payload.get(field) is None:
            continue
        value = _number(payload.get(field))
        if value is None or value < minimum or (maximum is not None and value > maximum):
            raise ValueError(f"{field} is outside the allowed range")
        normalized[field] = value
    valid_minutes = int(payload.get("draft_valid_minutes") or 30)
    if not 5 <= valid_minutes <= 10080:
        raise ValueError("draft_valid_minutes must be between 5 and 10080")
    normalized["draft_valid_minutes"] = valid_minutes
    normalized["configured_for_sizing"] = bool(
        normalized.get("max_risk_amount") or normalized.get("max_risk_pct")
    )
    return normalized


class DecisionEngine:
    """Build compact, auditable decisions without asking a model for numbers."""

    @staticmethod
    def _current_price(profile: dict[str, Any] | None, market_evidence: dict[str, Any]) -> float | None:
        quote = dict((profile or {}).get("last_quote") or {})
        payload = quote.get("payload") if isinstance(quote.get("payload"), dict) else quote
        return (
            _number((payload or {}).get("last_price"))
            or _number((market_evidence.get("quote") or {}).get("last_price"))
            or _number(market_evidence.get("last_price"))
        )

    @staticmethod
    def _active_state(profile: dict[str, Any] | None) -> tuple[str, dict[str, Any] | None]:
        episodes = list((profile or {}).get("watch_episodes") or [])
        episode = next(
            (
                item
                for item in episodes
                if str(item.get("state") or "") in {"approaching", "testing", "confirmed"}
            ),
            None,
        )
        if not episode:
            return "normal", None
        facts = episode.get("facts") if isinstance(episode.get("facts"), dict) else {}
        scenario = facts.get("scenario") if isinstance(facts.get("scenario"), dict) else {}
        rule_id = str(episode.get("client_rule_id") or scenario.get("client_rule_id") or "")
        state = str(episode.get("state") or episode.get("phase") or "")
        if state == "approaching":
            return "approaching", episode
        if state == "confirmed":
            if "support-invalidation" in rule_id:
                return "break_confirmed", episode
            if "support-zone" in rule_id:
                return "hold_confirmed", episode
            if "resistance-zone" in rule_id:
                return "resistance_rejected", episode
            if "breakout" in rule_id:
                return "valid_breakout", episode
        if "support-invalidation" in rule_id:
            return "break_pending", episode
        if "support-zone" in rule_id:
            return "testing_support", episode
        if "resistance-zone" in rule_id:
            return "testing_resistance", episode
        if "breakout" in rule_id:
            return "breakout_pending", episode
        return "testing", episode

    @staticmethod
    def _levels(snapshot: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        ladder = snapshot.get("level_ladder") if isinstance(snapshot.get("level_ladder"), dict) else {}
        return {
            side: [dict(item) for item in list(ladder.get(side) or [])[:3] if isinstance(item, dict)]
            for side in ("support", "resistance")
        }

    @staticmethod
    def _risk_preference_ready(preference: dict[str, Any] | None) -> bool:
        return bool(
            preference
            and preference.get("configured_for_sizing")
            and preference.get("condition_order_permission") in {"local_draft", "broker_export"}
        )

    def build(
        self,
        *,
        symbol: str,
        name: str,
        profile_status: str,
        blockers: list[dict[str, Any]],
        continuity: dict[str, Any],
        volume_gate: dict[str, Any],
        snapshot: dict[str, Any],
        market_evidence: dict[str, Any],
        profile: dict[str, Any] | None,
        holding: dict[str, Any],
        risk_preference: dict[str, Any] | None,
        latest_draft: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if blockers or profile_status == "blocked":
            state, episode = "blocked", None
        elif profile_status == "building":
            state, episode = "building", None
        else:
            state, episode = self._active_state(profile)
        price = self._current_price(profile, market_evidence)
        levels = self._levels(snapshot)
        preference_ready = self._risk_preference_ready(risk_preference)
        data_status = "blocked" if blockers else "partial" if str(volume_gate.get("status")) == "pending_evidence" else "verified"

        state_copy = {
            "blocked": ("数据受阻，结论暂不升级", "warning", "unknown", "等待新数据或有限重试"),
            "building": ("监控档案正在建立", "normal", "neutral", "继续自动监控"),
            "normal": ("结构未触发，继续监控", "normal", "neutral", "继续自动监控"),
            "approaching": ("正在接近关键结构区", "attention", "neutral", "继续观察"),
            "testing": ("关键结构正在测试", "attention", "neutral", "等待确认"),
            "testing_support": ("支撑测试中，尚未确认企稳", "medium", "downside", "等待确认"),
            "hold_confirmed": ("支撑与量价已确认", "attention", "upside", "生成加仓草稿" if preference_ready else "先完成风险设置"),
            "break_pending": ("分钟级跌破，等待日线确认", "high", "downside", "暂停新增买入"),
            "break_confirmed": ("结构失效已确认", "severe", "downside", "生成减仓草稿" if preference_ready else "先完成风险设置"),
            "testing_resistance": ("阻力测试中，尚未确认受阻", "medium", "downside", "等待确认"),
            "resistance_rejected": ("阻力受阻与量价转弱已确认", "high", "downside", "生成减仓草稿" if preference_ready else "先完成风险设置"),
            "breakout_pending": ("阻力上沿已越过，突破待确认", "attention", "upside", "等待两根完成K线与量能"),
            "valid_breakout": ("有效突破已确认", "attention", "upside", "继续持有并重算点位"),
        }
        headline, risk_level, risk_direction, recommended_label = state_copy.get(
            state, state_copy["normal"]
        )

        choices_by_state = {
            "blocked": [
                _choice("wait_for_data", "等待新数据", "保留价格观察，证据变化后自动恢复。", recommended=True),
                _choice("conservative_observe", "保守观察模式", "只记录价格事实，不输出确认动作。"),
                _choice("manual_retry", "明确重试", "写入一次人工重试审计，不清除既有失败。"),
            ],
            "building": [
                _choice("continue_monitoring", "继续自动监控", "等待档案按固定阶段完成。", recommended=True),
                _choice("adjust_risk_preferences", "设置风险偏好", "提前补齐草稿数量计算所需约束。"),
            ],
            "normal": [
                _choice("continue_monitoring", "继续自动监控", "没有行动级触发，保持监控。", recommended=True),
                _choice("adjust_risk_preferences", "调整风险偏好", "设置风险预算和条件单草稿权限。"),
                _choice("pause_target", "暂停该标的", "暂停新的监控判断，保留历史。"),
            ],
            "approaching": [
                _choice("continue_observing", "继续观察", "等待价格真正进入结构区。", recommended=True),
                _choice("prepare_playbook", "提前生成预案", "查看成立和失效两条路径。"),
                _choice("ignore_once", "本次忽略", "记录已知悉，抑制相同证据重复提醒。"),
            ],
            "testing_support": [
                _choice("wait_confirmation", "等待确认", "当前仍可能只是正常波动。", recommended=True),
                _choice("inspect_structural_risk", "查看结构风险", "展开下一支撑层和持仓影响。"),
                _choice("pause_add_alerts", "暂停新增买入", "保留风险监控但冻结机会草稿。"),
            ],
            "hold_confirmed": [
                _choice(
                    "generate_add_draft" if preference_ready else "adjust_risk_preferences",
                    "生成加仓草稿" if preference_ready else "完成风险设置",
                    "只生成本地草稿，不提交订单。" if preference_ready else "未设置风险预算时不计算数量。",
                    recommended=True,
                    draft_type="add" if preference_ready else None,
                ),
                _choice("continue_observing", "继续观察", "不生成草稿，等待更多证据。"),
                _choice("ignore_once", "本次不参与", "记录本次不参与。"),
            ],
            "break_pending": [
                _choice("pause_add_alerts", "暂停新增买入", "分钟跌破先冻结机会动作。", recommended=True),
                _choice("wait_daily_confirmation", "等待日线确认", "原始结构周期尚未确认失效。"),
                _choice("inspect_risk_plan", "查看风险预案", "查看确认失效后的持仓影响。"),
            ],
            "break_confirmed": [
                _choice(
                    "generate_reduce_draft" if preference_ready else "adjust_risk_preferences",
                    "生成减仓草稿" if preference_ready else "完成风险设置",
                    "只生成本地草稿，不提交订单。" if preference_ready else "需要可卖数量和减仓比例。",
                    recommended=True,
                    draft_type="reduce" if preference_ready else None,
                ),
                _choice("accept_risk_observe", "知悉风险并观察", "记录已知悉，避免相同证据反复提醒。"),
                _choice("close_scenario", "关闭本次情景", "关闭本轮事件，保留历史。"),
            ],
            "testing_resistance": [
                _choice("wait_confirmation", "等待确认", "进入阻力区不等同于止盈。", recommended=True),
                _choice("prepare_reduce_plan", "准备减仓预案", "先查看确认条件，不生成数量。"),
                _choice("ignore_once", "本次忽略", "记录已知悉。"),
            ],
            "resistance_rejected": [
                _choice(
                    "generate_reduce_draft" if preference_ready else "adjust_risk_preferences",
                    "生成减仓草稿" if preference_ready else "完成风险设置",
                    "只生成本地草稿，不提交订单。" if preference_ready else "需要可卖数量和减仓比例。",
                    recommended=True,
                    draft_type="reduce" if preference_ready else None,
                ),
                _choice("continue_observing", "继续持有观察", "承担回落风险并继续监控。"),
                _choice("ignore_once", "本次忽略", "记录已知悉。"),
            ],
            "breakout_pending": [
                _choice("wait_confirmation", "等待突破确认", "不因单次越线追涨。", recommended=True),
                _choice("inspect_structural_risk", "查看回落风险", "展开假突破失效条件。"),
            ],
            "valid_breakout": [
                _choice("continue_and_recalculate", "继续持有并重算", "旧阻力草稿作废并计算新结构。", recommended=True),
                _choice("review_tracking_plan", "查看追踪预案", "查看新的保护与观察条件。"),
                _choice("ignore_once", "不参与突破", "记录本次不参与。"),
            ],
        }
        choices = choices_by_state.get(state, choices_by_state["normal"])

        support = next((item for item in levels["support"] if item.get("role") == "S1"), None)
        next_support = next((item for item in levels["support"] if item.get("role") == "S2"), None)
        risk_line = _number(((support or {}).get("invalidation") or {}).get("value"))
        next_level = _number((next_support or support or {}).get("lower"))
        quantity = _number(holding.get("quantity"))
        impact_pct = None
        impact_amount = None
        if price and next_level and price > next_level:
            impact_pct = (price - next_level) / price
            impact_amount = impact_pct * price * quantity if quantity is not None else None
        impact_level = (
            "unknown" if impact_pct is None else "low" if impact_pct < 0.03 else "medium"
            if impact_pct < 0.07 else "high" if impact_pct < 0.12 else "severe"
        )
        reasons = [
            "点位按战术、结构支撑和风险防线分层，不以离现价最近作为结构最可靠。",
            "触达只是观察；机会或风险动作必须通过原始周期和量价确认。",
            "未完成风险设置时不会给出默认加减仓数量。",
        ]
        if state == "testing_support":
            reasons[0] = "价格已进入支撑区，但收复上沿和量价门禁尚未同时完成。"
        elif state == "break_pending":
            reasons[0] = "分钟级跌破已发生，但日线结构失效尚未确认。"
        elif state == "break_confirmed":
            reasons[0] = "日线收盘与量能条件均已满足，结构风险已升级。"
        elif state == "blocked":
            reasons[0] = "行情连续性、量能或建档证据仍有未解决阻塞。"

        evidence_fingerprint = _hash(
            {
                "symbol": symbol,
                "state": state,
                "snapshot": snapshot.get("level_snapshot_id"),
                "price": price,
                "episode": (episode or {}).get("episode_id"),
                "preference_revision": (risk_preference or {}).get("revision"),
                "blockers": blockers,
            }
        )
        decision_id = "decision-" + evidence_fingerprint[:24]
        decision_revision = 1
        next_confirmation = {
            "testing_support": "已完成5分钟K线收复S1上沿，且同时间桶量价比不低于1.2。",
            "break_pending": "日线收盘跌破结构风险线，且相对前20日量比不低于1.2。",
            "testing_resistance": "阻力区出现完成K线转弱，并通过量价门禁。",
            "breakout_pending": "连续两根完成5分钟K线站上R1上沿，并通过量价门禁。",
        }.get(state, "价格进入下一结构区或当前证据指纹发生实质变化。")
        invalidation_text = (
            f"日线重新收回 {risk_line:.2f} 上方后，当前失效判断需要重算。"
            if risk_line is not None
            else "连续性、点位快照或原始周期证据变化后重算。"
        )

        return {
            "decision_id": decision_id,
            "decision_revision": decision_revision,
            "evidence_fingerprint": evidence_fingerprint,
            "level_snapshot_id": snapshot.get("level_snapshot_id"),
            "selection_mode": "deterministic_state_machine",
            "market_state": state,
            "thesis_changed_at": (episode or {}).get("updated_at") or (profile or {}).get("updated_at"),
            "monitoring_thesis": {
                "symbol": symbol,
                "name": name,
                "state": state,
                "dominant_horizon": "structural" if state in {"break_pending", "break_confirmed"} else "multi_horizon",
                "supporting_evidence": reasons[:2],
                "counter_evidence": ["量价缺失或冲突会暂停确认。"] if data_status != "verified" else [],
                "waiting_for": next_confirmation,
            },
            "decision_brief": {
                "headline": headline,
                "market_state": state,
                "risk_level": risk_level,
                "risk_direction": risk_direction,
                "recommended_choice_id": choices[0]["choice_id"],
                "recommended_action": "observe" if "draft" not in choices[0]["choice_id"] else "prepare_draft",
                "summary": f"AI建议：{recommended_label}。触达点位本身不是买卖指令。",
                "why_now": reasons[:3],
                "counter_evidence": ["缺少完成K线或量价确认时，当前判断可能仍属于正常波动。"][:2],
                "next_confirmation": next_confirmation,
                "invalidation": invalidation_text,
                "data_status": data_status,
                "confidence": "low" if data_status == "blocked" else "medium" if data_status == "partial" else "high",
                "choices": choices,
            },
            "risk_assessment": {
                "risk_level": risk_level,
                "risk_direction": risk_direction,
                "risk_probability": None,
                "probability_status": "insufficient_calibrated_samples",
                "risk_impact": impact_level,
                "estimated_impact_pct": round(impact_pct, 6) if impact_pct is not None else None,
                "estimated_impact_amount": round(impact_amount, 2) if impact_amount is not None else None,
                "data_confidence": "low" if data_status == "blocked" else "medium" if data_status == "partial" else "high",
                "basis": {
                    "current_price": price,
                    "next_structural_level": next_level,
                    "holding_quantity": quantity,
                },
            },
            "level_ladder": levels,
            "action_playbook": {
                "do_now": recommended_label,
                "why": reasons[0],
                "if_holds": "结构与量价确认后，才评估机会草稿和收益风险比。",
                "if_breaks": "按原始结构周期确认失效后，才评估减仓草稿。",
                "do_not": "不要把进入区间直接解释成加仓或止损。",
                "review_deadline": "证据指纹变化或下一根相应周期K线完成时",
                "eligible_draft_types": [
                    item["eligible_draft_type"]
                    for item in choices
                    if item.get("eligible_draft_type")
                ],
            },
            "available_choices": choices,
            "risk_preference": risk_preference,
            "latest_draft": latest_draft,
            "scenario_comparison": {
                "base": "价格继续在当前结构区间内波动。",
                "positive": "支撑确认或阻力有效突破后重算结构。",
                "negative": "原始周期确认失效后风险升级。",
                "most_likely": "base" if state in {"normal", "approaching", "testing_support", "testing_resistance"} else "negative" if state in {"break_pending", "break_confirmed"} else "positive",
                "most_dangerous": "negative",
            },
        }

    def create_draft(
        self,
        *,
        decision: dict[str, Any],
        choice_id: str,
        risk_preference: dict[str, Any] | None,
        holding: dict[str, Any],
        cash: Any,
    ) -> dict[str, Any]:
        choices = {item["choice_id"]: item for item in decision.get("available_choices") or []}
        choice = choices.get(choice_id)
        if not choice or not choice.get("eligible_draft_type"):
            raise ValueError("the selected choice is not eligible for a condition-order draft")
        draft_type = str(choice["eligible_draft_type"])
        symbol = str((decision.get("monitoring_thesis") or {}).get("symbol") or "").upper()
        now = datetime.now(timezone.utc)
        preference = risk_preference or {}
        valid_until = now + timedelta(minutes=int(preference.get("draft_valid_minutes") or 30))
        levels = decision.get("level_ladder") or {}
        supports = [item for item in levels.get("support") or [] if isinstance(item, dict)]
        resistances = [item for item in levels.get("resistance") or [] if isinstance(item, dict)]
        support = next((item for item in supports if item.get("role") == "S1"), supports[0] if supports else {})
        resistance = next((item for item in resistances if item.get("role") == "R1"), resistances[0] if resistances else {})
        risk_line = _number((support.get("invalidation") or {}).get("value"))
        entry_price = _number(support.get("upper"))
        current_price = _number((decision.get("risk_assessment") or {}).get("basis", {}).get("current_price"))
        quantity: int | None = None
        formula: dict[str, Any] = {}
        status = "draft"
        side = "buy" if draft_type == "add" else "sell"
        missing: list[str] = []
        permission = str(preference.get("condition_order_permission") or "only_alert")
        if not preference.get("configured_for_sizing"):
            missing.append("risk_budget")
        if permission not in {"local_draft", "broker_export"}:
            missing.append("condition_order_permission")

        if side == "buy":
            if entry_price is None or risk_line is None or entry_price <= risk_line:
                missing.append("valid_entry_and_risk_line")
            available_cash = _number(cash)
            if available_cash is None:
                missing.append("available_cash")
            if not missing:
                risk_budget = _number(preference.get("max_risk_amount"))
                if risk_budget is None:
                    missing.append("absolute_risk_budget")
                else:
                    risk_cap = risk_budget / (entry_price - risk_line)
                    cash_cap = available_cash / entry_price
                    amount_cap = _number(preference.get("max_add_amount"))
                    add_cap = amount_cap / entry_price if amount_cap is not None else cash_cap
                    current_quantity = _number(holding.get("quantity")) or 0.0
                    current_value = current_quantity * (current_price or entry_price)
                    position_cap_amount = _number(preference.get("max_position_amount"))
                    position_cap = (
                        max(0.0, position_cap_amount - current_value) / entry_price
                        if position_cap_amount is not None
                        else cash_cap
                    )
                    raw_quantity = min(risk_cap, cash_cap, add_cap, position_cap)
                    quantity = int(math.floor(raw_quantity / 100) * 100)
                    reward = _number(resistance.get("lower"))
                    reward_risk = (
                        (reward - entry_price) / (entry_price - risk_line)
                        if reward is not None and reward > entry_price
                        else None
                    )
                    minimum_rr = _number(preference.get("minimum_reward_risk"))
                    if minimum_rr is not None and (reward_risk is None or reward_risk < minimum_rr):
                        quantity = None
                        status = "constraints_failed"
                        missing.append("minimum_reward_risk")
                    formula = {
                        "risk_quantity_cap": math.floor(risk_cap),
                        "cash_quantity_cap": math.floor(cash_cap),
                        "single_add_quantity_cap": math.floor(add_cap),
                        "position_quantity_cap": math.floor(position_cap),
                        "reward_risk": round(reward_risk, 4) if reward_risk is not None else None,
                        "lot_size": 100,
                    }
        else:
            sellable = _number(preference.get("sellable_quantity"))
            fraction = _number(preference.get("default_reduce_fraction"))
            if sellable is None:
                missing.append("sellable_quantity")
            if fraction is None or fraction <= 0:
                missing.append("default_reduce_fraction")
            if not missing:
                quantity = int(math.floor((sellable * fraction) / 100) * 100)
                formula = {
                    "sellable_quantity": sellable,
                    "reduce_fraction": fraction,
                    "intraday_added_quantity": _number(preference.get("intraday_added_quantity")),
                    "lot_size": 100,
                }
        if missing and status == "draft":
            status = "needs_risk_preferences"
            quantity = None
        if quantity == 0:
            quantity = None
            status = "constraints_failed"
            missing.append("minimum_board_lot")

        trigger = (
            {
                "entry": "5m price reclaims S1 upper after entering the support zone",
                "confirmation": "completed 5m bar and volume/turnover ratio >= 1.2",
                "price": entry_price,
                "interval": "5m",
            }
            if side == "buy"
            else {
                "entry": "5m early break only pauses new buying",
                "confirmation": "daily close below structural risk line and 20d volume ratio >= 1.2",
                "price": risk_line,
                "interval": "1d",
            }
        )
        tick_size = 0.001 if symbol.startswith(("15", "16", "50", "51", "52", "56", "58")) else 0.01
        return {
            "draft_id": uuid.uuid4().hex,
            "decision_id": decision["decision_id"],
            "decision_revision": decision["decision_revision"],
            "symbol": symbol,
            "side": side,
            "draft_type": draft_type,
            "status": status,
            "trigger": trigger,
            "limit_boundary": preference.get("max_buy_price") if side == "buy" else preference.get("min_sell_price"),
            "slippage_bps": preference.get("slippage_bps"),
            "quantity": quantity,
            "quantity_formula": formula,
            "missing_requirements": list(dict.fromkeys(missing)),
            "evidence_fingerprint": decision["evidence_fingerprint"],
            "level_snapshot_id": decision.get("level_snapshot_id"),
            "valid_until": valid_until.isoformat(),
            "automatic_cancellation": [
                "evidence_fingerprint_changed",
                "level_snapshot_changed",
                "risk_preference_changed",
                "valid_until_reached",
            ],
            "market_constraints": {
                "t_plus_one_checked_from_sellable_quantity": side == "sell",
                "lot_size": 100,
                "tick_size": tick_size,
                "price_limit_requires_fresh_quote_validation": True,
                "broker_compound_condition_supported": False,
            },
            "warnings": [
                "触发不保证成交。",
                "券商不能表达完整复合条件时，只保留应用内监控和本地草稿。",
                "该草稿不包含真实订单提交能力。",
            ],
            "trade_execution": "forbidden",
            "order_submission": "forbidden",
            "created_at": now.isoformat(),
        }
