"""Get the daily Zerodha Kite access token and store it in `.env`.

Usage:
    python scripts/zerodha_session.py

Opens the Kite login URL, waits for you to sign in, then takes the one-time
`request_token` Kite puts on the redirect URL. Paste either the whole redirected
URL or just the request_token. The access token is written to `ZERODHA_ACCESS_TOKEN`
in `.env` and is never printed. Kite tokens expire every morning, so run this once
before each trading day you execute on Zerodha.
"""

from __future__ import annotations

import sys
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import load_settings
from modules.execution.errors import ExecutionAuthError
from modules.execution.zerodha.auth import (
    build_login_url,
    exchange_request_token,
    persist_access_token,
)


def _extract_request_token(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    if "request_token" in raw:
        query = urlparse(raw).query or raw
        values = parse_qs(query).get("request_token")
        if values:
            return values[0].strip()
    return raw


def main() -> int:
    settings = load_settings()
    if not settings.zerodha_api_key or not settings.zerodha_api_secret:
        print("Set ZERODHA_API_KEY and ZERODHA_API_SECRET in .env before running this script.")
        return 2

    login_url = build_login_url()
    print("Open this URL to log in to Kite if the browser does not launch:")
    print(login_url)
    webbrowser.open(login_url)

    print("\nAfter logging in, Kite redirects to your app's redirect URL with a")
    print("`request_token` on it. Paste the full redirected URL (or just the token):")
    pasted = input("request_token> ").strip()
    request_token = _extract_request_token(pasted)
    if not request_token:
        print("No request_token provided.")
        return 1

    try:
        data = exchange_request_token(request_token)
        persist_access_token(str(data["access_token"]))
    except ExecutionAuthError as exc:
        print(f"Token exchange failed: {exc}")
        return 1
    print(f"Access token stored. user_id={data.get('user_id', 'unknown')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
