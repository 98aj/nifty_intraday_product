"""Replay the Nifty opening-range option rule on 5-minute bars. No I/O."""

from __future__ import annotations

from datetime import datetime, time

import pandas as pd

from config.settings import IST
from modules.options.selector import size_lots
from modules.strategy.orb import calculate_opening_range, evaluate_market_signals

COST_BPS = 5.0


def replay_option_rule(
    index_bars: pd.DataFrame,
    option_bars: dict[tuple[int, str], pd.DataFrame],
    capital: float,
    risk_pct: float,
    stop_frac: float,
    target_frac: float,
    lot_size: int = 65,
    lots: int | None = None,
) -> pd.DataFrame:
    """One long ATM option per session. Premium path comes from `option_bars`."""

    if index_bars is None or index_bars.empty:
        return pd.DataFrame()
    index = _as_ist(index_bars)
    options = {key: _as_ist(frame) for key, frame in option_bars.items() if frame is not None and not frame.empty}
    equity = float(capital)
    rows: list[dict] = []
    index["session"] = index["timestamp"].dt.date
    for session, day in index.groupby("session", sort=True):
        day = day.reset_index(drop=True)
        trade = _session(
            day, options, session, equity, capital, risk_pct, stop_frac, target_frac, lot_size, lots
        )
        if trade is None:
            continue
        equity = float(trade["equity"])
        rows.append(trade)
    return pd.DataFrame(rows)


def _session(day, options, session, equity, capital, risk_pct, stop_frac, target_frac, lot_size, lots):
    clock_now = datetime.combine(session, time(15, 20), tzinfo=IST)
    opening = calculate_opening_range(day, now=clock_now)
    if not opening.ready:
        return None
    entry_row = None
    side = "SCANNING"
    for _, bar in day.iterrows():
        stamp = bar["timestamp"]
        if stamp.time() < time(9, 30) or stamp.time() >= time(14, 30):
            continue
        side = evaluate_market_signals(float(bar["close"]), opening.high_boundary, opening.low_boundary)
        if side in {"BUY", "SELL"}:
            entry_row = bar
            break
    if entry_row is None:
        return None
    option_type = "CE" if side == "BUY" else "PE"
    spot = float(entry_row["close"])
    strike = _atm_strike(options, spot, option_type)
    if strike is None:
        return None
    series = options[(strike, option_type)]
    entry_time = entry_row["timestamp"]
    entry_bar = series[series["timestamp"] == entry_time]
    if entry_bar.empty:
        return None
    entry = float(entry_bar.iloc[0]["close"])
    if lots is None:
        lots, _cash, _risk = size_lots(capital, risk_pct, entry, lot_size, stop_frac)
    else:
        cash = entry * lot_size * int(lots)
        risk = cash * stop_frac
        lots = int(lots) if cash <= capital and risk <= 3000 else 0
    if lots < 1 or entry <= 0:
        return None
    stop = entry * (1.0 - stop_frac)
    target = entry * (1.0 + target_frac)
    after = series[(series["timestamp"] > entry_time) & (series["timestamp"].dt.time < time(15, 0))]
    exit_price = float(after.iloc[-1]["close"]) if not after.empty else entry
    exit_time = after.iloc[-1]["timestamp"] if not after.empty else entry_time
    reason = "SQUARE_OFF_1500"
    for _, bar in after.iterrows():
        if float(bar["low"]) <= stop:
            exit_price, exit_time, reason = stop, bar["timestamp"], "PREMIUM_STOP"
            break
        if float(bar["high"]) >= target:
            exit_price, exit_time, reason = target, bar["timestamp"], "PREMIUM_TARGET"
            break
    cost = entry * (COST_BPS / 10_000.0) * 2.0
    points = (exit_price - entry) - cost
    qty = lots * lot_size
    pnl = round(points * qty, 2)
    equity = round(equity + pnl, 2)
    outcome = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "FLAT"
    return {
        "date": session.isoformat(),
        "side": "BUY",
        "option": option_type,
        "strike": strike,
        "entry_time": entry_time.strftime("%H:%M"),
        "exit_time": pd.Timestamp(exit_time).strftime("%H:%M"),
        "entry": round(entry, 2),
        "exit": round(float(exit_price), 2),
        "lots": lots,
        "qty": qty,
        "rupee_pl": pnl,
        "equity": equity,
        "outcome": outcome,
        "exit_reason": reason,
    }


def _atm_strike(options: dict[tuple[int, str], pd.DataFrame], spot: float, option_type: str) -> int | None:
    strikes = [strike for strike, side in options if side == option_type]
    if not strikes or spot <= 0:
        return None
    return min(strikes, key=lambda strike: (abs(strike - spot), strike))


def _as_ist(frame: pd.DataFrame) -> pd.DataFrame:
    working = frame.copy()
    working["timestamp"] = pd.to_datetime(working["timestamp"], utc=False)
    if working["timestamp"].dt.tz is None:
        working["timestamp"] = working["timestamp"].dt.tz_localize(IST)
    else:
        working["timestamp"] = working["timestamp"].dt.tz_convert(IST)
    return working.sort_values("timestamp").reset_index(drop=True)
