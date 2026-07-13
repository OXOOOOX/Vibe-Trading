"""Stable research-topic routing for external chat channels."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Literal

from src.data_layer.prewarm import ChinaMarketCalendar
from src.portfolio.analysis import (
    MARKET_ANALYSIS_INTRADAY_CUTOFF,
    MARKET_ANALYSIS_TIMEZONE,
    current_market_analysis_time,
)
from src.portfolio.state import normalize_symbol

ResearchSessionKind = Literal["symbol", "portfolio", "premarket"]

RESEARCH_CONTROL_NEW_CONVERSATION = "new_conversation"
RESEARCH_CONTROL_REFRESH_REPORT = "refresh_report"

BOT_MENU_RESEARCH_HUB = "research_hub"
BOT_MENU_NEW_CONVERSATION = RESEARCH_CONTROL_NEW_CONVERSATION
BOT_MENU_REFRESH_REPORT = RESEARCH_CONTROL_REFRESH_REPORT

_CALENDAR = ChinaMarketCalendar()


def _cached_is_trading_day(value: date) -> bool:
    if value.weekday() >= 5:
        return False
    cached_days = getattr(_CALENDAR, "_days", set())
    return value.isoformat() in cached_days if cached_days else True


def warm_market_calendar() -> None:
    """Best-effort background refresh; routing never waits for this network call."""
    _CALENDAR.is_trading_day(datetime.now(MARKET_ANALYSIS_TIMEZONE).date())


@dataclass(frozen=True)
class ResearchSessionRoute:
    """Resolved persistent route for one research topic."""

    base_key: str
    route_key: str
    kind: ResearchSessionKind
    action: str
    label: str
    title: str
    symbol: str = ""
    target_date: str = ""

    def metadata(self) -> dict[str, str | bool]:
        return {
            "research_base_session_key": self.base_key,
            "research_route_key": self.route_key,
            "research_session_kind": self.kind,
            "research_session_title": self.title,
            "research_symbol": self.symbol,
            "research_target_date": self.target_date,
            "research_activate": True,
        }


def _as_market_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=MARKET_ANALYSIS_TIMEZONE)
    return value.astimezone(MARKET_ANALYSIS_TIMEZONE)


def resolve_premarket_target_date(
    now: datetime | None = None,
    *,
    is_trading_day: Callable[[date], bool] | None = None,
) -> date:
    """Resolve the market date represented by a newly requested premarket report."""

    local_now = _as_market_time(now or current_market_analysis_time())
    checker = is_trading_day or _cached_is_trading_day
    candidate = local_now.date()
    if local_now.time() >= MARKET_ANALYSIS_INTRADAY_CUTOFF or not checker(candidate):
        candidate += timedelta(days=1)
    for _ in range(14):
        if checker(candidate):
            return candidate
        candidate += timedelta(days=1)
    return candidate


def build_research_session_route(
    *,
    base_key: str,
    action: str,
    symbol: str = "",
    name: str = "",
    now: datetime | None = None,
    is_trading_day: Callable[[date], bool] | None = None,
) -> ResearchSessionRoute:
    """Build a stable topic key and friendly title for a research action."""

    if action == "premarket":
        target = resolve_premarket_target_date(now, is_trading_day=is_trading_day)
        target_text = target.isoformat()
        return ResearchSessionRoute(
            base_key=base_key,
            route_key=f"{base_key}:research:premarket:{target_text}",
            kind="premarket",
            action="premarket",
            label="盘前分析",
            title=f"飞书·盘前分析·{target_text}",
            target_date=target_text,
        )

    if action == "portfolio":
        return ResearchSessionRoute(
            base_key=base_key,
            route_key=f"{base_key}:research:portfolio",
            kind="portfolio",
            action="portfolio",
            label="持仓分析",
            title="飞书·持仓分析",
        )

    if action not in {"holding", "custom_stock"}:
        raise ValueError(f"unsupported research route action: {action}")
    normalized = normalize_symbol(symbol).upper()
    if not normalized:
        raise ValueError("a symbol is required for a stock research session")
    display_name = name.strip() or normalized
    return ResearchSessionRoute(
        base_key=base_key,
        route_key=f"{base_key}:research:symbol:{normalized}",
        kind="symbol",
        action=action,
        label=f"{display_name}个股分析",
        title=f"飞书·个股·{display_name}（{normalized}）",
        symbol=normalized,
    )


def friendly_route_name(base_key: str, route_key: str) -> str:
    """Return a compact user-facing label for a persisted route key."""

    if route_key == f"{base_key}:general" or route_key == base_key:
        return "通用对话"
    prefix = f"{base_key}:research:"
    suffix = route_key[len(prefix) :] if route_key.startswith(prefix) else route_key
    if suffix == "portfolio":
        return "持仓分析"
    if suffix.startswith("premarket:"):
        return f"盘前分析 {suffix.split(':', 1)[1]}"
    if suffix.startswith("symbol:"):
        return f"个股 {suffix.split(':', 1)[1]}"
    return suffix or "当前对话"
