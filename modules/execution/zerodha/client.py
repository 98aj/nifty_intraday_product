"""Raw ``requests`` client for the Zerodha Kite Connect v3 REST API.

No SDK. Mirrors the Upstox client's discipline: GET may retry once, order POSTs
never retry so a timeout cannot double-fire. Kite authenticates with the header
``Authorization: token <api_key>:<access_token>`` and ``X-Kite-Version: 3``.
Successful payloads are ``{"status": "success", "data": ...}``; failures are
``{"status": "error", "message": ...}``.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from config.settings import HTTP_TIMEOUT_SECONDS, ZERODHA_API_BASE
from modules.execution.errors import ExecutionAPIError, ExecutionAuthError

LOGGER = logging.getLogger(__name__)


class ZerodhaClient:
    """Authenticated HTTP client for Kite Connect v3."""

    def __init__(
        self,
        api_key: str,
        access_token: str,
        base_url: str = ZERODHA_API_BASE,
        timeout: float = HTTP_TIMEOUT_SECONDS,
    ) -> None:
        if not api_key:
            raise ExecutionAuthError("ZERODHA_API_KEY is empty")
        if not access_token:
            raise ExecutionAuthError("ZERODHA_ACCESS_TOKEN is empty")
        self.api_key = api_key
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "X-Kite-Version": "3",
                "Authorization": f"token {api_key}:{access_token}",
            }
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request_json("GET", path, params=params, retry=True)

    def post(self, path: str, form_body: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request_json("POST", path, form_body=form_body, retry=False)

    def get_text(self, path: str) -> str:
        """Fetch a non-JSON body (the instruments dump is CSV)."""

        url = self._url(path)
        try:
            response = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ExecutionAPIError(f"Kite GET {url} failed: {exc}") from exc
        if response.status_code == 403:
            raise ExecutionAuthError("Kite rejected the access token (HTTP 403)")
        if response.status_code >= 400:
            raise ExecutionAPIError(
                f"Kite GET {url} returned HTTP {response.status_code}: {response.text}",
                status_code=response.status_code,
            )
        return response.text

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.base_url}{path}"

    def _request_json(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        form_body: dict[str, Any] | None = None,
        retry: bool = False,
    ) -> dict[str, Any]:
        url = self._url(path)
        attempts = 2 if retry and method == "GET" else 1
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    data=form_body,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
                LOGGER.warning("Kite %s %s failed (attempt %s): %s", method, url, attempt, exc)
                continue

            # Kite returns 403 for an expired/invalid token.
            if response.status_code == 403:
                raise ExecutionAuthError("Kite rejected the access token (HTTP 403)")
            if response.status_code >= 400:
                raise ExecutionAPIError(
                    f"Kite {method} {url} returned HTTP {response.status_code}: {response.text}",
                    status_code=response.status_code,
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise ExecutionAPIError(f"Kite returned non-JSON from {url}") from exc
            if isinstance(payload, dict) and payload.get("status") == "error":
                raise ExecutionAPIError(f"Kite error payload from {url}: {payload.get('message') or payload}")
            return payload if isinstance(payload, dict) else {"data": payload}

        raise ExecutionAPIError(f"Kite {method} {url} dropped after {attempts} attempt(s): {last_error}")
