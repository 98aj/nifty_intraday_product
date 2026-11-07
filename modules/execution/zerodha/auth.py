"""Zerodha Kite Connect login / daily access-token exchange.

Kite access tokens expire every morning. The flow: open the login URL, sign in,
and Kite redirects back with a one-time ``request_token``. That token plus a
SHA-256 checksum is exchanged for the day's ``access_token`` at
``POST /session/token``. This module never logs the secret or the token value.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

from config.settings import (
    PROJECT_ROOT,
    ZERODHA_API_BASE,
    ZERODHA_LOGIN_URL,
    load_settings,
)
from modules.execution.errors import ExecutionAuthError

LOGGER = logging.getLogger(__name__)


def build_login_url(api_key: str | None = None) -> str:
    """Return the Kite hosted login URL for the daily token flow."""

    client_id = api_key or load_settings().zerodha_api_key
    if not client_id:
        raise ExecutionAuthError("ZERODHA_API_KEY is required to start the Kite login")
    return f"{ZERODHA_LOGIN_URL}?{urlencode({'v': '3', 'api_key': client_id})}"


def exchange_request_token(
    request_token: str,
    api_key: str | None = None,
    api_secret: str | None = None,
) -> dict[str, Any]:
    """Swap a one-time ``request_token`` for the day's ``access_token``."""

    settings = load_settings()
    client_id = api_key or settings.zerodha_api_key
    client_secret = api_secret or settings.zerodha_api_secret
    if not request_token:
        raise ExecutionAuthError("request_token is empty")
    if not client_id or not client_secret:
        raise ExecutionAuthError("ZERODHA_API_KEY and ZERODHA_API_SECRET are required")

    checksum = hashlib.sha256(
        (client_id + request_token.strip() + client_secret).encode("utf-8")
    ).hexdigest()
    try:
        response = requests.post(
            f"{ZERODHA_API_BASE}/session/token",
            headers={"X-Kite-Version": "3"},
            data={
                "api_key": client_id,
                "request_token": request_token.strip(),
                "checksum": checksum,
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        raise ExecutionAuthError(f"Kite session endpoint unreachable: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise ExecutionAuthError(
            f"Kite session endpoint returned non-JSON (HTTP {response.status_code})"
        ) from exc

    data = payload.get("data") if isinstance(payload, dict) else None
    if response.status_code >= 400 or not isinstance(data, dict) or "access_token" not in data:
        raise ExecutionAuthError(f"Kite token exchange failed (HTTP {response.status_code}): {payload}")

    access_token = str(data["access_token"])
    os.environ["ZERODHA_ACCESS_TOKEN"] = access_token
    LOGGER.info("Kite access token exchanged for user_id=%s", data.get("user_id", "unknown"))
    return data


def persist_access_token(access_token: str, env_path: Path | None = None) -> Path:
    """Write or replace ``ZERODHA_ACCESS_TOKEN`` in ``.env`` without echoing it."""

    if not access_token:
        raise ExecutionAuthError("Refusing to persist an empty access token")
    path = env_path or PROJECT_ROOT / ".env"
    lines: list[str] = []
    replaced = False
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("ZERODHA_ACCESS_TOKEN="):
                lines.append(f"ZERODHA_ACCESS_TOKEN={access_token}")
                replaced = True
            else:
                lines.append(line)
    if not replaced:
        lines.append(f"ZERODHA_ACCESS_TOKEN={access_token}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ["ZERODHA_ACCESS_TOKEN"] = access_token
    return path
