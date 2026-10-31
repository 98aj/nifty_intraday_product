from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

from config.settings import IST
from modules.journal.trade_journal import TradeJournal, max_drawdown
from modules.risk.risk_manager import PaperTradingBroker


def _buy(destination: str = "PAPER") -> dict:
    return {
        "Timestamp": datetime(2026, 10, 12, 10, 5, tzinfo=IST).isoformat(),
        "Ticker": "NIFTY 22500 CE 13 OCT 26",
        "TransactionType": "BUY",
        "Quantity": 65,
        "ExecutionPrice": 100.0,
        "StopLoss": 60.0,
        "destination": destination,
        "live_order_id": "OID-1" if destination == "LIVE" else "",
        "status": "OPEN",
        "target_premium": 150.0,
        "option_type": "CE",
        "strike": 22500,
        "expiry": "2026-10-13",
        "lots": 1,
    }


def test_journal_adds_a_buy_then_fills_the_sell_on_the_same_row(tmp_path: Path) -> None:
    journal = TradeJournal(tmp_path)
    opened = _buy("LIVE")
    journal.record(opened)
    closed = dict(opened)
    closed.update(
        {
            "status": "CLOSED",
            "ExitPrice": 150.0,
            "ExitTimestamp": datetime(2026, 10, 12, 11, 0, tzinfo=IST).isoformat(),
            "RealizedPL": 3250.0,
            "exit_reason": "PREMIUM_TARGET",
        }
    )
    journal.record(closed)

    sheet = journal.table()
    assert len(sheet) == 1
    assert sheet.iloc[0]["Mode"] == "Live"
    assert sheet.iloc[0]["Status"] == "Closed"
    assert sheet.iloc[0]["Call or put"] == "Call"
    assert sheet.iloc[0]["Buy price"] == 100
    assert sheet.iloc[0]["Sell price"] == 150
    assert sheet.iloc[0]["Result"] == 3250
    assert sheet.iloc[0]["Why sold"] == "Price hit the target"

    book = load_workbook(tmp_path / "trade_journal.xlsx")
    assert book.active.title == "Trades"
    assert book.active["A1"].value == "Date"
    assert book.active.max_row == 2


def test_journal_keeps_paper_and_does_not_duplicate_an_open_row(tmp_path: Path) -> None:
    journal = TradeJournal(tmp_path)
    opened = _buy("PAPER")
    journal.record(opened)
    journal.record(opened)
    sheet = journal.table()
    assert len(sheet) == 1
    assert sheet.iloc[0]["Mode"] == "Paper"
    assert sheet.iloc[0]["Status"] == "Open"
    assert sheet.iloc[0]["Sell price"] == ""


def test_drawdown_is_the_drop_from_the_highest_account_value(tmp_path: Path) -> None:
    journal = TradeJournal(tmp_path)
    first = _buy("PAPER")
    journal.record(first)
    first_close = dict(first)
    first_close.update(
        {
            "status": "CLOSED",
            "ExitPrice": 150.0,
            "ExitTimestamp": datetime(2026, 10, 12, 11, 0, tzinfo=IST).isoformat(),
            "RealizedPL": 1000.0,
            "exit_reason": "PREMIUM_TARGET",
        }
    )
    journal.record(first_close)

    second = _buy("PAPER")
    second["Timestamp"] = datetime(2026, 10, 13, 10, 5, tzinfo=IST).isoformat()
    second["Ticker"] = "NIFTY 22600 CE 13 OCT 26"
    journal.record(second)
    second_close = dict(second)
    second_close.update(
        {
            "status": "CLOSED",
            "ExitPrice": 40.0,
            "ExitTimestamp": datetime(2026, 10, 13, 12, 0, tzinfo=IST).isoformat(),
            "RealizedPL": -400.0,
            "exit_reason": "PREMIUM_STOP",
        }
    )
    journal.record(second_close)

    sheet = journal.table(10_000)
    assert sheet.iloc[0]["Drawdown"] == 0
    assert sheet.iloc[0]["Drawdown %"] == 0
    assert sheet.iloc[1]["Drawdown"] == 400
    assert sheet.iloc[1]["Drawdown %"] == 3.64
    assert max_drawdown(sheet) == (400, 3.64)

    still_open = _buy("PAPER")
    still_open["Timestamp"] = datetime(2026, 10, 14, 10, 5, tzinfo=IST).isoformat()
    still_open["Ticker"] = "NIFTY 22700 CE 14 OCT 26"
    journal.record(still_open)
    opened = journal.table(10_000)
    assert opened.iloc[2]["Drawdown"] == ""
    assert opened.iloc[2]["Drawdown %"] == ""


