"""Bank Nifty late-session range break. No I/O and no Gemini imports.

The session high and low are built from 09:15 until 14:00 IST. The 14:00 bar
is not part of that range. From 14:00 through 14:45, the first completed
5-minute close through a boundary is the only signal. A wick is ignored.
Entries are allowed during the 14:45 minute so that close can still be bought,
and are refused from 14:46. Previous-day CPR is display context only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Literal

import pandas as pd

from config.settings import IST, SESSION_CLOSE, SESSION_OPEN

Signal = Literal["BUY", "SELL", "SCANNING"]

SESSION_RANGE_END = time(14, 0)
ENTRY_DEADLINE = time(14, 45)
ENTRY_CUTOFF = time(14, 46)
BAR = timedelta(minutes=5)
NARROW_CPR_PCT = 0.25
# A 0.5% index stop on a monthly at-the-money option is about a 25% premium stop.
# The target is twice that stop. The ₹6,000 cap fits one such lot.
PREMIUM_STOP_PCT = 25
PREMIUM_TARGET_PCT = 50
MAX_LOSS_INR = 6000.0


@dataclass(frozen=True)
class SessionRange:
    """High and low of the cash session up to, but not including, 14:00 IST."""

    high_boundary: float
    low_boundary: float
    bar_count: int
    ready: bool
    session_date: str


@dataclass(frozen=True)
class CentralPivot:
    """Previous-session central pivot. ``narrow`` is context, not a trade gate."""

    pivot: float
    top: float
    bottom: float
    width_pct: float
    narrow: bool
    ready: bool
    session_date: str


def calculate_session_range(df: pd.DataFrame, now: datetime | None = None) -> SessionRange:
    """High and low from 09:15 until 14:00 on the clock's session date.

    The range is ready only once the clock has reached 14:00. Bars from any
    other date are ignored.
    """

    clock = _aware(now)
    session_date = clock.date().isoformat()
    working = _bars(df)
    if working is None:
        return SessionRange(0.0, 0.0, 0, False, "")

    start = datetime.combine(clock.date(), SESSION_OPEN, tzinfo=IST)
    end = datetime.combine(clock.date(), SESSION_RANGE_END, tzinfo=IST)
    window = working[(working["timestamp"] >= start) & (working["timestamp"] < end)]
    if window.empty:
        return SessionRange(0.0, 0.0, 0, False, session_date)

    high_boundary = float(window["high"].max())
    low_boundary = float(window["low"].min())
    if high_boundary <= 0 or low_boundary <= 0 or high_boundary < low_boundary:
        return SessionRange(0.0, 0.0, int(len(window)), False, session_date)
    return SessionRange(
        high_boundary=high_boundary,
        low_boundary=low_boundary,
        bar_count=int(len(window)),
        ready=clock >= end,
        session_date=session_date,
    )


def evaluate_late_break(
    df: pd.DataFrame,
    high_boundary: float,
    low_boundary: float,
    now: datetime | None = None,
) -> Signal:
    """First completed 5-minute close through the session range.

    Bars open at 14:00, 14:05, ... 14:40. A bar counts only after it has
    closed, and only its close is compared with the range. The first close
    outside the range wins for the rest of the day.
    """

    clock = _aware(now)
    if high_boundary <= 0 or low_boundary <= 0 or high_boundary < low_boundary:
        return "SCANNING"
    if clock.timetz().replace(tzinfo=None) < SESSION_RANGE_END:
        return "SCANNING"
    working = _bars(df)
    if working is None:
        return "SCANNING"

    start = datetime.combine(clock.date(), SESSION_RANGE_END, tzinfo=IST)
    deadline = datetime.combine(clock.date(), ENTRY_DEADLINE, tzinfo=IST)
    window = working[(working["timestamp"] >= start) & (working["timestamp"] < deadline)]
    window = window.sort_values("timestamp")
    for stamp, close in zip(window["timestamp"], window["close"], strict=False):
        opened = _as_datetime(stamp)
        if clock < opened + BAR:
            continue
        price = float(close)
        if price > high_boundary:
            return "BUY"
        if price < low_boundary:
            return "SELL"
    return "SCANNING"


def previous_day_cpr(df: pd.DataFrame, now: datetime | None = None) -> CentralPivot:
    """CPR of the latest session before the clock's date. Empty when unknown."""

    clock = _aware(now)
    blank = CentralPivot(0.0, 0.0, 0.0, 0.0, False, False, "")
    working = _bars(df)
    if working is None:
        return blank
    prior = sorted({stamp.date() for stamp in working["timestamp"] if stamp.date() < clock.date()})
    if not prior:
        return blank
    day = prior[-1]
    start = datetime.combine(day, SESSION_OPEN, tzinfo=IST)
    end = datetime.combine(day, SESSION_CLOSE, tzinfo=IST)
    window = working[(working["timestamp"] >= start) & (working["timestamp"] < end)]
    if window.empty:
        return blank
    ordered = window.sort_values("timestamp")
    high = float(ordered["high"].max())
    low = float(ordered["low"].min())
    close = float(ordered.iloc[-1]["close"])
    if high <= 0 or low <= 0 or close <= 0 or high < low:
        return blank
    return central_pivot(high, low, close, day.isoformat())


