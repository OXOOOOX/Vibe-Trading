"""Tests for structured portfolio state."""

from __future__ import annotations

import json

from src.portfolio.state import normalize_symbol, parse_holdings_text
from src.tools.portfolio_state_tool import PortfolioStateTool


HOLDINGS_TEXT = """
科创50ETF汇添富 588870 7,300 1.956 2.167 15,819.10 +1,540.30 +10.77%
券商ETF银华 159842 16,800 1.161 1.095 18,396.00 -1,108.80 -5.68%
沪深300ETF华泰柏瑞 510300 3,100 4.240 4.219 13,078.90 -65.10 -0.50%
格力电器 000651 500 44.535 41.900 20,950.00 -1,317.50 -5.92%
红利低波ETF华泰柏瑞 512890 18,700 1.034 1.082 20,233.40 +897.60 +4.64%
新能源ETF南方 516160 23,800 0.551 0.546 12,994.80 -119.00 -0.91%
招商银行 600036 600 44.760 46.100 27,660.00 +804.00 +2.99%
兴业银行 601166 1,000 20.482 20.950 20,950.00 +468.00 +2.28%
军工ETF广发 512680 11,300 1.177 1.166 13,175.80 -124.30 -0.93%
"""


def test_normalize_symbol_for_china_etf_and_stock_codes() -> None:
    assert normalize_symbol("588870") == "588870.SH"
    assert normalize_symbol("159842") == "159842.SZ"
    assert normalize_symbol("510300") == "510300.SH"
    assert normalize_symbol("000651") == "000651.SZ"
    assert normalize_symbol("600036") == "600036.SH"
    assert normalize_symbol("430139") == "430139.BJ"


def test_parse_holdings_text_preserves_exact_names_and_symbols() -> None:
    rows = parse_holdings_text(HOLDINGS_TEXT)

    assert [row["symbol"] for row in rows] == [
        "588870.SH",
        "159842.SZ",
        "510300.SH",
        "000651.SZ",
        "512890.SH",
        "516160.SH",
        "600036.SH",
        "601166.SH",
        "512680.SH",
    ]
    assert rows[0]["name"] == "科创50ETF汇添富"
    assert rows[0]["quantity"] == 7300.0
    assert rows[0]["cost_price"] == 1.956
    assert rows[0]["last_price"] == 2.167


def test_parse_simple_holdings_table_infers_known_stock_codes() -> None:
    rows = parse_holdings_text(
        """
名称 证券代码 持仓数量 成本价
科创50指 588870 2100 1.975
券商ETF 159842 1300 0.750
300ETF 510300 1000 4.710
招商银行 个股无ETF 200 36.597
格力电器 个股无ETF 100 37.050
1000基金 560010 2000 3.400
红利低波 512890 5000 1.120
HK创新药 513120 6600 1.171
兴业银行 个股无ETF 300 18.000
军工基金 512680 8000 1.315
"""
    )

    assert [row["symbol"] for row in rows] == [
        "588870.SH",
        "159842.SZ",
        "510300.SH",
        "600036.SH",
        "000651.SZ",
        "560010.SH",
        "512890.SH",
        "513120.SH",
        "601166.SH",
        "512680.SH",
    ]
    assert rows[0]["name"] == "科创50指"
    assert rows[0]["quantity"] == 2100.0
    assert rows[0]["cost_price"] == 1.975
    assert rows[0]["last_price"] is None
    inferred = [row for row in rows if row.get("symbol_inferred")]
    assert [row["symbol"] for row in inferred] == ["600036.SH", "000651.SZ", "601166.SH"]
    assert {row["original_code_text"] for row in inferred} == {"个股无ETF"}


def test_portfolio_state_tool_update_get_and_record_trade(tmp_path, monkeypatch) -> None:
    path = tmp_path / "portfolio_state.json"
    monkeypatch.setenv("VIBE_TRADING_PORTFOLIO_STATE_PATH", str(path))
    tool = PortfolioStateTool()

    update_payload = json.loads(
        tool.execute(action="update_holdings", raw_text=HOLDINGS_TEXT, cash=30000, cash_currency="CNY")
    )
    assert update_payload["status"] == "ok"
    assert update_payload["state"]["cash"] == 30000.0
    assert len(update_payload["state"]["holdings"]) == 9

    trade_payload = json.loads(
        tool.execute(
            action="record_trade",
            trade={"code": "588870", "symbol": "588870.SH", "name": "科创50ETF汇添富", "side": "sell", "quantity": 3600, "price": 2.16},
        )
    )
    assert trade_payload["state"]["recent_trades"][0]["symbol"] == "588870.SH"
    assert trade_payload["state"]["holdings"][0]["quantity"] == 3700.0

    get_payload = json.loads(tool.execute(action="get"))
    assert get_payload["state"]["holdings"][6]["symbol"] == "600036.SH"
    assert get_payload["state"]["recent_trades"][0]["side"] == "sell"
