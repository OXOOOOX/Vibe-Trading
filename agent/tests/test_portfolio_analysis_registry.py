from __future__ import annotations

from src.session.service import _PORTFOLIO_ANALYSIS_TOOL_NAMES


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
