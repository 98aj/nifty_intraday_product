"""Order-execution layer: factory selection, Upstox delegation, Zerodha REST."""

from __future__ import annotations

import pytest

from config.settings import Settings
from modules.execution import OrderRequest, get_execution_broker, reset_cache
from modules.execution.errors import ExecutionAPIError, ExecutionAuthError
from modules.execution.upstox_exec import UpstoxExecutionBroker
from modules.execution.types import OrderTicket as ExecTicket
from modules.execution.zerodha.adapter import ZerodhaExecutionBroker
from modules.execution.zerodha.instruments import InstrumentResolver, parse_nfo_dump
from modules.execution.zerodha.orders import (
    get_mis_positions,
    place_market_order,
    square_off_all,
)

NFO_CSV = (
    "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
    "tick_size,lot_size,instrument_type,segment,exchange\n"
    "12345,48,NIFTY2511323500CE,NIFTY,0,2025-11-13,23500,0.05,75,CE,NFO-OPT,NFO\n"
    "12346,49,NIFTY2511323500PE,NIFTY,0,2025-11-13,23500,0.05,75,PE,NFO-OPT,NFO\n"
    "12347,50,NIFTY2511323600CE,NIFTY,0,2025-11-13,23600,0.05,75,CE,NFO-OPT,NFO\n"
    "99999,51,RELIANCE25NOVFUT,RELIANCE,0,2025-11-27,0,0.05,500,FUT,NFO-FUT,NFO\n"
)


class FakeClient:
    """Stands in for ZerodhaClient; records posts and serves canned responses."""

    def __init__(self, *, csv_text: str = NFO_CSV, positions=None, order_id: str = "ORD1") -> None:
        self._csv = csv_text
        self._positions = positions or {"status": "success", "data": {"net": []}}
        self._order_id = order_id
        self.posted: list[dict] = []

    def get_text(self, path: str) -> str:
        return self._csv

    def get(self, path: str, params=None) -> dict:
        if path == "/portfolio/positions":
            return self._positions
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, form_body=None) -> dict:
        self.posted.append({"path": path, "body": form_body})
        return {"status": "success", "data": {"order_id": self._order_id}}


def _settings(**overrides) -> Settings:
    base = dict(
        upstox_api_key="",
        upstox_secret_key="",
        upstox_access_token="utok",
        upstox_redirect_uri="http://127.0.0.1:8000/callback",
        execution_mode="LIVE",
        active_strategy="ORB_TIGHT",
    )
    base.update(overrides)
    return Settings(**base)


def _request(side: str = "BUY") -> OrderRequest:
    return OrderRequest(
        instrument_token="NSE_FO|12345",
        symbol="NIFTY 23500 CE",
        side=side,
        quantity=75,
        tag="NIFTY-OPT",
        strike=23500.0,
        expiry="2025-11-13",
        option_type="CE",
    )


# -- factory ---------------------------------------------------------------

def test_factory_defaults_to_upstox() -> None:
    reset_cache()
    broker = get_execution_broker(_settings(order_broker="upstox"))
    assert isinstance(broker, UpstoxExecutionBroker)
    assert broker.name == "upstox"


def test_factory_selects_zerodha_when_configured() -> None:
    reset_cache()
    broker = get_execution_broker(
        _settings(
            order_broker="zerodha",
            zerodha_api_key="k",
            zerodha_api_secret="s",
            zerodha_access_token="t",
        )
    )
    assert isinstance(broker, ZerodhaExecutionBroker)
    assert broker.name == "zerodha"


def test_factory_zerodha_without_credentials_raises() -> None:
    reset_cache()
    with pytest.raises(ExecutionAuthError):
        get_execution_broker(_settings(order_broker="zerodha"))


# -- upstox delegation -----------------------------------------------------

