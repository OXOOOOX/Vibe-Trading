"""Deterministic guards for contradictions between portfolio tools and answers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


_EMPTY_PORTFOLIO_PATTERNS = (
    re.compile(r"持仓(?:状态|数据)?(?:已经|已)?清空"),
    re.compile(r"当前(?:没有|无)持仓"),
    re.compile(r"当前持仓(?:状态|数据)?(?:为|是)?空"),
    re.compile(r"未(?:能|成功)?(?:读取|获取|找到|提供).*持仓"),
    re.compile(r"无法(?:读取|获取|找到).*持仓"),
    re.compile(r"portfolio\s+(?:state\s+)?is\s+empty", re.IGNORECASE),
    re.compile(r"no\s+current\s+holdings", re.IGNORECASE),
    re.compile(r"no\s+holdings\s+(?:were\s+)?found", re.IGNORECASE),
)


@dataclass(frozen=True)
class PortfolioAnswerConflict:
    """A non-empty structured state contradicted by generated prose."""

    state: dict[str, Any]
    matched_text: str
    conflict_code: str = "portfolio_state_contradiction"
    primary_symbol: str = ""
    proxy_symbols: tuple[str, ...] = ()

    @property
    def holdings(self) -> list[dict[str, Any]]:
        rows = self.state.get("holdings") or []
        return [dict(row) for row in rows if isinstance(row, dict)]


_HOLDING_FACT_KEYS = (
    "name",
    "code",
    "symbol",
    "quantity",
    "cost_price",
    "last_price",
    "market_value",
    "pnl",
    "pnl_pct",
    "market_status",
    "market_verified_at",
    "updated_at",
    "cost_basis_kind",
    "lot_completeness",
    "provenance",
)
_TRADE_FACT_KEYS = (
    "trade_id",
    "name",
    "code",
    "symbol",
    "side",
    "quantity",
    "price",
    "trade_date",
    "recorded_at",
    "notes",
    "fees",
    "taxes",
    "broker_reported_pnl",
    "exactness",
    "event_status",
)


def compact_portfolio_tool_result(result: str, *, limit: int) -> str:
    """Keep portfolio tool JSON valid while removing verbose cache metadata."""
    try:
        payload = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return result[:limit]
    if not isinstance(payload, dict) or not isinstance(payload.get("state"), dict):
        return result[:limit]

    state = payload["state"]
    compact_state = {
        "holdings": [
            {key: row.get(key) for key in _HOLDING_FACT_KEYS if key in row}
            for row in (state.get("holdings") or [])
            if isinstance(row, dict)
        ],
        "recent_trades": [
            {key: row.get(key) for key in _TRADE_FACT_KEYS if key in row}
            for row in (state.get("recent_trades") or [])[:20]
            if isinstance(row, dict)
        ],
        "cash": state.get("cash"),
        "cash_currency": state.get("cash_currency"),
        "updated_at": state.get("updated_at"),
        "schema_version": state.get("schema_version"),
        "revision": state.get("revision"),
        "provenance": state.get("provenance"),
        "performance": state.get("performance"),
    }
    compact = json.dumps(
        {
            key: value
            for key, value in {
                "status": payload.get("status"),
                "path": payload.get("path"),
                "state": compact_state,
            }.items()
            if value is not None
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(compact) <= limit:
        return compact

    # Holdings are the non-negotiable facts. Drop journal history before ever
    # truncating the structured state into invalid JSON.
    compact_state["recent_trades"] = []
    compact = json.dumps(
        {"status": payload.get("status"), "path": payload.get("path"), "state": compact_state},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(compact) <= limit:
        return compact

    essential_holdings = [
        {
            key: row.get(key)
            for key in ("name", "symbol", "quantity", "cost_price")
            if key in row
        }
        for row in compact_state["holdings"]
    ]
    compact_state["holdings"] = essential_holdings
    compact = json.dumps(
        {"status": payload.get("status"), "path": payload.get("path"), "state": compact_state},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(compact) <= limit:
        return compact

    kept: list[dict[str, Any]] = []
    for row in essential_holdings:
        candidate_state = {
            **compact_state,
            "holdings": [*kept, row],
            "holdings_count": len(essential_holdings),
            "holdings_truncated": True,
        }
        candidate = json.dumps(
            {"status": payload.get("status"), "state": candidate_state},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(candidate) > limit:
            break
        kept.append(row)
    return json.dumps(
        {
            "status": payload.get("status"),
            "state": {
                **compact_state,
                "holdings": kept,
                "holdings_count": len(essential_holdings),
                "holdings_truncated": True,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def latest_portfolio_state(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the latest successful ``portfolio_state`` tool payload."""
    for message in reversed(messages):
        if message.get("role") != "tool" or message.get("name") != "portfolio_state":
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        try:
            payload = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("status") not in {None, "ok"}:
            continue
        state = payload.get("state")
        if isinstance(state, dict):
            return state
    return None


