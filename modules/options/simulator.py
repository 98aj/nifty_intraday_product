"""Estimate Nifty weekly option prices from the index path. No I/O.

Premiums are European Black-Scholes prices. A call's high follows Nifty's high.
A put's high follows Nifty's low. The replay engine consumes the same
``(strike, "CE"|"PE")`` tapes it uses for real option candles.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from math import erf, sqrt

import numpy as np
import pandas as pd

from config.settings import IST

STRIKE_STEP = 50
FALLBACK_VOL = 0.14
VOL_LOOKBACK = 20
MIN_VOL = 0.08
MAX_VOL = 0.60
RATE = 0.06
EXPIRY_CUTOVER = date(2025, 9, 1)
SESSION_CLOSE = time(15, 30)
SECONDS_PER_YEAR = 365.25 * 24 * 3600
MIN_T_YEARS = 60.0 / SECONDS_PER_YEAR
_NORM = np.vectorize(erf, otypes=[float])


def weekly_expiry(session: date) -> date:
    """Thursday before 1 Sep 2025, Tuesday from that date on."""

    for offset in range(8):
        candidate = session + timedelta(days=offset)
        weekday = 3 if candidate < EXPIRY_CUTOVER else 1
        if candidate.weekday() == weekday:
            return candidate
    return session


def bs_price(
    spot: float,
    strike: float,
    t_years: float,
    sigma: float,
    rate: float,
    option_type: str,
) -> float:
    """One European option price. ``option_type`` is ``CE`` or ``PE``."""

    prices = _bs_prices(
        np.array([spot], dtype=float),
        float(strike),
        np.array([t_years], dtype=float),
        float(sigma),
        float(rate),
        option_type,
    )
    return float(prices[0])


def realized_vol(index_bars: pd.DataFrame, session: date) -> float:
    """Annualized move from the prior 20 daily closes, or 14% when that window is short."""

    if index_bars is None or index_bars.empty:
        return FALLBACK_VOL
    frame = _as_ist(index_bars)
    daily = frame.groupby(frame["timestamp"].dt.date, sort=True)["close"].last()
    prior = daily[daily.index < session].tail(VOL_LOOKBACK)
    if len(prior) < 5:
        return FALLBACK_VOL
    returns = prior.pct_change().dropna()
    if len(returns) < 4:
        return FALLBACK_VOL
    sigma = float(returns.std(ddof=1) * sqrt(252))
    if sigma != sigma:
        return FALLBACK_VOL
    return float(min(max(sigma, MIN_VOL), MAX_VOL))


def session_volatility(vix_bars: pd.DataFrame, sessions: list[date]) -> dict[date, float]:
    """Previous session's India VIX close, as a decimal, for each trading day.

    India VIX is quoted in percent, so 14.2 becomes 0.142. A day with no earlier
    close uses 14%.
    """

    levels: dict[date, float] = {}
    if vix_bars is not None and not vix_bars.empty and "close" in vix_bars.columns:
        frame = _as_ist(vix_bars)
        daily = frame.groupby(frame["timestamp"].dt.date, sort=True)["close"].last()
        levels = {day: float(value) for day, value in daily.items() if float(value) == float(value)}
    ordered = sorted(levels)
    out: dict[date, float] = {}
    for session in sessions:
        prior = [day for day in ordered if day < session]
        out[session] = _vix_to_sigma(levels[prior[-1]]) if prior else FALLBACK_VOL
    return out


def _vix_to_sigma(level: float) -> float:
    sigma = float(level) / 100.0 if float(level) > 1.5 else float(level)
    if sigma != sigma or sigma <= 0:
        return FALLBACK_VOL
    return float(min(max(sigma, MIN_VOL), MAX_VOL))


def average_volatility(
    index_bars: pd.DataFrame,
    history: pd.DataFrame | None = None,
) -> float:
    """Mean session volatility over ``index_bars``, using earlier closes in ``history``."""

    if index_bars is None or index_bars.empty:
        return FALLBACK_VOL
    frame = _as_ist(index_bars)
    hist = frame if history is None else _as_ist(history)
    sessions = sorted(set(frame["timestamp"].dt.date))
    if not sessions:
        return FALLBACK_VOL
    vols = [realized_vol(hist, session) for session in sessions]
    return float(sum(vols) / len(vols))


def simulate_option_bars(
    index_bars: pd.DataFrame,
    sigma: float | None = None,
    rate: float = RATE,
    strike_step: int = STRIKE_STEP,
    history: pd.DataFrame | None = None,
    session_sigma: dict[date, float] | None = None,
) -> dict[tuple[int, str], pd.DataFrame]:
    """Build call and put OHLC for each 50-point strike inside each session's range.

    ``session_sigma`` wins when that day is present. Otherwise a fixed ``sigma``
    is used. With neither, each session uses :func:`realized_vol` on ``history``.
    """

    if index_bars is None or index_bars.empty:
        return {}
    frame = _as_ist(index_bars)
    hist = frame if history is None else _as_ist(history)
    fixed = None if sigma is None else float(min(max(float(sigma), MIN_VOL), MAX_VOL))
    by_day = session_sigma or {}
    buckets: dict[tuple[int, str], list[pd.DataFrame]] = {}
    for session, day in frame.groupby(frame["timestamp"].dt.date, sort=True):
        if session in by_day:
            sigma_day = float(min(max(float(by_day[session]), MIN_VOL), MAX_VOL))
        elif fixed is not None:
            sigma_day = fixed
        else:
            sigma_day = realized_vol(hist, session)
        expiry = datetime.combine(weekly_expiry(session), SESSION_CLOSE, tzinfo=IST)
        seconds = (pd.Timestamp(expiry) - day["timestamp"]).dt.total_seconds().to_numpy(dtype=float)
        t_years = np.maximum(seconds / SECONDS_PER_YEAR, MIN_T_YEARS)
        low = float(day["low"].min())
        high = float(day["high"].max())
        strikes = _strike_grid(low, high, strike_step)
        if not strikes:
            continue
        spots = {
            "open": day["open"].to_numpy(dtype=float),
            "high": day["high"].to_numpy(dtype=float),
            "low": day["low"].to_numpy(dtype=float),
            "close": day["close"].to_numpy(dtype=float),
        }
        stamps = day["timestamp"].to_numpy()
        for strike in strikes:
            for side in ("CE", "PE"):
                priced = {
                    name: _bs_prices(values, strike, t_years, sigma_day, rate, side)
                    for name, values in spots.items()
                }
                if side == "PE":
                    priced["high"], priced["low"] = priced["low"], priced["high"]
                opt_high = np.maximum(
                    np.maximum(priced["open"], priced["high"]),
                    np.maximum(priced["low"], priced["close"]),
                )
                opt_low = np.minimum(
                    np.minimum(priced["open"], priced["high"]),
                    np.minimum(priced["low"], priced["close"]),
                )
                piece = pd.DataFrame(
                    {
                        "timestamp": stamps,
                        "open": np.round(priced["open"], 2),
                        "high": np.round(opt_high, 2),
                        "low": np.round(opt_low, 2),
                        "close": np.round(priced["close"], 2),
                        "volume": 0,
                    }
                )
                buckets.setdefault((strike, side), []).append(piece)
    return {key: pd.concat(parts, ignore_index=True) for key, parts in buckets.items()}


def _strike_grid(low: float, high: float, step: int) -> list[int]:
    if step <= 0 or low <= 0 or high <= 0:
        return []
    start = int(np.floor(min(low, high) / step) * step)
    end = int(np.ceil(max(low, high) / step) * step)
    return list(range(start, end + step, step))


def _bs_prices(
    spots: np.ndarray,
    strike: float,
    t_years: np.ndarray,
    sigma: float,
    rate: float,
    option_type: str,
) -> np.ndarray:
    spot = np.maximum(np.asarray(spots, dtype=float), 1e-8)
    t = np.maximum(np.asarray(t_years, dtype=float), MIN_T_YEARS)
    sigma = max(float(sigma), 1e-4)
    strike = max(float(strike), 1e-8)
    vol_sqrt = sigma * np.sqrt(t)
    d1 = (np.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t) / vol_sqrt
    d2 = d1 - vol_sqrt
    discount = np.exp(-rate * t)
    call = spot * _norm_cdf(d1) - strike * discount * _norm_cdf(d2)
    if option_type == "CE":
        return np.maximum(call, 0.05)
    put = call - spot + strike * discount
    return np.maximum(put, 0.05)


def _norm_cdf(values: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + _NORM(np.asarray(values, dtype=float) / sqrt(2.0)))


def _as_ist(frame: pd.DataFrame) -> pd.DataFrame:
    working = frame.copy()
    working["timestamp"] = pd.to_datetime(working["timestamp"], utc=False)
    if working["timestamp"].dt.tz is None:
        working["timestamp"] = working["timestamp"].dt.tz_localize(IST)
    else:
        working["timestamp"] = working["timestamp"].dt.tz_convert(IST)
    return working.sort_values("timestamp").reset_index(drop=True)
