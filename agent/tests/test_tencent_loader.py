from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from backtest.loaders.tencent_loader import DataLoader


def _minute_response():
    payload = {
        "code": 0,
        "data": {
            "sh588870": {
                "data": {
                    "date": "20260710",
                    "data": [
                        "0930 2.100 10 2100.00",
                        "0931 2.110 15 3155.00",
                        "0934 2.090 20 4200.00",
                        "0935 2.120 30 6320.00",
                        "1501 2.120 30 6320.00",
                    ],
                }
            }
        },
    }
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
    return response


def test_fetch_intraday_keeps_incremental_volume_and_amount(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_DATA_CACHE", str(tmp_path / "loader-cache"))
    with patch("urllib.request.urlopen", return_value=_minute_response()):
        frame = DataLoader().fetch(
            ["588870.SH"], "2026-07-10", "2026-07-10",
            interval="1m", adjustment="raw",
        )["588870.SH"]

    assert list(frame["volume"]) == [10.0, 5.0, 5.0, 10.0]
    assert list(frame["amount"]) == [2100.0, 1055.0, 1045.0, 2120.0]
    assert frame.index[-1].strftime("%H:%M") == "09:35"


def test_fetch_intraday_aggregates_five_minute_bars(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_DATA_CACHE", str(tmp_path / "loader-cache"))
    with patch("urllib.request.urlopen", return_value=_minute_response()):
        frame = DataLoader().fetch(
            ["588870.SH"], "2026-07-10", "2026-07-10",
            interval="5m", adjustment="raw",
        )["588870.SH"]

    assert len(frame) == 2
    assert frame.iloc[0]["open"] == 2.1
    assert frame.iloc[0]["high"] == 2.11
    assert frame.iloc[0]["low"] == 2.09
    assert frame.iloc[0]["close"] == 2.09
    assert frame.iloc[0]["volume"] == 20.0