def find_portfolio_answer_conflict(
    messages: list[dict[str, Any]],
    answer: str,
    research_turn_context: dict[str, Any] | None = None,
) -> PortfolioAnswerConflict | None:
    """Detect portfolio, subject and fund-flow semantic contradictions."""
    state = latest_portfolio_state(messages)
    if state and (state.get("holdings") or []):
        for pattern in _EMPTY_PORTFOLIO_PATTERNS:
            match = pattern.search(answer or "")
            if match:
                return PortfolioAnswerConflict(state=state, matched_text=match.group(0))

    context = dict(research_turn_context or {})
    primary = str(context.get("primary_symbol") or "").upper()
    proxies = tuple(
        str(item.get("symbol") or "").upper()
        for item in (context.get("proxy_relations") or [])
        if isinstance(item, dict) and item.get("symbol")
    )
    text = str(answer or "")
    fallback_state = state or dict(context.get("portfolio_snapshot") or {})
    if primary and proxies:
        for proxy in proxies:
            code = proxy.split(".", 1)[0]
            if code not in text and proxy not in text.upper():
                continue
            price_or_holding = re.search(
                rf"(?:{re.escape(code)}|{re.escape(proxy)}).{{0,18}}(?:现价|价格|持仓|成本|盈亏|日高|昨收)|"
                rf"(?:现价|价格|持仓|成本|盈亏|日高|昨收).{{0,18}}(?:{re.escape(code)}|{re.escape(proxy)})",
                text,
                re.I,
            )
            if price_or_holding:
                return PortfolioAnswerConflict(
                    state=fallback_state,
                    matched_text=price_or_holding.group(0),
                    conflict_code="proxy_used_as_primary_fact",
                    primary_symbol=primary,
                    proxy_symbols=proxies,
                )
            if not re.search(r"二级市场|大单|订单档位|份额变化|申赎|参考|代理|proxy", text, re.I):
                return PortfolioAnswerConflict(
                    state=fallback_state,
                    matched_text=code,
                    conflict_code="unlabelled_proxy_metric",
                    primary_symbol=primary,
                    proxy_symbols=proxies,
                )
    actor_claim = re.search(
        r"(?:机构|主力|散户).{0,18}(?:清仓|出逃|接盘|流入|流出|买入|卖出)|"
        r"(?:清仓|出逃|接盘|流入|流出).{0,18}(?:机构|主力|散户)",
        text,
    )
    if actor_claim and any(
        message.get("role") == "tool" and message.get("name") == "get_fund_flow"
        for message in messages
    ):
        return PortfolioAnswerConflict(
            state=fallback_state,
            matched_text=actor_claim.group(0),
            conflict_code="unsupported_fund_flow_actor_attribution",
            primary_symbol=primary,
            proxy_symbols=proxies,
        )
    if context.get("live_required"):
        exact_live_claim = re.search(
            r"(?:现价|当前价格|最新价|日高|最高价|最低价|昨收)[^。；\n]{0,24}\d",
            text,
        )
        has_as_of = bool(
            re.search(r"(?:截至|数据时间|更新时间|as[_ -]?of).{0,16}(?:\d{1,2}:\d{2}|20\d{2}-\d{2}-\d{2})", text, re.I)
        )
        if exact_live_claim and not has_as_of:
            return PortfolioAnswerConflict(
                state=fallback_state,
                matched_text=exact_live_claim.group(0),
                conflict_code="exact_live_claim_missing_as_of",
                primary_symbol=primary,
                proxy_symbols=proxies,
            )
    return None


