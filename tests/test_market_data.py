from __future__ import annotations

from datetime import date

import pandas as pd

from config.settings import IST
from modules.upstox.market_data import get_historical_candles, resample_ohlcv


class _FakeClient:
    def get(self, path: str, params=None):
        if "intraday" in path:
            return {"data": {"candles": []}}
        candles = [
            ["2026-04-08T09:15:00+05:30", 100.0, 100.6, 99.8, 100.4, 1000, 0],
            ["2026-04-08T09:16:00+05:30", 100.4, 101.0, 100.2, 100.8, 1100, 0],
            ["2026-04-08T09:17:00+05:30", 100.8, 100.9, 100.1, 100.2, 900, 0],
            ["2026-04-08T09:20:00+05:30", 100.2, 100.5, 99.9, 100.1, 800, 0],
        ]
        return {"data": {"candles": candles}}


def test_five_minute_resample_is_ohlcv_correct() -> None:
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2026-04-08 09:15:00",
                    "2026-04-08 09:16:00",
                    "2026-04-08 09:17:00",
                    "2026-04-08 09:20:00",
                ]
            ).tz_localize(IST),
            "open": [100.0, 100.4, 100.8, 100.2],
            "high": [100.6, 101.0, 100.9, 100.5],
            "low": [99.8, 100.2, 100.1, 99.9],
            "close": [100.4, 100.8, 100.2, 100.1],
            "volume": [1000, 1100, 900, 800],
        }
    )
    out = resample_ohlcv(frame, "5min")
    first = out.iloc[0]
    assert first["open"] == 100.0
    assert first["high"] == 101.0
    assert first["low"] == 99.8
    assert first["close"] == 100.2
    assert first["volume"] == 3000


def test_get_historical_candles_normalizes_columns() -> None:
    df = get_historical_candles(
        "NSE_EQ|INE002A01018",
        interval="5minute",
        from_date=date(2026, 4, 8),
        to_date=date(2026, 4, 8),
        client=_FakeClient(),  # type: ignore[arg-type]
        include_today=False,
    )
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert not df.empty
    assert df["timestamp"].is_monotonic_increasing
