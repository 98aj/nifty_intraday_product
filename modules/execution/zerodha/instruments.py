"""Resolve a Zerodha NFO trading symbol from a broker-neutral contract.

Kite has no option-chain endpoint and places orders by ``tradingsymbol``, not by
the Upstox instrument key the rest of the app carries. So for an order we look up
the matching NFO contract in Kite's instruments dump (a CSV at
``/instruments/NFO``) by underlying name, expiry, strike and option type.

The dump changes at most once a day, so it is cached in-memory per calendar day.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import date, datetime

from config.settings import IST
from modules.execution.errors import ExecutionAPIError
from modules.execution.zerodha.client import ZerodhaClient

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class NfoInstrument:
    tradingsymbol: str
    instrument_token: str
    exchange: str
    name: str
    expiry: str
    strike: float
    option_type: str
    lot_size: int


def parse_nfo_dump(csv_text: str) -> list[NfoInstrument]:
    """Parse the Kite NFO instruments CSV into option rows (CE/PE only)."""

    rows: list[NfoInstrument] = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for raw in reader:
        option_type = (raw.get("instrument_type") or "").strip().upper()
        if option_type not in {"CE", "PE"}:
            continue
        try:
            strike = float(raw.get("strike") or 0.0)
            lot_size = int(float(raw.get("lot_size") or 0))
        except (TypeError, ValueError):
            continue
        symbol = (raw.get("tradingsymbol") or "").strip()
        if not symbol or strike <= 0:
            continue
        rows.append(
            NfoInstrument(
                tradingsymbol=symbol,
                instrument_token=str(raw.get("instrument_token") or "").strip(),
                exchange=(raw.get("exchange") or "NFO").strip() or "NFO",
                name=(raw.get("name") or "").strip().upper(),
                expiry=(raw.get("expiry") or "").strip()[:10],
                strike=strike,
                option_type=option_type,
                lot_size=lot_size,
            )
        )
    return rows


class InstrumentResolver:
    """Caches the NFO dump for a day and resolves contracts to Kite symbols."""

    def __init__(self, client: ZerodhaClient) -> None:
        self._client = client
        self._dump: list[NfoInstrument] = []
        self._loaded_on: date | None = None

    def _ensure_loaded(self) -> None:
        today = datetime.now(IST).date()
        if self._loaded_on == today and self._dump:
            return
        text = self._client.get_text("/instruments/NFO")
        parsed = parse_nfo_dump(text)
        if not parsed:
            raise ExecutionAPIError("Kite NFO instruments dump was empty or unparseable")
        self._dump = parsed
        self._loaded_on = today
        LOGGER.info("Loaded %d NFO option contracts from Kite.", len(parsed))

    def resolve(
        self,
        underlying: str,
        expiry: str,
        strike: float,
        option_type: str,
    ) -> NfoInstrument:
        """Find the one NFO contract matching the broker-neutral identity."""

        self._ensure_loaded()
        name = (underlying or "").strip().upper()
        want_type = (option_type or "").strip().upper()
        want_expiry = (expiry or "").strip()[:10]
        matches = [
            row
            for row in self._dump
            if row.name == name
            and row.option_type == want_type
            and row.expiry == want_expiry
            and abs(row.strike - strike) < 0.5
        ]
        if not matches:
            raise ExecutionAPIError(
                f"No Kite NFO contract for {name} {want_expiry} {strike:g} {want_type}"
            )
        return matches[0]
