"""Backtest page. Estimated option prices from Nifty, then the opening-range rule."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from config.settings import IST, PROJECT_ROOT, load_settings
from modules.options.backtest import SPREAD_INR, refusal_counts, replay_option_rule
from modules.options.selector import contracts_for_expiry, lot_size_for, nearest_weekly_expiry
from modules.options.simulator import session_volatility, simulate_option_bars
from modules.upstox.client import UpstoxClient
from modules.upstox.market_data import get_historical_candles
from modules.upstox.option_chain import INDIA_VIX_KEY, NIFTY_INDEX_KEY, fetch_nifty_contracts

DEFAULT_LOT = 65
CHECK_DAYS = 21
VIX_WARMUP_DAYS = 10


def render_backtest_page() -> None:
    settings = load_settings()
    st.markdown('<p class="desk-title">Backtest</p>', unsafe_allow_html=True)
    st.markdown(
        '<p class="desk-sub">Same opening-range rule. Option prices are estimated from Nifty '
        "using India VIX. The last weeks are also checked against real option prices.</p>",
        unsafe_allow_html=True,
    )
    capital, lots, stop_frac, target_frac, days = _inputs()
    run_col, _rest = st.columns([1, 3])
    if run_col.button("Run backtest", type="primary", key="run_backtest"):
        if not settings.upstox_configured:
            st.error("The backtest needs your Upstox token in the .env file.")
        else:
            try:
                with st.spinner(
                    "Loading Nifty and India VIX, then checking recent days against real option prices. "
                    "The first run can take a few minutes."
                ):
                    st.session_state.backtest_view = _run_backtest(
                        settings, int(days), capital, lots, stop_frac, target_frac
                    )
            except Exception as exc:
                st.error(f"The backtest could not finish: {exc}")
    view = st.session_state.get("backtest_view")
    if not view:
        st.info("Set the capital, lots, stop, and target, then run the backtest.")
        return
    _show_result(view)


def _inputs() -> tuple[float, int, float, float, int]:
    capital_col, lots_col, stop_col, target_col = st.columns(4)
    capital = capital_col.number_input(
        "Starting capital (₹)",
        min_value=1_000.0,
        max_value=50_000_000.0,
        step=10_000.0,
        format="%.0f",
        key="bt_capital",
        value=500_000.0,
    )
    lots = lots_col.number_input("Lots", min_value=1, max_value=20, step=1, key="bt_lots", value=1)
    stop_pct = stop_col.number_input(
        "Stop loss (%)",
        min_value=20,
        max_value=60,
        step=5,
        key="bt_stop_pct",
        value=40,
    )
    target_pct = target_col.number_input(
        "Take profit (%)",
        min_value=20,
        max_value=100,
        step=5,
        key="bt_target_pct",
        value=60,
    )
    days = st.slider("Days", min_value=20, max_value=180, value=60, step=10, key="bt_days")
    return float(capital), int(lots), float(stop_pct) / 100.0, float(target_pct) / 100.0, int(days)


def _show_result(view: dict) -> None:
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
    ended, result, trades, wins = st.columns(4)
    ended.metric("Ended with", f"₹{view['final_equity']:,.0f}")
    result.metric("Result", f"₹{view['rupee_pl']:,.0f}")
    trades.metric("Trades", f"{view['trades']}")
    wins.metric("Wins", f"{view['win_rate']}%")
    vol_note = (
        f"Average India VIX used: {float(view['volatility']) * 100:.1f}%."
        if view.get("volatility_source") == "vix"
        else "India VIX was missing, so 14% volatility was used."
    )
    st.caption(
        f"Premiums are estimated from Nifty, not past option prices. {vol_note} "
        f"{view['start']} to {view['end']} · ₹{view['capital']:,.0f} capital · "
        f"{view['lots']} lot(s) · exchange lot {view['lot_size']} · "
        f"₹{SPREAD_INR:.2f} spread on the buy and again on the sell. This test follows the price rule only."
    )
    _show_check(view.get("check"))
    if book.empty:
        st.warning(
            "No trades. Nifty stayed inside the morning range, or those lots did not fit the capital or the ₹3,000 stop."
        )
        return
    curve = book[["date", "rupee_pl"]].copy()
    curve["date"] = pd.to_datetime(curve["date"])
    curve["P&L"] = curve["rupee_pl"].cumsum()
    st.markdown("**Profit and loss**")
    st.line_chart(curve.set_index("date")["P&L"], height=280)
    shown = pd.DataFrame(
        {
            "Date": book["date"],
            "Call or put": book["option"].map(lambda side: "Call" if side == "CE" else "Put"),
            "Strike": book["strike"],
            "Bought at": book["entry_time"],
            "Sold at": book["exit_time"],
            "Price paid": book["entry"].map(_money),
            "Price sold": book["exit"].map(_money),
            "Lots": book["lots"],
            "Result ₹": book["rupee_pl"].map(lambda value: f"{float(value):,.0f}"),
            "Why it was sold": book["exit_reason"].map(_exit_words),
        }
    )
    st.dataframe(shown, width="stretch", hide_index=True, height=360)


def _show_check(check: dict | None) -> None:
    if not check:
        return
    st.markdown("**Same days, real option prices**")
    if check.get("error"):
        st.caption(f"The real-price check did not finish: {check['error']}")
        return
    real_col, estimate_col, match_col = st.columns(3)
    real_col.metric("Real prices", f"₹{check['real_pl']:,.0f}")
    estimate_col.metric("Estimated prices", f"₹{check['sim_pl']:,.0f}")
    match_col.metric("Same call or put", f"{check['same_option']} of {check['shared_days']}")
    refused = int(check.get("real_refused") or 0)
    priced = int(check.get("real_priced") or 0)
    cap_note = ""
    if refused:
        cap_note = (
            f" Real prices had a premium on {priced} break days, and {refused} of those were skipped "
            "because the stop on those lots would lose more than ₹3,000."
        )
    st.caption(
        f"Current weekly expiry {check['expiry']}, {check['start']} to {check['end']}. "
        f"Real prices produced {check['real_trades']} trades. The estimate produced {check['sim_trades']}. "
        f"The exit reason matched on {check['same_exit']} of the shared days.{cap_note} "
        "Read the long result only if these two books are close."
    )


def _run_backtest(settings, days: int, capital: float, lots: int, stop_frac: float, target_frac: float) -> dict:
    end = datetime.now(IST).date() - timedelta(days=1)
    start = end - timedelta(days=int(days))
    client = UpstoxClient(settings.upstox_access_token)
    window = _cached_candles(
        settings.upstox_access_token,
        NIFTY_INDEX_KEY,
        start.isoformat(),
        end.isoformat(),
        client,
        "5minute",
    )
    window = _between(window, start, end)
    if window.empty:
        raise RuntimeError("No Nifty prices for that window.")
    sessions = sorted(set(window["timestamp"].dt.date))
    vix, source = _load_vix(settings.upstox_access_token, start, end, client)
    sigmas = session_volatility(vix, sessions)
    volatility = float(sum(sigmas.values()) / len(sigmas)) if sigmas else 0.14
    tapes = simulate_option_bars(window, session_sigma=sigmas)
    lot = _lot_size(client)
    book = replay_option_rule(
        window,
        tapes,
        capital,
        1.0,
        stop_frac,
        target_frac,
        lot_size=lot,
        lots=int(lots),
    )
    check = _real_check(settings.upstox_access_token, client, window, capital, lots, stop_frac, target_frac, lot, sigmas)
    final = float(book.iloc[-1]["equity"]) if not book.empty else float(capital)
    wins = int((book["outcome"] == "WIN").sum()) if not book.empty else 0
    trades = int(len(book))
    return {
        "book": book.to_dict(orient="records"),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "capital": capital,
        "lots": int(lots),
        "lot_size": lot,
        "volatility": volatility,
        "volatility_source": source,
        "final_equity": final,
        "rupee_pl": round(final - float(capital), 2),
        "trades": trades,
        "win_rate": round(100.0 * wins / trades, 1) if trades else 0.0,
        "check": check,
    }


def _load_vix(access_token: str, start, end, client: UpstoxClient) -> tuple[pd.DataFrame, str]:
    vix_start = start - timedelta(days=VIX_WARMUP_DAYS)
    try:
        frame = _cached_candles(
            access_token,
            INDIA_VIX_KEY,
            vix_start.isoformat(),
            end.isoformat(),
            client,
            "day",
        )
    except Exception:
        return pd.DataFrame(), "fallback"
    if frame is None or frame.empty:
        return pd.DataFrame(), "fallback"
    return frame, "vix"


def _real_check(access_token, client, window, capital, lots, stop_frac, target_frac, lot, sigmas) -> dict:
    end = max(window["timestamp"].dt.date)
    start = max(min(window["timestamp"].dt.date), end - timedelta(days=CHECK_DAYS))
    check_index = _between(window, start, end)
    try:
        rows = fetch_nifty_contracts(client)
        expiry = nearest_weekly_expiry(rows, end)
        if not expiry:
            return {"error": "No Nifty expiry found."}
        quotes = contracts_for_expiry(rows, expiry)
        low = float(check_index["low"].min())
        high = float(check_index["high"].max())
        chosen = [quote for quote in quotes if low - 200 <= quote.strike <= high + 200]
        tapes: dict[tuple[int, str], pd.DataFrame] = {}
        for quote in chosen:
            try:
                frame = _cached_candles(
                    access_token,
                    quote.instrument_key,
                    start.isoformat(),
                    end.isoformat(),
                    client,
                    "5minute",
                )
            except Exception:
                continue
            if frame is not None and not frame.empty:
                tapes[(int(round(quote.strike)), quote.option_type)] = frame
        if not tapes:
            return {"error": "No real option prices came back for this weekly contract."}
        real = replay_option_rule(
            check_index, tapes, capital, 1.0, stop_frac, target_frac, lot_size=lot, lots=int(lots)
        )
        estimated = simulate_option_bars(check_index, session_sigma=sigmas)
        sim = replay_option_rule(
            check_index, estimated, capital, 1.0, stop_frac, target_frac, lot_size=lot, lots=int(lots)
        )
        same_option, same_exit, shared = _agreement(real, sim)
        refused = refusal_counts(check_index, tapes, capital, stop_frac, lot, int(lots))
    except Exception as exc:
        return {"error": str(exc)}
    return {
        "expiry": expiry,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "real_pl": _pl(real),
        "sim_pl": _pl(sim),
        "real_trades": int(len(real)),
        "sim_trades": int(len(sim)),
        "shared_days": shared,
        "same_option": same_option,
        "same_exit": same_exit,
        "real_priced": refused["priced"],
        "real_refused": refused["refused"],
    }


def _agreement(real: pd.DataFrame, sim: pd.DataFrame) -> tuple[int, int, int]:
    if real.empty or sim.empty:
        return 0, 0, 0
    left = real.drop_duplicates("date").set_index("date")
    right = sim.drop_duplicates("date").set_index("date")
    shared_dates = sorted(set(left.index) & set(right.index))
    same_option = 0
    same_exit = 0
    for day in shared_dates:
        if left.loc[day, "option"] != right.loc[day, "option"]:
            continue
        same_option += 1
        if left.loc[day, "exit_reason"] == right.loc[day, "exit_reason"]:
            same_exit += 1
    return same_option, same_exit, len(shared_dates)


def _pl(book: pd.DataFrame) -> float:
    if book.empty:
        return 0.0
    return round(float(book["rupee_pl"].sum()), 2)


def _cached_candles(
    access_token: str,
    instrument_key: str,
    start: str,
    end: str,
    client: UpstoxClient,
    interval: str,
) -> pd.DataFrame:
    safe = instrument_key.replace("|", "_").replace(" ", "_")
    path = PROJECT_ROOT / "data" / "cache" / f"{safe}_{start}_{end}_{interval}.pkl"
    if path.exists():
        return pd.read_pickle(path)
    frame = get_historical_candles(
        instrument_key,
        interval=interval,
        from_date=datetime.fromisoformat(start).date(),
        to_date=datetime.fromisoformat(end).date(),
        client=client,
        include_today=False,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_pickle(path)
    return frame


def _lot_size(client: UpstoxClient) -> int:
    try:
        rows = fetch_nifty_contracts(client)
        expiry = nearest_weekly_expiry(rows, datetime.now(IST).date())
        if not expiry:
            return DEFAULT_LOT
        return lot_size_for(rows, expiry)
    except Exception:
        return DEFAULT_LOT


def _between(frame: pd.DataFrame, start, end) -> pd.DataFrame:
    working = frame.copy()
    working["timestamp"] = pd.to_datetime(working["timestamp"], utc=False)
    if working["timestamp"].dt.tz is None:
        working["timestamp"] = working["timestamp"].dt.tz_localize(IST)
    else:
        working["timestamp"] = working["timestamp"].dt.tz_convert(IST)
    dates = working["timestamp"].dt.date
    return working.loc[(dates >= start) & (dates <= end)].reset_index(drop=True)


def _money(value: object) -> str:
    try:
        return f"₹{float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _exit_words(value: object) -> str:
    return {
        "PREMIUM_STOP": "Price fell to the stop",
        "PREMIUM_TARGET": "Price hit the target",
        "SQUARE_OFF_1500": "Sold at 3:00 pm",
        "EMERGENCY_CLOSE": "Closed by hand",
    }.get(str(value or ""), str(value or ""))


render_backtest_page()
