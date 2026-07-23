from __future__ import annotations

from src.session.service import (
    _CHANNEL_RESEARCH_TOOL_NAMES,
    _PORTFOLIO_ANALYSIS_TOOL_NAMES,
    _PORTFOLIO_DAILY_RUN_TOOL_NAMES,
    _PORTFOLIO_WEEKLY_RUN_TOOL_NAMES,
    _research_tool_names_for_session,
)


def test_portfolio_analysis_registry_excludes_order_execution_tools() -> None:
    allowed = set(_PORTFOLIO_ANALYSIS_TOOL_NAMES)

    assert {
        "portfolio_state",
        "publish_obsidian_note",
        "get_data_context",
        "web_search",
        "weekly_report",
    } <= allowed
    assert not {"verified_market_data", "get_market_data", "get_stock_news"} & allowed
    assert not {"trading_place_order", "trading_cancel_order", "trading_account", "run_swarm", "write_file"} & allowed
    assert not any(name.startswith("trading_") for name in allowed)


def test_channel_research_registry_allows_backtests_but_excludes_remote_controls() -> None:
    allowed = set(_CHANNEL_RESEARCH_TOOL_NAMES)

    assert {
        "portfolio_state",
        "get_data_context",
        "backtest",
        "alpha_bench",
        "factor_analysis",
        "run_research_autopilot",
        "read_document",
        "analyze_image",
        "financial_rigor",
        "report_audit",
    } <= allowed
    assert not {"verified_market_data", "get_market_data", "get_stock_news"} & allowed
    forbidden = {
        "trading_connections",
        "trading_select_connection",
        "trading_check",
        "trading_account",
        "trading_positions",
        "trading_orders",
        "trading_quote",
        "trading_history",
        "trading_place_order",
        "trading_cancel_order",
        "propose_mandate_profiles",
        "run_swarm",
        "bash",
        "write_file",
        "edit_file",
    }
    assert not forbidden & allowed
    assert not any(name.startswith("trading_") for name in allowed)


def test_portfolio_daily_run_registry_cannot_refetch_or_mutate_state() -> None:
    allowed = set(_PORTFOLIO_DAILY_RUN_TOOL_NAMES)

    assert allowed == {"load_skill"}
    assert not {"portfolio_state", "get_data_context", "write_file", "run_swarm"} & allowed


def test_portfolio_weekly_method_registry_is_frozen_input_only() -> None:
    allowed = set(_PORTFOLIO_WEEKLY_RUN_TOOL_NAMES)

    assert allowed == {"load_skill"}
    assert _research_tool_names_for_session(
        {"portfolio_weekly_run": {"research_only": True}}
    ) == ["load_skill"]


def test_channel_policy_wins_after_daily_report_session_is_rebound_to_feishu() -> None:
    allowed = set(
        _research_tool_names_for_session(
            {
                "portfolio_daily_run": {
                    "research_only": True,
                    "run_id": "dpr_today",
                    "symbol": "512680.SH",
                },
                "channel_policy": {
                    "research_only": True,
                    "allow_shell_tools": False,
                    "allow_trading_tools": False,
                },
            }
        )
        or []
    )

    assert {"portfolio_state", "get_data_context", "web_search", "read_url"} <= allowed
    assert not {"write_file", "run_swarm", "trading_place_order"} & allowed
