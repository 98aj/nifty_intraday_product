from __future__ import annotations

from datetime import datetime
from pathlib import Path

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