def test_upstox_adapter_delegates(monkeypatch) -> None:
    from modules.upstox import orders as upstox_orders

    captured = {}

    class _Ticket:
        order_id = "U1"
        instrument_token = "NSE_FO|12345"
        symbol = "NIFTY 23500 CE"
        side = "BUY"
        quantity = 75
        product = "I"
        live = True
        raw: dict = {}

    def _fake_place(**kwargs):
        captured.update(kwargs)
        return _Ticket()

    monkeypatch.setattr(upstox_orders, "place_mis_market_order", _fake_place)
    ticket = UpstoxExecutionBroker(_settings()).place_order(_request())
    assert isinstance(ticket, ExecTicket)
    assert ticket.order_id == "U1"
    # Upstox routes on the instrument key, not the contract identity.
    assert captured["instrument_token"] == "NSE_FO|12345"
    assert captured["side"] == "BUY"
    assert captured["quantity"] == 75


# -- zerodha instruments / resolver ---------------------------------------

def test_parse_nfo_dump_keeps_only_options() -> None:
    rows = parse_nfo_dump(NFO_CSV)
    assert len(rows) == 3  # the FUT row is dropped
    assert {r.option_type for r in rows} == {"CE", "PE"}


def test_resolver_finds_the_matching_contract() -> None:
    resolver = InstrumentResolver(FakeClient())
    contract = resolver.resolve("NIFTY", "2025-11-13", 23500.0, "CE")
    assert contract.tradingsymbol == "NIFTY2511323500CE"
    assert contract.lot_size == 75


def test_resolver_raises_when_absent() -> None:
    resolver = InstrumentResolver(FakeClient())
    with pytest.raises(ExecutionAPIError):
        resolver.resolve("NIFTY", "2025-11-13", 99999.0, "CE")


# -- zerodha orders --------------------------------------------------------

def test_zerodha_place_order_builds_mis_market_payload() -> None:
    client = FakeClient()
    resolver = InstrumentResolver(client)
    ticket = place_market_order(client, resolver, _request())
    assert ticket.order_id == "ORD1"
    assert ticket.symbol == "NIFTY2511323500CE"
    body = client.posted[0]["body"]
    assert client.posted[0]["path"] == "/orders/regular"
    assert body["tradingsymbol"] == "NIFTY2511323500CE"
    assert body["exchange"] == "NFO"
    assert body["transaction_type"] == "BUY"
    assert body["order_type"] == "MARKET"
    assert body["product"] == "MIS"
    assert body["validity"] == "DAY"
    assert body["quantity"] == 75


def test_zerodha_positions_keep_only_mis() -> None:
    client = FakeClient(
        positions={
            "status": "success",
            "data": {
                "net": [
                    {
                        "tradingsymbol": "NIFTY2511323500CE",
                        "instrument_token": 12345,
                        "product": "MIS",
                        "quantity": 75,
                        "average_price": 60.0,
                        "last_price": 72.0,
                        "pnl": 900.0,
                    },
                    {
                        "tradingsymbol": "INFY",
                        "product": "CNC",
                        "quantity": 10,
                        "average_price": 1500.0,
                        "last_price": 1510.0,
                        "pnl": 100.0,
                    },
                ]
            },
        }
    )
    positions = get_mis_positions(client)
    assert len(positions) == 1
    assert positions[0].symbol == "NIFTY2511323500CE"
    assert positions[0].side == "BUY"


def test_zerodha_square_off_sends_opposite_orders() -> None:
    client = FakeClient(
        positions={
            "status": "success",
            "data": {
                "net": [
                    {
                        "tradingsymbol": "NIFTY2511323500CE",
                        "instrument_token": 12345,
                        "product": "MIS",
                        "quantity": 75,
                        "average_price": 60.0,
                        "last_price": 72.0,
                        "pnl": 900.0,
                    }
                ]
            },
        }
    )
    tickets = square_off_all(client)
    assert len(tickets) == 1
    assert client.posted[0]["body"]["transaction_type"] == "SELL"
    assert client.posted[0]["body"]["quantity"] == 75


def test_zerodha_adapter_blocks_orders_in_mock_mode() -> None:
    reset_cache()
    broker = ZerodhaExecutionBroker(
        _settings(
            execution_mode="MOCK",
            order_broker="zerodha",
            zerodha_api_key="k",
            zerodha_api_secret="s",
            zerodha_access_token="t",
        )
    )
    with pytest.raises(ExecutionAuthError):
        broker.place_order(_request())
