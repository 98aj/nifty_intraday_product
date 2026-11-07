"""Pick the configured order-execution broker.

``ORDER_BROKER`` chooses where LIVE orders go. Market data, the option chain and
the price feed are unaffected — they always come from Upstox. The adapter is
cached per-process, keyed by broker name, so the Zerodha instruments dump and the
HTTP session are reused across cycles.
"""

from __future__ import annotations

import threading

from config.settings import Settings
from modules.execution.base import ExecutionBroker
from modules.execution.upstox_exec import UpstoxExecutionBroker

_LOCK = threading.Lock()
_CACHE: dict[str, ExecutionBroker] = {}


def get_execution_broker(settings: Settings) -> ExecutionBroker:
    """Return the execution broker named by ``settings.order_broker``.

    Cached per (broker, access token) so a daily token refresh rebuilds the
    adapter, while a stable token reuses the HTTP session and (for Zerodha) the
    cached instruments dump across cycles.
    """

    broker_name = settings.order_broker
    token = (
        settings.zerodha_access_token
        if broker_name == "zerodha"
        else settings.upstox_access_token
    )
    key = f"{broker_name}:{token}"
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached
        broker = _build(broker_name, settings)
        _CACHE[key] = broker
        return broker


def reset_cache() -> None:
    """Drop cached adapters (used by tests and after a credentials change)."""

    with _LOCK:
        _CACHE.clear()


def _build(broker_name: str, settings: Settings) -> ExecutionBroker:
    if broker_name == "zerodha":
        from modules.execution.zerodha import ZerodhaExecutionBroker

        return ZerodhaExecutionBroker(settings)
    return UpstoxExecutionBroker(settings)
