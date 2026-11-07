"""Broker-neutral order and position types shared by every execution adapter.

These mirror the Upstox :class:`OrderTicket` / :class:`BrokerPosition` so the
Upstox adapter can delegate without translation, while giving the Zerodha
adapter everything it needs. An :class:`OrderRequest` carries the Upstox
instrument handle *and* the contract identity (strike/expiry/option type), so a
broker that uses its own trading symbol can resolve it without assuming the two
brokers share one symbol format.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Side = Literal["BUY", "SELL"]


@dataclass(frozen=True)
class OrderRequest:
    """A broker-neutral request to place one intraday (MIS) market order.

    ``instrument_token`` is the Upstox instrument key the rest of the app already
    uses. ``symbol`` is the human-readable trading symbol. The contract-identity
    fields let a broker that cannot use the Upstox key (Zerodha) resolve its own
    exchange trading symbol from the underlying, expiry, strike and option type.
    """

    instrument_token: str
    symbol: str
    side: Side
    quantity: int
    tag: str = "ORB-MIS"
    underlying: str = "NIFTY"
    strike: float = 0.0
    expiry: str = ""
    option_type: str = ""


@dataclass(frozen=True)
class OrderTicket:
    """Immutable result of a live MIS placement."""

    order_id: str
    instrument_token: str
    symbol: str
    side: Side
    quantity: int
    product: str
    live: bool
    raw: dict[str, Any]


@dataclass(frozen=True)
class BrokerPosition:
    """Normalized short-term (intraday) position row."""

    instrument_token: str
    symbol: str
    product: str
    quantity: int
    average_price: float
    last_price: float
    unrealized_pnl: float
    side: Side
