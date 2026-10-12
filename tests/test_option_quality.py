from modules.copilot.option_quality import QualityView, quality_allows, quality_summary


def test_unconfigured_gemini_leaves_the_rule_alone() -> None:
    view = QualityView(False, False, "OFF", "", False)
    assert quality_allows("BUY", view) == (True, "")
    title, _body = quality_summary(view)
    assert title == "Gemini is off"


def test_clean_bullish_allows_only_the_call() -> None:
    view = QualityView(True, True, "BULLISH", "Gift Nifty is bid and the open is one-sided.", True)
    assert quality_allows("BUY", view)[0] is True
    assert quality_allows("SELL", view)[0] is False
    assert quality_summary(view)[0] == "Gemini allows a call"


def test_choppy_or_neutral_open_is_skipped() -> None:
    choppy = QualityView(True, True, "BULLISH", "The range is wide and two-sided.", False)
    neutral = QualityView(True, True, "NEUTRAL", "No clear direction in the first half hour.", True)
    assert quality_allows("BUY", choppy)[0] is False
    assert quality_allows("SELL", neutral)[0] is False
    assert quality_summary(neutral)[0] == "Gemini will skip today's break"


def test_clean_bearish_allows_only_the_put() -> None:
    view = QualityView(True, True, "BEARISH", "Global futures are offered and the open is trending down.", True)
    assert quality_allows("SELL", view)[0] is True
    assert quality_allows("BUY", view)[0] is False
    assert quality_summary(view)[0] == "Gemini allows a put"
