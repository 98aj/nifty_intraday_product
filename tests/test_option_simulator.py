from __future__ import annotations

from datetime import date, datetime, time

import pandas as pd

from config.settings import IST
from modules.options.backtest import replay_option_rule
from modules.options.simulator import (
    FALLBACK_VOL,
    RATE,
    SECONDS_PER_YEAR,
    bs_price,
    realized_vol,
    session_volatility,
    simulate_option_bars,
    weekly_expiry,
)


def _years_to_close(stamp: datetime, session: date) -> float:
    expiry = datetime.combine(weekly_expiry(session), time(15, 30), tzinfo=IST)
    return max((expiry - stamp).total_seconds() / SECONDS_PER_YEAR, 60.0 / SECONDS_PER_YEAR)


def test_call_rises_and_put_falls_with_nifty() -> None:
    t = 5 / 365.25
    strike = 24_000
    lower = 24_000
    higher = 24_200
    assert bs_price(higher, strike, t, 0.14, RATE, "CE") > bs_price(lower, strike, t, 0.14, RATE, "CE")
    assert bs_price(higher, strike, t, 0.14, RATE, "PE") < bs_price(lower, strike, t, 0.14, RATE, "PE")


def test_deep_in_the_money_call_is_near_intrinsic() -> None:
    price = bs_price(25_000, 20_000, 2 / 365.25, 0.14, RATE, "CE")
    assert abs(price - 5_000) / 5_000 < 0.02


def test_weekly_expiry_switches_from_thursday_to_tuesday() -> None:
    assert weekly_expiry(date(2025, 8, 28)) == date(2025, 8, 28)
    assert weekly_expiry(date(2025, 8, 29)) == date(2025, 9, 2)
    assert weekly_expiry(date(2026, 10, 12)) == date(2026, 10, 13)


def test_session_volatility_uses_the_prior_vix_close() -> None:
    bars = pd.DataFrame(
        [
            {"timestamp": datetime(2026, 10, 12, 15, 30, tzinfo=IST), "close": 12.0},
            {"timestamp": datetime(2026, 10, 13, 15, 30, tzinfo=IST), "close": 18.0},
        ]
    )
    vols = session_volatility(bars, [date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)])
    assert vols[date(2026, 10, 12)] == FALLBACK_VOL
    assert abs(vols[date(2026, 10, 13)] - 0.12) < 1e-9
    assert abs(vols[date(2026, 10, 14)] - 0.18) < 1e-9


def test_higher_vix_raises_the_call_premium() -> None:
    stamp = datetime(2026, 10, 13, 10, 0, tzinfo=IST)
    bars = pd.DataFrame(
        [{"timestamp": stamp, "open": 24_050, "high": 24_100, "low": 24_000, "close": 24_050, "volume": 1}]
    )
    calm = simulate_option_bars(bars, session_sigma={date(2026, 10, 13): 0.10})
    stressed = simulate_option_bars(bars, session_sigma={date(2026, 10, 13): 0.25})
    assert float(stressed[(24_050, "CE")].iloc[0]["close"]) > float(calm[(24_050, "CE")].iloc[0]["close"])


def test_short_history_uses_fallback_volatility() -> None:
    stamp = datetime(2026, 10, 12, 9, 15, tzinfo=IST)
    bars = pd.DataFrame(
        [{"timestamp": stamp, "open": 24_000, "high": 24_010, "low": 23_990, "close": 24_000, "volume": 1}]
    )
    assert realized_vol(bars, date(2026, 10, 12)) == FALLBACK_VOL


def test_synthetic_bars_keep_call_order_and_invert_puts() -> None:
    stamp = datetime(2026, 10, 12, 10, 0, tzinfo=IST)
    bars = pd.DataFrame(
        [
            {
                "timestamp": stamp,
                "open": 24_050,
                "high": 24_100,
                "low": 24_000,
                "close": 24_050,
                "volume": 1,
            }
        ]
    )
    tapes = simulate_option_bars(bars, sigma=0.15)
    t = _years_to_close(stamp, date(2026, 10, 12))
    strike = 24_050
    call = tapes[(strike, "CE")].iloc[0]
    put = tapes[(strike, "PE")].iloc[0]
    assert call["high"] >= call["close"] >= call["low"]
    assert put["high"] >= put["close"] >= put["low"]
    call_at_high = bs_price(24_100, strike, t, 0.15, RATE, "CE")
    call_at_low = bs_price(24_000, strike, t, 0.15, RATE, "CE")
    put_at_low = bs_price(24_000, strike, t, 0.15, RATE, "PE")
    put_at_high = bs_price(24_100, strike, t, 0.15, RATE, "PE")
    assert abs(float(call["high"]) - round(call_at_high, 2)) < 0.02
    assert abs(float(call["low"]) - round(call_at_low, 2)) < 0.02
    assert abs(float(put["high"]) - round(put_at_low, 2)) < 0.02
    assert abs(float(put["low"]) - round(put_at_high, 2)) < 0.02
    assert float(put["high"]) > float(put["low"])


def test_replay_buys_a_call_from_simulated_premiums() -> None:
    start = datetime(2026, 10, 12, 9, 15, tzinfo=IST)
    rows = []
    path = [
        (24_000, 24_020, 23_980, 24_000),
        (24_000, 24_020, 23_980, 24_000),
        (24_000, 24_020, 23_980, 24_000),
        (24_050, 24_150, 24_040, 24_120),
        (24_120, 24_450, 24_100, 24_400),
        (24_400, 24_500, 24_350, 24_450),
    ]
    for offset, (open_, high, low, close) in enumerate(path):
        rows.append(
            {
                "timestamp": start + pd.Timedelta(minutes=5 * offset),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": 1,
            }
        )
    index = pd.DataFrame(rows)
    tapes = simulate_option_bars(index, sigma=0.12)
    book = replay_option_rule(
        index,
        tapes,
        capital=500_000,
        risk_pct=1.0,
        stop_frac=0.4,
        target_frac=0.5,
        lot_size=65,
        lots=1,
    )
    assert len(book) == 1
    assert book.iloc[0]["option"] == "CE"
    assert float(book.iloc[0]["rupee_pl"]) != 0.0
