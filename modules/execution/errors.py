"""Typed failures for the execution layer, broker-agnostic."""

from __future__ import annotations


class ExecutionError(Exception):
    """Base error for any order-execution failure."""


class ExecutionAuthError(ExecutionError):
    """The selected broker's credentials are missing, invalid or expired."""


class ExecutionAPIError(ExecutionError):
    """Transport, timeout, or non-success response while placing/reading orders."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
