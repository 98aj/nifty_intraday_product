from __future__ import annotations

import pytest
from pydantic import ValidationError

from modules.copilot.schemas import MarketBias, PreMarketBrief


def test_valid_brief_parses() -> None:
    brief = PreMarketBrief.model_validate(
        {
            "market_bias": "BULLISH",
            "macro_reason": "GIFT Nifty bid and a softer dollar.",
            "volatility_risk_multiplier": 0.75,
        }
    )
    assert brief.market_bias is MarketBias.BULLISH
    assert brief.volatility_risk_multiplier == 0.75


def test_multiplier_bounds_and_bias_enum() -> None:
    with pytest.raises(ValidationError):
        PreMarketBrief.model_validate(
            {
                "market_bias": "SIDEWAYS",
                "macro_reason": "Not a valid bias label for this contract.",
                "volatility_risk_multiplier": 0.8,
            }
        )
    with pytest.raises(ValidationError):
        PreMarketBrief.model_validate(
            {
                "market_bias": "NEUTRAL",
                "macro_reason": "Too hot.",
                "volatility_risk_multiplier": 1.2,
            }
        )


def test_conservative_fallback() -> None:
    brief = PreMarketBrief.conservative_fallback("parse failed")
    assert brief.market_bias is MarketBias.NEUTRAL
    assert brief.volatility_risk_multiplier == 0.5
