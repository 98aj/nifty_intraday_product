"""Upstox order execution — a thin adapter over ``modules.upstox.orders``.

This preserves today's exact Upstox behaviour (MIS market orders on the HFT host,
no retry on POST). It only re-shapes the existing Upstox order/position objects
into the broker-neutral execution types.
"""

from __future__ import annotations

from config.settings import Settings
from modules.execution.base import ExecutionBroker
from modules.execution.types import BrokerPosition, OrderRequest, OrderTicket


class UpstoxExecutionBroker(ExecutionBroker):
    name = "upstox"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def place_order(self, request: OrderRequest) -> OrderTicket:
        from modules.upstox.orders import place_mis_market_order

        ticket = place_mis_market_order(
            instrument_token=request.instrument_token,
            side=request.side,
            quantity=request.quantity,
            symbol=request.symbol,
            access_token=self._settings.upstox_access_token,
            tag=request.tag,
        )
        return _to_ticket(ticket)

    def get_positions(self) -> list[BrokerPosition]:
        from modules.upstox.orders import get_mis_positions

        return [_to_position(pos) for pos in get_mis_positions(
            access_token=self._settings.upstox_access_token
        )]

    def square_off_all(self) -> list[OrderTicket]:
        from modules.upstox.orders import square_off_all_mis

        return [_to_ticket(t) for t in square_off_all_mis(
            access_token=self._settings.upstox_access_token
        )]


def _to_ticket(ticket) -> OrderTicket:
    return OrderTicket(
        order_id=ticket.order_id,
        instrument_token=ticket.instrument_token,
        symbol=ticket.symbol,
        side=ticket.side,
        quantity=ticket.quantity,
        product=ticket.product,
        live=ticket.live,
        raw=ticket.raw,
    )


def _to_position(pos) -> BrokerPosition:
    return BrokerPosition(
        instrument_token=pos.instrument_token,
        symbol=pos.symbol,
        product=pos.product,
        quantity=pos.quantity,
        average_price=pos.average_price,
        last_price=pos.last_price,
        unrealized_pnl=pos.unrealized_pnl,
        side=pos.side,
    )