def _closed(destination: str, day: int, hour: int, strike: int, pl: float) -> dict:
    buy = _buy(destination)
    buy["Timestamp"] = datetime(2026, 10, day, 10, 5, tzinfo=IST).isoformat()
    buy["Ticker"] = f"NIFTY {strike} CE {day:02d} OCT 26"
    buy["strike"] = strike
    close = dict(buy)
    close.update(
        {
            "status": "CLOSED",
            "ExitPrice": 40.0,
            "ExitTimestamp": datetime(2026, 10, day, hour, 0, tzinfo=IST).isoformat(),
            "RealizedPL": pl,
            "exit_reason": "PREMIUM_TARGET" if pl >= 0 else "PREMIUM_STOP",
        }
    )
    return close


def test_paper_and_live_drawdowns_are_tracked_on_separate_curves(tmp_path: Path) -> None:
    journal = TradeJournal(tmp_path)
    # Live book: +2000 then -1000, so a 1000 drop from an 12000 peak.
    journal.record(_closed("LIVE", 12, 11, 22500, 2000.0))
    journal.record(_closed("LIVE", 13, 11, 22600, -1000.0))
    # Paper book: a single -500 loss, a 500 drop from the 10000 start.
    journal.record(_closed("PAPER", 14, 11, 22700, -500.0))

    sheet = journal.table(10_000)
    assert max_drawdown(sheet, "Live") == (1000.0, 8.33)
    assert max_drawdown(sheet, "Paper") == (500.0, 5.0)
    # A paper loss must not bleed into the live curve, nor the reverse.
    assert max_drawdown(sheet) == (1000.0, 8.33)


def test_closed_trades_are_walked_in_sell_order_not_buy_order(tmp_path: Path) -> None:
    journal = TradeJournal(tmp_path)
    # Bought first but sold last, after a position bought later.
    journal.record(_closed("PAPER", 12, 15, 22500, -300.0))
    journal.record(_closed("PAPER", 12, 11, 22600, 1000.0))

    sheet = journal.table(10_000)
    # Sorted by sell time: +1000 (11:00) lifts the peak, then -300 (15:00) is the drop.
    by_sell = sheet.sort_values("Sold at")
    assert list(pd.to_numeric(by_sell["Drawdown"])) == [0, 300]
    assert max_drawdown(sheet) == (300.0, 2.73)


def test_no_capital_leaves_the_percent_blank_but_keeps_the_rupee_drop(tmp_path: Path) -> None:
    journal = TradeJournal(tmp_path)
    journal.record(_closed("PAPER", 12, 11, 22500, -400.0))

    sheet = journal.table(0)
    assert sheet.iloc[0]["Drawdown"] == 400
    assert sheet.iloc[0]["Drawdown %"] == ""
    assert max_drawdown(sheet) == (400.0, 0.0)


def test_broker_close_is_written_into_the_journal(tmp_path: Path) -> None:
    broker = PaperTradingBroker(tmp_path / "book.json")
    journal = TradeJournal(tmp_path)
    filled = broker.fill(
        "NIFTY 22500 PE",
        "BUY",
        65,
        80.0,
        48.0,
        destination="PAPER",
        extra={"option_type": "PE", "strike": 22500, "lots": 1, "target_premium": 120.0},
    )
    journal.record(filled)
    closed = broker.close_tickers({"NIFTY 22500 PE"}, {"NIFTY 22500 PE": 100.0}, "PREMIUM_TARGET")
    journal.record(closed[0])
    sheet = journal.table()
    assert sheet.iloc[0]["Status"] == "Closed"
    assert sheet.iloc[0]["Sell price"] == 100
    assert float(sheet.iloc[0]["Result"]) == 1300.0
