"""Upstox option-contract and option-chain reads. No order placement."""

from __future__ import annotations

from modules.upstox.client import UpstoxClient

NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"
BANKNIFTY_INDEX_KEY = "NSE_INDEX|Nifty Bank"
INDIA_VIX_KEY = "NSE_INDEX|India VIX"


def fetch_nifty_contracts(client: UpstoxClient, underlying: str = NIFTY_INDEX_KEY) -> list[dict]:
    payload = client.get("/option/contract", params={"instrument_key": underlying})
    data = payload.get("data") or []
    return data if isinstance(data, list) else []


def fetch_option_chain(
    client: UpstoxClient,
    expiry: str,
    underlying: str = NIFTY_INDEX_KEY,
) -> list[dict]:
    payload = client.get(
        "/option/chain",
        params={"instrument_key": underlying, "expiry_date": expiry},
    )
    data = payload.get("data") or []
    return data if isinstance(data, list) else []
