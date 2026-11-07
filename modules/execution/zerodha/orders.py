"""Zerodha MIS order placement and position reads over the raw REST client.

Every live order is forced to ``product=MIS`` (intraday) and ``order_type=MARKET``
with ``validity=DAY``. POSTs are never retried (handled by the client).
"""

from __future__ import annotations

import logging

from modules.execution.errors import ExecutionAPIError
from modules.execution.types import BrokerPosition, OrderRequest, OrderTicket, Side
from modules.execution.zerodha.client import ZerodhaClient
from modules.execution.zerodha.instruments import InstrumentResolver

LOGGER = logging.getLogger(__name__)

PRODUCT_MIS = "MIS"


def place_market_order(
    client: ZerodhaClient,
    resolver: InstrumentResolver,
    request: OrderRequest,
) -> OrderTicket:
    """Place one DAY-valid MIS market order, resolving the Kite trading symbol."""

    if request.side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    if request.quantity < 1:
        raise ValueError("quantity must be a positive integer from the risk sizer")

    contract = resolver.resolve(
        underlying=request.underlying,
        expiry=request.expiry,
        strike=request.strike,
        option_type=request.option_type,
    )
    body = {
        "tradingsymbol": contract.tradingsymbol,
        "exchange": contract.exchange or "NFO",
        "transaction_type": request.side,
        "order_type": "MARKET",
        "quantity": int(request.quantity),
        "product": PRODUCT_MIS,
        "validity": "DAY",
        "tag": request.tag[:20] if request.tag else "",
    }
    payload = client.post("/orders/regular", form_body=body)
    data = payload.get("data") or {}
    order_id = str(data.get("order_id") or "")
    if not order_id:
        raise ExecutionAPIError(f"Kite accepted the order without an order_id: {payload}")
    LOGGER.info(
        "Kite MIS %s %s qty=%s order_id=%s",
        request.side,
        contract.tradingsymbol,
        request.quantity,
        order_id,
    )
    return OrderTicket(
        order_id=order_id,
        instrument_token=contract.instrument_token,
        symbol=contract.tradingsymbol,
        side=request.side,
        quantity=int(request.quantity),
        product=PRODUCT_MIS,
        live=True,
        raw=payload,
    )


def get_mis_positions(client: ZerodhaClient) -> list[BrokerPosition]:
    """Read net positions and keep only intraday (MIS) rows."""

    payload = client.get("/portfolio/positions")
    data = payload.get("data") or {}
    net = data.get("net") if isinstance(data, dict) else None
    if not isinstance(net, list):
        return []

    positions: list[BrokerPosition] = []
    for raw in net:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("product") or "").upper() != PRODUCT_MIS:
            continue
        quantity = int(raw.get("quantity") or 0)
        try:
            average_price = float(raw.get("average_price") or 0.0)
            last_price = float(raw.get("last_price") or 0.0)
            unrealized = float(raw.get("unrealised", raw.get("pnl")) or 0.0)
        except (TypeError, ValueError):
            continue
        positions.append(
            BrokerPosition(
                instrument_token=str(raw.get("instrument_token") or ""),
                symbol=str(raw.get("tradingsymbol") or ""),
                product=PRODUCT_MIS,
                quantity=quantity,
                average_price=average_price,
                last_price=last_price,
                unrealized_pnl=unrealized,
                side="BUY" if quantity >= 0 else "SELL",
            )
        )
    return positions


def square_off_all(client: ZerodhaClient) -> list[OrderTicket]:
    """Flatten every open MIS position with an opposite market order."""

    tickets: list[OrderTicket] = []
    for position in get_mis_positions(client):
        if position.quantity == 0:
            continue
        flatten_side: Side = "SELL" if position.quantity > 0 else "BUY"
        body = {
            "tradingsymbol": position.symbol,
            "exchange": "NFO",
            "transaction_type": flatten_side,
            "order_type": "MARKET",
            "quantity": abs(position.quantity),
            "product": PRODUCT_MIS,
            "validity": "DAY",
            "tag": "EMRG-SQOFF",
        }
        payload = client.post("/orders/regular", form_body=body)
        order_id = str((payload.get("data") or {}).get("order_id") or "")
        if not order_id:
            raise ExecutionAPIError(f"Kite square-off accepted without order_id: {payload}")
        tickets.append(
            OrderTicket(
                order_id=order_id,
                instrument_token=position.instrument_token,
                symbol=position.symbol,
                side=flatten_side,
                quantity=abs(position.quantity),
                product=PRODUCT_MIS,
                live=True,
                raw=payload,
            )
        )
    return tickets
