"""Deterministic single-subject research context and proxy authorization."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from src.portfolio.state import load_state, normalize_symbol


_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?:\.(SH|SZ|BJ))?(?!\d)", re.I)
_PROXY_AUTH_RE = re.compile(
    r"(?:资金流|资金面|大单|申赎).{0,24}(?:可以|可|允许|用|作为).{0,12}(?:参考|对照)|"
    r"(?:可以|可|允许|用|作为).{0,12}(?:参考|对照).{0,24}(?:资金流|资金面|大单|申赎)",
    re.I,
)
_COMPARE_RE = re.compile(r"对比|比较|区别|差异|相比|versus|\bvs\.?\b", re.I)
_LIVE_RE = re.compile(r"现在|当前|最新|刚才|午盘|午间|下午|盘中|日高|最高|最低|昨收|现价", re.I)


@dataclass
class ResearchTurnContext:
    primary_symbol: str = ""
    security_name: str = ""
    proxy_relations: list[dict[str, Any]] = field(default_factory=list)
    allowed_data_symbols: list[str] = field(default_factory=list)
    portfolio_revision: int | None = None
    portfolio_snapshot: dict[str, Any] = field(default_factory=dict)
    live_required: bool = False
    request_content: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary_symbol": self.primary_symbol,
            "security_name": self.security_name,
            "proxy_relations": self.proxy_relations,
            "allowed_data_symbols": self.allowed_data_symbols,
            "portfolio_revision": self.portfolio_revision,
            "portfolio_snapshot": self.portfolio_snapshot,
            "live_required": self.live_required,
            "request_content": self.request_content,
        }

    def prompt_block(self) -> str:
        if not self.primary_symbol:
            return ""
        proxies = [
            {
                "symbol": item.get("symbol"),
                "role": item.get("role"),
                "restriction": "fund-flow reference only; never use for subject price, holding or P&L",
            }
            for item in self.proxy_relations
        ]
        return (
            "[RESEARCH_TURN_CONTEXT]\n"
            f"primary_symbol={self.primary_symbol}\n"
            f"security_name={self.security_name}\n"
            f"portfolio_revision={self.portfolio_revision}\n"
            f"portfolio_snapshot={self.portfolio_snapshot}\n"
            f"proxy_relations={proxies}\n"
            f"live_required={str(self.live_required).lower()}\n"
            "Rules: primary_symbol is the only subject for price, holdings, P&L and actions. "
            "A proxy may be used only for its declared role and every proxy figure must name "
            "the proxy symbol, metric family, source and as-of time. Never infer institutions "
            "or retail investors from order-size buckets. Unknown fees, taxes, dividends, dates "
            "and P&L must remain unknown.\n"
            "[/RESEARCH_TURN_CONTEXT]"
        )


def symbols_in_text(text: str) -> list[str]:
    result: list[str] = []
    for code, exchange in _CODE_RE.findall(str(text or "")):
        symbol = normalize_symbol(f"{code}.{exchange}" if exchange else code)
        if symbol and symbol not in result:
            result.append(symbol)
    return result


def update_proxy_authorizations(
    research_config: dict[str, Any],
    user_messages: Iterable[tuple[str, str]],
) -> bool:
    """Persist explicit fund-flow-reference consent found in user messages."""

    primary = normalize_symbol(
        str(research_config.get("symbol") or research_config.get("resolved_symbol") or "")
    )
    existing = [dict(item) for item in (research_config.get("proxy_relations") or []) if isinstance(item, dict)]
    known = {(str(item.get("symbol") or "").upper(), str(item.get("role") or "")) for item in existing}
    changed = False
    for message_id, content in user_messages:
        if not _PROXY_AUTH_RE.search(str(content or "")):
            continue
        for symbol in symbols_in_text(content):
            if not symbol or symbol == primary or (symbol, "fund_flow_reference") in known:
                continue
            existing.append(
                {
                    "symbol": symbol,
                    "role": "fund_flow_reference",
                    "authorized_by_message_id": message_id,
                }
            )
            known.add((symbol, "fund_flow_reference"))
            changed = True
    if changed:
        research_config["proxy_relations"] = existing
    return changed


def build_research_turn_context(
    session_config: dict[str, Any] | None,
    request_content: str,
) -> ResearchTurnContext:
    research = dict((session_config or {}).get("research_session") or {})
    primary = normalize_symbol(
        str(research.get("symbol") or research.get("resolved_symbol") or "")
    )
    if not primary:
        return ResearchTurnContext(request_content=request_content)
    state = load_state()
    primary_holding = next(
        (
            dict(item)
            for item in state.holdings
            if normalize_symbol(str(item.get("symbol") or item.get("code") or "")) == primary
        ),
        None,
    )
    explicit_symbols = symbols_in_text(request_content)
    allowed_data_symbols = [primary]
    if _COMPARE_RE.search(request_content):
        allowed_data_symbols.extend(symbol for symbol in explicit_symbols if symbol != primary)
    proxies = [
        dict(item)
        for item in (research.get("proxy_relations") or [])
        if isinstance(item, dict) and item.get("symbol")
    ]
    return ResearchTurnContext(
        primary_symbol=primary,
        security_name=str(research.get("security_name") or research.get("name") or ""),
        proxy_relations=proxies,
        allowed_data_symbols=list(dict.fromkeys(allowed_data_symbols)),
        portfolio_revision=state.revision,
        portfolio_snapshot={
            "revision": state.revision,
            "holding": primary_holding,
            "cash": state.cash,
            "cash_currency": state.cash_currency,
            "provenance": state.provenance,
        },
        live_required=bool(_LIVE_RE.search(request_content)),
        request_content=request_content,
    )
