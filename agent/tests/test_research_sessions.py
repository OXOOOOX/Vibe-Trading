from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from src.channels.research_sessions import (
    build_research_session_route,
    resolve_premarket_target_date,
)
from src.channels.registry import discover_channel_names


def _weekday(value: date) -> bool:
    return value.weekday() < 5


def test_research_session_helpers_are_not_discovered_as_a_channel() -> None:
    assert "research_sessions" not in discover_channel_names()


def test_symbol_routes_are_shared_between_holding_and_custom_code() -> None:
    holding = build_research_session_route(
        base_key="feishu:ou_user",
        action="holding",
        symbol="600036.SH",
        name="招商银行",
    )
    custom = build_research_session_route(
        base_key="feishu:ou_user",
        action="custom_stock",
        symbol="600036",
        name="招商银行",
    )

    assert holding.route_key == custom.route_key
    assert holding.route_key.endswith(":research:symbol:600036.SH")
    assert holding.metadata()["research_activate"] is True


def test_premarket_routes_roll_by_target_trading_day() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    monday_morning = datetime(2026, 7, 13, 8, 0, tzinfo=tz)
    monday_noon = datetime(2026, 7, 13, 12, 0, tzinfo=tz)
    friday_close = datetime(2026, 7, 17, 16, 0, tzinfo=tz)

    assert resolve_premarket_target_date(
        monday_morning, is_trading_day=_weekday
    ) == date(2026, 7, 13)
    assert resolve_premarket_target_date(
        monday_noon, is_trading_day=_weekday
    ) == date(2026, 7, 14)
    assert resolve_premarket_target_date(
        friday_close, is_trading_day=_weekday
    ) == date(2026, 7, 20)


def test_premarket_route_skips_exchange_holiday() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    sunday = datetime(2026, 7, 12, 10, 0, tzinfo=tz)

    def exchange_day(value: date) -> bool:
        return value.weekday() < 5 and value != date(2026, 7, 13)

    route = build_research_session_route(
        base_key="feishu:ou_user",
        action="premarket",
        now=sunday,
        is_trading_day=exchange_day,
    )

    assert route.target_date == "2026-07-14"
    assert route.route_key.endswith(":research:premarket:2026-07-14")
