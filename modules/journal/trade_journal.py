"""Excel trade journal for paper and live option fills. No orders."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from io import BytesIO
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from config.settings import IST, PROJECT_ROOT

LOGGER = logging.getLogger("desk.journal")

JOURNAL_JSON = PROJECT_ROOT / "data" / "trade_journal.json"
JOURNAL_XLSX = PROJECT_ROOT / "data" / "trade_journal.xlsx"

COLUMNS = [
    "Date",
    "Mode",
    "Status",
    "Contract",
    "Call or put",
    "Strike",
    "Expiry",
    "Lots",
    "Quantity",
    "Bought at",
    "Buy price",
    "Sold at",
    "Sell price",
    "Stop",
    "Target",
    "Result",
    "Why sold",
    "Order id",
]

_EXIT_WORDS = {
    "PREMIUM_STOP": "Price fell to the stop",
    "PREMIUM_TARGET": "Price hit the target",
    "SQUARE_OFF_1515": "Sold at 3:15 pm",
    "EMERGENCY_CLOSE": "Closed by hand",
    "STOP": "Price fell to the stop",
    "FORCE_SQUARE_OFF_1500": "Sold at the cutoff",
}


class TradeJournal:
    """One row per option. A buy adds the row. A sell fills the rest of it."""

    def __init__(self, directory: Path | None = None) -> None:
        folder = directory or (PROJECT_ROOT / "data")
        folder.mkdir(parents=True, exist_ok=True)
        self.json_path = folder / "trade_journal.json"
        self.xlsx_path = folder / "trade_journal.xlsx"

    def record(self, position: dict) -> None:
        key = _key(position)
        if not key:
            return
        rows = self._load()
        built = _sheet_row(position)
        for existing in rows:
            if existing.get("_key") != key:
                continue
            if str(position.get("status") or "") == "CLOSED":
                existing.update(built)
                existing["_key"] = key
            self._save(rows)
            return
        rows.append({"_key": key, **built})
        self._save(rows)

    def backfill(self, positions: list[dict]) -> None:
        """Copy an existing book once, so older fills are not missing from the sheet."""

        if self.json_path.exists():
            return
        for position in positions:
            self.record(position)

    def table(self) -> pd.DataFrame:
        rows = [{column: row.get(column, "") for column in COLUMNS} for row in self._load()]
        return pd.DataFrame(rows, columns=COLUMNS)

    def _load(self) -> list[dict]:
        if not self.json_path.exists():
            return []
        try:
            payload = json.loads(self.json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.exception("Trade journal could not be read")
            return []
        rows = payload.get("trades") if isinstance(payload, dict) else payload
        return [row for row in rows or [] if isinstance(row, dict)]

    def _save(self, rows: list[dict]) -> None:
        self.json_path.write_text(
            json.dumps({"trades": rows}, indent=2),
            encoding="utf-8",
        )
        try:
            self.xlsx_path.write_bytes(_workbook(rows))
        except OSError:
            LOGGER.exception("Excel journal is locked. The trade is saved and will be written when the file is free.")


def _key(position: dict) -> str:
    stamp = str(position.get("Timestamp") or "")
    ticker = str(position.get("Ticker") or "")
    if not stamp or not ticker:
        return ""
    return f"{stamp}|{ticker}"


def _sheet_row(position: dict) -> dict:
    bought = _stamp(position.get("Timestamp"))
    closed = str(position.get("status") or "") == "CLOSED"
    sold = _stamp(position.get("ExitTimestamp")) if closed else None
    option = str(position.get("option_type") or "").upper()
    result = position.get("RealizedPL") if closed else ""
    return {
        "Date": bought.strftime("%Y-%m-%d") if bought else "",
        "Mode": "Live" if str(position.get("destination") or "").upper() == "LIVE" else "Paper",
        "Status": "Closed" if closed else "Open",
        "Contract": str(position.get("Ticker") or ""),
        "Call or put": "Call" if option == "CE" else "Put" if option == "PE" else "",
        "Strike": _number(position.get("strike")),
        "Expiry": str(position.get("expiry") or ""),
        "Lots": _number(position.get("lots")),
        "Quantity": _number(position.get("Quantity")),
        "Bought at": bought.strftime("%H:%M:%S") if bought else "",
        "Buy price": _number(position.get("ExecutionPrice")),
        "Sold at": sold.strftime("%H:%M:%S") if sold else "",
        "Sell price": _number(position.get("ExitPrice")) if closed else "",
        "Stop": _number(position.get("StopLoss")),
        "Target": _number(position.get("target_premium")),
        "Result": _number(result) if closed else "",
        "Why sold": _EXIT_WORDS.get(str(position.get("exit_reason") or ""), str(position.get("exit_reason") or "")),
        "Order id": str(position.get("live_order_id") or ""),
    }


def _stamp(value: object) -> datetime | None:
    text = str(value or "")
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return stamp
    return stamp.astimezone(IST)


def _number(value: object) -> float | int | str:
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number.is_integer():
        return int(number)
    return round(number, 2)


def _workbook(rows: list[dict]) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "Trades"
    sheet.append(COLUMNS)
    header_fill = PatternFill("solid", fgColor="1F4E79")
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
    for row in rows:
        sheet.append([row.get(column, "") for column in COLUMNS])
    sheet.auto_filter.ref = sheet.dimensions
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{max(sheet.max_row, 1)}"
    widths = {
        "Contract": 28,
        "Call or put": 12,
        "Why sold": 24,
        "Order id": 18,
        "Expiry": 14,
    }
    for index, name in enumerate(COLUMNS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = widths.get(name, 14)
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()
