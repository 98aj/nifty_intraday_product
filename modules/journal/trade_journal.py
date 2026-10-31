"""Excel trade journal for paper and live option fills. No orders."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from io import BytesIO
from pathlib import Path

import pandas as pd
from openpyxl import Workbook, load_workbook
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
    "Drawdown",
    "Drawdown %",
    "Why sold",
    "Order id",
]

_EXIT_WORDS = {
    "PREMIUM_STOP": "Price fell to the stop",
    "PREMIUM_TARGET": "Price hit the target",
    "SQUARE_OFF_1500": "Sold at 3:00 pm",
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

    def table(self, capital: float | None = None) -> pd.DataFrame:
        rows, stored = self._read()
        used = stored if capital is None else float(capital)
        if rows and (used != stored or not self._excel_has_drawdown()):
            self._save(rows, used)
        return pd.DataFrame(_with_drawdown(rows, used), columns=COLUMNS)

    def _load(self) -> list[dict]:
        rows, _capital = self._read()
        return rows

    def _read(self) -> tuple[list[dict], float]:
        if not self.json_path.exists():
            return [], 0.0
        try:
            payload = json.loads(self.json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.exception("Trade journal could not be read")
            return [], 0.0
        if isinstance(payload, dict):
            rows = payload.get("trades")
            try:
                capital = float(payload.get("capital") or 0)
            except (TypeError, ValueError):
                capital = 0.0
        else:
            rows = payload
            capital = 0.0
        return [row for row in rows or [] if isinstance(row, dict)], capital

    def _save(self, rows: list[dict], capital: float | None = None) -> None:
        if capital is None:
            _rows, capital = self._read()
        self.json_path.write_text(
            json.dumps({"trades": rows, "capital": capital}, indent=2),
            encoding="utf-8",
        )
        try:
            self.xlsx_path.write_bytes(_workbook(_with_drawdown(rows, capital)))
        except OSError:
            LOGGER.exception("Excel journal is locked. The trade is saved and will be written when the file is free.")

    def _excel_has_drawdown(self) -> bool:
        if not self.xlsx_path.exists():
            return False
        try:
            book = load_workbook(self.xlsx_path, read_only=True, data_only=True)
            headers = [cell.value for cell in next(book.active.iter_rows(max_row=1))]
            book.close()
        except (OSError, StopIteration):
            return False
        return "Drawdown" in headers and "Drawdown %" in headers


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


def max_drawdown(sheet: pd.DataFrame, mode: str | None = None) -> tuple[float, float]:
    """Largest drop from the high, as rupees and the percentage at that same point.

    Pass ``mode`` ("Live" or "Paper") to read one book only. Paper and live trades
    never share an equity curve, so without a mode the worse of the two is returned.
    """

    if sheet.empty or "Drawdown" not in sheet.columns:
        return 0.0, 0.0
    if mode is not None and "Mode" in sheet.columns:
        sheet = sheet[sheet["Mode"] == mode]
        if sheet.empty:
            return 0.0, 0.0
    values = pd.to_numeric(sheet["Drawdown"], errors="coerce")
    percents = pd.to_numeric(sheet["Drawdown %"], errors="coerce")
    usable = values.notna()
    if not usable.any():
        return 0.0, 0.0
    values = values.where(usable, 0.0)
    percents = percents.where(usable, 0.0).fillna(0.0)
    worst = percents.idxmax()
    if float(percents.loc[worst] or 0) == 0:
        worst = values.idxmax()
    return round(float(values.loc[worst]), 2), round(float(percents.loc[worst]), 2)


def _with_drawdown(rows: list[dict], capital: float) -> list[dict]:
    """Rupee and percent drop from the highest account value after each closed trade.

    Paper and live rows each get their own equity curve starting from ``capital``.
    Closed trades are walked in the order they were sold, not the order they were
    bought, so overlapping positions do not scramble the path. The sheet keeps its
    original row order. With no capital the rupee drop is still shown but the
    percent is left blank, since there is no base to divide by.
    """

    shown = [{column: row.get(column, "") for column in COLUMNS} for row in rows]
    for item in shown:
        item["Drawdown"] = ""
        item["Drawdown %"] = ""

    closed: list[tuple[tuple[str, str, int], dict, float]] = []
    for index, item in enumerate(shown):
        if item.get("Status") != "Closed" or item.get("Result") == "":
            continue
        try:
            result = float(item.get("Result"))
        except (TypeError, ValueError):
            continue
        sold_at = (str(item.get("Date") or ""), str(item.get("Sold at") or ""), index)
        closed.append((sold_at, item, result))
    closed.sort(key=lambda entry: entry[0])

    start = float(capital) if capital > 0 else 0.0
    curves: dict[str, tuple[float, float]] = {}
    for _stamp_key, item, result in closed:
        mode = str(item.get("Mode") or "Paper")
        equity, peak = curves.get(mode, (start, start))
        equity += result
        peak = max(peak, equity)
        curves[mode] = (equity, peak)
        drop = peak - equity
        item["Drawdown"] = _number(round(drop, 2))
        item["Drawdown %"] = _number(round((drop / peak) * 100.0, 2)) if peak > 0 else ""
    return shown


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
        "Drawdown %": 14,
    }
    for index, name in enumerate(COLUMNS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = widths.get(name, 14)
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()
