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