def central_pivot(high: float, low: float, close: float, session_date: str = "") -> CentralPivot:
    """Pivot ``(H+L+C)/3`` and the band around ``(H+L)/2``. Narrow is under 0.25%."""

    pivot = (high + low + close) / 3.0
    if pivot <= 0 or high < low:
        return CentralPivot(0.0, 0.0, 0.0, 0.0, False, False, session_date)
    mid = (high + low) / 2.0
    top = max(mid, (2.0 * pivot) - mid)
    bottom = min(mid, (2.0 * pivot) - mid)
    width_pct = ((top - bottom) / pivot) * 100.0
    return CentralPivot(
        pivot=pivot,
        top=top,
        bottom=bottom,
        width_pct=width_pct,
        narrow=width_pct < NARROW_CPR_PCT,
        ready=True,
        session_date=session_date,
    )


def late_entry_allowed(now: datetime, signal: str) -> tuple[bool, str]:
    """Bank Nifty may open a new option from 14:00 through the 14:45 minute."""

    clock_dt = _aware(now)
    if clock_dt.weekday() >= 5:
        return False, "Cash and derivatives market is closed today."
    clock = clock_dt.timetz().replace(tzinfo=None)
    if clock < SESSION_RANGE_END:
        return False, "Session range is still building. No option until 14:00 IST."
    if clock >= ENTRY_CUTOFF:
        return False, "Entry window is closed (14:45 IST). Open options flatten at 15:00."
    if signal not in {"BUY", "SELL"}:
        return False, "Bank Nifty is still inside the session range. No option yet."
    return True, "ok"


def _bars(df: pd.DataFrame | None) -> pd.DataFrame | None:
    if df is None or df.empty or "timestamp" not in df.columns:
        return None
    working = df.copy()
    working["timestamp"] = pd.to_datetime(working["timestamp"], utc=False)
    if working["timestamp"].dt.tz is None:
        working["timestamp"] = working["timestamp"].dt.tz_localize(IST)
    else:
        working["timestamp"] = working["timestamp"].dt.tz_convert(IST)
    return working


def _aware(now: datetime | None) -> datetime:
    clock = now or datetime.now(IST)
    if clock.tzinfo is None:
        return clock.replace(tzinfo=IST)
    return clock.astimezone(IST)


def _as_datetime(stamp) -> datetime:
    opened = pd.Timestamp(stamp).to_pydatetime()
    if opened.tzinfo is None:
        return opened.replace(tzinfo=IST)
    return opened.astimezone(IST)
