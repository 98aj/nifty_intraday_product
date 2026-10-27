"""Order clock for the instance. It does not decide a new Nifty or Bank Nifty rule.

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
from modules.copilot.option_quality import load_option_quality, quality_allows
from modules.journal.trade_journal import TradeJournal
from modules.options.selector import (
    OptionQuote,
    choose_atm,
    entry_allowed,
    listed_lot_size,
    lot_size_for,
    manage_exit,
    nearest_listed_expiry,
    nearest_weekly_expiry,
    order_for_lots,
    parse_chain_rows,
)
from modules.risk.risk_manager import BANKNIFTY_PORTFOLIO_PATH, PaperTradingBroker
from modules.strategy.banknifty_late import (
    MAX_LOSS_INR,
    PREMIUM_STOP_PCT,
    PREMIUM_TARGET_PCT,
    calculate_session_range,
    evaluate_late_break,
    late_entry_allowed,
)
from modules.strategy.orb import calculate_opening_range, evaluate_market_signals
from modules.upstox.market_data import get_full_quote, get_historical_candles
from modules.upstox.option_chain import (
    BANKNIFTY_INDEX_KEY,
    NIFTY_INDEX_KEY,
    fetch_nifty_contracts,
    fetch_option_chain,
)

LOGGER = logging.getLogger("desk.loop")
STATUS_PATH = PROJECT_ROOT / "data" / "desk_loop_status.json"
LOCK_PATH = PROJECT_ROOT / "data" / "desk_loop.lock"
NIFTY_CONTROLS = PROJECT_ROOT / "data" / "nifty_desk.json"
BANK_CONTROLS = PROJECT_ROOT / "data" / "banknifty_desk.json"
BANK_JOURNAL = PROJECT_ROOT / "data" / "banknifty"
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
    bank = str(raw.get("banknifty") or "watching")
    return f"Instance loop is on. This browser can be closed. Nifty: {nifty} Bank Nifty: {bank}"


def save_desk_controls(name: str, payload: dict[str, Any], path: Path | None = None) -> None:
    file = path or (NIFTY_CONTROLS if name == "nifty" else BANK_CONTROLS)
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


def load_bank_controls(path: Path | None = None) -> dict[str, Any]:
    raw = _read_json(path or BANK_CONTROLS)
    return {
        "capital": _float(raw.get("capital"), 500_000.0),
        "lots": max(1, _int(raw.get("lots"), 1)),
        "stop_frac": _float(raw.get("stop_frac"), PREMIUM_STOP_PCT / 100.0),
        "target_frac": _float(raw.get("target_frac"), PREMIUM_TARGET_PCT / 100.0),
        "max_loss": _float(raw.get("max_loss"), MAX_LOSS_INR),
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
    contract: OptionQuote | None,
    journal: TradeJournal | None = None,
    gemini_ok: bool = True,
    gemini_why: str = "",
    prices: dict[str, float] | None = None,
    place_order: Any = None,
) -> str:
    """One pass of the existing Nifty opening-range rule. No new entry condition."""

    book = journal or TradeJournal()
    opening = calculate_opening_range(candles, now=now)
    signal = "SCANNING"
    if opening.ready and spot > 0:
        signal = evaluate_market_signals(spot, opening.high_boundary, opening.low_boundary)
    exit_note = _exit_open(broker, settings, now, book, prices, place_order, "OPT-EXIT")
    if controls.get("paused"):
        return exit_note or "Paused. No new Nifty buy."
    if _traded_today(broker, now):
        return exit_note or "Already bought one Nifty option today."
    allowed, why = entry_allowed(now, signal)
    if not allowed:
        return exit_note or why
    if not gemini_ok:
        return gemini_why or "Gemini skipped this break."
    if contract is None or signal not in {"BUY", "SELL"}:
        return "No at-the-money Nifty option."
    order, note = order_for_lots(
        contract,
        int(controls["lots"]),
        float(controls["capital"]),
        float(controls["stop_frac"]),
        float(controls["target_frac"]),
        float(controls.get("max_loss") or 3000.0),
    )
    if order is None:
        return note or "One lot does not fit the loss limit."
    if controls.get("destination") == "LIVE" and not _live_ready(settings, controls):
        return "Live trade is selected, but real orders are not armed."
    return _buy(broker, settings, controls, now, order, book, place_order, "NIFTY-OPT")


def run_bank_cycle(
    *,
    now: datetime,
    broker: PaperTradingBroker,
    settings: Settings,
    controls: dict[str, Any],
    spot: float,
    candles: pd.DataFrame,
    contract: OptionQuote | None,
    journal: TradeJournal | None = None,
    prices: dict[str, float] | None = None,
    place_order: Any = None,
) -> str:
    """One pass of the existing Bank Nifty 14:00–14:45 rule."""

    book = journal or TradeJournal(BANK_JOURNAL)
    session = calculate_session_range(candles, now=now)
    signal = "SCANNING"
    if session.ready and spot > 0:
        signal = evaluate_late_break(candles, session.high_boundary, session.low_boundary, now)
    exit_note = _exit_open(broker, settings, now, book, prices, place_order, "BN-EXIT")
    if controls.get("paused"):
        return exit_note or "Paused. No new Bank Nifty buy."
    if _traded_today(broker, now):
        return exit_note or "Already bought one Bank Nifty option today."
    allowed, why = late_entry_allowed(now, signal)
    if not allowed:
        return exit_note or why
    if contract is None or signal not in {"BUY", "SELL"}:
        return "No at-the-money Bank Nifty option."
    order, note = order_for_lots(
        contract,
        int(controls["lots"]),
        float(controls["capital"]),
        float(controls["stop_frac"]),
        float(controls["target_frac"]),
        float(controls["max_loss"]),
    )
    if order is None:
        return note or "One lot does not fit the loss limit."
    if controls.get("destination") == "LIVE" and not _live_ready(settings, controls):
        return "Live trade is selected, but real orders are not armed."
    return _buy(broker, settings, controls, now, order, book, place_order, "BN-OPT")


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
    _write_status(datetime.now(IST), "Starting.", "Starting.")
    while True:
        now = datetime.now(IST)
        nifty_note = "Watching Nifty."
        bank_note = "Watching Bank Nifty."
        try:
            settings = load_settings()
            if settings.upstox_configured:
                nifty_note = _nifty_live(settings, now)
                bank_note = _bank_live(settings, now)
            else:
                nifty_note = bank_note = "Add the Upstox token in the .env file."
        except Exception as exc:
            LOGGER.exception("Instance trade loop failed")
            nifty_note = f"Trade loop error: {exc.__class__.__name__}"
        _write_status(now, nifty_note, bank_note)
        _remember(f"Nifty: {nifty_note} | Bank Nifty: {bank_note}")
        _sleep(now)


def main() -> None:
    serve()


def _nifty_live(settings: Settings, now: datetime) -> str:
    controls = load_nifty_controls()
    broker = PaperTradingBroker()
    spot, candles, contract = _nifty_market(settings, now)
    opening = calculate_opening_range(candles, now=now)
    signal = "SCANNING"
    if opening.ready and spot > 0:
        signal = evaluate_market_signals(spot, opening.high_boundary, opening.low_boundary)
    view = load_option_quality(
        now,
        spot,
        float(opening.high_boundary or 0),
        float(opening.low_boundary or 0),
        bool(opening.ready),
    )
    gemini_ok, gemini_why = quality_allows(signal, view)
    with desk_lock():
        return run_nifty_cycle(
            now=now,
            broker=broker,
            settings=settings,
            controls=controls,
            spot=spot,
            candles=candles,
            contract=contract if signal in {"BUY", "SELL"} else None,
            gemini_ok=gemini_ok,
            gemini_why=gemini_why,
        )


def _bank_live(settings: Settings, now: datetime) -> str:
    controls = load_bank_controls()
    broker = PaperTradingBroker(BANKNIFTY_PORTFOLIO_PATH)
    spot, candles, contract, signal = _bank_market(settings, now)
    with desk_lock():
        return run_bank_cycle(
            now=now,
            broker=broker,
            settings=settings,
            controls=controls,
            spot=spot,
            candles=candles,
            contract=contract if signal in {"BUY", "SELL"} else None,
        )


def _nifty_market(settings: Settings, now: datetime) -> tuple[float, pd.DataFrame, OptionQuote | None]:
    from modules.upstox.client import UpstoxClient

    client = UpstoxClient(settings.upstox_access_token)
    spot = float(get_full_quote(NIFTY_INDEX_KEY, client=client).last_price)
    candles = _candles(client, NIFTY_INDEX_KEY, now, days=3)
    opening = calculate_opening_range(candles, now=now)
    signal = "SCANNING"
    if opening.ready and spot > 0:
        signal = evaluate_market_signals(spot, opening.high_boundary, opening.low_boundary)
    if spot <= 0 or signal not in {"BUY", "SELL"}:
        return spot, candles, None
    rows = fetch_nifty_contracts(client)
    expiry = nearest_weekly_expiry(rows, now.date())
    if not expiry:
        return spot, candles, None
    chain = _chain(client, NIFTY_INDEX_KEY, expiry)
    quotes = parse_chain_rows(chain, expiry, lot_size_for(rows, expiry))
    return spot, candles, choose_atm(quotes, spot, signal)


def _bank_market(settings: Settings, now: datetime) -> tuple[float, pd.DataFrame, OptionQuote | None, str]:
    from modules.upstox.client import UpstoxClient

    client = UpstoxClient(settings.upstox_access_token)
    spot = float(get_full_quote(BANKNIFTY_INDEX_KEY, client=client).last_price)
    candles = _candles(client, BANKNIFTY_INDEX_KEY, now, days=7)
    session = calculate_session_range(candles, now=now)
    signal = "SCANNING"
    if session.ready and spot > 0:
        signal = evaluate_late_break(candles, session.high_boundary, session.low_boundary, now)
    if spot <= 0 or signal not in {"BUY", "SELL"}:
        return spot, candles, None, signal
    rows = fetch_nifty_contracts(client, BANKNIFTY_INDEX_KEY)
    expiry = nearest_listed_expiry(rows, now.date())
    lot_size = listed_lot_size(rows, expiry or "")
    if not expiry or lot_size is None:
        return spot, candles, None, signal
    chain = _chain(client, BANKNIFTY_INDEX_KEY, expiry)
    quotes = [row for row in parse_chain_rows(chain, expiry, lot_size) if row.lot_size > 0]
    return spot, candles, choose_atm(quotes, spot, signal), signal


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
            instrument_token=order.contract.instrument_key,
            side="BUY",
            quantity=order.quantity,
            symbol=order.contract.trading_symbol,
            access_token=settings.upstox_access_token,
            tag=tag,
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


def _exit_open(broker, settings, now, journal, prices, place_order, tag: str) -> str:
    opens = broker.open_positions()
    if not opens:
        return ""
    marks = dict(prices) if prices is not None else _live_prices(settings, opens)
    broker.mark_to_market(marks)
    notes: list[str] = []
    for row in list(broker.open_positions()):
        symbol = str(row.get("Ticker") or "")
        reason = manage_exit(
            float(row.get("ExecutionPrice") or 0.0),
            float(marks.get(symbol) or 0.0),
            float(row.get("StopLoss") or 0.0),
            float(row.get("target_premium") or 0.0),
            now,
        )
        if not reason:
            continue
        if row.get("destination") == "LIVE":
            if not settings.live_execution_enabled:
                notes.append("Live sell skipped — EXECUTION_MODE is MOCK.")
                continue
            send = place_order or _send_live
            try:
                send(
                    instrument_token=str(row.get("instrument_token") or ""),
                    side="SELL",
                    quantity=int(row.get("Quantity") or 0),
                    symbol=symbol,
                    access_token=settings.upstox_access_token,
                    tag=tag,
                )
            except Exception as exc:
                notes.append(f"Live exit failed for {symbol}: {exc}")
                continue
        for closed in broker.close_tickers({symbol}, marks, reason):
            journal.record(closed)
        notes.append(f"{reason} — sold {symbol}.")
    return " ".join(notes)


def _live_prices(settings: Settings, rows: list[dict]) -> dict[str, float]:
    prices: dict[str, float] = {}
    for row in rows:
        symbol = str(row.get("Ticker") or "")
        token = str(row.get("instrument_token") or "")
        if token and settings.upstox_configured:
            try:
                prices[symbol] = get_full_quote(token, access_token=settings.upstox_access_token).last_price
                continue
            except Exception:
                LOGGER.exception("Option quote failed for %s", symbol)
        prices[symbol] = float(row.get("ExecutionPrice") or 0.0)
    return prices


def _send_live(**kwargs: Any) -> Any:
    from modules.upstox.orders import place_mis_market_order

    return place_mis_market_order(**kwargs)


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


def _write_status(now: datetime, nifty: str, bank: str) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": now.isoformat(), "nifty": nifty, "banknifty": bank}
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


def _sleep(now: datetime) -> None:
    clock = now.astimezone(IST).time().replace(tzinfo=None)
    market = wall_clock(9, 10) <= clock < wall_clock(15, 35) and now.weekday() < 5
    time.sleep(LOOP_SECONDS if market else 60)


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


if __name__ == "__main__":
    main()