def _holding_lines(conflict: PortfolioAnswerConflict) -> list[str]:
    lines: list[str] = []
    for holding in conflict.holdings:
        symbol = str(holding.get("symbol") or holding.get("code") or "").strip()
        name = str(holding.get("name") or symbol or "未命名标的").strip()
        quantity = holding.get("quantity")
        cost = holding.get("cost_price")
        lines.append(f"- {name}（{symbol}）：数量 {quantity}，成本 {cost}")
    return lines


def build_portfolio_correction_prompt(conflict: PortfolioAnswerConflict) -> str:
    """Build a one-shot retry instruction grounded in the exact tool result."""
    if conflict.conflict_code != "portfolio_state_contradiction":
        return (
            "System fact validation rejected the previous draft. Rewrite the complete answer once. "
            f"conflict_code={conflict.conflict_code}; matched={conflict.matched_text!r}; "
            f"primary_symbol={conflict.primary_symbol}; proxy_symbols={list(conflict.proxy_symbols)}. "
            "Use the primary symbol for price, holdings, P&L and actions. A proxy figure must be "
            "labelled with proxy symbol, metric family, source and as-of time. Order-size flow "
            "cannot identify institutions or retail investors. Do not repeat the rejected claim."
        )
    snapshot = "\n".join(_holding_lines(conflict))
    return f"""系统事实校验发现你上一版草稿与刚才的工具结果冲突：你写了“{conflict.matched_text}”，但同一轮 portfolio_state(action="get") 已成功返回 {len(conflict.holdings)} 个真实持仓。

请丢弃上一版草稿并从头重写完整答案。以下结构化持仓是本轮唯一权威事实，不得称为“关注列表”，不得声称持仓为空、已清空、未提供或无法读取：
{snapshot}

重写时必须使用真实数量和成本；如果其它市场数据不足，只标记对应市场数据受限，不得把市场数据缺口说成持仓缺失。"""


def build_portfolio_conflict_fallback(conflict: PortfolioAnswerConflict) -> str:
    """Return a safe deterministic response if a retry still contradicts state."""
    if conflict.conflict_code != "portfolio_state_contradiction":
        primary = conflict.primary_symbol or "当前研究标的"
        return (
            "系统已拦截与研究标的或资金流口径冲突的回答，因此没有发送其中的价格、持仓或"
            "投资者身份推断。当前主标的是 "
            f"{primary}。请重新发起查询；系统将只使用带标的、指标口径、来源和时间的数据。"
        )
    rows = [
        "| 标的 | 代码 | 数量 | 成本 |",
        "|---|---:|---:|---:|",
    ]
    for holding in conflict.holdings:
        symbol = str(holding.get("symbol") or holding.get("code") or "").strip()
        name = str(holding.get("name") or symbol or "未命名标的").strip()
        rows.append(
            f"| {name} | {symbol} | {holding.get('quantity')} | {holding.get('cost_price')} |"
        )
    table = "\n".join(rows)
    return f"""## 系统已拦截错误分析

模型连续生成了与结构化持仓冲突的内容，因此本次错误分析没有发送。

`portfolio_state` 已成功读取当前 {len(conflict.holdings)} 个真实持仓，持仓并未清空：

{table}

请重新发起该研究任务；系统会继续以以上结构化持仓为唯一事实来源。"""
