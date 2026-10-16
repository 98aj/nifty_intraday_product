from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]


def test_options_desk_renders_without_stock_scanner() -> None:
    app = AppTest.from_file(ROOT / "app.py", default_timeout=60)
    app.run()
    assert not app.exception
    radio_values = [str(option) for radio in app.radio for option in radio.options]
    assert any("paper" in value.lower() for value in radio_values)
    assert any("live" in value.lower() for value in radio_values)
    markdown = " ".join(str(block.value) for block in app.markdown)
    assert "Nifty weekly options" in markdown
    assert "Nifty 50 ticker" not in markdown
    assert "Gemini" in markdown
    labels = [str(item.label) for item in app.number_input]
    assert any("capital" in label.lower() for label in labels)


def test_banknifty_desk_renders_the_late_session() -> None:
    app = AppTest.from_file(ROOT / "modules" / "ui" / "banknifty_desk_page.py", default_timeout=60)
    app.run()
    assert not app.exception
    radio_values = [str(option) for radio in app.radio for option in radio.options]
    assert any("paper" in value.lower() for value in radio_values)
    assert any("live" in value.lower() for value in radio_values)
    markdown = " ".join(str(block.value) for block in app.markdown)
    assert "Bank Nifty monthly options" in markdown
    assert "does not block a trade" in markdown
    assert "Nifty weekly options" not in markdown
    loss = next(item for item in app.number_input if "max loss" in str(item.label).lower())
    assert loss.value == 6000


def test_banknifty_page_keeps_its_own_book() -> None:
    desk = (ROOT / "modules" / "ui" / "banknifty_desk_page.py").read_text(encoding="utf-8")
    nifty = (ROOT / "modules" / "ui" / "desk_page.py").read_text(encoding="utf-8")
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "BANKNIFTY_PORTFOLIO_PATH" in desk
    assert "nearest_listed_expiry" in desk
    assert "listed_lot_size" in desk
    assert "evaluate_market_signals" not in desk
    assert "nearest_weekly_expiry" not in desk
    assert "late_entry_allowed" in desk
    assert "simulated_portfolio" not in desk
    assert "calculate_opening_range" in nifty
    assert "BANKNIFTY_PORTFOLIO_PATH" not in nifty
    assert "PaperTradingBroker()" in nifty
    assert "banknifty_desk_page.py" in app


def test_backtest_page_shows_capital_lots_stop_and_target() -> None:
    app = AppTest.from_file(ROOT / "modules" / "ui" / "backtest_page.py", default_timeout=30)
    app.run()
    assert not app.exception
    labels = " ".join(str(item.label) for item in app.number_input).lower()
    assert "capital" in labels
    assert "lots" in labels
    assert "stop" in labels
    assert "take profit" in labels
