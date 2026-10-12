"""Automated Nifty weekly options desk.

Paper or Live. The machine buys one ATM call or put after the Nifty opening
range breaks, then sells it on a premium stop, a premium target, or 15:15 IST.

`streamlit run app.py`
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import IST, PROJECT_ROOT, Settings, load_settings
from modules.copilot.option_quality import load_option_quality, quality_allows, quality_summary
from modules.journal.trade_journal import TradeJournal
from modules.options.backtest import replay_option_rule
from modules.options.selector import (
    FLATTEN_AT,
    OptionQuote,
    choose_atm,
    entry_allowed,
    lot_size_for,
    manage_exit,
    nearest_weekly_expiry,
    order_for_lots,
    parse_chain_rows,
)
from modules.risk.risk_manager import PaperTradingBroker
from modules.strategy.orb import calculate_opening_range, evaluate_market_signals
from modules.ui.styles import inject_css
from modules.upstox.client import UpstoxClient
from modules.upstox.market_data import get_full_quote, get_historical_candles
from modules.upstox.option_chain import NIFTY_INDEX_KEY, fetch_nifty_contracts, fetch_option_chain
from modules.upstox.orders import place_mis_market_order

LOGGER = logging.getLogger("desk")
REFRESH_SECONDS = 2
PAPER = "PAPER"
LIVE = "LIVE"
DESTINATION_LABELS = {PAPER: "Paper trade", LIVE: "Live trade"}


def render_desk() -> None:
    st.set_page_config(
        page_title="Nifty Options",
        layout="wide",
        page_icon="▣",
        initial_sidebar_state="collapsed",
    )
    inject_css()
    settings = load_settings()
    _bootstrap()
    now = datetime.now(IST)
    broker = PaperTradingBroker()
    destination, paused = _top_bar()
    hero_box = st.empty()
    level_box = st.empty()
    quality_box = st.empty()
    notice_box = st.empty()

    index_error = ""
    spot = 0.0
    candles = pd.DataFrame()
    opening = calculate_opening_range(candles, now=now)
    signal = "SCANNING"
    contract: OptionQuote | None = None
    sample: OptionQuote | None = None
    chain_error = ""
    if settings.upstox_configured:
        spot, candles, index_error = _load_index(settings)
        opening = calculate_opening_range(candles, now=now)
        if opening.ready and spot > 0:
            signal = evaluate_market_signals(spot, opening.high_boundary, opening.low_boundary)
        contract, sample, chain_error = _load_contract(settings, spot, signal, now)

    quality = load_option_quality(
        now,
        spot,
        float(opening.high_boundary or 0),
        float(opening.low_boundary or 0),
        bool(opening.ready),
    )
    allowed_by_gemini, gemini_why = quality_allows(signal, quality)

    capital, lots, stop_frac, target_frac = _money_row()
    preview, lot_note = (None, "")
    if sample is not None:
        preview, lot_note = order_for_lots(sample, lots, capital, stop_frac, target_frac)
    order = preview if contract is not None else None
    _show_lot_cost(sample, preview, lot_note, lots)

    _mark_and_exit(broker, settings, destination, now)
    if not paused and allowed_by_gemini:
        _maybe_enter(broker, settings, destination, now, signal, order)

    _paint_market(
        hero_box,
        level_box,
        notice_box,
        spot,
        opening,
        signal,
        order,
        lot_note,
        paused,
        broker,
        now,
        index_error,
        chain_error,
        allowed_by_gemini,
        gemini_why,
    )
    _show_quality(quality_box, quality)
    _show_ticket(order, paused, broker, now, allowed_by_gemini)
    _live_gate(settings, destination)

    book_tab, backtest_tab = st.tabs(["Trades", "Test"])
    with book_tab:
        _render_book(broker)
    with backtest_tab:
        _render_backtest(settings, capital, lots, stop_frac, target_frac)

    @st.fragment(run_every=REFRESH_SECONDS)
    def _beat() -> None:
        if st.session_state.get("machine_paused"):
            return
        beat_now = datetime.now(IST)
        before = str(st.session_state.get("flash") or "")
        _mark_and_exit(broker, settings, destination, beat_now)
        beat_signal, beat_order, beat_ok = _live_setup(
            settings, beat_now, capital, lots, stop_frac, target_frac
        )
        if beat_ok:
            _maybe_enter(broker, settings, destination, beat_now, beat_signal, beat_order)
        if str(st.session_state.get("flash") or "") != before:
            st.rerun()

    _beat()
    st.caption(f"{DESTINATION_LABELS[destination]} · {now.strftime('%H:%M')} IST · sells by {FLATTEN_AT.strftime('%H:%M')}")


def _bootstrap() -> None:
    st.session_state.setdefault("destination", PAPER)
    st.session_state.setdefault("initial_capital", 500_000.0)
    st.session_state.setdefault("trade_lots", 1)
    st.session_state.setdefault("premium_stop_pct", 40)
    st.session_state.setdefault("premium_target_pct", 50)
    st.session_state.setdefault("machine_paused", False)
    st.session_state.setdefault("live_armed", False)
    st.session_state.setdefault("flash", "")
    st.session_state.setdefault("flash_error", "")


def _top_bar() -> tuple[str, bool]:
    title, mode, pause = st.columns([2.3, 1.7, 1.5], vertical_alignment="center")
    title.markdown('<p class="desk-title">Nifty weekly options</p>', unsafe_allow_html=True)
    title.markdown(
        '<p class="desk-sub">It buys one call or put after 9:30, then sells it for you.</p>',
        unsafe_allow_html=True,
    )
    if st.session_state.destination not in DESTINATION_LABELS:
        st.session_state.destination = PAPER
    destination = mode.radio(
        "Mode",
        [PAPER, LIVE],
        format_func=lambda key: DESTINATION_LABELS[key],
        key="destination",
        horizontal=True,
    )
    paused = bool(st.session_state.machine_paused)
    if paused:
        if pause.button("Paused — resume buys", key="resume_button"):
            st.session_state.machine_paused = False
            st.rerun()
    elif pause.button("Pause new buys", key="pause_button"):
        st.session_state.machine_paused = True
        st.rerun()
    return destination, paused


def _money_row() -> tuple[float, int, float, float]:
    capital_col, lots_col, stop_col, target_col = st.columns(4)
    capital = capital_col.number_input(
        "Starting capital (₹)",
        min_value=1_000.0,
        max_value=50_000_000.0,
        step=10_000.0,
        format="%.0f",
        key="initial_capital",
    )
    lots = lots_col.number_input("Lots", min_value=1, max_value=20, step=1, key="trade_lots")
    stop_pct = stop_col.number_input(
        "Sell if price falls (%)",
        min_value=20,
        max_value=60,
        step=5,
        key="premium_stop_pct",
    )
    target_pct = target_col.number_input(
        "Sell if price rises (%)",
        min_value=20,
        max_value=100,
        step=5,
        key="premium_target_pct",
    )
    return float(capital), int(lots), float(stop_pct) / 100.0, float(target_pct) / 100.0


def _show_lot_cost(sample: OptionQuote | None, preview, note: str, lots: int) -> None:
    if sample is None:
        st.caption("Pick how many lots to buy. Nifty’s exchange lot is 65. A trade is skipped if the cash exceeds your capital, or the stop can lose more than ₹3,000.")
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
        st.session_state.live_armed = False
        return
    st.checkbox("Send real Upstox orders", key="live_armed")
    if not settings.upstox_configured:
        st.warning("Add your Upstox token in the .env file.")
    elif not settings.live_execution_enabled:
        st.warning("Real orders stay blocked until EXECUTION_MODE=LIVE in the .env file.")


def _paint_market(
    hero_box,
    level_box,
    notice_box,
    spot,
    opening,
    signal,
    order,
    lot_note,
    paused,
    broker,
    now,
    index_error,
    chain_error,
    allowed_by_gemini,
    gemini_why,
) -> None:
    allowed, why = entry_allowed(now, signal)
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
        allowed_by_gemini,
        gemini_why,
    )
    hero_box.markdown(
        f'<div class="hero {tone}"><div class="kicker">Right now</div>'
        f'<div class="action">{title}</div><p>{body}</p></div>',
        unsafe_allow_html=True,
    )
    high = f"{opening.high_boundary:,.0f}" if opening.ready else "after 9:30"
    low = f"{opening.low_boundary:,.0f}" if opening.ready else "after 9:30"
    cards = [
        ("Nifty", f"{spot:,.0f}" if spot else "—", ""),
        ("Call above", high, ""),
        ("Put below", low, ""),
    ]
    with level_box.container():
        for column, (label, value, extra) in zip(st.columns(3), cards):
            value_class = f"metric-value {extra}" if extra else "metric-value"
            column.markdown(
                f'<div class="metric-card"><div class="metric-label">{label}</div>'
                f'<div class="{value_class}">{value}</div></div>',
                unsafe_allow_html=True,
            )
    flash = str(st.session_state.get("flash") or "")
    error = str(st.session_state.get("flash_error") or "")
    st.session_state.flash = ""
    st.session_state.flash_error = ""
    problem = " ".join(text for text in (index_error, chain_error, error) if text)
    if problem:
        notice_box.warning(problem)
    elif flash:
        notice_box.success(flash)


def _show_quality(quality_box, quality) -> None:
    title, body = quality_summary(quality)
    quality_box.markdown(
        f'<div class="guide-card"><h4>{title}</h4><p>{body}</p></div>',
        unsafe_allow_html=True,
    )


def _show_ticket(order, paused, broker, now, allowed_by_gemini: bool) -> None:
    if order is None or paused or not allowed_by_gemini or _traded_today(broker, now):
        return
    kind = "call" if order.contract.option_type == "CE" else "put"
    st.markdown(
        f'<div class="ticket"><h3>It will buy this {kind}</h3>'
        f"<p><strong>{order.contract.trading_symbol}</strong> · {order.lots} lot"
        f"{'' if order.lots == 1 else 's'} · {order.quantity} quantity</p>"
        f"<p>About ₹{order.premium:,.2f} per unit · ₹{order.cash_required:,.0f} cash</p>"
        f"<p>Sell at ₹{order.stop_premium:,.2f}, at ₹{order.target_premium:,.2f}, or at 3:15 pm.</p></div>",
        unsafe_allow_html=True,
    )


def _today_story(
    paused, signal, order, lot_note, traded, opens, allowed, why, allowed_by_gemini, gemini_why
) -> tuple[str, str, str]:
    if paused:
        return (
            "Paused",
            "It will not buy a new option. Anything already open is still sold if the price hits your stop, your target, or 3:15 pm.",
            "wait",
        )
    if opens:
        return (
            "Holding an option",
            "It is watching the price. The option is sold if it falls to your stop, rises to your target, or at 3:15 pm.",
            "hold",
        )
    if traded:
        return (
            "Finished for today",
            "It buys one option a day. Leave this window open again from 9:15 am on the next trading day.",
            "wait",
        )
    if "closed today" in why.lower():
        return ("Market is closed", "Nifty options trade Monday to Friday. Nothing is bought today.", "wait")
    if "09:30" in why:
        return (
            "Marking today's high and low",
            "Until 9:30 am it only watches Nifty. No option is bought during that window.",
            "wait",
        )
    if "14:30" in why:
        return ("Too late for a new buy", "New buys stop at 2:30 pm. Anything still open is sold by 3:15 pm.", "wait")
    if signal in {"BUY", "SELL"} and order is not None and not allowed_by_gemini:
        return ("Gemini skipped this break", gemini_why or "The open does not match Gemini's view.", "stop")
    if signal == "BUY" and order is not None:
        return (
            "Nifty broke up",
            "Price is above this morning's high, so it buys a call and sells that call later.",
            "go",
        )
    if signal == "SELL" and order is not None:
        return (
            "Nifty broke down",
            "Price is below this morning's low, so it buys a put and sells that put later.",
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
            "Waiting for Nifty to leave the range",
            "It is still between this morning's high and low. No option until it breaks one of those levels.",
            "wait",
        )
    return ("Waiting", why or "The machine is watching Nifty.", "wait")


def _render_book(broker: PaperTradingBroker) -> None:
    journal = TradeJournal()
    journal.backfill(broker.open_positions() + broker.closed_positions())
    sheet = journal.table()
    closed_pl = 0.0
    if not sheet.empty and "Result" in sheet.columns:
        closed_pl = pd.to_numeric(sheet["Result"], errors="coerce").fillna(0).sum()
    st.caption(
        f"{len(sheet)} trades in the journal · finished result ₹{closed_pl:,.0f} · "
        "paper and live both go in this sheet"
    )
    if sheet.empty:
        st.info("No trades yet. Each paper or live trade is written into data/trade_journal.xlsx.")
    else:
        st.dataframe(sheet, width="stretch", hide_index=True, height=360)
    st.caption("Each trade is saved in the project at data/trade_journal.xlsx.")


def _money(value: object) -> str:
    try:
        return f"₹{float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _exit_words(value: object) -> str:
    return {
        "PREMIUM_STOP": "Price fell to the stop",
        "PREMIUM_TARGET": "Price hit the target",
        "SQUARE_OFF_1515": "Sold at 3:15 pm",
        "EMERGENCY_CLOSE": "Closed by hand",
    }.get(str(value or ""), str(value or ""))


def _render_backtest(settings: Settings, capital: float, lots: int, stop_frac: float, target_frac: float) -> None:
    st.write(
        "Same rule, on past option prices: the lot count, stop, and target from above. "
        "Only the current weekly contract is available, about three weeks."
    )
    days = st.slider("Days", min_value=10, max_value=40, value=20, step=5, key="opt_backtest_days")
    if st.button("Run the test", type="primary"):
        if not settings.upstox_configured:
            st.session_state.flash_error = "The test needs your Upstox token in the .env file."
            st.rerun()
        try:
            with st.spinner("Loading prices. The first run can take a few minutes."):
                st.session_state.option_backtest = _run_option_backtest(
                    settings, int(days), capital, lots, stop_frac, target_frac
                )
        except Exception as exc:
            st.session_state.flash_error = f"The test could not finish: {exc}"
            st.rerun()
    view = st.session_state.get("option_backtest")
    if not view:
        st.info("Press Run the test. It uses the capital and lots above.")
        return
    book = pd.DataFrame(view["book"])
    made = float(view["rupee_pl"])
    verb = "made" if made >= 0 else "lost"
    st.markdown(
        f'<div class="hero {"go" if made >= 0 else "stop"}"><div class="kicker">Result</div>'
        f'<div class="action">{verb.title()} ₹{abs(made):,.0f}</div>'
        f"<p>Started with ₹{view['capital']:,.0f}. Ended at ₹{view['final_equity']:,.0f}. "
        f"{view['trades']} trades, won {view['win_rate']}% of them.</p></div>",
        unsafe_allow_html=True,
    )
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Ended with", f"₹{view['final_equity']:,.0f}")
    c2.metric("Result", f"₹{view['rupee_pl']:,.0f}")
    c3.metric("Trades", f"{view['trades']}")
    c4.metric("Wins", f"{view['win_rate']}%")
    st.caption(
        f"Contract expiring {view['expiry']} · {view['start']} to {view['end']} · "
        f"₹{view['capital']:,.0f} capital · {view['lots']} lot(s) · a small cost is taken on each buy and sell"
    )
    if book.empty:
        st.warning("No trades. Nifty stayed inside the morning range, or those lots did not fit the capital or the ₹3,000 stop.")
        return
    curve = book[["date", "equity"]].copy()
    curve["date"] = pd.to_datetime(curve["date"])
    st.markdown("**Account value after each trade**")
    st.line_chart(curve.set_index("date")["equity"], height=240)
    shown = pd.DataFrame(
        {
            "Date": book["date"],
            "Call or put": book["option"].map(lambda side: "Call" if side == "CE" else "Put"),
            "Strike": book["strike"],
            "Bought at": book["entry_time"].map(_short_clock),
            "Sold at": book["exit_time"].map(_short_clock),
            "Price paid": book["entry"].map(_money),
            "Price sold": book["exit"].map(_money),
            "Lots": book["lots"],
            "Quantity": book["qty"],
            "Result ₹": book["rupee_pl"].map(lambda value: f"{float(value):,.0f}"),
            "Account after": book["equity"].map(lambda value: f"₹{float(value):,.0f}"),
            "Why it was sold": book["exit_reason"].map(_exit_words),
        }
    )
    st.dataframe(shown, width="stretch", hide_index=True, height=320)


def _short_clock(value: object) -> str:
    text = str(value or "")
    if "T" in text:
        return text.split("T", 1)[1][:5]
    return text[-5:] if len(text) >= 5 else text


def _run_option_backtest(settings, days, capital, lots, stop_frac, target_frac) -> dict:
    end = datetime.now(IST).date() - timedelta(days=1)
    start = end - timedelta(days=int(days))
    client = UpstoxClient(settings.upstox_access_token)
    index = get_historical_candles(
        NIFTY_INDEX_KEY,
        interval="5minute",
        from_date=start,
        to_date=end,
        client=client,
        include_today=False,
    )
    contracts = fetch_nifty_contracts(client)
    expiry = nearest_weekly_expiry(contracts, end)
    if not expiry:
        raise RuntimeError("No Nifty expiry found.")
    chain = fetch_option_chain(client, expiry)
    lot = lot_size_for(contracts, expiry)
    quotes = parse_chain_rows(chain, expiry, lot)
    low = float(index["low"].min())
    high = float(index["high"].max())
    chosen = [
        quote
        for quote in quotes
        if low - 200 <= quote.strike <= high + 200
    ]
    tapes: dict[tuple[int, str], pd.DataFrame] = {}
    for quote in chosen:
        frame = _option_history(settings.upstox_access_token, quote.instrument_key, start.isoformat(), end.isoformat())
        if frame is not None and not frame.empty:
            tapes[(int(round(quote.strike)), quote.option_type)] = frame
    book = replay_option_rule(
        index, tapes, capital, 1.0, stop_frac, target_frac, lot_size=lot, lots=int(lots)
    )
    final = float(book.iloc[-1]["equity"]) if not book.empty else float(capital)
    wins = int((book["outcome"] == "WIN").sum()) if not book.empty else 0
    trades = int(len(book))
    return {
        "book": book.to_dict(orient="records"),
        "expiry": expiry,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "capital": capital,
        "lots": int(lots),
        "final_equity": final,
        "rupee_pl": round(final - float(capital), 2),
        "trades": trades,
        "win_rate": round(100.0 * wins / trades, 1) if trades else 0.0,
    }


def _option_history(access_token: str, instrument_key: str, start: str, end: str) -> pd.DataFrame:
    safe = instrument_key.replace("|", "_")
    path = PROJECT_ROOT / "data" / "cache" / f"{safe}_{start}_{end}_5m.pkl"
    if path.exists():
        return pd.read_pickle(path)
    frame = get_historical_candles(
        instrument_key,
        interval="5minute",
        from_date=datetime.fromisoformat(start).date(),
        to_date=datetime.fromisoformat(end).date(),
        client=UpstoxClient(access_token),
        include_today=False,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_pickle(path)
    return frame


def _live_setup(settings: Settings, now: datetime, capital: float, lots: int, stop_frac: float, target_frac: float):
    """Read Nifty again and return the signal, the sized order, and whether Gemini allows it."""

    signal = "SCANNING"
    order = None
    if not settings.upstox_configured:
        return signal, order, False
    spot, candles, _index_error = _load_index(settings)
    opening = calculate_opening_range(candles, now=now)
    if opening.ready and spot > 0:
        signal = evaluate_market_signals(spot, opening.high_boundary, opening.low_boundary)
    contract, _sample, _chain_error = _load_contract(settings, spot, signal, now)
    quality = load_option_quality(
        now,
        spot,
        float(opening.high_boundary or 0),
        float(opening.low_boundary or 0),
        bool(opening.ready),
    )
    allowed, _why = quality_allows(signal, quality)
    if contract is not None:
        order, _note = order_for_lots(contract, lots, capital, stop_frac, target_frac)
    return signal, order, allowed


def _maybe_enter(broker, settings: Settings, destination: str, now: datetime, signal: str, order) -> None:
    if order is None or _traded_today(broker, now):
        return
    allowed, why = entry_allowed(now, signal)
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
                tag="NIFTY-OPT",
            )
            live_id = placed.order_id
        except Exception as exc:
            st.session_state.flash_error = f"Live option order failed: {exc}"
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
    TradeJournal().record(filled)
    prefix = "LIVE" if destination == LIVE else "PAPER"
    st.session_state.flash = (
        f"{prefix} BUY {order.lots} lot(s) {order.contract.trading_symbol} @ ₹{order.premium:,.2f}"
        + (f" · {live_id}" if live_id else "")
    )


def _mark_and_exit(broker, settings: Settings, destination: str, now: datetime) -> None:
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
            TradeJournal().record(closed)
        st.session_state.flash = f"{reason} — sold {symbol}."


def _sell_live(row: dict, settings: Settings) -> bool:
    if row.get("destination") != "LIVE":
        return True
    if not settings.live_execution_enabled:
        st.session_state.flash_error = "Live sell skipped — EXECUTION_MODE is MOCK."
        return False
    try:
        place_mis_market_order(
            instrument_token=str(row.get("instrument_token") or ""),
            side="SELL",
            quantity=int(row.get("Quantity") or 0),
            symbol=str(row.get("Ticker") or ""),
            access_token=settings.upstox_access_token,
            tag="OPT-EXIT",
        )
        return True
    except Exception as exc:
        st.session_state.flash_error = f"Live exit failed for {row.get('Ticker')}: {exc}"
        return False


def _traded_today(broker, now: datetime) -> bool:
    day = now.date().isoformat()
    rows = broker.open_positions() + broker.closed_positions()
    return any(str(row.get("Timestamp") or "").startswith(day) for row in rows)


def _live_ready(settings: Settings) -> bool:
    return bool(
        st.session_state.get("destination") == LIVE
        and st.session_state.get("live_armed")
        and settings.live_execution_enabled
        and settings.upstox_configured
    )


def _load_index(settings: Settings) -> tuple[float, pd.DataFrame, str]:
    try:
        quote, candles = _cached_index(settings.upstox_access_token)
        return quote.last_price, candles, ""
    except Exception as exc:
        return 0.0, pd.DataFrame(), f"Nifty index feed failed: {exc}"


@st.cache_data(ttl=30, show_spinner=False)
def _cached_index(access_token: str):
    client = UpstoxClient(access_token)
    quote = get_full_quote(NIFTY_INDEX_KEY, client=client)
    candles = get_historical_candles(
        NIFTY_INDEX_KEY,
        interval="5minute",
        from_date=datetime.now(IST).date() - timedelta(days=3),
        client=client,
    )
    return quote, candles


def _load_contract(
    settings: Settings, spot: float, signal: str, now: datetime
) -> tuple[OptionQuote | None, OptionQuote | None, str]:
    """Return the tradable contract, a sample for pricing lots, and an error."""

    if spot <= 0:
        return None, None, ""
    try:
        rows = _cached_contracts(settings.upstox_access_token)
        expiry = nearest_weekly_expiry(rows, now.date())
        if not expiry:
            return None, None, "No Nifty expiry on or after today."
        chain = _cached_chain(settings.upstox_access_token, expiry)
        quotes = parse_chain_rows(chain, expiry, lot_size_for(rows, expiry))
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
    return fetch_nifty_contracts(UpstoxClient(access_token))


@st.cache_data(ttl=15, show_spinner=False)
def _cached_chain(access_token: str, expiry: str) -> list[dict]:
    return fetch_option_chain(UpstoxClient(access_token), expiry)


render_desk()
