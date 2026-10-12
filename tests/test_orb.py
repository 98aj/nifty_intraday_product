from __future__ import annotations

from datetime import datetime

import pandas as pd

from config.settings import IST
from modules.strategy.orb import calculate_opening_range, evaluate_market_signals, stop_loss_for_signal


def _session_bars() -> pd.DataFrame:
    stamps = [
        "2026-04-08 09:15:00",
        "2026-04-08 09:20:00",
        "2026-04-08 09:25:00",
        "2026-04-08 09:30:00",
        "2026-04-08 09:35:00",
    ]
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(stamps).tz_localize(IST),
            "open": [100.0, 100.4, 100.6, 101.2, 101.4],
            "high": [100.5, 101.0, 100.8, 102.0, 101.8],
            "low": [99.0, 99.2, 98.5, 100.0, 101.0],
            "close": [100.4, 100.6, 100.7, 101.4, 101.5],
            "volume": [1000, 1100, 1200, 1300, 1400],
        }
    )


def test_opening_range_excludes_0930_bar() -> None:
    now = datetime(2026, 4, 8, 10, 0, tzinfo=IST)
    opening = calculate_opening_range(_session_bars(), now=now)
    assert opening.ready is True
    assert opening.high_boundary == 101.0
    assert opening.low_boundary == 98.5
    assert opening.bar_count == 3


def test_opening_range_not_ready_before_0930() -> None:
    now = datetime(2026, 4, 8, 9, 22, tzinfo=IST)
    early = _session_bars().iloc[:2]
    opening = calculate_opening_range(early, now=now)
    assert opening.ready is False
    assert opening.high_boundary == 101.0
    assert opening.low_boundary == 99.0


def test_breakout_signals_are_strict() -> None:
    assert evaluate_market_signals(101.01, 101.0, 98.5) == "BUY"
    assert evaluate_market_signals(98.49, 101.0, 98.5) == "SELL"
    assert evaluate_market_signals(101.0, 101.0, 98.5) == "SCANNING"
    assert evaluate_market_signals(98.5, 101.0, 98.5) == "SCANNING"
    assert evaluate_market_signals(99.9, 101.0, 98.5) == "SCANNING"
    assert evaluate_market_signals(0, 101.0, 98.5) == "SCANNING"


def test_stop_is_opposite_bound() -> None:
    assert stop_loss_for_signal("BUY", 101.0, 98.5) == 98.5
    assert stop_loss_for_signal("SELL", 101.0, 98.5) == 101.0
    assert stop_loss_for_signal("SCANNING", 101.0, 98.5) == 0.0
