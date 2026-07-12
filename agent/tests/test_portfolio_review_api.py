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


def test_record_portfolio_trade_prepends_recent_trade(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    first = client.post(
        "/portfolio/trades",
        json={"code": "588870", "side": "buy", "quantity": 1000, "price": 2.1, "trade_date": "2026-07-01"},
    )
    second = client.post(
        "/portfolio/trades",
        json={"symbol": "159842", "side": "sell", "quantity": 500, "price": 1.1, "trade_date": "2026-07-02"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    trades = second.json()["portfolio_state"]["recent_trades"]
    assert trades[0]["symbol"] == "159842.SZ"
    assert trades[1]["symbol"] == "588870.SH"


def test_record_portfolio_trade_requires_symbol_or_code(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    response = client.post("/portfolio/trades", json={"side": "buy", "quantity": 100})

    assert response.status_code == 400


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
