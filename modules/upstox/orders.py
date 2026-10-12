"""MIS order routing. This module is unreachable from the Gemini copilot.

Every live order is forced to `product="I"` (intraday / MIS). Callers must
pass an already-computed integer quantity from `calculate_position_size`.
POSTs are never retried.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from config.settings import UPSTOX_HFT_BASE
from modules.upstox.client import UpstoxClient
from modules.upstox.exceptions import UpstoxAPIError, UpstoxAuthError

LOGGER = logging.getLogger(__name__)

Side = Literal["BUY", "SELL"]
INTRADAY_PRODUCT = "I"


@dataclass(frozen=True)
class OrderTicket:
    """Immutable result of a mock or live MIS placement."""

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
    """Normalized short-term position row."""

    instrument_token: str
    symbol: str
    product: str
    quantity: int
    average_price: float
    last_price: float
    unrealized_pnl: float
    side: Side


def place_mis_market_order(
    instrument_token: str,
    side: Side,
    quantity: int,
    symbol: str = "",
    client: UpstoxClient | None = None,
    access_token: str | None = None,
    tag: str = "ORB-MIS",
) -> OrderTicket:
    """Place a DAY-valid MIS market order on the HFT host."""

    if side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    if quantity < 1:
        raise ValueError("quantity must be a positive integer from the risk sizer")
    if not instrument_token:
        raise ValueError("instrument_token is required")

    from config.settings import load_settings

    if not load_settings().live_execution_enabled:
        raise UpstoxAuthError("Live MIS orders are locked while EXECUTION_MODE=MOCK")

    http = client or _hft_client(access_token)
    body = {
        "quantity": int(quantity),
        "product": INTRADAY_PRODUCT,
        "validity": "DAY",
        "price": 0,
        "instrument_token": instrument_token,
        "order_type": "MARKET",
        "transaction_type": side,
        "disclosed_quantity": 0,
        "trigger_price": 0,
        "is_amo": False,
        "market_protection": 0,
        "tag": tag,
    }
    payload = http.post("/order/place", json_body=body, retry=False)
    data = payload.get("data") or {}
    order_id = str(data.get("order_id") or data.get("orderId") or "")
    if not order_id:
        raise UpstoxAPIError(f"Order accepted without order_id: {payload}")
    LOGGER.info("MIS %s %s qty=%s order_id=%s", side, symbol or instrument_token, quantity, order_id)
    return OrderTicket(
        order_id=order_id,
        instrument_token=instrument_token,
        symbol=symbol,
        side=side,
        quantity=int(quantity),
        product=INTRADAY_PRODUCT,
        live=True,
        raw=payload,
    )


def flatten_mis_position(
    instrument_token: str,
    open_side: Side,
    quantity: int,
    symbol: str = "",
    client: UpstoxClient | None = None,
    access_token: str | None = None,
) -> OrderTicket:
    """Flatten one open MIS row with an opposite MARKET order (`product="I"`)."""

    flatten_side: Side = "SELL" if open_side == "BUY" else "BUY"
    return place_mis_market_order(
        instrument_token=instrument_token,
        side=flatten_side,
        quantity=quantity,
        symbol=symbol,
        client=client,
        access_token=access_token,
        tag="STOP-SQOFF",
    )


def square_off_all_mis(
    client: UpstoxClient | None = None,
    access_token: str | None = None,
) -> list[OrderTicket]:
    """Flatten every open MIS position with an opposite MARKET order (`product="I"`)."""

    http = client or _hft_client(access_token)
    tickets: list[OrderTicket] = []
    for position in get_mis_positions(client=http):
        if position.quantity == 0:
            continue
        flatten_side: Side = "SELL" if position.quantity > 0 else "BUY"
        tickets.append(
            place_mis_market_order(
                instrument_token=position.instrument_token,
                side=flatten_side,
                quantity=abs(position.quantity),
                symbol=position.symbol,
                client=http,
                tag="EMERGENCY-SQOFF",
            )
        )
    return tickets


def get_mis_positions(
    client: UpstoxClient | None = None,
    access_token: str | None = None,
) -> list[BrokerPosition]:
    """Read short-term positions and keep only MIS (`product="I"`) rows."""

    http = client or _data_client(access_token)
    payload = http.get("/portfolio/short-term-positions")
    rows = payload.get("data") or []
    if not isinstance(rows, list):
        return []

    positions: list[BrokerPosition] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        product = str(raw.get("product") or "")
        if product != INTRADAY_PRODUCT:
            continue
        quantity = int(raw.get("quantity") or 0)
        last_price = float(raw.get("last_price") or 0.0)
        average_price = float(raw.get("average_price") or 0.0)
        unrealized = raw.get("unrealised", raw.get("pnl"))
        try:
            unrealized_pnl = float(unrealized if unrealized is not None else 0.0)
        except (TypeError, ValueError):
            unrealized_pnl = (last_price - average_price) * quantity
        positions.append(
            BrokerPosition(
                instrument_token=str(raw.get("instrument_token") or raw.get("instrument_key") or ""),
                symbol=str(raw.get("trading_symbol") or raw.get("symbol") or ""),
                product=product,
                quantity=quantity,
                average_price=average_price,
                last_price=last_price,
                unrealized_pnl=unrealized_pnl,
                side="BUY" if quantity >= 0 else "SELL",
            )
        )
    return positions


def _hft_client(access_token: str | None) -> UpstoxClient:
    token = access_token or _token_from_env()
    return UpstoxClient(token, base_url=UPSTOX_HFT_BASE)


def _data_client(access_token: str | None) -> UpstoxClient:
    token = access_token or _token_from_env()
    return UpstoxClient(token)


def _token_from_env() -> str:
    from config.settings import load_settings

    token = load_settings().upstox_access_token
    if not token:
        raise UpstoxAuthError("UPSTOX_ACCESS_TOKEN is not set")
    return token
