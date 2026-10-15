from __future__ import annotations

from datetime import datetime

from config.settings import IST
from modules.options.selector import (
    OptionQuote,
    build_order,
    choose_atm,
    entry_allowed,
    manage_exit,
    nearest_weekly_expiry,
    order_for_lots,
    parse_chain_rows,
    size_lots,
)


def test_nearest_weekly_expiry_skips_past_dates() -> None:
    rows = [
        {"expiry": "2026-10-06", "weekly": True},
        {"expiry": "2026-10-13", "weekly": True},
        {"expiry": "2026-10-27", "weekly": False},
    ]
    assert nearest_weekly_expiry(rows, datetime(2026, 10, 11).date()) == "2026-10-13"


def test_choose_atm_call_on_bullish_break_and_put_on_bearish() -> None:
    quotes = [
        OptionQuote("CE1", "NIFTY 22500 CE", "2026-10-13", 22500, "CE", 65, 120),
        OptionQuote("CE2", "NIFTY 22600 CE", "2026-10-13", 22600, "CE", 65, 80),
        OptionQuote("PE1", "NIFTY 22500 PE", "2026-10-13", 22500, "PE", 65, 110),
        OptionQuote("PE2", "NIFTY 22400 PE", "2026-10-13", 22400, "PE", 65, 70),
    ]
    call = choose_atm(quotes, 22520, "BUY")
    put = choose_atm(quotes, 22520, "SELL")
    assert call is not None and call.option_type == "CE" and call.strike == 22500
    assert put is not None and put.option_type == "PE" and put.strike == 22500


def test_size_lots_refuses_when_one_lot_exceeds_risk_budget() -> None:
    lots, cash, risk = size_lots(100_000, 0.5, premium=150, lot_size=65, stop_frac=0.4)
    assert lots == 0
    assert cash == 0
    assert risk == 0


def test_size_lots_buys_one_lot_when_capital_covers_stop() -> None:
    lots, cash, risk = size_lots(500_000, 1.0, premium=100, lot_size=65, stop_frac=0.4, daily_loss_cap=3000)
    assert lots == 1
    assert cash == 6500
    assert risk == 2600


def test_order_for_lots_uses_the_count_you_pick() -> None:
    contract = OptionQuote("K", "NIFTY 22500 CE", "2026-10-13", 22500, "CE", 65, 20)
    order, note = order_for_lots(contract, 2, 500_000, 0.4, 0.5)
    assert note == ""
    assert order is not None
    assert order.lots == 2
    assert order.quantity == 130
    assert order.cash_required == 2600
    assert order.risk_inr == 1040


def test_order_for_lots_refuses_when_the_stop_exceeds_the_cap() -> None:
    contract = OptionQuote("K", "NIFTY 22500 CE", "2026-10-13", 22500, "CE", 65, 100)
    order, note = order_for_lots(contract, 2, 500_000, 0.4, 0.5)
    assert order is None
    assert "3,000" in note


def test_build_order_sets_stop_and_target() -> None:
    contract = OptionQuote("K", "NIFTY 22500 CE", "2026-10-13", 22500, "CE", 65, 100)
    order = build_order(contract, 500_000, 1.0, 0.4, 0.5)
    assert order is not None
    assert order.quantity == 65
    assert order.stop_premium == 60
    assert order.target_premium == 150


def test_entry_blocked_on_weekend_and_inside_range() -> None:
    sunday = datetime(2026, 10, 11, 10, 0, tzinfo=IST)
    ok, why = entry_allowed(sunday, "BUY")
    assert ok is False
    assert "closed" in why.lower()
    monday = datetime(2026, 10, 12, 10, 15, tzinfo=IST)
    waiting, _ = entry_allowed(monday, "SCANNING")
    assert waiting is False
    allowed, _ = entry_allowed(monday, "BUY")
    assert allowed is True


def test_manage_exit_stop_target_and_flatten() -> None:
    morning = datetime(2026, 10, 12, 11, 0, tzinfo=IST)
    assert manage_exit(100, 59, 60, 150, morning) == "PREMIUM_STOP"
    assert manage_exit(100, 150, 60, 150, morning) == "PREMIUM_TARGET"
    assert manage_exit(100, 110, 60, 150, morning) is None
    late = datetime(2026, 10, 12, 15, 0, tzinfo=IST)
    assert manage_exit(100, 110, 60, 150, late) == "SQUARE_OFF_1500"


def test_parse_chain_reads_upstox_leg() -> None:
    chain = [
        {
            "strike_price": 22500,
            "call_options": {"instrument_key": "NSE_FO|1", "market_data": {"ltp": 88.5}},
            "put_options": {"instrument_key": "NSE_FO|2", "market_data": {"ltp": 77.0}},
        }
    ]
    quotes = parse_chain_rows(chain, "2026-10-13", lot_size=65)
    assert len(quotes) == 2
    assert {row.option_type for row in quotes} == {"CE", "PE"}
    assert quotes[0].lot_size == 65
