from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from config.settings import IST
from modules.options.selector import listed_lot_size, lot_size_for, nearest_listed_expiry, nearest_weekly_expiry
from modules.strategy.banknifty_late import (
    calculate_session_range,
    central_pivot,
    evaluate_late_break,
    late_entry_allowed,
    previous_day_cpr,
)


def _bars(rows: list[tuple[str, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime([row[0] for row in rows]).tz_localize(IST),
            "open": [row[1] for row in rows],
            "high": [row[2] for row in rows],
            "low": [row[1] for row in rows],
            "close": [row[3] for row in rows],
            "volume": [1000] * len(rows),
        }
    )


def _day() -> pd.DataFrame:
    return _bars(
        [
            ("2026-10-15 09:15:00", 50000, 50100, 50040),
            ("2026-10-15 13:55:00", 50040, 50200, 50150),
            ("2026-10-15 14:00:00", 50150, 50350, 50300),
            ("2026-10-15 14:05:00", 50300, 50300, 49900),
            ("2026-10-15 14:40:00", 50100, 50000, 49900),
            ("2026-10-15 14:45:00", 49900, 49800, 49700),
        ]
    )


def test_session_range_excludes_the_1400_bar_and_waits_until_then() -> None:
    early = datetime(2026, 10, 15, 13, 59, tzinfo=IST)
    ready_at = datetime(2026, 10, 15, 14, 0, tzinfo=IST)
    building = calculate_session_range(_day(), now=early)
    opened = calculate_session_range(_day(), now=ready_at)
    assert building.ready is False
    assert opened.ready is True
    assert opened.high_boundary == 50200
    assert opened.low_boundary == 50000
    assert opened.bar_count == 2
    assert evaluate_late_break(_day(), opened.high_boundary, opened.low_boundary, now=early) == "SCANNING"


def test_wick_is_ignored_and_a_close_breaks() -> None:
    wick = _bars(
        [
            ("2026-10-15 09:15:00", 50000, 50100, 50040),
            ("2026-10-15 14:00:00", 50080, 50300, 50080),
        ]
    )
    closed = datetime(2026, 10, 15, 14, 5, tzinfo=IST)
    assert evaluate_late_break(wick, 50100, 50000, now=closed) == "SCANNING"
    broken = wick.copy()
    broken.loc[1, "close"] = 50200
    forming = datetime(2026, 10, 15, 14, 2, tzinfo=IST)
    assert evaluate_late_break(broken, 50100, 50000, now=forming) == "SCANNING"
    assert evaluate_late_break(broken, 50100, 50000, now=closed) == "BUY"


def test_first_close_wins_and_equality_stays_inside() -> None:
    session = calculate_session_range(_day(), now=datetime(2026, 10, 15, 14, 10, tzinfo=IST))
    later = datetime(2026, 10, 15, 14, 10, tzinfo=IST)
    assert evaluate_late_break(_day(), session.high_boundary, session.low_boundary, now=later) == "BUY"
    tied = _bars(
        [
            ("2026-10-15 09:15:00", 50000, 50100, 50040),
            ("2026-10-15 14:00:00", 50040, 50200, 50100),
        ]
    )
    assert evaluate_late_break(tied, 50100, 50000, now=datetime(2026, 10, 15, 14, 5, tzinfo=IST)) == "SCANNING"


def test_last_window_bar_is_1440_and_1445_is_ignored() -> None:
    only_late = _bars(
        [
            ("2026-10-15 09:15:00", 50000, 50100, 50040),
            ("2026-10-15 14:40:00", 50040, 50080, 50050),
            ("2026-10-15 14:45:00", 49900, 49800, 49000),
        ]
    )
    at_close = datetime(2026, 10, 15, 14, 50, tzinfo=IST)
    assert evaluate_late_break(only_late, 50100, 50000, now=at_close) == "SCANNING"
    only_late.loc[1, "close"] = 49900
    assert evaluate_late_break(only_late, 50100, 50000, now=datetime(2026, 10, 15, 14, 44, tzinfo=IST)) == "SCANNING"
    assert evaluate_late_break(only_late, 50100, 50000, now=datetime(2026, 10, 15, 14, 45, tzinfo=IST)) == "SELL"


def test_entry_window_is_1400_through_1445() -> None:
    monday = datetime(2026, 10, 12, 14, 10, tzinfo=IST)
    assert late_entry_allowed(monday, "BUY") == (True, "ok")
    building = datetime(2026, 10, 12, 13, 59, tzinfo=IST)
    allowed, why = late_entry_allowed(building, "BUY")
    assert allowed is False and "14:00" in why
    last_minute = datetime(2026, 10, 12, 14, 45, tzinfo=IST)
    assert late_entry_allowed(last_minute, "SELL")[0] is True
    closed = datetime(2026, 10, 12, 14, 46, tzinfo=IST)
    allowed, why = late_entry_allowed(closed, "BUY")
    assert allowed is False and "14:45" in why
    inside = datetime(2026, 10, 12, 14, 20, tzinfo=IST)
    assert late_entry_allowed(inside, "SCANNING")[0] is False
    sunday = datetime(2026, 10, 11, 14, 10, tzinfo=IST)
    assert late_entry_allowed(sunday, "BUY")[0] is False


def test_cpr_uses_the_previous_session_and_the_width_rule() -> None:
    narrow = central_pivot(100.0, 99.0, 99.2, "2026-10-14")
    wide = central_pivot(100.0, 98.0, 100.0, "2026-10-14")
    assert narrow.ready is True and narrow.narrow is True
    assert narrow.width_pct < 0.25
    assert wide.narrow is False and wide.width_pct > 0.25
    assert narrow.top >= narrow.bottom

    bars = _bars(
        [
            ("2026-10-14 09:15:00", 100.0, 100.0, 99.2),
            ("2026-10-14 15:20:00", 99.2, 100.0, 99.2),
            ("2026-10-15 09:15:00", 50.0, 80.0, 40.0),
        ]
    )
    # The 09:15 low is stored as the open in _bars. Set an explicit low of 99.
    bars.loc[0, "low"] = 99.0
    bars.loc[1, "low"] = 99.0
    cpr = previous_day_cpr(bars, now=datetime(2026, 10, 15, 14, 0, tzinfo=IST))
    assert cpr.ready is True
    assert cpr.session_date == "2026-10-14"
    assert cpr.narrow is True
    assert previous_day_cpr(bars.iloc[2:], now=datetime(2026, 10, 15, 14, 0, tzinfo=IST)).ready is False


def test_listed_expiry_ignores_weekly_and_refuses_a_missing_lot() -> None:
    rows = [
        {"expiry": "2026-10-27", "weekly": False, "lot_size": 30},
        {"expiry": "2026-11-03", "weekly": True, "lot_size": 65},
    ]
    today = date(2026, 10, 16)
    # Both pickers now take the genuinely nearest expiry. Preferring the
    # exchange "weekly" flag used to skip the nearer 2026-10-27 contract and
    # price a costlier, farther-dated option (the cause of the no-trade block).
    assert nearest_weekly_expiry(rows, today) == "2026-10-27"
    assert nearest_listed_expiry(rows, today) == "2026-10-27"
    assert listed_lot_size(rows, "2026-10-27") == 30
    assert listed_lot_size(rows, "2026-12-29") is None
    assert lot_size_for(rows, "2026-12-29") == 65


def test_banknifty_risk_is_separate_from_nifty() -> None:
    from modules.options.selector import OptionQuote, order_for_lots
    from modules.strategy.banknifty_late import MAX_LOSS_INR, PREMIUM_STOP_PCT, PREMIUM_TARGET_PCT

    assert (PREMIUM_STOP_PCT, PREMIUM_TARGET_PCT, MAX_LOSS_INR) == (25, 50, 6000.0)
    contract = OptionQuote("K", "BANKNIFTY 55800 CE", "2026-10-27", 55800, "CE", 30, 575.35)
    order, note = order_for_lots(
        contract,
        1,
        500_000,
        PREMIUM_STOP_PCT / 100,
        PREMIUM_TARGET_PCT / 100,
        daily_loss_cap=MAX_LOSS_INR,
    )
    assert note == ""
    assert order is not None
    assert order.stop_premium == round(575.35 * 0.75, 2)
    assert order.target_premium == round(575.35 * 1.50, 2)
    blocked, why = order_for_lots(contract, 1, 500_000, 0.40, 0.60, daily_loss_cap=3000)
    assert blocked is None
    assert "3,000" in why
