"""Gemini filter for the Nifty option open. It never imports the order router."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from config.settings import IST, PROJECT_ROOT, load_settings
from modules.copilot.schemas import MarketBias, PreMarketBrief

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

LOGGER = logging.getLogger("desk.gemini")


@dataclass(frozen=True)
class QualityView:
    configured: bool
    ready: bool
    bias: str
    reason: str
    clean: bool


def quality_allows(signal: str, view: QualityView) -> tuple[bool, str]:
    """True when this break is clean and Gemini's direction matches it.

    With no API key the opening-range rule is left alone. A configured brief
    that is choppy, neutral, or the other way is skipped.
    """

    if not view.configured or not view.ready:
        return True, ""
    if not view.clean:
        return False, "Gemini says this open is choppy, so the break is skipped."
    if signal == "BUY" and view.bias == MarketBias.BULLISH.value:
        return True, "Gemini agrees with the call."
    if signal == "SELL" and view.bias == MarketBias.BEARISH.value:
        return True, "Gemini agrees with the put."
    if view.bias == MarketBias.NEUTRAL.value:
        return False, "Gemini sees no clear direction, so no option is bought."
    return False, "Gemini's direction does not match this break, so it is skipped."


def quality_summary(view: QualityView) -> tuple[str, str]:
    if not view.configured:
        return (
            "Gemini is off",
            "Add GEMINI_API_KEY in the .env file. Until then the opening-range rule decides on its own.",
        )
    if not view.ready:
        return (
            "Gemini waits for 9:30",
            "After the high and low are marked, it allows a call only on a clean bullish open, or a put only on a clean bearish open.",
        )
    call_ok, _ = quality_allows("BUY", view)
    put_ok, _ = quality_allows("SELL", view)
    if call_ok:
        headline = "Gemini allows a call"
    elif put_ok:
        headline = "Gemini allows a put"
    else:
        headline = "Gemini will skip today's break"
    return headline, view.reason


def load_option_quality(now: datetime, spot: float, high: float, low: float, ready: bool) -> QualityView:
    """One Gemini read per day, after the opening range exists. Cached on disk."""

    settings = load_settings()
    if not settings.gemini_configured:
        return QualityView(False, False, "OFF", "", False)
    if not ready or spot <= 0 or high <= 0 or low <= 0:
        return QualityView(True, False, "OFF", "", False)

    clock = now.astimezone(IST) if now.tzinfo else now.replace(tzinfo=IST)
    day = clock.date().isoformat()
    path = PROJECT_ROOT / "data" / f"option_quality_{day}.json"
    if path.exists():
        try:
            brief = PreMarketBrief.model_validate_json(path.read_text(encoding="utf-8"))
            return _view(brief)
        except (OSError, ValidationError, ValueError):
            LOGGER.warning("Cached option quality was unreadable and will be refreshed")

    try:
        raw = _ask_gemini(settings.gemini_api_key, settings.gemini_model, clock, spot, high, low)
        brief = PreMarketBrief.model_validate(_parse_json_object(raw))
    except Exception as exc:
        LOGGER.exception("Gemini option quality failed")
        brief = PreMarketBrief.conservative_fallback(
            f"Gemini could not review the open ({exc.__class__.__name__}), so the break is skipped."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(brief.model_dump_json(indent=2), encoding="utf-8")
    return _view(brief)


def _parse_json_object(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    fenced = _JSON_FENCE.search(text)
    if fenced:
        text = fenced.group(1)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Gemini JSON parse failure: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Gemini JSON root is not an object")
    return payload


def _view(brief: PreMarketBrief) -> QualityView:
    return QualityView(
        configured=True,
        ready=True,
        bias=brief.market_bias.value,
        reason=brief.macro_reason,
        clean=brief.volatility_risk_multiplier >= 1.0,
    )


def _ask_gemini(api_key: str, model: str, clock: datetime, spot: float, high: float, low: float) -> str:
    from google import genai
    from google.genai import types

    width = (high - low) / spot * 100.0 if spot else 0.0
    prompt = (
        "You filter one Nifty weekly option trade. You do not place orders. "
        f"IST time: {clock.isoformat()}. Nifty spot: {spot:.2f}. "
        f"Opening range high: {high:.2f}. Low: {low:.2f}. Width: {width:.2f}%. "
        "The desk buys an at-the-money call only if Nifty breaks the high, "
        "or an at-the-money put only if Nifty breaks the low. "
        "Set market_bias to BULLISH only when a call is the quality trade, "
        "BEARISH only when a put is, and NEUTRAL when the open should be skipped. "
        "Set volatility_risk_multiplier to 1.0 only for a clean directional open, "
        "and 0.5 when the range is wide, news-driven, or two-sided. "
        "macro_reason is one or two sentences a trader can read. Return JSON only."
    )
    config: dict[str, Any] = {
        "temperature": 0.2,
        "response_mime_type": "application/json",
        "response_json_schema": PreMarketBrief.model_json_schema(),
    }
    client = genai.Client(api_key=api_key)
    try:
        config["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(**config),
        )
    except Exception:
        config.pop("tools", None)
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(**config),
        )
    text = getattr(response, "text", None)
    if not text:
        raise ValueError("Gemini returned an empty response")
    return text
