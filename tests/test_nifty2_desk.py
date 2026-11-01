from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from config.settings import IST, Settings
from modules.desk_loop import (
    BANK_CONTROLS,
    NIFTY2_CONTROLS,
    NIFTY2_JOURNAL,
    NIFTY_CONTROLS,
    load_bank_controls,
    load_nifty2_controls,
    load_nifty_controls,
    run_nifty_cycle,
    save_desk_controls,
)
from modules.journal.trade_journal import TradeJournal
from modules.options.selector import OptionQuote
from modules.risk.risk_manager import (
    BANKNIFTY_PORTFOLIO_PATH,
    NIFTY2_PORTFOLIO_PATH,
    PORTFOLIO_PATH,
)
from modules.risk.risk_manager import PaperTradingBroker

ROOT = Path(__file__).resolve().parents[1]


def _settings() -> Settings:
    return Settings(
        upstox_api_key="",
        upstox_secret_key="",
        upstox_access_token="token",
        upstox_redirect_uri="http://127.0.0.1:8000/callback",
        gemini_api_key="",
        gemini_model="gemini-2.5-flash",
        execution_mode="MOCK",
        active_strategy="ORB_TIGHT",
    )


def _candles() -> pd.DataFrame:
    stamps = [
        "2026-10-19 09:15:00",
        "2026-10-19 09:20:00",
        "2026-10-19 09:25:00",
        "2026-10-19 12:05:00",
    ]
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(stamps).tz_localize(IST),
            "open": [23150.0, 23153.0, 23107.0, 22960.0],
            "high": [23175.0, 23157.0, 23107.0, 22970.0],
            "low": [23074.0, 23104.0, 22970.5, 22948.0],
            "close": [23153.0, 23106.0, 22981.0, 22954.0],
            "volume": [1, 1, 1, 1],
        }
    )


def _controls() -> dict:
    return {
        "capital": 500_000.0,
        "lots": 1,
        "stop_frac": 0.40,
        "target_frac": 0.60,
        "destination": "PAPER",
        "paused": False,
        "live_armed": False,
    }


def test_nifty2_storage_paths_are_separate() -> None:
    paths = {PORTFOLIO_PATH, NIFTY2_PORTFOLIO_PATH, BANKNIFTY_PORTFOLIO_PATH}
    assert len(paths) == 3
    controls = {NIFTY_CONTROLS, NIFTY2_CONTROLS, BANK_CONTROLS}
    assert len(controls) == 3
    assert NIFTY2_PORTFOLIO_PATH.name == "nifty2_portfolio.json"
    assert NIFTY2_JOURNAL.name == "nifty2"


def test_nifty2_controls_round_trip(tmp_path) -> None:
    path = tmp_path / "nifty2_desk.json"
    save_desk_controls(
        "nifty2",
        {
            "capital": 250_000.0,
            "lots": 3,
            "stop_frac": 0.35,
            "target_frac": 0.70,
            "max_loss": 2500.0,
            "destination": "LIVE",
            "paused": True,
            "live_armed": True,
        },
        path=path,
    )
    loaded = load_nifty2_controls(path)
    assert loaded["capital"] == 250_000.0
    assert loaded["lots"] == 3
    assert loaded["destination"] == "LIVE"
    assert loaded["paused"] is True
    assert loaded["live_armed"] is True


def test_only_nifty2_controls_carry_the_trailing_stop(tmp_path) -> None:
    # Nifty 2 defaults to a wider target with the trail armed...
    n2 = load_nifty2_controls(tmp_path / "missing_nifty2.json")
    assert n2["trail_armed"] is True
    assert n2["trail_activate_frac"] == 0.40
    assert n2["trail_frac"] == 0.30
    assert n2["target_frac"] == 1.00
    # ...while Nifty and Bank Nifty never expose trail keys, so they stay fixed.
    assert "trail_frac" not in load_nifty_controls(tmp_path / "missing_nifty.json")
    assert "trail_frac" not in load_bank_controls(tmp_path / "missing_bank.json")


def test_nifty2_has_no_max_loss_cap_but_nifty_does(tmp_path) -> None:
    n2 = load_nifty2_controls(tmp_path / "missing_nifty2.json")
    assert "max_loss" not in n2  # the cap is gone for Nifty 2
    assert load_nifty_controls(tmp_path / "missing_nifty.json")["max_loss"] == 3000.0


def test_nifty2_takes_a_trade_the_loss_cap_would_block_on_nifty(tmp_path) -> None:
    # Premium 150 -> stop loss 150*65*0.4 = 3,900, over the old ₹3,000 cap but
    # well within capital. Nifty (with a cap) refuses it; Nifty 2 (no cap) buys.
    quote = OptionQuote("K", "NIFTY 22950 PE 2026-10-19", "2026-10-19", 22950, "PE", 65, 150.0)
    now = datetime(2026, 10, 19, 12, 5, tzinfo=IST)

    capped = PaperTradingBroker(tmp_path / "nifty.json")
    reason = run_nifty_cycle(
        now=now, broker=capped, settings=_settings(),
        controls={**_controls(), "max_loss": 3000.0},
        spot=22954.0, candles=_candles(), quotes=[quote],
        journal=TradeJournal(tmp_path / "nifty_journal"), gemini_ok=True,
    )
    assert capped.open_positions() == []
    assert "3,000" in reason

    uncapped = PaperTradingBroker(tmp_path / "nifty2.json")
    reason2 = run_nifty_cycle(
        now=now, broker=uncapped, settings=_settings(),
        controls=load_nifty2_controls(tmp_path / "missing.json"),
        spot=22954.0, candles=_candles(), quotes=[quote],
        journal=TradeJournal(tmp_path / "nifty2_journal"), gemini_ok=True,
        buy_tag="NIFTY2-OPT", exit_tag="OPT2-EXIT",
    )
    assert "PAPER BUY" in reason2
    assert uncapped.open_positions()[0]["Quantity"] == 65


