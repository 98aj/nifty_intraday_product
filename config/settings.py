"""Environment-backed settings and the NSE session clock.

Secrets are read from `os.environ` after an optional local `.env` load.
Gemini is configured here and is never imported by the order router.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

IST = ZoneInfo("Asia/Kolkata")
PROJECT_ROOT = Path(__file__).resolve().parent.parent

SESSION_OPEN = time(9, 15)
OPENING_RANGE_END = time(9, 30)
SESSION_CLOSE = time(15, 30)
TRADE_CUTOFF = time(15, 0)
FORCE_SQUARE_OFF = time(15, 0)
MAX_DAILY_LOSS_INR = 3000.0

UPSTOX_API_BASE = "https://api.upstox.com/v2"
UPSTOX_FEED_AUTHORIZE_V3 = "https://api.upstox.com/v3/feed/market-data-feed/authorize"
UPSTOX_FEED_AUTHORIZE_V2 = "https://api.upstox.com/v2/feed/market-data-feed/authorize"
UPSTOX_HFT_BASE = "https://api-hft.upstox.com/v2"
UPSTOX_AUTHORIZE_URL = "https://api.upstox.com/v2/login/authorization/dialog"
UPSTOX_TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"

DEFAULT_MAX_INR_RISK = 500.0
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
HTTP_TIMEOUT_SECONDS = 8.0
DEFAULT_STRATEGY = "ORB_TIGHT"

@dataclass(frozen=True)
class Settings:
    """Immutable process configuration resolved at startup."""

    upstox_api_key: str
    upstox_secret_key: str
    upstox_access_token: str
    upstox_redirect_uri: str
    gemini_api_key: str
    gemini_model: str
    execution_mode: str
    active_strategy: str
    max_inr_risk_default: float = DEFAULT_MAX_INR_RISK

    @property
    def live_execution_enabled(self) -> bool:
        return self.execution_mode == "LIVE"

    @property
    def upstox_configured(self) -> bool:
        return bool(self.upstox_access_token)

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key)


def _require_mode(raw: str) -> str:
    mode = (raw or "MOCK").strip().upper()
    if mode not in {"MOCK", "LIVE"}:
        raise ValueError("EXECUTION_MODE must be MOCK or LIVE")
    return mode


def load_settings(dotenv_path: Path | None = None) -> Settings:
    """Load `.env` (if present) then bind settings from `os.environ`."""

    load_dotenv(dotenv_path or PROJECT_ROOT / ".env", override=True)
    return Settings(
        upstox_api_key=os.environ.get("UPSTOX_API_KEY", "").strip(),
        upstox_secret_key=os.environ.get("UPSTOX_SECRET_KEY", "").strip(),
        upstox_access_token=os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip(),
        upstox_redirect_uri=os.environ.get(
            "UPSTOX_REDIRECT_URI", "http://127.0.0.1:8000/callback"
        ).strip(),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
        gemini_model=os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip()
        or DEFAULT_GEMINI_MODEL,
        execution_mode=_require_mode(os.environ.get("EXECUTION_MODE", "MOCK")),
        active_strategy=(os.environ.get("ACTIVE_STRATEGY", DEFAULT_STRATEGY).strip().upper() or DEFAULT_STRATEGY),
    )
