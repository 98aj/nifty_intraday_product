from __future__ import annotations

from datetime import datetime

import pandas as pd

from config.settings import IST, Settings
from modules.desk_loop import loop_is_fresh, run_nifty_cycle
from modules.journal.trade_journal import TradeJournal
from modules.options.selector import OptionQuote
from modules.risk.risk_manager import PaperTradingBroker


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


def test_nifty_rule_still_buys_the_midday_breakdown(tmp_path) -> None:
    broker = PaperTradingBroker(tmp_path / "book.json")
    reason = run_nifty_cycle(
        now=datetime(2026, 10, 19, 12, 5, tzinfo=IST),
        broker=broker,
        settings=_settings(),
        controls=_controls(),
        spot=22954.0,
        candles=_candles(),
        contract=OptionQuote("K", "NIFTY 22950 PE 2026-10-19", "2026-10-19", 22950, "PE", 65, 60.3),
        journal=TradeJournal(tmp_path / "journal"),
        gemini_ok=True,
    )
    assert "PAPER BUY" in reason
    assert broker.open_positions()[0]["Quantity"] == 65


def test_nifty_rule_still_refuses_after_1430(tmp_path) -> None:
    broker = PaperTradingBroker(tmp_path / "book.json")
    reason = run_nifty_cycle(
        now=datetime(2026, 10, 19, 14, 31, tzinfo=IST),
        broker=broker,
        settings=_settings(),
        controls=_controls(),
        spot=22889.0,
        candles=_candles(),
        contract=OptionQuote("K", "NIFTY 22900 PE 2026-10-19", "2026-10-19", 22900, "PE", 65, 11.0),
        journal=TradeJournal(tmp_path / "journal"),
        gemini_ok=True,
    )
    assert "14:30" in reason
    assert broker.open_positions() == []


def test_fresh_loop_heartbeat(tmp_path) -> None:
    path = tmp_path / "status.json"
    path.write_text(
        '{"updated_at": "2026-10-19T12:05:00+05:30", "nifty": "watching", "banknifty": "watching"}',
        encoding="utf-8",
    )
    now = datetime(2026, 10, 19, 12, 5, 10, tzinfo=IST)
    assert loop_is_fresh(now, path) is True
    later = datetime(2026, 10, 19, 12, 6, tzinfo=IST)
    assert loop_is_fresh(later, path) is False