def test_nifty2_controls_trail_round_trip(tmp_path) -> None:
    path = tmp_path / "nifty2_desk.json"
    save_desk_controls(
        "nifty2",
        {
            "capital": 500_000.0,
            "lots": 1,
            "stop_frac": 0.40,
            "target_frac": 1.00,
            "max_loss": 3000.0,
            "destination": "PAPER",
            "paused": False,
            "live_armed": False,
            "trail_armed": False,
            "trail_activate_frac": 0.50,
            "trail_frac": 0.25,
        },
        path=path,
    )
    loaded = load_nifty2_controls(path)
    assert loaded["trail_armed"] is False
    assert loaded["trail_activate_frac"] == 0.50
    assert loaded["trail_frac"] == 0.25


def test_nifty2_trailing_stop_sells_after_a_pullback_from_the_peak(tmp_path) -> None:
    broker = PaperTradingBroker(tmp_path / "nifty2.json")
    broker.fill(
        "NIFTY 100 CE",
        "BUY",
        65,
        100.0,
        60.0,  # fixed stop
        "tok",
        destination="PAPER",
        extra={"target_premium": 200.0},  # target_frac 1.0
    )
    controls = {
        **_controls(),
        "target_frac": 1.00,
        "trail_armed": True,
        "trail_activate_frac": 0.40,
        "trail_frac": 0.30,
    }
    now = datetime(2026, 10, 19, 12, 5, tzinfo=IST)
    journal = TradeJournal(tmp_path / "nifty2_journal")

    # Tick 1: price runs to 160 -> the peak is banked, nothing sells yet.
    run_nifty_cycle(
        now=now, broker=broker, settings=_settings(), controls=controls,
        spot=22954.0, candles=_candles(), journal=journal, gemini_ok=True,
        prices={"NIFTY 100 CE": 160.0},
    )
    assert broker.open_positions()
    assert float(broker.open_positions()[0]["peak_premium"]) == 160.0

    # Tick 2: price falls to 110 -> below the 160*0.70 = 112 trail, so it sells.
    run_nifty_cycle(
        now=now, broker=broker, settings=_settings(), controls=controls,
        spot=22954.0, candles=_candles(), journal=journal, gemini_ok=True,
        prices={"NIFTY 100 CE": 110.0},
    )
    assert broker.open_positions() == []
    closed = broker.closed_positions()[0]
    assert closed["exit_reason"] == "TRAILING_STOP"
    assert float(closed["ExitPrice"]) == 110.0


def test_nifty_desk_without_trail_keys_holds_through_the_same_pullback(tmp_path) -> None:
    # The same path with the first Nifty desk's controls (no trail keys) must NOT
    # trail out: 110 is above the fixed stop and below the fixed target, so it holds.
    broker = PaperTradingBroker(tmp_path / "nifty.json")
    broker.fill(
        "NIFTY 100 CE", "BUY", 65, 100.0, 60.0, "tok",
        destination="PAPER", extra={"target_premium": 200.0},
    )
    now = datetime(2026, 10, 19, 12, 5, tzinfo=IST)
    for mark in (160.0, 110.0):
        run_nifty_cycle(
            now=now, broker=broker, settings=_settings(), controls=_controls(),
            spot=22954.0, candles=_candles(), journal=TradeJournal(tmp_path / "j"),
            gemini_ok=True, prices={"NIFTY 100 CE": mark},
        )
    assert broker.open_positions()  # still holding
    assert "peak_premium" not in broker.open_positions()[0]  # peak never tracked


def test_nifty2_book_is_isolated_from_the_first_desk(tmp_path) -> None:
    nifty_broker = PaperTradingBroker(tmp_path / "nifty.json")
    nifty2_broker = PaperTradingBroker(tmp_path / "nifty2.json")
    quote = OptionQuote("K", "NIFTY 22950 PE 2026-10-19", "2026-10-19", 22950, "PE", 65, 60.3)
    reason = run_nifty_cycle(
        now=datetime(2026, 10, 19, 12, 5, tzinfo=IST),
        broker=nifty2_broker,
        settings=_settings(),
        controls=_controls(),
        spot=22954.0,
        candles=_candles(),
        quotes=[quote],
        journal=TradeJournal(tmp_path / "nifty2_journal"),
        gemini_ok=True,
        buy_tag="NIFTY2-OPT",
        exit_tag="OPT2-EXIT",
    )
    assert "PAPER BUY" in reason
    # The buy landed only in the second book; the first desk stays empty.
    assert nifty2_broker.open_positions()[0]["Quantity"] == 65
    assert nifty_broker.open_positions() == []


def test_nifty2_page_keeps_its_own_book() -> None:
    desk = (ROOT / "modules" / "ui" / "nifty2_desk_page.py").read_text(encoding="utf-8")
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "NIFTY2_PORTFOLIO_PATH" in desk
    assert 'PROJECT_ROOT / "data" / "nifty2"' in desk
    assert "calculate_opening_range" in desk
    assert "simulated_portfolio" not in desk
    assert "BANKNIFTY_PORTFOLIO_PATH" not in desk
    assert "nifty2_desk_page.py" in app


def test_nifty2_desk_renders() -> None:
    app = AppTest.from_file(ROOT / "modules" / "ui" / "nifty2_desk_page.py", default_timeout=60)
    app.run()
    assert not app.exception
    markdown = " ".join(str(block.value) for block in app.markdown)
    assert "Nifty 2 weekly options" in markdown
    labels = [str(item.label) for item in app.number_input]
    assert any("capital" in label.lower() for label in labels)
