"""Paper broker + hard risk interceptor sitting in front of every ticket.

Nothing in this module imports `modules.upstox.orders`. Live routing cannot
happen here. The interceptor is mandatory before `PaperTradingBroker.fill`.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from config.settings import IST, MAX_DAILY_LOSS_INR, PROJECT_ROOT, TRADE_CUTOFF

LOGGER = logging.getLogger(__name__)

Side = Literal["BUY", "SELL"]
PORTFOLIO_PATH = PROJECT_ROOT / "data" / "simulated_portfolio.json"
NIFTY2_PORTFOLIO_PATH = PROJECT_ROOT / "data" / "nifty2_portfolio.json"
BANKNIFTY_PORTFOLIO_PATH = PROJECT_ROOT / "data" / "banknifty_portfolio.json"
PORTFOLIO_FIELDS = (
    "Timestamp",
    "Ticker",
    "TransactionType",
    "Quantity",
    "ExecutionPrice",
    "StopLoss",
    "LiveUnrealizedPL",
)


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    code: str
    message: str

    def __bool__(self) -> bool:
        return self.allowed


class RiskInterceptor:
    """Absolute safety gates. All three limits are hardcoded."""

    MAX_DAILY_LOSS_INR = MAX_DAILY_LOSS_INR
    CUTOFF_IST = TRADE_CUTOFF

    def validate_trade_safety(
        self,
        ticker: str,
        quantity: int,
        entry_price: float,
        current_daily_loss: float,
        now: datetime | None = None,
    ) -> RiskDecision:
        try:
            qty = int(quantity)
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            return RiskDecision(False, "INVALID_POSITION_SIZING", "INVALID_POSITION_SIZING")

        try:
            loss = float(current_daily_loss)
        except (TypeError, ValueError):
            loss = 0.0
        if loss > self.MAX_DAILY_LOSS_INR:
            LOGGER.error("Blocked %s: daily loss %.2f exceeds %.2f", ticker, loss, self.MAX_DAILY_LOSS_INR)
            return RiskDecision(False, "DAILY_LOSS_LIMIT_EXCEEDED", "DAILY_LOSS_LIMIT_EXCEEDED")

        clock = now or datetime.now(IST)
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=IST)
        else:
            clock = clock.astimezone(IST)
        if clock.timetz().replace(tzinfo=None) >= self.CUTOFF_IST:
            LOGGER.error("Blocked %s after cutoff %s", ticker, self.CUTOFF_IST.isoformat())
            return RiskDecision(False, "TRADE_RESTRICTED_NEAR_MARKET_CLOSE", "TRADE_RESTRICTED_NEAR_MARKET_CLOSE")

        try:
            price = float(entry_price)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            return RiskDecision(False, "INVALID_POSITION_SIZING", "INVALID_POSITION_SIZING")

        return RiskDecision(True, "OK", "OK")


class PaperTradingBroker:
    """Local MIS ledger written to `data/simulated_portfolio.json`."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or PORTFOLIO_PATH
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def fill(
        self,
        ticker: str,
        side: Side,
        quantity: int,
        execution_price: float,
        stop_loss: float,
        instrument_token: str = "",
        destination: str = "PAPER",
        live_order_id: str = "",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = {
            "Timestamp": datetime.now(IST).isoformat(),
            "Ticker": ticker,
            "TransactionType": side,
            "Quantity": int(quantity),
            "ExecutionPrice": float(execution_price),
            "StopLoss": float(stop_loss),
            "LiveUnrealizedPL": 0.0,
            "instrument_token": instrument_token,
            "destination": destination,
            "live_order_id": live_order_id,
            "status": "OPEN",
        }
        for key, value in (extra or {}).items():
            if key not in row:
                row[key] = value
        with self._lock:
            book = self._read()
            book["positions"].append(row)
            book["updated_at"] = datetime.now(IST).isoformat()
            self._write(book)
        LOGGER.info("PAPER %s %s qty=%s @ %.2f sl=%.2f", side, ticker, quantity, execution_price, stop_loss)
        return row

    def emergency_close(self, ltp_by_ticker: dict[str, float] | None = None) -> int:
        """Flatten every open row into the journal. Returns how many were closed."""

        return self.force_square_off(ltp_by_ticker, reason="EMERGENCY_CLOSE")

    def close_at_stops(self, ltp_by_ticker: dict[str, float] | None = None) -> int:
        """Close any open row whose live price has hit the stored stop."""

        prices = ltp_by_ticker or {}
        with self._lock:
            book = self._read()
            realized = float(book.get("daily_realized_pl") or 0.0)
            rows: list[dict[str, Any]] = []
            closed = 0
            for pos in book.get("positions") or []:
                if pos.get("status") != "OPEN":
                    rows.append(pos)
                    continue
                ticker = str(pos.get("Ticker") or "")
                side = str(pos.get("TransactionType") or "BUY")
                stop = float(pos.get("StopLoss") or 0.0)
                ltp = float(prices.get(ticker) or pos.get("ExecutionPrice") or 0.0)
                hit = stop > 0 and ((side == "BUY" and ltp <= stop) or (side == "SELL" and ltp >= stop))
                if not hit:
                    rows.append(pos)
                    continue
                pnl = _unrealized(side, float(pos.get("ExecutionPrice") or 0.0), stop, int(pos.get("Quantity") or 0))
                realized += pnl
                pos["status"] = "CLOSED"
                pos["exit_reason"] = "STOP"
                pos["ExitPrice"] = stop
                pos["ExitTimestamp"] = datetime.now(IST).isoformat()
                pos["LiveUnrealizedPL"] = 0.0
                pos["RealizedPL"] = pnl
                rows.append(pos)
                closed += 1
            if closed:
                book["positions"] = rows
                book["daily_realized_pl"] = round(realized, 2)
                book["updated_at"] = datetime.now(IST).isoformat()
                self._write(book)
            return closed

    def close_tickers(
        self,
        tickers: set[str],
        ltp_by_ticker: dict[str, float] | None = None,
        reason: str = "TARGET",
    ) -> list[dict[str, Any]]:
        """Close specific open rows and return the rows that were just closed."""

        if not tickers:
            return []
        prices = ltp_by_ticker or {}
        closed_rows: list[dict[str, Any]] = []
        with self._lock:
            book = self._read()
            realized = float(book.get("daily_realized_pl") or 0.0)
            rows: list[dict[str, Any]] = []
            for pos in book.get("positions") or []:
                ticker = str(pos.get("Ticker") or "")
                if pos.get("status") != "OPEN" or ticker not in tickers:
                    rows.append(pos)
                    continue
                ltp = float(prices.get(ticker) or pos.get("ExecutionPrice") or 0.0)
                pnl = _unrealized(
                    str(pos.get("TransactionType") or "BUY"),
                    float(pos.get("ExecutionPrice") or 0.0),
                    ltp,
                    int(pos.get("Quantity") or 0),
                )
                realized += pnl
                pos["status"] = "CLOSED"
                pos["exit_reason"] = reason
                pos["ExitPrice"] = ltp
                pos["ExitTimestamp"] = datetime.now(IST).isoformat()
                pos["LiveUnrealizedPL"] = 0.0
                pos["RealizedPL"] = pnl
                rows.append(pos)
                closed_rows.append(dict(pos))
            if closed_rows:
                book["positions"] = rows
                book["daily_realized_pl"] = round(realized, 2)
                book["updated_at"] = datetime.now(IST).isoformat()
                self._write(book)
        return closed_rows

    def force_square_off(self, ltp_by_ticker: dict[str, float] | None = None, reason: str = "FORCE_SQUARE_OFF_1500") -> int:
        """Close every open paper row at mark. The options desk flattens at 15:00 IST."""

        prices = ltp_by_ticker or {}
        with self._lock:
            book = self._read()
            realized = float(book.get("daily_realized_pl") or 0.0)
            rows: list[dict[str, Any]] = []
            closed = 0
            for pos in book.get("positions") or []:
                if pos.get("status") != "OPEN":
                    rows.append(pos)
                    continue
                ticker = str(pos.get("Ticker") or "")
                ltp = float(prices.get(ticker) or pos.get("ExecutionPrice") or 0.0)
                pnl = _unrealized(
                    str(pos.get("TransactionType") or "BUY"),
                    float(pos.get("ExecutionPrice") or 0.0),
                    ltp,
                    int(pos.get("Quantity") or 0),
                )
                realized += pnl
                pos["status"] = "CLOSED"
                pos["exit_reason"] = reason
                pos["ExitPrice"] = ltp
                pos["ExitTimestamp"] = datetime.now(IST).isoformat()
                pos["LiveUnrealizedPL"] = 0.0
                pos["RealizedPL"] = pnl
                rows.append(pos)
                closed += 1
            if closed:
                book["positions"] = rows
                book["daily_realized_pl"] = round(realized, 2)
                book["updated_at"] = datetime.now(IST).isoformat()
                self._write(book)
        if closed:
            LOGGER.warning("PAPER force square-off closed %s row(s) (%s)", closed, reason)
        return closed

    def closed_positions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [row for row in self._read().get("positions") or [] if row.get("status") == "CLOSED"]

    def mark_to_market(self, ltp_by_ticker: dict[str, float]) -> list[dict[str, Any]]:
        with self._lock:
            book = self._read()
            rows = []
            changed = False
            for pos in book.get("positions") or []:
                if pos.get("status") != "OPEN":
                    rows.append(pos)
                    continue
                ticker = str(pos.get("Ticker") or "")
                ltp = float(ltp_by_ticker.get(ticker) or pos.get("ExecutionPrice") or 0.0)
                pnl = _unrealized(
                    str(pos.get("TransactionType") or "BUY"),
                    float(pos.get("ExecutionPrice") or 0.0),
                    ltp,
                    int(pos.get("Quantity") or 0),
                )
                if pos.get("LiveUnrealizedPL") != pnl:
                    pos["LiveUnrealizedPL"] = pnl
                    changed = True
                rows.append(pos)
            book["positions"] = rows
            if changed:
                book["updated_at"] = datetime.now(IST).isoformat()
                self._write(book)
            return [dict(row) for row in rows]

    def open_positions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [row for row in self._read().get("positions") or [] if row.get("status") == "OPEN"]

    def daily_realized_pl(self) -> float:
        with self._lock:
            return float(self._read().get("daily_realized_pl") or 0.0)

    def daily_total_pl(self, ltp_by_ticker: dict[str, float] | None = None) -> float:
        rows = self.mark_to_market(ltp_by_ticker or {}) if ltp_by_ticker is not None else self.open_positions()
        floating = sum(float(row.get("LiveUnrealizedPL") or 0.0) for row in rows)
        return round(self.daily_realized_pl() + floating, 2)

    def current_daily_loss(self, ltp_by_ticker: dict[str, float] | None = None) -> float:
        pnl = self.daily_total_pl(ltp_by_ticker)
        return round(max(0.0, -pnl), 2)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty_book()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.exception("simulated_portfolio.json unreadable; resetting")
            return self._empty_book()
        if not isinstance(payload, dict):
            return self._empty_book()
        payload.setdefault("positions", [])
        payload.setdefault("daily_realized_pl", 0.0)
        return payload

    def _write(self, book: dict[str, Any]) -> None:
        payload = json.dumps(book, indent=2)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        try:
            tmp.replace(self.path)
        except OSError:
            # Windows can lock the live ledger while Streamlit is reading it.
            self.path.write_text(payload, encoding="utf-8")
            try:
                tmp.unlink()
            except OSError:
                pass

    def _empty_book(self, realized: float = 0.0) -> dict[str, Any]:
        return {
            "updated_at": datetime.now(IST).isoformat(),
            "daily_realized_pl": realized,
            "positions": [],
        }


def _unrealized(side: str, entry: float, ltp: float, quantity: int) -> float:
    direction = 1 if side == "BUY" else -1
    return round((ltp - entry) * quantity * direction, 2)
