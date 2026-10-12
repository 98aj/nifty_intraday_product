"""Pydantic contract forced on every Gemini pre-market response."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator


class MarketBias(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class PreMarketBrief(BaseModel):
    """Strict JSON payload emitted by Gemini 2.5 and validated in-process."""

    market_bias: MarketBias
    macro_reason: str = Field(..., min_length=8, max_length=800)
    volatility_risk_multiplier: float = Field(
        ...,
        ge=0.5,
        le=1.0,
        description="Scales Week-2 max INR risk down during choppy sessions.",
    )

    @field_validator("macro_reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("macro_reason cannot be empty")
        return cleaned

    @classmethod
    def conservative_fallback(cls, reason: str) -> "PreMarketBrief":
        return cls(
            market_bias=MarketBias.NEUTRAL,
            macro_reason=reason,
            volatility_risk_multiplier=0.5,
        )
