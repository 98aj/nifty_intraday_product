"""Order clock for the instance. It does not decide a new Nifty rule.

The Live desk page used to be the only place a buy could be sent, so a browser
had to stay open. This process calls those same checks every few seconds.
The pages stay as the screen you open when you want to look.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from contextlib import contextmanager
from datetime import datetime, time as wall_clock, timedelta
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from config.settings import IST, PROJECT_ROOT, Settings, load_settings
from modules.journal.trade_journal import TradeJournal
from modules.options.selector import (
    OptionQuote,
    affordable_order,
    entry_allowed,
    lot_size_for,
    manage_exit,
    nearest_weekly_expiry,
    parse_chain_rows,
)
from modules.risk.risk_manager import PaperTradingBroker
from modules.strategy.orb import calculate_opening_range, evaluate_market_signals
from modules.upstox.feed import get_feed
from modules.upstox.market_data import get_full_quote, get_historical_candles
from modules.upstox.option_chain import (
    NIFTY_INDEX_KEY,
    fetch_nifty_contracts,
    fetch_option_chain,
)

LOGGER = logging.getLogger("desk.loop")
STATUS_PATH = PROJECT_ROOT / "data" / "desk_loop_status.json"
LOCK_PATH = PROJECT_ROOT / "data" / "desk_loop.lock"
NIFTY_CONTROLS = PROJECT_ROOT / "data" / "nifty_desk.json"

# A desk's controls file is chosen by name. New desks register here.
CONTROLS_PATHS = {
    "nifty": NIFTY_CONTROLS,
}
HEARTBEAT_SECONDS = 30
LOOP_SECONDS = 5

_CACHE: dict[str, Any] = {}


def loop_is_fresh(now: datetime | None = None, path: Path | None = None) -> bool:
    """True when the instance loop has reported in the last half minute."""

    file = path or STATUS_PATH
    if not file.exists():
        return False
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
        stamp = datetime.fromisoformat(str(raw.get("updated_at") or ""))
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=IST)
    current = now or datetime.now(IST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=IST)
    age = (current.astimezone(IST) - stamp.astimezone(IST)).total_seconds()
    return 0 <= age <= HEARTBEAT_SECONDS


def loop_caption(now: datetime | None = None) -> str:
    if not loop_is_fresh(now):
        return "Instance loop is off. Leave this page open, or a buy will not be sent."
    try:
        raw = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    nifty = str(raw.get("nifty") or "watching")
    return f"Instance loop is on. This browser can be closed. Nifty: {nifty}"


def save_desk_controls(name: str, payload: dict[str, Any], path: Path | None = None) -> None:
    file = path or CONTROLS_PATHS.get(name, NIFTY_CONTROLS)
    file.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2)
    tmp = file.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        tmp.replace(file)
    except OSError:
        file.write_text(text, encoding="utf-8")


def load_nifty_controls(path: Path | None = None) -> dict[str, Any]:
    raw = _read_json(path or NIFTY_CONTROLS)
    return {
        "capital": _float(raw.get("capital"), 500_000.0),
        "lots": max(1, _int(raw.get("lots"), 1)),
        "stop_frac": _float(raw.get("stop_frac"), 0.40),
        "target_frac": _float(raw.get("target_frac"), 0.60),
        "max_loss": _float(raw.get("max_loss"), 3000.0),
        "destination": _mode(raw.get("destination")),
        "paused": bool(raw.get("paused")),
        "live_armed": bool(raw.get("live_armed")),
    }


def run_nifty_cycle(
    *,
    now: datetime,
    broker: PaperTradingBroker,
    settings: Settings,
    controls: dict[str, Any],
    spot: float,
    candles: pd.DataFrame,
    quotes: list[OptionQuote] | None = None,
    journal: TradeJournal | None = None,
    prices: dict[str, float] | None = None,
    place_order: Any = None,
    buy_tag: str = "NIFTY-OPT",
    exit_tag: str = "OPT-EXIT",
) -> str:
    """One pass of the existing Nifty opening-range rule. No new entry condition."""

    book = journal or TradeJournal()
    opening = calculate_opening_range(candles, now=now)
    signal = "SCANNING"
    if opening.ready and spot > 0:
        signal = evaluate_market_signals(spot, opening.high_boundary, opening.low_boundary)
    exit_note = _exit_open(broker, settings, now, book, prices, place_order, exit_tag)
    if controls.get("paused"):
        return exit_note or "Paused. No new Nifty buy."
    if _traded_today(broker, now):
        return exit_note or "Already bought one Nifty option today."
    allowed, why = entry_allowed(now, signal)
    if not allowed:
        return exit_note or why
    if not quotes or signal not in {"BUY", "SELL"}:
        return "No at-the-money Nifty option."
    order, note = affordable_order(
        quotes,
        spot,
        signal,
        int(controls["lots"]),
        float(controls["capital"]),
        float(controls["stop_frac"]),
        float(controls["target_frac"]),
        _loss_cap(controls),
    )
    if order is None:
        return note or "One lot does not fit the loss limit."
    if controls.get("destination") == "LIVE" and not _live_ready(settings, controls):
        return "Live trade is selected, but real orders are not armed."
    return _buy(broker, settings, controls, now, order, book, place_order, buy_tag)


@contextmanager
def desk_lock() -> Iterator[None]:
    """One order at a time, so the page and the instance loop cannot both buy."""

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "a+b")
    try:
        if handle.seek(0, 2) < 1:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def serve() -> None:
    _configure_logging()
    LOGGER.info("Instance trade loop started")
    _write_status(datetime.now(IST), "Starting.")
    while True:
        now = datetime.now(IST)
        nifty_note = "Watching Nifty."
        try:
            settings = load_settings()
            if not settings.upstox_configured:
                nifty_note = "Add the Upstox token in the .env file."
            elif not market_is_open(now):
                nifty_note = "Market closed. No quotes pulled."
            else:
                # Keep the index ticks flowing before the first trade decision.
                get_feed(settings.upstox_access_token, [NIFTY_INDEX_KEY])
                nifty_note = _nifty_live(settings, now)
        except Exception as exc:
            LOGGER.exception("Instance trade loop failed")
            nifty_note = f"Trade loop error: {exc.__class__.__name__}"
        _write_status(now, nifty_note)
        _remember(f"Nifty: {nifty_note}")
        _sleep(now)


def main() -> None:
    serve()


def _nifty_live(settings: Settings, now: datetime) -> str:
    controls = load_nifty_controls()
    broker = PaperTradingBroker()
    spot, candles, quotes = _nifty_market(settings, now)
    opening = calculate_opening_range(candles, now=now)
    signal = "SCANNING"
    if opening.ready and spot > 0:
        signal = evaluate_market_signals(spot, opening.high_boundary, opening.low_boundary)
    with desk_lock():
        return run_nifty_cycle(
            now=now,
            broker=broker,
            settings=settings,
            controls=controls,
            spot=spot,
            candles=candles,
            quotes=quotes if signal in {"BUY", "SELL"} else None,
        )


def _spot_ltp(key: str, settings: Settings, client: Any) -> float:
    """Index spot: the latest streamed tick, or the REST quote when none is fresh.

    Only the *source* of the number changes. The value handed to the opening-range
    and signal functions is the same ``float`` it has always been, so the rule is
    untouched; it simply reacts to a fresher price.
    """

    if settings.upstox_configured:
        live = get_feed(settings.upstox_access_token, [key]).last_price(key)
        if live is not None:
            return float(live)
    return float(get_full_quote(key, client=client).last_price)


def _nifty_market(settings: Settings, now: datetime) -> tuple[float, pd.DataFrame, list[OptionQuote]]:
    from modules.upstox.client import UpstoxClient

    client = UpstoxClient(settings.upstox_access_token)
    spot = _spot_ltp(NIFTY_INDEX_KEY, settings, client)
    candles = _candles(client, NIFTY_INDEX_KEY, now, days=3)
    opening = calculate_opening_range(candles, now=now)
    signal = "SCANNING"
    if opening.ready and spot > 0:
        signal = evaluate_market_signals(spot, opening.high_boundary, opening.low_boundary)
    if spot <= 0 or signal not in {"BUY", "SELL"}:
        return spot, candles, []
    rows = fetch_nifty_contracts(client)
    expiry = nearest_weekly_expiry(rows, now.date())
    if not expiry:
        return spot, candles, []
    chain = _chain(client, NIFTY_INDEX_KEY, expiry)
    quotes = parse_chain_rows(chain, expiry, lot_size_for(rows, expiry))
    return spot, candles, quotes


def _candles(client: Any, key: str, now: datetime, days: int) -> pd.DataFrame:
    clock = time.monotonic()
    cached = _CACHE.get(f"candles:{key}")
    if cached and clock - float(cached[0]) < 20:
        return cached[1]
    frame = get_historical_candles(
        key,
        interval="5minute",
        from_date=now.date() - timedelta(days=days),
        client=client,
    )
    _CACHE[f"candles:{key}"] = (clock, frame)
    return frame


def _chain(client: Any, key: str, expiry: str) -> list[dict]:
    clock = time.monotonic()
    name = f"chain:{key}:{expiry}"
    cached = _CACHE.get(name)
    if cached and clock - float(cached[0]) < 15:
        return cached[1]
    chain = fetch_option_chain(client, expiry, key)
    _CACHE[name] = (clock, chain)
    return chain


def _buy(broker, settings, controls, now, order, journal: TradeJournal, place_order: Any, tag: str) -> str:
    live_id = ""
    if controls.get("destination") == "LIVE":
        send = place_order or _send_live
        placed = send(
            _order_request(order.contract, "BUY", order.quantity, tag),
            settings,
        )
        live_id = str(getattr(placed, "order_id", "") or "")
    filled = broker.fill(
        order.contract.trading_symbol,
        "BUY",
        order.quantity,
        order.premium,
        order.stop_premium,
        order.contract.instrument_key,
        destination=str(controls.get("destination") or "PAPER"),
        live_order_id=live_id,
        extra={
            "target_premium": order.target_premium,
            "option_type": order.contract.option_type,
            "strike": order.contract.strike,
            "expiry": order.contract.expiry,
            "lots": order.lots,
        },
    )
    journal.record(filled)
    prefix = "LIVE" if controls.get("destination") == "LIVE" else "PAPER"
    return f"{prefix} BUY {order.lots} lot(s) {order.contract.trading_symbol} @ ₹{order.premium:,.2f}"


def _exit_open(
    broker,
    settings,
    now,
    journal,
    prices,
    place_order,
    tag: str,
    trail_activate_frac: float = 0.0,
    trail_frac: float = 0.0,
) -> str:
    opens = broker.open_positions()
    if not opens:
        return ""
    marks = dict(prices) if prices is not None else _live_prices(settings, opens)
    broker.mark_to_market(marks)
    # Only trailing desks track a high-water premium; fixed-stop desks pass 0.0.
    peaks = broker.bump_peaks(marks) if trail_frac > 0.0 else {}
    notes: list[str] = []
    for row in list(broker.open_positions()):
        symbol = str(row.get("Ticker") or "")
        reason = manage_exit(
            float(row.get("ExecutionPrice") or 0.0),
            float(marks.get(symbol) or 0.0),
            float(row.get("StopLoss") or 0.0),
            float(row.get("target_premium") or 0.0),
            now,
            peak_premium=float(peaks.get(symbol) or 0.0),
            trail_activate_frac=trail_activate_frac,
            trail_frac=trail_frac,
        )
        if not reason:
            continue
        if row.get("destination") == "LIVE":
            if not settings.live_execution_enabled:
                notes.append("Live sell skipped — EXECUTION_MODE is MOCK.")
                continue
            send = place_order or _send_live
            try:
                send(_order_request_from_row(row, "SELL", tag), settings)
            except Exception as exc:
                notes.append(f"Live exit failed for {symbol}: {exc}")
                continue
        for closed in broker.close_tickers({symbol}, marks, reason):
            journal.record(closed)
        notes.append(f"{reason} — sold {symbol}.")
    return " ".join(notes)


def _live_prices(settings: Settings, rows: list[dict]) -> dict[str, float]:
    """Marks for the open options, stream-first so stops react on the tick.

    For each position: use the latest streamed tick if there is one; otherwise
    make sure the option is subscribed (so the next cycle has a tick) and fall
    back to the same REST quote used before, then to the entry price. The exit
    rule (``manage_exit``) sees the identical ``float`` mark either way.
    """

    prices: dict[str, float] = {}
    feed = get_feed(settings.upstox_access_token) if settings.upstox_configured else None
    for row in rows:
        symbol = str(row.get("Ticker") or "")
        token = str(row.get("instrument_token") or "")
        if token and settings.upstox_configured:
            live = feed.last_price(token) if feed else None
            if live is not None:
                prices[symbol] = float(live)
                continue
            if feed is not None:
                feed.subscribe([token])
            try:
                prices[symbol] = get_full_quote(token, access_token=settings.upstox_access_token).last_price
                continue
            except Exception:
                LOGGER.exception("Option quote failed for %s", symbol)
        prices[symbol] = float(row.get("ExecutionPrice") or 0.0)
    return prices


def _order_request(contract, side: str, quantity: int, tag: str):
    from modules.execution import OrderRequest

    return OrderRequest(
        instrument_token=contract.instrument_key,
        symbol=contract.trading_symbol,
        side=side,
        quantity=int(quantity),
        tag=tag,
        strike=float(contract.strike),
        expiry=str(contract.expiry),
        option_type=str(contract.option_type),
    )


def _order_request_from_row(row: dict, side: str, tag: str):
    from modules.execution import OrderRequest

    return OrderRequest(
        instrument_token=str(row.get("instrument_token") or ""),
        symbol=str(row.get("Ticker") or ""),
        side=side,
        quantity=int(row.get("Quantity") or 0),
        tag=tag,
        strike=float(row.get("strike") or 0.0),
        expiry=str(row.get("expiry") or ""),
        option_type=str(row.get("option_type") or ""),
    )


def _send_live(request: Any, settings: Settings) -> Any:
    from modules.execution import get_execution_broker

    return get_execution_broker(settings).place_order(request)


def _live_ready(settings: Settings, controls: dict[str, Any]) -> bool:
    return bool(
        controls.get("destination") == "LIVE"
        and controls.get("live_armed")
        and settings.live_execution_enabled
        and settings.upstox_configured
    )


def _traded_today(broker: PaperTradingBroker, now: datetime) -> bool:
    day = now.astimezone(IST).date().isoformat() if now.tzinfo else now.date().isoformat()
    rows = broker.open_positions() + broker.closed_positions()
    return any(str(row.get("Timestamp") or "").startswith(day) for row in rows)


def _write_status(now: datetime, nifty: str) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": now.isoformat(), "nifty": nifty}
    text = json.dumps(payload, indent=2)
    tmp = STATUS_PATH.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        tmp.replace(STATUS_PATH)
    except OSError:
        STATUS_PATH.write_text(text, encoding="utf-8")


def _remember(line: str) -> None:
    if getattr(_remember, "last", None) == line:
        return
    setattr(_remember, "last", line)
    LOGGER.info(line)


def market_is_open(now: datetime | None = None) -> bool:
    """True on a weekday between 9:08 and 15:35 IST, when quotes are worth pulling.

    The window opens a little before 9:15 so the first candle is ready, and stays
    open past the 15:15 square-off so a late exit can still go out. Outside it there
    is nothing to trade, so the page and the loop both stop hitting Upstox.
    """

    current = now or datetime.now(IST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=IST)
    clock = current.astimezone(IST).time().replace(tzinfo=None)
    return current.weekday() < 5 and wall_clock(9, 8) <= clock < wall_clock(15, 35)


def _sleep(now: datetime) -> None:
    time.sleep(LOOP_SECONDS if market_is_open(now) else 60)


def _configure_logging() -> None:
    if LOGGER.handlers:
        return
    path = PROJECT_ROOT / "data" / "desk_loop.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _mode(value: Any) -> str:
    mode = str(value or "PAPER").upper()
    return mode if mode in {"PAPER", "LIVE"} else "PAPER"


def _loss_cap(controls: dict[str, Any]) -> float | None:
    """Per-trade loss cap. The Nifty desk always carries ``max_loss``."""

    raw = controls.get("max_loss")
    return float(raw) if raw else None


if __name__ == "__main__":
    main()
