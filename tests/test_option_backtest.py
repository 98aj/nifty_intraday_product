from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from config.settings import IST
from modules.options.backtest import replay_option_rule


def _bars(start: datetime, closes: list[float], highs: list[float] | None = None, lows: list[float] | None = None) -> pd.DataFrame:
    rows = []
    for i, close in enumerate(closes):
        high = close if highs is None else highs[i]
        low = close if lows is None else lows[i]
        rows.append(
            {
                "timestamp": start + timedelta(minutes=5 * i),
                "open": close,
                "high": max(high, close),
                "low": min(low, close),
                "close": close,
                "volume": 1000,
            }
        )
    return pd.DataFrame(rows)


def test_replay_buys_call_and_hits_target() -> None:
    start = datetime(2026, 10, 8, 9, 15, tzinfo=IST)
    index = _bars(
        start,
        [100, 100, 100, 102, 103, 104],
        highs=[100.4, 100.4, 100.4, 102, 103, 104],
        lows=[99.6, 99.6, 99.6, 101, 102, 103],
    )
    option = _bars(
        start,
        [10, 10, 10, 12, 16, 17],
        highs=[10, 10, 10, 12, 19, 17],
        lows=[10, 10, 10, 11, 12, 16],
    )
    book = replay_option_rule(
        index,
        {(100, "CE"): option, (100, "PE"): option},
        capital=500_000,
        risk_pct=1.0,
        stop_frac=0.4,
        target_frac=0.5,
        lot_size=65,
    )
    assert len(book) == 1
    assert book.iloc[0]["option"] == "CE"
    assert book.iloc[0]["exit_reason"] == "PREMIUM_TARGET"
    assert float(book.iloc[0]["rupee_pl"]) > 0
    assert int(book.iloc[0]["qty"]) >= 65
    pinned = replay_option_rule(
        index,
        {(100, "CE"): option, (100, "PE"): option},
        capital=500_000,
        risk_pct=1.0,
        stop_frac=0.4,
        target_frac=0.5,
        lot_size=65,
        lots=1,
    )
    assert int(pinned.iloc[0]["lots"]) == 1
    assert int(pinned.iloc[0]["qty"]) == 65
