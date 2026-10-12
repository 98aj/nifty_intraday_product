"""15-minute Opening Range Breakout for NSE cash (09:15–09:30 IST).

Signal language is closed: ``BUY``, ``SELL``, or ``SCANNING``. Equality at
either boundary stays ``SCANNING`` — a breakout requires a strict print
through the range. This module has no I/O and no Gemini imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Literal

import pandas as pd

from config.settings import FORCE_SQUARE_OFF, IST, OPENING_RANGE_END, SESSION_CLOSE, SESSION_OPEN

Signal = Literal["BUY", "SELL", "SCANNING"]


@dataclass(frozen=True)
class OpeningRange:
    """Absolute high/low established in the first 15 minutes of the cash session."""

    high_boundary: float
    low_boundary: float
    bar_count: int
    ready: bool
    session_date: str


def calculate_opening_range(df: pd.DataFrame, now: datetime | None = None) -> OpeningRange:
    """Extract the 09:15–09:30 IST opening-range high and low.

    The 09:30 bar is excluded (range is the first 15 minutes, not a 15-minute
    close-at-09:30 bar). If the window is incomplete, ``ready`` is False and
    the strategy must remain in ``SCANNING``.
    """

    if df is None or df.empty or "timestamp" not in df.columns:
        return OpeningRange(0.0, 0.0, 0, False, "")

    working = df.copy()
    working["timestamp"] = pd.to_datetime(working["timestamp"], utc=False)
    if working["timestamp"].dt.tz is None:
        working["timestamp"] = working["timestamp"].dt.tz_localize(IST)
    else:
        working["timestamp"] = working["timestamp"].dt.tz_convert(IST)

    latest = working["timestamp"].max()
    session_date = latest.date()
    start = datetime.combine(session_date, SESSION_OPEN, tzinfo=IST)
    end = datetime.combine(session_date, OPENING_RANGE_END, tzinfo=IST)
    window = working[(working["timestamp"] >= start) & (working["timestamp"] < end)]
    if window.empty:
        return OpeningRange(0.0, 0.0, 0, False, session_date.isoformat())

    high_boundary = float(window["high"].max())
    low_boundary = float(window["low"].min())
    if high_boundary <= 0 or low_boundary <= 0 or high_boundary < low_boundary:
        return OpeningRange(0.0, 0.0, int(len(window)), False, session_date.isoformat())

    clock = now or datetime.now(IST)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=IST)
    else:
        clock = clock.astimezone(IST)
    ready = bool(clock >= end or latest >= end)
    return OpeningRange(
        high_boundary=high_boundary,
        low_boundary=low_boundary,
        bar_count=int(len(window)),
        ready=ready,
        session_date=session_date.isoformat(),
    )


def evaluate_market_signals(
    current_price: float,
    high_boundary: float,
    low_boundary: float,
) -> Signal:
    """Return ``BUY``, ``SELL``, or ``SCANNING`` from a strict range breakout."""

    if current_price <= 0 or high_boundary <= 0 or low_boundary <= 0:
        return "SCANNING"
    if high_boundary < low_boundary:
        return "SCANNING"
    if current_price > high_boundary:
        return "BUY"
    if current_price < low_boundary:
        return "SELL"
    return "SCANNING"


def stop_loss_for_signal(
    signal: Signal,
    high_boundary: float,
    low_boundary: float,
) -> float:
    """Opposite opening-range bound is the mandatory hard stop."""

    if signal == "BUY":
        return float(low_boundary)
    if signal == "SELL":
        return float(high_boundary)
    return 0.0


def session_clock_state(now: datetime | None = None) -> str:
    """Coarse session label for the dashboard status strip."""

    current = now or datetime.now(IST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=IST)
    else:
        current = current.astimezone(IST)
    clock = current.time().replace(tzinfo=None)
    if clock < time(9, 0):
        return "PRE_OPEN"
    if clock < SESSION_OPEN:
        return "PREMARKET"
    if clock < OPENING_RANGE_END:
        return "OPENING_RANGE"
    if clock < FORCE_SQUARE_OFF:
        return "TRADEABLE"
    if clock <= SESSION_CLOSE:
        return "FORCE_SQUARE_OFF"
    return "CLOSED"
