"""Environment-backed settings and the NSE session clock.

Secrets are read from `os.environ` after an optional local `.env` load.
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

ZERODHA_API_BASE = "https://api.kite.trade"
ZERODHA_LOGIN_URL = "https://kite.trade/connect/login"

DEFAULT_MAX_INR_RISK = 500.0
HTTP_TIMEOUT_SECONDS = 8.0
DEFAULT_STRATEGY = "ORB_TIGHT"
DEFAULT_ORDER_BROKER = "upstox"
SUPPORTED_ORDER_BROKERS = {"upstox", "zerodha"}

@dataclass(frozen=True)
class Settings:
    """Immutable process configuration resolved at startup."""

    upstox_api_key: str
    upstox_secret_key: str
    upstox_access_token: str
    upstox_redirect_uri: str
    execution_mode: str
    active_strategy: str
    order_broker: str = DEFAULT_ORDER_BROKER
    zerodha_api_key: str = ""
    zerodha_api_secret: str = ""
    zerodha_access_token: str = ""
    max_inr_risk_default: float = DEFAULT_MAX_INR_RISK

    @property
    def live_execution_enabled(self) -> bool:
        return self.execution_mode == "LIVE"

    @property
    def upstox_configured(self) -> bool:
        return bool(self.upstox_access_token)

    @property
    def zerodha_configured(self) -> bool:
        return bool(
            self.zerodha_api_key and self.zerodha_api_secret and self.zerodha_access_token
        )

    @property
    def execution_broker_configured(self) -> bool:
        """True when the broker selected for order execution has its credentials."""

        if self.order_broker == "zerodha":
            return self.zerodha_configured
        return self.upstox_configured


def _require_mode(raw: str) -> str:
    mode = (raw or "MOCK").strip().upper()
    if mode not in {"MOCK", "LIVE"}:
        raise ValueError("EXECUTION_MODE must be MOCK or LIVE")
    return mode


def _require_order_broker(raw: str) -> str:
    broker = (raw or DEFAULT_ORDER_BROKER).strip().lower()
    if broker not in SUPPORTED_ORDER_BROKERS:
        raise ValueError(
            f"ORDER_BROKER must be one of {sorted(SUPPORTED_ORDER_BROKERS)}"
        )
    return broker


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
        execution_mode=_require_mode(os.environ.get("EXECUTION_MODE", "MOCK")),
        active_strategy=(os.environ.get("ACTIVE_STRATEGY", DEFAULT_STRATEGY).strip().upper() or DEFAULT_STRATEGY),
        order_broker=_require_order_broker(os.environ.get("ORDER_BROKER", DEFAULT_ORDER_BROKER)),
        zerodha_api_key=os.environ.get("ZERODHA_API_KEY", "").strip(),
        zerodha_api_secret=os.environ.get("ZERODHA_API_SECRET", "").strip(),
        zerodha_access_token=os.environ.get("ZERODHA_ACCESS_TOKEN", "").strip(),
    )
