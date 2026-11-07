"""The interface every order-execution broker must implement.

The desks and the UI only ever talk to an :class:`ExecutionBroker`; they never
import a broker SDK directly. Market data, the option chain and the live feed are
*not* part of this interface — those always come from Upstox regardless of which
broker executes the order.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from modules.execution.types import BrokerPosition, OrderRequest, OrderTicket


@runtime_checkable
class ExecutionBroker(Protocol):
    """Places and reads intraday (MIS) orders for one broker."""

    name: str

    def place_order(self, request: OrderRequest) -> OrderTicket:
        """Place one DAY-valid MIS market order and return its ticket."""

    def get_positions(self) -> list[BrokerPosition]:
        """Return the open intraday (MIS) positions for this account."""

    def square_off_all(self) -> list[OrderTicket]:
        """Flatten every open MIS position with opposite market orders."""
