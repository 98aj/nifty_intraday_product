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
    load_nifty2_controls,
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
