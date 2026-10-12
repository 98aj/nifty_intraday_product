"""Typed failures for Upstox HTTP, auth, and payload-shape errors."""

from __future__ import annotations


class UpstoxError(Exception):
    """Base error for every Upstox integration failure."""


class UpstoxAuthError(UpstoxError):
    """OAuth2 exchange failed or the access token is missing/invalid."""


class UpstoxAPIError(UpstoxError):
    """Transport, timeout, or non-success HTTP response from Upstox."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class UpstoxDataError(UpstoxError):
    """Candle or quote payload could not be normalized into a DataFrame."""
