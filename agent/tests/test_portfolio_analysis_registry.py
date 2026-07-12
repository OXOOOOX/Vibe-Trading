from __future__ import annotations

from src.session.service import _CHANNEL_RESEARCH_TOOL_NAMES, _PORTFOLIO_ANALYSIS_TOOL_NAMES


def test_portfolio_analysis_registry_excludes_order_execution_tools() -> None:
    allowed = set(_PORTFOLIO_ANALYSIS_TOOL_NAMES)

    assert {
        "portfolio_state",
        "publish_obsidian_note",
        "verified_market_data",
        "get_market_data",
        "get_stock_news",
        "web_search",
    } <= allowed
    assert not {"trading_place_order", "trading_cancel_order", "trading_account", "run_swarm", "write_file"} & allowed
    assert not any(name.startswith("trading_") for name in allowed)


def test_channel_research_registry_allows_backtests_but_excludes_remote_controls() -> None:
    allowed = set(_CHANNEL_RESEARCH_TOOL_NAMES)

    assert {
        "portfolio_state",
        "verified_market_data",
        "get_market_data",
        "backtest",
        "alpha_bench",
        "factor_analysis",
        "run_research_autopilot",
        "read_document",
        "analyze_image",
        "financial_rigor",
        "report_audit",
    } <= allowed
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
