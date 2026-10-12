"""Historical and live market data normalized to a deterministic DataFrame.

Upstox v2 historical/intraday candle endpoints natively accept `1minute` and
`30minute` (plus `day`/`week`/`month` on the historical route). A requested
`5minute` interval is fulfilled by pulling 1-minute bars and resampling with
Pandas — the ORB engine always sees `[timestamp, open, high, low, close, volume]`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import pandas as pd

from config.settings import IST
from modules.upstox.client import UpstoxClient
from modules.upstox.exceptions import UpstoxDataError

CANDLE_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]
NATIVE_INTERVALS = {"1minute", "30minute", "day", "week", "month"}
RESAMPLE_RULES = {
    "3minute": "3min",
    "5minute": "5min",
    "15minute": "15min",
}


@dataclass(frozen=True)
class MarketQuote:
    """Snapshot used by the dashboard metric cards."""

    instrument_token: str
    symbol: str
    last_price: float
    day_open: float
    day_high: float
    day_low: float
    day_close: float
    volume: float


def get_historical_candles(
    instrument_token: str,
    interval: str = "5minute",
    from_date: date | None = None,
    to_date: date | None = None,
    client: UpstoxClient | None = None,
    access_token: str | None = None,
    include_today: bool = True,
) -> pd.DataFrame:
    """Pull OHLC history and return a chronological Pandas DataFrame.

    Parameters
    ----------
    instrument_token:
        Upstox instrument key, e.g. ``NSE_EQ|INE002A01018``.
    interval:
        Target bar size. ``5minute`` is resampled from native 1-minute bars.
    """

    if not instrument_token:
        raise UpstoxDataError("instrument_token is required")

    http = client or UpstoxClient(access_token or _token_from_env())
    end = to_date or datetime.now(IST).date()
    start = from_date or (end - timedelta(days=5))
    fetch_interval = "1minute" if interval in RESAMPLE_RULES else interval
    if fetch_interval not in NATIVE_INTERVALS:
        raise UpstoxDataError(
            f"Unsupported interval '{interval}'. Use 1minute, 5minute, 15minute, 30minute, or day."
        )

    frames: list[pd.DataFrame] = []
    encoded = quote(instrument_token, safe="")
    chunk_days = 25 if fetch_interval == "1minute" else 365
    cursor = start
    last_error: Exception | None = None
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=chunk_days))
        historical_path = (
            f"/historical-candle/{encoded}/{fetch_interval}/{chunk_end.isoformat()}/{cursor.isoformat()}"
        )
        try:
            frames.append(_candles_to_frame(_extract_candles(http.get(historical_path))))
            last_error = None
        except Exception as exc:
            last_error = exc
        cursor = chunk_end + timedelta(days=1)
    if not frames and last_error is not None:
        raise UpstoxDataError(f"Historical candle request failed: {last_error}") from last_error

    if include_today and fetch_interval in {"1minute", "30minute"}:
        try:
            intra_path = f"/historical-candle/intraday/{encoded}/{fetch_interval}"
            frames.append(_candles_to_frame(_extract_candles(http.get(intra_path))))
        except Exception:
            # Intraday feed is optional outside session hours or on holidays.
            pass

    if not frames:
        raise UpstoxDataError(f"No candles returned for {instrument_token}")

    frame = (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates(subset=["timestamp"])
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    if interval in RESAMPLE_RULES:
        frame = resample_ohlcv(frame, RESAMPLE_RULES[interval])
    return frame.loc[:, CANDLE_COLUMNS]


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Aggregate 1-minute bars into a coarser OHLCV interval."""

    if df.empty:
        return df.loc[:, CANDLE_COLUMNS] if set(CANDLE_COLUMNS).issubset(df.columns) else df
    working = df.copy()
    working["timestamp"] = pd.to_datetime(working["timestamp"], utc=False)
    if working["timestamp"].dt.tz is None:
        working["timestamp"] = working["timestamp"].dt.tz_localize(IST)
    else:
        working["timestamp"] = working["timestamp"].dt.tz_convert(IST)
    resampled = (
        working.set_index("timestamp")
        .resample(rule, label="left", closed="left")
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        .dropna(subset=["open", "high", "low", "close"])
        .reset_index()
    )
    return resampled.loc[:, CANDLE_COLUMNS]


