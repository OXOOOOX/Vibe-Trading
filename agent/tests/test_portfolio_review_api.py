from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

import api_server


HOLDINGS_TEXT = """
科创50ETF汇添富 588870 7,300 1.956 2.167 15,819.10 +1,540.30 +10.77%
券商ETF银华 159842 16,800 1.161 1.095 18,396.00 -1,108.80 -5.68%
"""


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("VIBE_TRADING_PORTFOLIO_STATE_PATH", str(tmp_path / "portfolio_state.json"))
    monkeypatch.setenv("VIBE_TRADING_VERIFIED_MARKET_CACHE_DIR", str(tmp_path / "verified_cache"))
    monkeypatch.setenv("VIBE_TRADING_MARKET_CACHE_DB", str(tmp_path / "market_cache.sqlite3"))
    return TestClient(api_server.app, client=("127.0.0.1", 50000))


def test_portfolio_review_get_returns_empty_state(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    response = client.get("/portfolio/review")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["portfolio_state"]["holdings"] == []
    assert payload["verified_market_cache"] == []


def test_update_portfolio_holdings_parses_and_normalizes_symbols(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/portfolio/holdings",
        json={"raw_text": HOLDINGS_TEXT, "cash": 30000, "cash_currency": "CNY"},
    )

    assert response.status_code == 200
    state = response.json()["portfolio_state"]
    assert state["cash"] == 30000
    assert [row["symbol"] for row in state["holdings"]] == ["588870.SH", "159842.SZ"]


def test_update_portfolio_holdings_accepts_simple_table_and_infers_stocks(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/portfolio/holdings",
        json={
            "raw_text": """
名称 证券代码 持仓数量 成本价
科创50指 588870 2100 1.975
招商银行 个股无ETF 200 36.597
格力电器 个股无ETF 100 37.050
兴业银行 个股无ETF 300 18.000
"""
        },
    )

    assert response.status_code == 200
    holdings = response.json()["portfolio_state"]["holdings"]
    assert [row["symbol"] for row in holdings] == ["588870.SH", "600036.SH", "000651.SZ", "601166.SH"]
    assert [row["original_code_text"] for row in holdings if row.get("symbol_inferred")] == [
        "个股无ETF",
        "个股无ETF",
        "个股无ETF",
    ]


def test_update_portfolio_holdings_rejects_unparseable_text_without_overwrite(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    ok = client.post("/portfolio/holdings", json={"raw_text": HOLDINGS_TEXT})
    assert ok.status_code == 200

    bad = client.post("/portfolio/holdings", json={"raw_text": "this is not a holdings table"})

    assert bad.status_code == 400
    review = client.get("/portfolio/review").json()
    assert [row["symbol"] for row in review["portfolio_state"]["holdings"]] == ["588870.SH", "159842.SZ"]


def test_record_portfolio_trade_updates_holdings_and_prepends_recent_trade(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    seeded = client.post(
        "/portfolio/holdings",
        json={"raw_text": "科创50指 588870 2100 1.975\n券商ETF 159842 1300 0.750"},
    )
    assert seeded.status_code == 200

    first = client.post(
        "/portfolio/trades",
        json={"code": "588870", "symbol": "588870.SH", "name": "科创50指", "side": "buy", "quantity": 1000, "price": 2.1, "trade_date": "2026-07-01"},
    )
    second = client.post(
        "/portfolio/trades",
        json={"code": "159842", "symbol": "159842.SZ", "name": "券商ETF", "side": "sell", "quantity": 500, "price": 1.1, "trade_date": "2026-07-02"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    state = second.json()["portfolio_state"]
    trades = state["recent_trades"]
    assert trades[0]["symbol"] == "159842.SZ"
    assert trades[1]["symbol"] == "588870.SH"
    assert trades[0]["applied_to_holdings"] is True
    assert state["holdings"][0]["quantity"] == 3100
    assert round(state["holdings"][0]["cost_price"], 6) == round((2100 * 1.975 + 1000 * 2.1) / 3100, 6)
    assert state["holdings"][1]["quantity"] == 800
    assert state["holdings"][1]["cost_price"] == 0.75


def test_record_portfolio_trade_creates_and_closes_holding(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    bought = client.post(
        "/portfolio/trades",
        json={"code": "513120", "symbol": "513120.SH", "name": "HK创新药", "side": "buy", "quantity": 2000, "price": 1.178},
    )
    assert bought.status_code == 200
    holding = bought.json()["portfolio_state"]["holdings"][0]
    assert holding["symbol"] == "513120.SH"
    assert holding["quantity"] == 2000
    assert holding["cost_price"] == 1.178

    sold = client.post(
        "/portfolio/trades",
        json={"code": "513120", "symbol": "513120.SH", "name": "HK创新药", "side": "sell", "quantity": 2000, "price": 1.2},
    )
    assert sold.status_code == 200
    assert sold.json()["portfolio_state"]["holdings"] == []


def test_record_portfolio_trade_rejects_oversell_without_recording_trade(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    assert client.post("/portfolio/holdings", json={"raw_text": "科创50指 588870 100 1.975"}).status_code == 200

    response = client.post(
        "/portfolio/trades",
        json={"code": "588870", "symbol": "588870.SH", "name": "科创50指", "side": "sell", "quantity": 101, "price": 2.1},
    )

    assert response.status_code == 400
    state = client.get("/portfolio/review").json()["portfolio_state"]
    assert state["holdings"][0]["quantity"] == 100
    assert state["recent_trades"] == []


def test_record_portfolio_trade_requires_complete_resolved_security(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/portfolio/trades",
        json={"code": "588", "side": "buy", "quantity": 100, "price": 2.1},
    )

    assert response.status_code == 422


def test_lookup_portfolio_security_returns_exact_network_match(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    from src.tools import symbol_search_tool

    monkeypatch.setattr(
        symbol_search_tool,
        "lookup_exact_ashare",
        lambda code: ({"symbol": "588870.SH", "name": "科创50ETF汇添富", "market": "cn", "source": "eastmoney"}, "ok"),
    )

    response = client.get("/portfolio/security-lookup?code=588870")

    assert response.status_code == 200
    assert response.json() == {
        "code": "588870",
        "symbol": "588870.SH",
        "name": "科创50ETF汇添富",
        "market": "cn",
        "source": "eastmoney",
    }


def test_edit_portfolio_holding_updates_quantity_cost_and_derived_values(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    assert client.post("/portfolio/holdings", json={"raw_text": "科创50指 588870 100 1.975"}).status_code == 200

    from src.portfolio.state import load_state, save_state

    state = load_state()
    state.holdings[0]["last_price"] = 2.1
    save_state(state)
    response = client.patch(
        "/portfolio/holdings/588870.SH",
        json={"quantity": 250, "cost_price": 2.0},
    )

    assert response.status_code == 200
    holding = response.json()["portfolio_state"]["holdings"][0]
    assert holding["quantity"] == 250
    assert holding["cost_price"] == 2.0
    assert holding["market_value"] == 525.0
    assert round(holding["pnl"], 2) == 25.0


def test_delete_portfolio_trade_does_not_reverse_holding_change(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    assert client.post("/portfolio/holdings", json={"raw_text": "科创50指 588870 100 1.975"}).status_code == 200
    recorded = client.post(
        "/portfolio/trades",
        json={"code": "588870", "symbol": "588870.SH", "name": "科创50指", "side": "buy", "quantity": 50, "price": 2.1},
    )
    trade_id = recorded.json()["portfolio_state"]["recent_trades"][0]["trade_id"]
    assert recorded.json()["portfolio_state"]["holdings"][0]["quantity"] == 150

    deleted = client.delete(f"/portfolio/trades/{trade_id}")

    assert deleted.status_code == 200
    state = deleted.json()["portfolio_state"]
    assert state["recent_trades"] == []
    assert state["holdings"][0]["quantity"] == 150


def test_delete_legacy_trade_without_stored_id(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "portfolio_state.json"
    state_path.write_text(json.dumps({
        "holdings": [{"code": "588870", "symbol": "588870.SH", "name": "科创50指", "quantity": 150, "cost_price": 2.0}],
        "recent_trades": [{"code": "588870", "symbol": "588870.SH", "name": "科创50指", "side": "buy", "quantity": 50, "price": 2.1, "recorded_at": "2026-07-12T13:00:00Z"}],
        "cash": None,
        "cash_currency": "CNY",
    }, ensure_ascii=False), encoding="utf-8")
    client = _client(tmp_path, monkeypatch)
    trade_id = client.get("/portfolio/review").json()["portfolio_state"]["recent_trades"][0]["trade_id"]

    deleted = client.delete(f"/portfolio/trades/{trade_id}")

    assert deleted.status_code == 200
    assert deleted.json()["portfolio_state"]["recent_trades"] == []
    assert deleted.json()["portfolio_state"]["holdings"][0]["quantity"] == 150


def test_refresh_portfolio_market_data_updates_holdings_and_cache(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    ok = client.post("/portfolio/holdings", json={"raw_text": "科创50指 588870 2100 1.975\n券商ETF 159842 1300 0.750"})
    assert ok.status_code == 200

    def fake_fetcher(**kwargs):
        close = {"588870.SH": 2.1, "159842.SZ": 0.8}[kwargs["symbol"]]
        return {
            "requested_source": kwargs["requested_source"],
            "actual_source": kwargs["requested_source"],
            "adapter_name": "tests.fake",
            "source_fingerprint": kwargs["requested_source"],
            "requested_adjustment": kwargs["adjustment"],
            "actual_adjustment": kwargs["adjustment"],
            "adjustment_confidence": "test",
            "records": [{
                "trade_date": kwargs["end_date"], "open": close, "high": close,
                "low": close, "close": close, "volume": 100, "amount": close * 10_000,
            }],
        }

    from src.market_cache import get_market_refresh_service

    get_market_refresh_service().fetcher = fake_fetcher

    response = client.post("/portfolio/refresh-market-data", json={"start_date": "2026-07-01", "end_date": "2026-07-10"})

    assert response.status_code == 200
    payload = response.json()
    holdings = payload["portfolio_state"]["holdings"]
    assert holdings[0]["last_price"] == 2.1
    assert holdings[0]["market_value"] == 4410.0
    assert round(holdings[0]["pnl"], 2) == 262.5
    assert holdings[0]["market_status"] == "verified"
    assert payload["market_refresh"]["summary"]["updated_holdings"] == 2
    cache_rows = payload["verified_market_cache"]
    assert {row["symbol"] for row in cache_rows} == {"159842.SZ", "588870.SH"}
    assert {(row["interval"], row["actual_adjustment"]) for row in cache_rows} == {
        ("1m", "raw"), ("5m", "raw"), ("1D", "raw"), ("1D", "qfq"),
    }


def test_background_market_cache_refresh_reports_progress_and_query_endpoints(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    assert client.post("/portfolio/holdings", json={"raw_text": "科创50指 588870 2100 1.975"}).status_code == 200

    def fake_fetcher(**kwargs):
        time.sleep(0.01)
        return {
            "requested_source": kwargs["requested_source"],
            "actual_source": kwargs["requested_source"],
            "adapter_name": "tests.fake",
            "source_fingerprint": kwargs["requested_source"],
            "requested_adjustment": kwargs["adjustment"],
            "actual_adjustment": kwargs["adjustment"],
            "adjustment_confidence": "test",
            "records": [{
                "trade_date": kwargs["end_date"], "open": 2.1, "high": 2.1,
                "low": 2.1, "close": 2.1, "volume": 100, "amount": 21_000,
            }],
        }

    from src.market_cache import get_market_refresh_service

    get_market_refresh_service().fetcher = fake_fetcher
    accepted = client.post("/market-cache/refresh", json={})
    assert accepted.status_code == 202
    run_id = accepted.json()["run_id"]

    run = None
    for _ in range(100):
        run = client.get(f"/market-cache/runs/{run_id}").json()
        if run["status"] in {"completed", "partial", "failed"}:
            break
        time.sleep(0.02)
    assert run is not None and run["status"] == "completed"
    assert run["completed_items"] == run["total_items"] == 4

    assert client.get("/market-cache/quotes?symbols=588870.SH").json()["quotes"][0]["last_price"] == 2.1
    assert client.get("/market-cache/coverage?symbols=588870.SH").json()["coverage"]
    bars = client.get("/market-cache/bars?symbol=588870.SH&interval=1D&adjustment=raw").json()["bars"]
    assert bars[-1]["close"] == 2.1
