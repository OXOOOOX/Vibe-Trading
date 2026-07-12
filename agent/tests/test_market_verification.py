"""Tests for multi-source verified market-data cache."""

from __future__ import annotations

import json

import pandas as pd

from src.market_verification import verify_market_data


def _fetcher_factory(closes: dict[str, float]):
    def fetcher(*, codes, start_date, end_date, source, interval="1D", max_rows=10):
        idx = pd.to_datetime([end_date])
        idx.name = "trade_date"
        return {
            codes[0]: [
                {
                    "trade_date": idx[0].isoformat(),
                    "close": closes[source],
                }
            ]
        }

    return fetcher


def test_verify_market_data_writes_verified_cache(tmp_path) -> None:
    out = verify_market_data(
        codes=["510300.SH"],
        start_date="2026-06-30",
        end_date="2026-07-01",
        sources=["tencent", "eastmoney"],
        adjustment="qfq",
        tolerance_pct=0.5,
        fetcher=_fetcher_factory({"tencent": 4.219, "eastmoney": 4.220}),
        cache_dir=tmp_path,
    )

    item = out["results"]["510300.SH"]
    assert item["status"] == "verified"
    assert item["spread_pct"] < 0.5
    assert item["requested_adjustment"] == "qfq"
    assert item["source_adjustments"]["tencent"]["adjustment"] == "qfq"
    cache_path = tmp_path / "510300.SH__adj-qfq.json"
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cached["observations"][0]["source"] == "tencent"
    assert cached["observations"][0]["adjustment"] == "qfq"


def test_verify_market_data_marks_conflicts(tmp_path) -> None:
    out = verify_market_data(
        codes=["159842.SZ"],
        start_date="2026-06-30",
        end_date="2026-07-01",
        sources=["tencent", "eastmoney"],
        tolerance_pct=0.5,
        fetcher=_fetcher_factory({"tencent": 1.095, "eastmoney": 1.130}),
        cache_dir=tmp_path,
    )

    item = out["results"]["159842.SZ"]
    assert item["status"] == "conflict"
    assert item["spread_pct"] > 0.5


def test_verify_market_data_marks_source_default_adjustment_for_etf_source(tmp_path) -> None:
    out = verify_market_data(
        codes=["159842.SZ"],
        start_date="2026-06-30",
        end_date="2026-07-01",
        sources=["akshare"],
        fetcher=_fetcher_factory({"akshare": 1.095}),
        cache_dir=tmp_path,
    )

    item = out["results"]["159842.SZ"]
    assert item["requested_adjustment"] == "source_default"
    assert item["source_adjustments"]["akshare"]["adjustment"] == "source_default"
    assert item["observations"][0]["adjustment_confidence"] == "loader_code"
