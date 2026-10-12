"""Automated Upstox OAuth2 authorization-code exchange.

Usage:
    python scripts/oauth_exchange.py

Registers a one-shot localhost callback, opens the Upstox login dialog,
swaps the returned `code` for an access token, and writes
`UPSTOX_ACCESS_TOKEN` into `.env`. The token is never printed.
"""

from __future__ import annotations

import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import load_settings
from modules.upstox.auth import build_authorize_url, exchange_code_for_token, persist_access_token
from modules.upstox.exceptions import UpstoxAuthError


class _CallbackHandler(BaseHTTPRequestHandler):
    server_version = "NiftyIntradayOAuth/1.0"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        code = (params.get("code") or [""])[0]
        error = (params.get("error") or [""])[0]
        self.server.auth_code = code  # type: ignore[attr-defined]
        self.server.auth_error = error  # type: ignore[attr-defined]
        body = b"Authorization complete. You can close this tab and return to the terminal."
        if error or not code:
            body = b"Authorization failed. Check the terminal."
        self.send_response(200 if code and not error else 400)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def _serve_callback(host: str, port: int) -> HTTPServer:
    httpd = HTTPServer((host, port), _CallbackHandler)
    httpd.auth_code = ""  # type: ignore[attr-defined]
    httpd.auth_error = ""  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.handle_request, daemon=True)
    thread.start()
    return httpd


def main() -> int:
    settings = load_settings()
    if not settings.upstox_api_key or not settings.upstox_secret_key:
        print("Set UPSTOX_API_KEY and UPSTOX_SECRET_KEY in .env before running this script.")
        return 2

    parsed = urlparse(settings.upstox_redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8000
    httpd = _serve_callback(host, port)
    authorize_url = build_authorize_url()
    print("Open this URL if the browser does not launch:")
    print(authorize_url)
    webbrowser.open(authorize_url)

    try:
        httpd.timeout = 180
        # handle_request is already running on a daemon thread.
        waiter = threading.Event()
        def _poll() -> None:
            while not getattr(httpd, "auth_code", "") and not getattr(httpd, "auth_error", ""):
                waiter.wait(0.2)
                if waiter.is_set():
                    break
        poller = threading.Thread(target=_poll, daemon=True)
        poller.start()
        poller.join(timeout=180)
    finally:
        httpd.server_close()

    code = getattr(httpd, "auth_code", "")
    error = getattr(httpd, "auth_error", "")
    if error:
        print(f"Upstox returned error={error}")
        return 1
    if not code:
        print("No authorization code received. Paste the `code` query param manually.")
        code = input("code> ").strip()
    try:
        payload = exchange_code_for_token(code)
        persist_access_token(str(payload["access_token"]))
    except UpstoxAuthError as exc:
        print(f"Token exchange failed: {exc}")
        return 1
    print(f"Access token stored. user_id={payload.get('user_id', 'unknown')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
