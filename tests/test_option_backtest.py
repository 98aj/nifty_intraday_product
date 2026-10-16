from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from config.settings import IST
from modules.options.backtest import refusal_counts, replay_option_rule


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


def test_round_trip_charges_one_and_a_half_rupee_each_side() -> None:
    start = datetime(2026, 10, 8, 9, 15, tzinfo=IST)
    index = _bars(
        start,
        [100, 100, 100, 102, 102, 102],
        highs=[100.4, 100.4, 100.4, 102, 102, 102],
        lows=[99.6, 99.6, 99.6, 101, 101, 101],
    )
    option = _bars(start, [10, 10, 10, 10, 10, 10])
    book = replay_option_rule(
        index,
        {(100, "CE"): option, (100, "PE"): option},
        capital=500_000,
        risk_pct=1.0,
        stop_frac=0.4,
        target_frac=0.6,
        lot_size=65,
        lots=1,
    )
    assert len(book) == 1
    assert book.iloc[0]["exit_reason"] == "SQUARE_OFF_1500"
    assert float(book.iloc[0]["rupee_pl"]) == -3.0 * 65


def test_exit_stays_inside_the_entry_day() -> None:
    start = datetime(2026, 10, 8, 9, 15, tzinfo=IST)
    index = pd.concat(
        [
            _bars(
                start,
                [100, 100, 100, 102, 102],
                highs=[100.4, 100.4, 100.4, 102, 102],
                lows=[99.6, 99.6, 99.6, 101, 101],
            ),
            _bars(start + timedelta(days=1), [100], highs=[100], lows=[99]),
        ],
        ignore_index=True,
    )
    option = pd.concat(
        [
            _bars(start, [10, 10, 10, 10, 10]),
            _bars(start + timedelta(days=1), [1], highs=[1], lows=[1]),
        ],
        ignore_index=True,
    )
    book = replay_option_rule(
        index,
        {(100, "CE"): option, (100, "PE"): option},
        capital=500_000,
        risk_pct=1.0,
        stop_frac=0.4,
        target_frac=0.6,
        lot_size=65,
        lots=1,
    )
    assert len(book) == 1
    assert book.iloc[0]["date"] == "2026-10-08"
    assert book.iloc[0]["exit_reason"] == "SQUARE_OFF_1500"
    assert book.iloc[0]["exit_time"] == "09:35"


def test_refusal_counts_when_the_stop_exceeds_the_cap() -> None:
    start = datetime(2026, 10, 8, 9, 15, tzinfo=IST)
    index = _bars(
        start,
        [100, 100, 100, 102, 102],
        highs=[100.4, 100.4, 100.4, 102, 102],
        lows=[99.6, 99.6, 99.6, 101, 101],
    )
    option = _bars(start, [200, 200, 200, 200, 200])
    tapes = {(100, "CE"): option, (100, "PE"): option}
    counts = refusal_counts(index, tapes, 500_000, 0.4, 65, 1)
    assert counts == {"priced": 1, "refused": 1}
    book = replay_option_rule(
        index, tapes, 500_000, 1.0, 0.4, 0.6, lot_size=65, lots=1
    )
    assert book.empty
