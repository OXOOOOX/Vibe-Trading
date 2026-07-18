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

    model_id = "evidence-policy-v3"

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