def get_quotes_batch(
    instrument_tokens: list[str],
    client: UpstoxClient | None = None,
    access_token: str | None = None,
    chunk_size: int = 50,
) -> dict[str, dict[str, float | str]]:
    """Pull full quotes for many cash names. Keyed by instrument token."""

    http = client or UpstoxClient(access_token or _token_from_env())
    out: dict[str, dict[str, float | str]] = {}
    tokens = [token for token in instrument_tokens if token]
    for start in range(0, len(tokens), chunk_size):
        chunk = tokens[start : start + chunk_size]
        try:
            payload = http.get("/market-quote/quotes", params={"instrument_key": ",".join(chunk)})
        except Exception:
            continue
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            continue
        for raw in data.values():
            if not isinstance(raw, dict):
                continue
            token = str(raw.get("instrument_token") or raw.get("instrument_key") or "")
            ohlc = raw.get("ohlc") or {}
            try:
                last_price = float(raw.get("last_price") or 0.0)
                day_open = float(ohlc.get("open") or 0.0)
                day_high = float(ohlc.get("high") or 0.0)
                day_low = float(ohlc.get("low") or 0.0)
                day_close = float(ohlc.get("close") or 0.0)
                net_change = float(raw.get("net_change") or 0.0)
                volume = float(raw.get("volume") or 0.0)
            except (TypeError, ValueError):
                continue
            if not token:
                continue
            out[token] = {
                "instrument_token": token,
                "symbol": str(raw.get("symbol") or raw.get("trading_symbol") or ""),
                "last_price": last_price,
                "day_open": day_open,
                "day_high": day_high,
                "day_low": day_low,
                "day_close": day_close,
                "net_change": net_change,
                "volume": volume,
            }
    return out


def get_full_quote(
    instrument_token: str,
    client: UpstoxClient | None = None,
    access_token: str | None = None,
) -> MarketQuote:
    """Return LTP plus session OHLC for a single cash instrument."""

    http = client or UpstoxClient(access_token or _token_from_env())
    try:
        payload = http.get("/market-quote/quotes", params={"instrument_key": instrument_token})
    except Exception as exc:
        raise UpstoxDataError(f"Quote request failed: {exc}") from exc

    data = payload.get("data") or {}
    if not isinstance(data, dict) or not data:
        raise UpstoxDataError(f"Empty quote payload for {instrument_token}")
    raw = next(iter(data.values()))
    if not isinstance(raw, dict):
        raise UpstoxDataError(f"Malformed quote payload for {instrument_token}")

    ohlc = raw.get("ohlc") or {}
    try:
        return MarketQuote(
            instrument_token=str(raw.get("instrument_token") or instrument_token),
            symbol=str(raw.get("symbol") or raw.get("trading_symbol") or ""),
            last_price=float(raw.get("last_price") or 0.0),
            day_open=float(ohlc.get("open") or 0.0),
            day_high=float(ohlc.get("high") or 0.0),
            day_low=float(ohlc.get("low") or 0.0),
            day_close=float(ohlc.get("close") or 0.0),
            volume=float(raw.get("volume") or 0.0),
        )
    except (TypeError, ValueError) as exc:
        raise UpstoxDataError(f"Quote fields were not numeric: {exc}") from exc


def empty_candle_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=CANDLE_COLUMNS)


def _extract_candles(payload: dict[str, Any]) -> list[list[Any]]:
    data = payload.get("data") or {}
    candles = data.get("candles") if isinstance(data, dict) else None
    if candles is None:
        return []
    if not isinstance(candles, list):
        raise UpstoxDataError("Candle payload is not a list")
    return candles


def _candles_to_frame(candles: list[list[Any]]) -> pd.DataFrame:
    if not candles:
        return empty_candle_frame()
    rows: list[dict[str, Any]] = []
    for item in candles:
        if not isinstance(item, (list, tuple)) or len(item) < 6:
            continue
        stamp = pd.to_datetime(item[0], utc=False)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize(IST)
        else:
            stamp = stamp.tz_convert(IST)
        try:
            rows.append(
                {
                    "timestamp": stamp,
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "close": float(item[4]),
                    "volume": float(item[5]),
                }
            )
        except (TypeError, ValueError):
            continue
    if not rows:
        return empty_candle_frame()
    return pd.DataFrame(rows, columns=CANDLE_COLUMNS)


def _token_from_env() -> str:
    from config.settings import load_settings

    token = load_settings().upstox_access_token
    if not token:
        raise UpstoxDataError("UPSTOX_ACCESS_TOKEN is not set")
    return token


def localize_ist(series: pd.Series) -> pd.Series:
    converted = pd.to_datetime(series, utc=False)
    if converted.dt.tz is None:
        return converted.dt.tz_localize(IST)
    return converted.dt.tz_convert(IST)
