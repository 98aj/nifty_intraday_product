"""Bank Nifty monthly options desk. Registered from `app.py`.

Buys one at-the-money call or put after a 5-minute close through the
09:15–14:00 session range. The paper book is separate from the Nifty desk.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from config.settings import IST, PROJECT_ROOT, Settings, load_settings
from modules.journal.trade_journal import TradeJournal, max_drawdown
from modules.options.selector import (
    FLATTEN_AT,
    OptionQuote,
    choose_atm,
    listed_lot_size,
    manage_exit,
    nearest_listed_expiry,
    order_for_lots,
    parse_chain_rows,
)
from modules.risk.risk_manager import BANKNIFTY_PORTFOLIO_PATH, PaperTradingBroker
from modules.strategy.banknifty_late import (
    MAX_LOSS_INR,
    PREMIUM_STOP_PCT,
    PREMIUM_TARGET_PCT,
    CentralPivot,
    SessionRange,
    calculate_session_range,
    evaluate_late_break,
    late_entry_allowed,
    previous_day_cpr,
)
from modules.upstox.client import UpstoxClient
from modules.upstox.market_data import get_full_quote, get_historical_candles
from modules.upstox.option_chain import BANKNIFTY_INDEX_KEY, fetch_nifty_contracts, fetch_option_chain
from modules.upstox.orders import place_mis_market_order

LOGGER = logging.getLogger("banknifty.desk")
REFRESH_SECONDS = 2
PAPER = "PAPER"
LIVE = "LIVE"
DESTINATION_LABELS = {PAPER: "Paper trade", LIVE: "Live trade"}
JOURNAL_DIR = PROJECT_ROOT / "data" / "banknifty"


def render_desk() -> None:
    settings = load_settings()
    _bootstrap()
    now = datetime.now(IST)
    broker = PaperTradingBroker(BANKNIFTY_PORTFOLIO_PATH)
    destination, paused = _top_bar()
    hero_box = st.empty()
    level_box = st.empty()
    cpr_box = st.empty()
    notice_box = st.empty()

    index_error = ""
    spot = 0.0
    candles = pd.DataFrame()
    session = calculate_session_range(candles, now=now)
    cpr = previous_day_cpr(candles, now=now)
    signal = "SCANNING"
    contract: OptionQuote | None = None
    sample: OptionQuote | None = None
    chain_error = ""
    if settings.upstox_configured:
        spot, candles, index_error = _load_index(settings)
        session = calculate_session_range(candles, now=now)
        cpr = previous_day_cpr(candles, now=now)
        if session.ready and spot > 0:
            signal = evaluate_late_break(candles, session.high_boundary, session.low_boundary, now)
        contract, sample, chain_error = _load_contract(settings, spot, signal, now)

    capital, lots, stop_frac, target_frac, max_loss = _money_row()
    preview, lot_note = (None, "")
    if sample is not None:
        preview, lot_note = order_for_lots(sample, lots, capital, stop_frac, target_frac, max_loss)
    order = preview if contract is not None else None
    _show_lot_cost(sample, preview, lot_note, lots, max_loss)

    _mark_and_exit(broker, settings, now)
    if not paused:
        _maybe_enter(broker, settings, destination, now, signal, order)

    _paint_market(
        hero_box,
        level_box,
        notice_box,
        spot,
        session,
        cpr,
        signal,
        order,
        lot_note,
        paused,
        broker,
        now,
        index_error,
        chain_error,
    )
    _show_cpr(cpr_box, cpr)
    _show_ticket(order, paused, broker, now)
    _live_gate(settings, destination)
    _render_book(broker, capital)

    @st.fragment(run_every=REFRESH_SECONDS)
    def _beat() -> None:
        if st.session_state.get("bn_machine_paused"):
            return
        beat_now = datetime.now(IST)
        before = str(st.session_state.get("bn_flash") or "")
        _mark_and_exit(broker, settings, beat_now)
        beat_signal, beat_order = _live_setup(
            settings, beat_now, capital, lots, stop_frac, target_frac, max_loss
        )
        _maybe_enter(broker, settings, destination, beat_now, beat_signal, beat_order)
        if str(st.session_state.get("bn_flash") or "") != before:
            st.rerun()

    _beat()
    st.caption(
        f"{DESTINATION_LABELS[destination]} · {now.strftime('%H:%M')} IST · sells by {FLATTEN_AT.strftime('%H:%M')}"
    )


def _bootstrap() -> None:
    st.session_state.setdefault("bn_destination", PAPER)
    st.session_state.setdefault("bn_initial_capital", 500_000.0)
    st.session_state.setdefault("bn_trade_lots", 1)
    st.session_state.setdefault("bn_stop_pct", PREMIUM_STOP_PCT)
    st.session_state.setdefault("bn_target_pct", PREMIUM_TARGET_PCT)
    st.session_state.setdefault("bn_max_loss", MAX_LOSS_INR)
    st.session_state.setdefault("bn_machine_paused", False)
    st.session_state.setdefault("bn_live_armed", False)
    st.session_state.setdefault("bn_flash", "")
    st.session_state.setdefault("bn_flash_error", "")


def _top_bar() -> tuple[str, bool]:
    title, mode, pause = st.columns([2.3, 1.7, 1.5], vertical_alignment="center")
    title.markdown('<p class="desk-title">Bank Nifty monthly options</p>', unsafe_allow_html=True)
    title.markdown(
        '<p class="desk-sub">After 2:00 pm it buys one call or put, then sells it for you.</p>',
        unsafe_allow_html=True,
    )
    if st.session_state.bn_destination not in DESTINATION_LABELS:
        st.session_state.bn_destination = PAPER
    destination = mode.radio(
        "Mode",
        [PAPER, LIVE],
        format_func=lambda key: DESTINATION_LABELS[key],
        key="bn_destination",
        horizontal=True,
    )
    paused = bool(st.session_state.bn_machine_paused)
    if paused:
        if pause.button("Paused — resume buys", key="bn_resume_button"):
            st.session_state.bn_machine_paused = False
            st.rerun()
    elif pause.button("Pause new buys", key="bn_pause_button"):
        st.session_state.bn_machine_paused = True
        st.rerun()
    return destination, paused


def _money_row() -> tuple[float, int, float, float, float]:
    capital_col, lots_col, stop_col, target_col, loss_col = st.columns(5)
    capital = capital_col.number_input(
        "Starting capital (₹)",
        min_value=1_000.0,
        max_value=50_000_000.0,
        step=10_000.0,
        format="%.0f",
        key="bn_initial_capital",
    )
    lots = lots_col.number_input("Lots", min_value=1, max_value=20, step=1, key="bn_trade_lots")
    stop_pct = stop_col.number_input(
        "Sell if price falls (%)",
        min_value=10,
        max_value=40,
        step=5,
        key="bn_stop_pct",
    )
    target_pct = target_col.number_input(
        "Sell if price rises (%)",
        min_value=20,
        max_value=80,
        step=5,
        key="bn_target_pct",
    )
    max_loss = loss_col.number_input(
        "Max loss (₹)",
        min_value=1_000.0,
        max_value=50_000.0,
        step=500.0,
        format="%.0f",
        key="bn_max_loss",
    )
    return float(capital), int(lots), float(stop_pct) / 100.0, float(target_pct) / 100.0, float(max_loss)


def _show_lot_cost(sample: OptionQuote | None, preview, note: str, lots: int, max_loss: float) -> None:
    if sample is None:
        st.caption(
            "The lot size comes from the Bank Nifty contract. "
            f"A trade is skipped if the cash exceeds your capital, or the stop can lose more than ₹{max_loss:,.0f}."
        )
        return
    kind = "call" if sample.option_type == "CE" else "put"
    if preview is None:
        st.warning(note)
        return
    st.caption(
        f"{lots} lot(s) = {preview.quantity} quantity. "
        f"A {kind} at ₹{preview.premium:,.2f} needs ₹{preview.cash_required:,.0f}. "
        f"If the stop hits, the loss is about ₹{preview.risk_inr:,.0f}."
    )


def _live_gate(settings: Settings, destination: str) -> None:
    if destination != LIVE:
        st.session_state.bn_live_armed = False
        return
    st.checkbox("Send real Upstox orders", key="bn_live_armed")
    if not settings.upstox_configured:
        st.warning("Add your Upstox token in the .env file.")
    elif not settings.live_execution_enabled:
        st.warning("Real orders stay blocked until EXECUTION_MODE=LIVE in the .env file.")


def _paint_market(
    hero_box,
    level_box,
    notice_box,
    spot,
    session: SessionRange,
    cpr: CentralPivot,
    signal,
    order,
    lot_note,
    paused,
    broker,
    now,
    index_error,
    chain_error,
) -> None:
    allowed, why = late_entry_allowed(now, signal)
    traded = _traded_today(broker, now)
    title, body, tone = _today_story(
        paused,
        signal,
        order,
        lot_note,
        traded,
        broker.open_positions(),
        allowed,
        why,
    )
    hero_box.markdown(
        f'<div class="hero {tone}"><div class="kicker">Right now</div>'
        f'<div class="action">{title}</div><p>{body}</p></div>',
        unsafe_allow_html=True,
    )
    high = f"{session.high_boundary:,.0f}" if session.ready else "after 2:00"
    low = f"{session.low_boundary:,.0f}" if session.ready else "after 2:00"
    if cpr.ready:
        cpr_value = f"{'Narrow' if cpr.narrow else 'Wide'} {cpr.width_pct:.2f}%"
    else:
        cpr_value = "—"
    cards = [
        ("Bank Nifty", f"{spot:,.0f}" if spot else "—"),
        ("Call above", high),
        ("Put below", low),
        ("Prior CPR", cpr_value),
    ]
    with level_box.container():
        for column, (label, value) in zip(st.columns(4), cards):
            column.markdown(
                f'<div class="metric-card"><div class="metric-label">{label}</div>'
                f'<div class="metric-value">{value}</div></div>',
                unsafe_allow_html=True,
            )
    flash = str(st.session_state.get("bn_flash") or "")
    error = str(st.session_state.get("bn_flash_error") or "")
    st.session_state.bn_flash = ""
    st.session_state.bn_flash_error = ""
    problem = " ".join(text for text in (index_error, chain_error, error) if text)
    if problem:
        notice_box.warning(problem)
    elif flash:
        notice_box.success(flash)


def _show_cpr(cpr_box, cpr: CentralPivot) -> None:
    if cpr.ready:
        kind = "Narrow" if cpr.narrow else "Wide"
        title = f"{kind} CPR from {cpr.session_date}"
        body = (
            f"Pivot {cpr.pivot:,.0f}, band {cpr.bottom:,.0f} to {cpr.top:,.0f}. "
            "A narrow band means the prior session was tight. It is context only and does not block a trade."
        )
    else:
        title = "Prior CPR"
        body = "The previous session is not in the loaded candles yet. That does not block a trade."
    cpr_box.markdown(
        f'<div class="guide-card"><h4>{title}</h4><p>{body}</p></div>',
        unsafe_allow_html=True,
    )


def _show_ticket(order, paused, broker, now) -> None:
    if order is None or paused or _traded_today(broker, now):
        return
    kind = "call" if order.contract.option_type == "CE" else "put"
    st.markdown(
        f'<div class="ticket"><h3>It will buy this {kind}</h3>'
        f"<p><strong>{order.contract.trading_symbol}</strong> · {order.lots} lot"
        f"{'' if order.lots == 1 else 's'} · {order.quantity} quantity</p>"
        f"<p>About ₹{order.premium:,.2f} per unit · ₹{order.cash_required:,.0f} cash</p>"
        f"<p>Sell at ₹{order.stop_premium:,.2f}, at ₹{order.target_premium:,.2f}, or at 3:00 pm.</p></div>",
        unsafe_allow_html=True,
    )


def _today_story(paused, signal, order, lot_note, traded, opens, allowed, why) -> tuple[str, str, str]:
    if paused:
        return (
            "Paused",
            "It will not buy a new option. Anything already open is still sold if the price hits your stop, your target, or 3:00 pm.",
            "wait",
        )
    if opens:
        return (
            "Holding an option",
            "It is watching the price. The option is sold if it falls to your stop, rises to your target, or at 3:00 pm.",
            "hold",
        )
    if traded:
        return (
            "Finished for today",
            "It buys one option a day. Leave this window open again from 9:15 am on the next trading day.",
            "wait",
        )
    if "closed today" in why.lower():
        return ("Market is closed", "Bank Nifty options trade Monday to Friday. Nothing is bought today.", "wait")
    if "14:00" in why:
        return (
            "Marking today's high and low",
            "Until 2:00 pm it only watches Bank Nifty. No option is bought during that window.",
            "wait",
        )
    if "14:45" in why:
        return ("Too late for a new buy", "New buys stop at 2:45 pm. Anything still open is sold by 3:00 pm.", "wait")
    if signal == "BUY" and order is not None:
        return (
            "Bank Nifty broke up",
            "A 5-minute candle closed above the session high, so it buys a call and sells that call later.",
            "go",
        )
    if signal == "SELL" and order is not None:
        return (
            "Bank Nifty broke down",
            "A 5-minute candle closed below the session low, so it buys a put and sells that put later.",
            "go",
        )
    if signal in {"BUY", "SELL"} and order is None:
        return (
            "The break does not fit your money",
            lot_note or "One lot would risk more than you allowed, so it stays out.",
            "stop",
        )
    if not allowed:
        return (
            "Waiting for a close outside the range",
            "Bank Nifty is still between the session high and low. No option until a 5-minute candle closes outside it.",
            "wait",
        )
    return ("Waiting", why or "The machine is watching Bank Nifty.", "wait")


def _render_book(broker: PaperTradingBroker, capital: float) -> None:
    journal = TradeJournal(JOURNAL_DIR)
    journal.backfill(broker.open_positions() + broker.closed_positions())
    sheet = journal.table(capital)
    closed_pl = 0.0
    if not sheet.empty and "Result" in sheet.columns:
        closed_pl = pd.to_numeric(sheet["Result"], errors="coerce").fillna(0).sum()
    drop, drop_pct = max_drawdown(sheet)
    st.caption(f"{len(sheet)} trades in the Bank Nifty journal · paper and live both go in this sheet")
    result_col, drop_col, pct_col = st.columns(3)
    result_col.metric("Finished result", f"₹{closed_pl:,.0f}")
    drop_col.metric("Drawdown", f"₹{drop:,.0f}")
    pct_col.metric("Drawdown %", f"{drop_pct:.2f}%")
    st.caption("Drawdown is the largest drop from the highest account value, using the starting capital above.")
    if sheet.empty:
        st.info("No trades yet. Each paper or live trade is written into data/banknifty/trade_journal.xlsx.")
    else:
        st.dataframe(sheet, width="stretch", hide_index=True, height=360)
    st.caption("Each trade is saved in the project at data/banknifty/trade_journal.xlsx.")


def _live_setup(
    settings: Settings,
    now: datetime,
    capital: float,
    lots: int,
    stop_frac: float,
    target_frac: float,
    max_loss: float,
):
    signal = "SCANNING"
    order = None
    if not settings.upstox_configured:
        return signal, order
    spot, candles, _index_error = _load_index(settings)
    session = calculate_session_range(candles, now=now)
    if session.ready and spot > 0:
        signal = evaluate_late_break(candles, session.high_boundary, session.low_boundary, now)
    contract, _sample, _chain_error = _load_contract(settings, spot, signal, now)
    if contract is not None:
        order, _note = order_for_lots(contract, lots, capital, stop_frac, target_frac, max_loss)
    return signal, order


def _maybe_enter(broker, settings: Settings, destination: str, now: datetime, signal: str, order) -> None:
    if order is None or _traded_today(broker, now):
        return
    allowed, _why = late_entry_allowed(now, signal)
    if not allowed:
        return
    if destination == LIVE and not _live_ready(settings):
        return
    live_id = ""
    if destination == LIVE:
        try:
            placed = place_mis_market_order(
                instrument_token=order.contract.instrument_key,
                side="BUY",
                quantity=order.quantity,
                symbol=order.contract.trading_symbol,
                access_token=settings.upstox_access_token,
                tag="BN-OPT",
            )
            live_id = placed.order_id
        except Exception as exc:
            st.session_state.bn_flash_error = f"Live option order failed: {exc}"
            return
    filled = broker.fill(
        order.contract.trading_symbol,
        "BUY",
        order.quantity,
        order.premium,
        order.stop_premium,
        order.contract.instrument_key,
        destination=destination,
        live_order_id=live_id,
        extra={
            "target_premium": order.target_premium,
            "option_type": order.contract.option_type,
            "strike": order.contract.strike,
            "expiry": order.contract.expiry,
            "lots": order.lots,
        },
    )
    TradeJournal(JOURNAL_DIR).record(filled)
    prefix = "LIVE" if destination == LIVE else "PAPER"
    st.session_state.bn_flash = (
        f"{prefix} BUY {order.lots} lot(s) {order.contract.trading_symbol} @ ₹{order.premium:,.2f}"
        + (f" · {live_id}" if live_id else "")
    )


def _mark_and_exit(broker, settings: Settings, now: datetime) -> None:
    opens = broker.open_positions()
    if not opens:
        return
    prices: dict[str, float] = {}
    for row in opens:
        token = str(row.get("instrument_token") or "")
        symbol = str(row.get("Ticker") or "")
        if token and settings.upstox_configured:
            try:
                prices[symbol] = get_full_quote(token, access_token=settings.upstox_access_token).last_price
            except Exception:
                LOGGER.exception("Option quote failed for %s", symbol)
        if symbol not in prices:
            prices[symbol] = float(row.get("ExecutionPrice") or 0.0)
    broker.mark_to_market(prices)
    for row in list(broker.open_positions()):
        symbol = str(row.get("Ticker") or "")
        reason = manage_exit(
            float(row.get("ExecutionPrice") or 0.0),
            float(prices.get(symbol) or 0.0),
            float(row.get("StopLoss") or 0.0),
            float(row.get("target_premium") or 0.0),
            now,
        )
        if not reason:
            continue
        if not _sell_live(row, settings):
            continue
        for closed in broker.close_tickers({symbol}, prices, reason):
            TradeJournal(JOURNAL_DIR).record(closed)
        st.session_state.bn_flash = f"{reason} — sold {symbol}."


def _sell_live(row: dict, settings: Settings) -> bool:
    if row.get("destination") != "LIVE":
        return True
    if not settings.live_execution_enabled:
        st.session_state.bn_flash_error = "Live sell skipped — EXECUTION_MODE is MOCK."
        return False
    try:
        place_mis_market_order(
            instrument_token=str(row.get("instrument_token") or ""),
            side="SELL",
            quantity=int(row.get("Quantity") or 0),
            symbol=str(row.get("Ticker") or ""),
            access_token=settings.upstox_access_token,
            tag="BN-EXIT",
        )
        return True
    except Exception as exc:
        st.session_state.bn_flash_error = f"Live exit failed for {row.get('Ticker')}: {exc}"
        return False


def _traded_today(broker, now: datetime) -> bool:
    day = now.date().isoformat()
    rows = broker.open_positions() + broker.closed_positions()
    return any(str(row.get("Timestamp") or "").startswith(day) for row in rows)


def _live_ready(settings: Settings) -> bool:
    return bool(
        st.session_state.get("bn_destination") == LIVE
        and st.session_state.get("bn_live_armed")
        and settings.live_execution_enabled
        and settings.upstox_configured
    )


def _load_index(settings: Settings) -> tuple[float, pd.DataFrame, str]:
    try:
        quote, candles = _cached_index(settings.upstox_access_token)
        return quote.last_price, candles, ""
    except Exception as exc:
        return 0.0, pd.DataFrame(), f"Bank Nifty index feed failed: {exc}"


@st.cache_data(ttl=30, show_spinner=False)
def _cached_index(access_token: str):
    client = UpstoxClient(access_token)
    quote = get_full_quote(BANKNIFTY_INDEX_KEY, client=client)
    candles = get_historical_candles(
        BANKNIFTY_INDEX_KEY,
        interval="5minute",
        from_date=datetime.now(IST).date() - timedelta(days=7),
        client=client,
    )
    return quote, candles


def _load_contract(
    settings: Settings, spot: float, signal: str, now: datetime
) -> tuple[OptionQuote | None, OptionQuote | None, str]:
    """Return the tradable monthly contract, a sample for pricing, and an error."""

    if spot <= 0:
        return None, None, ""
    try:
        rows = _cached_contracts(settings.upstox_access_token)
        expiry = nearest_listed_expiry(rows, now.date())
        if not expiry:
            return None, None, "No Bank Nifty expiry on or after today."
        lot_size = listed_lot_size(rows, expiry)
        if lot_size is None:
            return None, None, "Bank Nifty lot size is missing, so no order is sent."
        chain = _cached_chain(settings.upstox_access_token, expiry)
        quotes = [row for row in parse_chain_rows(chain, expiry, lot_size) if row.lot_size > 0]
        sample_side = signal if signal in {"BUY", "SELL"} else "BUY"
        sample = choose_atm(quotes, spot, sample_side)
        contract = sample if signal in {"BUY", "SELL"} else None
        if sample is None:
            return None, None, "No at-the-money option on today's chain."
        return contract, sample, ""
    except Exception as exc:
        return None, None, f"Option chain failed: {exc}"


@st.cache_data(ttl=300, show_spinner=False)
def _cached_contracts(access_token: str) -> list[dict]:
    return fetch_nifty_contracts(UpstoxClient(access_token), BANKNIFTY_INDEX_KEY)


@st.cache_data(ttl=15, show_spinner=False)
def _cached_chain(access_token: str, expiry: str) -> list[dict]:
    return fetch_option_chain(UpstoxClient(access_token), expiry, BANKNIFTY_INDEX_KEY)


render_desk()
