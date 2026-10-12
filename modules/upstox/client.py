"""Thin `requests` session wrapper. GET may retry; order POSTs never retry."""

from __future__ import annotations

import logging
from typing import Any

import requests

from config.settings import HTTP_TIMEOUT_SECONDS, UPSTOX_API_BASE
from modules.upstox.exceptions import UpstoxAPIError, UpstoxAuthError

LOGGER = logging.getLogger(__name__)


class UpstoxClient:
    """Authenticated HTTP client for Upstox API v2.

    Order placement must use a dedicated client pointed at the HFT host and
    must call `post(..., retry=False)` so a timeout cannot double-fire.
    """

    def __init__(
        self,
        access_token: str,
        base_url: str = UPSTOX_API_BASE,
        timeout: float = HTTP_TIMEOUT_SECONDS,
    ) -> None:
        if not access_token:
            raise UpstoxAuthError("UPSTOX_ACCESS_TOKEN is empty")
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
            }
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", path, params=params, retry=True)

    def post(
        self,
        path: str,
        json_body: dict[str, Any] | None = None,
        form_body: dict[str, Any] | None = None,
        retry: bool = False,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            path,
            json_body=json_body,
            form_body=form_body,
            retry=retry,
        )

    def ping_profile(self) -> dict[str, Any]:
        """Full-session token probe. Extended/read-only tokens often return 401."""

        return self.get("/user/profile")

    def ping_market_data(self, instrument_key: str = "NSE_EQ|INE002A01018") -> dict[str, Any]:
        """Read-only connectivity probe used by extended market-data tokens."""

        return self.get("/market-quote/ltp", params={"instrument_key": instrument_key})

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        form_body: dict[str, Any] | None = None,
        retry: bool = False,
    ) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        attempts = 2 if retry and method == "GET" else 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    data=form_body,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
                LOGGER.warning("Upstox %s %s failed (attempt %s): %s", method, url, attempt, exc)
                continue

            if response.status_code == 401:
                raise UpstoxAuthError("Upstox rejected the access token (HTTP 401)")
            if response.status_code >= 400:
                raise UpstoxAPIError(
                    f"Upstox {method} {url} returned HTTP {response.status_code}: {response.text}",
                    status_code=response.status_code,
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise UpstoxAPIError(f"Upstox returned non-JSON from {url}") from exc
            if isinstance(payload, dict) and payload.get("status") == "error":
                raise UpstoxAPIError(f"Upstox error payload from {url}: {payload}")
            return payload if isinstance(payload, dict) else {"data": payload}

        raise UpstoxAPIError(f"Upstox {method} {url} dropped after {attempts} attempt(s): {last_error}")
