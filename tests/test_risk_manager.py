from __future__ import annotations

from datetime import datetime
from pathlib import Path

from config.settings import IST
from modules.risk.risk_manager import PaperTradingBroker, RiskInterceptor


def test_interceptor_blocks_invalid_size() -> None:
    gate = RiskInterceptor()
    now = datetime(2026, 4, 8, 10, 0, tzinfo=IST)
    assert gate.validate_trade_safety("RELIANCE", 0, 1170.0, 0.0, now=now).code == "INVALID_POSITION_SIZING"
    assert gate.validate_trade_safety("RELIANCE", -2, 1170.0, 0.0, now=now).code == "INVALID_POSITION_SIZING"


def test_interceptor_blocks_daily_loss_cap() -> None:
    gate = RiskInterceptor()
    now = datetime(2026, 4, 8, 10, 0, tzinfo=IST)
    decision = gate.validate_trade_safety("RELIANCE", 10, 1170.0, 3000.01, now=now)
    assert decision.allowed is False
    assert decision.code == "DAILY_LOSS_LIMIT_EXCEEDED"


def test_interceptor_blocks_after_cutoff() -> None:
    gate = RiskInterceptor()
    now = datetime(2026, 4, 8, 15, 0, tzinfo=IST)
    decision = gate.validate_trade_safety("RELIANCE", 10, 1170.0, 0.0, now=now)
    assert decision.code == "TRADE_RESTRICTED_NEAR_MARKET_CLOSE"
    late = gate.validate_trade_safety("RELIANCE", 10, 1170.0, 0.0, now=datetime(2026, 4, 8, 15, 15, tzinfo=IST))
    assert late.code == "TRADE_RESTRICTED_NEAR_MARKET_CLOSE"


def test_interceptor_allows_valid_morning_ticket() -> None:
    gate = RiskInterceptor()
    now = datetime(2026, 4, 8, 10, 5, tzinfo=IST)
    decision = gate.validate_trade_safety("RELIANCE", 12, 1170.3, 250.0, now=now)
    assert decision.allowed is True
    assert decision.code == "OK"


def test_paper_broker_persists_and_marks(tmp_path: Path) -> None:
    broker = PaperTradingBroker(tmp_path / "simulated_portfolio.json")
    broker.fill("RELIANCE", "BUY", 10, 100.0, 98.0, "NSE_EQ|INE002A01018")
    rows = broker.mark_to_market({"RELIANCE": 101.5})
    assert len(rows) == 1
    assert rows[0]["Ticker"] == "RELIANCE"
    assert rows[0]["TransactionType"] == "BUY"
    assert rows[0]["LiveUnrealizedPL"] == 15.0
    assert broker.current_daily_loss({"RELIANCE": 101.5}) == 0.0
    assert broker.emergency_close() == 1
    assert broker.open_positions() == []


def test_paper_broker_closes_at_stop(tmp_path: Path) -> None:
    broker = PaperTradingBroker(tmp_path / "simulated_portfolio.json")
    broker.fill("INFY", "BUY", 10, 100.0, 98.0)
    assert broker.close_at_stops({"INFY": 99.0}) == 0
    assert broker.close_at_stops({"INFY": 97.5}) == 1
    assert broker.open_positions() == []
    closed = broker.closed_positions()
    assert closed[0]["exit_reason"] == "STOP"
    assert closed[0]["RealizedPL"] == -20.0


def test_paper_broker_force_square_off_at_mark(tmp_path: Path) -> None:
    broker = PaperTradingBroker(tmp_path / "simulated_portfolio.json")
    broker.fill("SBIN", "BUY", 10, 100.0, 98.0)
    closed = broker.force_square_off({"SBIN": 102.0}, reason="FORCE_SQUARE_OFF_1500")
    assert closed == 1
    assert broker.open_positions() == []
    assert broker.daily_realized_pl() == 20.0


def test_paper_broker_write_survives_locked_replace(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "simulated_portfolio.json"
    broker = PaperTradingBroker(path)
    broker.fill("INFY", "SELL", 5, 1800.0, 1810.0)

    def _locked_replace(self, target):
        raise OSError("Access is denied")

    monkeypatch.setattr(Path, "replace", _locked_replace)
    rows = broker.mark_to_market({"INFY": 1790.0})
    assert rows[0]["LiveUnrealizedPL"] == 50.0
    assert path.exists()
