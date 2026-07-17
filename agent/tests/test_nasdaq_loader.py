from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from backtest.loaders.nasdaq_loader import (
    DataLoader,
    _chart_to_frame,
    _history_to_frame,
    _to_nasdaq_symbol,
)


def _chart() -> list[dict[str, float | int]]:
    timestamps = pd.date_range("2026-07-14 13:30:00+00:00", periods=6, freq="min")
    prices = [313.0, 313.2, 312.9, 313.1, 313.3, 313.4]
    return [
        {"x": int(timestamp.timestamp() * 1000), "y": price}
        for timestamp, price in zip(timestamps, prices, strict=True)
    ]


def test_to_nasdaq_symbol_accepts_only_project_us_symbols() -> None:
    assert _to_nasdaq_symbol("AAPL.US") == "AAPL"
    assert _to_nasdaq_symbol(" aapl.us ") == "AAPL"
    assert _to_nasdaq_symbol("00700.HK") is None


def test_chart_to_frame_builds_minute_price_observations() -> None:
    frame = _chart_to_frame(
        _chart(), start_date="2026-07-14", end_date="2026-07-14", interval="1m"
    )

    assert frame is not None
    assert len(frame) == 6
    assert frame.index.tz is not None
    assert frame.index[0].tz_convert("UTC").strftime("%H:%M") == "17:30"
    assert frame.iloc[-1]["close"] == 313.4
    assert frame["volume"].isna().all()


def test_chart_to_frame_aggregates_five_minute_prices() -> None:
    frame = _chart_to_frame(
        _chart(), start_date="2026-07-14", end_date="2026-07-14", interval="5m"
    )

    assert frame is not None
    assert len(frame) == 2
    assert frame.iloc[0].to_dict() == {
        "open": 313.0,
        "high": 313.3,
        "low": 312.9,
        "close": 313.3,
        "volume": None,
    }
    assert frame.iloc[1]["close"] == 313.4


def test_history_to_frame_parses_nasdaq_display_values() -> None:
    frame = _history_to_frame(
        [
            {
                "date": "07/13/2026", "close": "$317.31", "volume": "43,257,800",
                "open": "$317.015", "high": "$323.45", "low": "$315.78",
            }
        ],
        start_date="2026-07-01",
        end_date="2026-07-14",
    )

    assert frame is not None
    assert frame.iloc[0]["close"] == 317.31
    assert frame.iloc[0]["volume"] == 43_257_800


def test_loader_calls_nasdaq_chart_endpoint(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_DATA_CACHE", "0")
    payload = {"data": {"chart": _chart()}}
    with patch(
        "backtest.loaders.nasdaq_loader._get_json", return_value=payload
    ) as get_json:
        frame = DataLoader().fetch(
            ["AAPL.US"], "2026-07-14", "2026-07-14", interval="1m", strict=True
        )["AAPL.US"]

    assert len(frame) == 6
    assert get_json.call_args.kwargs["params"] == {"assetclass": "stocks"}


def test_loader_fetches_daily_history(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_DATA_CACHE", "0")
    payload = {
        "data": {
            "tradesTable": {
                "rows": [
                    {
                        "date": "07/13/2026", "close": "$317.31",
                        "volume": "43,257,800", "open": "$317.015",
                        "high": "$323.45", "low": "$315.78",
                    }
                ]
            }
        }
    }
    with patch("backtest.loaders.nasdaq_loader._get_json", return_value=payload) as get_json:
        frame = DataLoader().fetch(
            ["AAPL.US"], "2026-07-01", "2026-07-14", interval="1D", strict=True
        )["AAPL.US"]

    assert frame.iloc[0]["close"] == 317.31
    assert get_json.call_args.kwargs["params"]["fromdate"] == "2026-07-01"


def test_loader_rejects_unsupported_intervals() -> None:
    with pytest.raises(ValueError, match="only 1m, 5m, and 1D"):
        DataLoader().fetch(["AAPL.US"], "2026-07-14", "2026-07-14", interval="1H")
