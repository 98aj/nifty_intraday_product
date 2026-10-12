"""OAuth2 authorization-code exchange for Upstox API v2.

The daily access token expires at 03:30 IST the next calendar morning.
This module only writes `UPSTOX_ACCESS_TOKEN` into the local `.env` file;
it never logs the secret or the token value.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

from config.settings import (
    PROJECT_ROOT,
    UPSTOX_AUTHORIZE_URL,
    UPSTOX_TOKEN_URL,
    load_settings,
)
from modules.upstox.exceptions import UpstoxAuthError

LOGGER = logging.getLogger(__name__)


def build_authorize_url(
    api_key: str | None = None,
    redirect_uri: str | None = None,
    state: str = "nifty-intraday",
) -> str:
    """Return the Upstox hosted login URL for the authorization-code grant."""

    settings = load_settings()
    client_id = api_key or settings.upstox_api_key
    redirect = redirect_uri or settings.upstox_redirect_uri
    if not client_id:
        raise UpstoxAuthError("UPSTOX_API_KEY is required to start OAuth2")
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect,
            "state": state,
        }
    )
    return f"{UPSTOX_AUTHORIZE_URL}?{query}"


def exchange_code_for_token(
    code: str,
    api_key: str | None = None,
    secret_key: str | None = None,
    redirect_uri: str | None = None,
) -> dict[str, Any]:
    """Server-side swap of a one-time `code` for an `access_token`."""

    settings = load_settings()
    client_id = api_key or settings.upstox_api_key
    client_secret = secret_key or settings.upstox_secret_key
    redirect = redirect_uri or settings.upstox_redirect_uri
    if not code:
        raise UpstoxAuthError("Authorization code is empty")
    if not client_id or not client_secret:
        raise UpstoxAuthError("UPSTOX_API_KEY and UPSTOX_SECRET_KEY are required")

    try:
        response = requests.post(
            UPSTOX_TOKEN_URL,
            headers={
                "accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "code": code.strip(),
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect,
                "grant_type": "authorization_code",
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        raise UpstoxAuthError(f"Token endpoint unreachable: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise UpstoxAuthError(f"Token endpoint returned non-JSON (HTTP {response.status_code})") from exc

    if response.status_code >= 400 or "access_token" not in payload:
        raise UpstoxAuthError(
            f"Token exchange failed (HTTP {response.status_code}): {payload}"
        )

    access_token = str(payload["access_token"])
    os.environ["UPSTOX_ACCESS_TOKEN"] = access_token
    LOGGER.info("Upstox access token exchanged for user_id=%s", payload.get("user_id", "unknown"))
    return payload


def persist_access_token(access_token: str, env_path: Path | None = None) -> Path:
    """Write or replace `UPSTOX_ACCESS_TOKEN` in `.env` without echoing the value."""

    if not access_token:
        raise UpstoxAuthError("Refusing to persist an empty access token")
    path = env_path or PROJECT_ROOT / ".env"
    lines: list[str] = []
    replaced = False
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("UPSTOX_ACCESS_TOKEN="):
                lines.append(f"UPSTOX_ACCESS_TOKEN={access_token}")
                replaced = True
            else:
                lines.append(line)
    if not replaced:
        lines.append(f"UPSTOX_ACCESS_TOKEN={access_token}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ["UPSTOX_ACCESS_TOKEN"] = access_token
    return path
