"""Zerodha order-execution adapter implementing :class:`ExecutionBroker`."""

from __future__ import annotations

from config.settings import Settings
from modules.execution.base import ExecutionBroker
from modules.execution.errors import ExecutionAuthError
from modules.execution.types import BrokerPosition, OrderRequest, OrderTicket
from modules.execution.zerodha.client import ZerodhaClient
from modules.execution.zerodha.instruments import InstrumentResolver
from modules.execution.zerodha.orders import (
    get_mis_positions,
    place_market_order,
    square_off_all,
)


class ZerodhaExecutionBroker(ExecutionBroker):
    name = "zerodha"

    def __init__(self, settings: Settings) -> None:
        if not settings.zerodha_configured:
            raise ExecutionAuthError(
                "Zerodha order execution needs ZERODHA_API_KEY, "
                "ZERODHA_API_SECRET and ZERODHA_ACCESS_TOKEN"
            )
        self._settings = settings
        self._client = ZerodhaClient(
            settings.zerodha_api_key, settings.zerodha_access_token
        )
        self._resolver = InstrumentResolver(self._client)

    def place_order(self, request: OrderRequest) -> OrderTicket:
        if not self._settings.live_execution_enabled:
            raise ExecutionAuthError("Live MIS orders are locked while EXECUTION_MODE=MOCK")
        return place_market_order(self._client, self._resolver, request)

    def get_positions(self) -> list[BrokerPosition]:
        return get_mis_positions(self._client)

    def square_off_all(self) -> list[OrderTicket]:
        if not self._settings.live_execution_enabled:
            raise ExecutionAuthError("Live MIS orders are locked while EXECUTION_MODE=MOCK")
        return square_off_all(self._client)
