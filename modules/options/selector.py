"""Pick a Nifty weekly option and the number of lots. No I/O, no orders.

Long premium only: a bullish index break buys the ATM call, a bearish break
buys the ATM put. Short options are not sized here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from math import floor

ENTRY_END = time(14, 30)
FLATTEN_AT = time(15, 0)


@dataclass(frozen=True)
class OptionQuote:
    instrument_key: str
    trading_symbol: str
    expiry: str
    strike: float
    option_type: str
    lot_size: int
    ltp: float


@dataclass(frozen=True)
class SizedOrder:
    contract: OptionQuote
    lots: int
    quantity: int
    premium: float
    stop_premium: float
    target_premium: float
    risk_inr: float
    cash_required: float


def nearest_weekly_expiry(rows: list[dict], today: date) -> str | None:
    """Soonest expiry on or after `today`. Weekly expiries win when any exist."""

    future = []
    for row in rows:
        raw = str(row.get("expiry") or "")
        try:
            stamp = date.fromisoformat(raw[:10])
        except ValueError:
            continue
        if stamp >= today:
            future.append((stamp, bool(row.get("weekly"))))
    if not future:
        return None
    weekly = [item for item in future if item[1]]
    pool = weekly or future
    return min(pool)[0].isoformat()


def lot_size_for(rows: list[dict], expiry: str) -> int:
    for row in rows:
        if str(row.get("expiry") or "")[:10] != expiry:
            continue
        try:
            size = int(float(row.get("lot_size") or 0))
        except (TypeError, ValueError):
            continue
        if size > 0:
            return size
    return 65


def contracts_for_expiry(rows: list[dict], expiry: str) -> list[OptionQuote]:
    out: list[OptionQuote] = []
    for row in rows:
        if str(row.get("expiry") or "")[:10] != expiry:
            continue
        side = str(row.get("instrument_type") or "").upper()
        if side not in {"CE", "PE"}:
            continue
        try:
            quote = OptionQuote(
                instrument_key=str(row.get("instrument_key") or ""),
                trading_symbol=str(row.get("trading_symbol") or ""),
                expiry=expiry,
                strike=float(row.get("strike_price") or 0.0),
                option_type=side,
                lot_size=int(float(row.get("lot_size") or 0)),
                ltp=float(row.get("ltp") or 0.0),
            )
        except (TypeError, ValueError):
            continue
        if quote.instrument_key and quote.strike > 0 and quote.lot_size > 0:
            out.append(quote)
    return out


def parse_chain_rows(chain: list[dict], expiry: str, lot_size: int = 65) -> list[OptionQuote]:
    """Flatten an Upstox option-chain payload into call and put quotes."""

    out: list[OptionQuote] = []
    for row in chain:
        strike = float(row.get("strike_price") or 0.0)
        if strike <= 0:
            continue
        for key, side in (("call_options", "CE"), ("put_options", "PE")):
            leg = row.get(key) or {}
            if not isinstance(leg, dict):
                continue
            market = leg.get("market_data") or {}
            try:
                ltp = float(market.get("ltp") or leg.get("ltp") or 0.0)
                leg_lot = int(float(leg.get("lot_size") or row.get("lot_size") or lot_size or 0))
            except (TypeError, ValueError):
                continue
            instrument_key = str(leg.get("instrument_key") or "")
            if not instrument_key or ltp <= 0:
                continue
            if leg_lot < 1:
                leg_lot = lot_size if lot_size > 0 else 65
            symbol = str(leg.get("trading_symbol") or f"NIFTY {strike:.0f} {side} {expiry}")
            out.append(
                OptionQuote(
                    instrument_key=instrument_key,
                    trading_symbol=symbol,
                    expiry=expiry,
                    strike=strike,
                    option_type=side,
                    lot_size=leg_lot,
                    ltp=ltp,
                )
            )
    return out


def choose_atm(quotes: list[OptionQuote], spot: float, index_side: str) -> OptionQuote | None:
    """ATM call when the index breaks up, ATM put when it breaks down."""

    wanted = "CE" if index_side == "BUY" else "PE" if index_side == "SELL" else ""
    pool = [row for row in quotes if row.option_type == wanted and row.ltp > 0]
    if not pool or spot <= 0:
        return None
    return min(pool, key=lambda row: (abs(row.strike - spot), row.strike))


def size_lots(
    capital: float,
    risk_pct: float,
    premium: float,
    lot_size: int,
    stop_frac: float,
    daily_loss_cap: float = 3000.0,
) -> tuple[int, float, float]:
    """Return (lots, cash required, planned loss at the premium stop).

    A Nifty lot is bought only when both the cash premium and the planned
    stop loss fit inside the account. Otherwise the machine stays flat.
    """

    if capital <= 0 or risk_pct <= 0 or premium <= 0 or lot_size < 1 or stop_frac <= 0:
        return 0, 0.0, 0.0
    per_lot_cash = premium * lot_size
    per_lot_risk = per_lot_cash * stop_frac
    budget = min(capital * (risk_pct / 100.0), daily_loss_cap)
    by_risk = floor(budget / per_lot_risk) if per_lot_risk > 0 else 0
    by_cash = floor(capital / per_lot_cash) if per_lot_cash > 0 else 0
    lots = max(0, min(by_risk, by_cash))
    return lots, round(per_lot_cash * lots, 2), round(per_lot_risk * lots, 2)


def order_for_lots(
    contract: OptionQuote,
    lots: int,
    capital: float,
    stop_frac: float,
    target_frac: float,
    daily_loss_cap: float = 3000.0,
) -> tuple[SizedOrder | None, str]:
    """Price a lot count the user picked.

    The order is refused when the premium does not fit the capital, or when
    the planned stop would lose more than the daily cap.
    """

    chosen = int(lots)
    if chosen < 1 or contract.ltp <= 0 or contract.lot_size < 1 or stop_frac <= 0:
        return None, "Choose at least 1 lot."
    cash = contract.ltp * contract.lot_size * chosen
    risk = cash * stop_frac
    if capital <= 0 or cash > capital:
        return None, (
            f"{chosen} lot(s) need about ₹{cash:,.0f}. "
            f"That is more than the ₹{capital:,.0f} capital."
        )
    if risk > daily_loss_cap:
        per_lot = contract.ltp * contract.lot_size * stop_frac
        room = floor(daily_loss_cap / per_lot) if per_lot > 0 else 0
        fewer = f" Use {room} lot(s) or fewer." if room else " Even 1 lot is over that limit at this price."
        return None, (
            f"{chosen} lot(s) can lose about ₹{risk:,.0f} if the stop is hit. "
            f"The limit is ₹{daily_loss_cap:,.0f}.{fewer}"
        )
    return (
        SizedOrder(
            contract=contract,
            lots=chosen,
            quantity=chosen * contract.lot_size,
            premium=round(contract.ltp, 2),
            stop_premium=round(contract.ltp * (1.0 - stop_frac), 2),
            target_premium=round(contract.ltp * (1.0 + target_frac), 2),
            risk_inr=round(risk, 2),
            cash_required=round(cash, 2),
        ),
        "",
    )


def build_order(
    contract: OptionQuote,
    capital: float,
    risk_pct: float,
    stop_frac: float,
    target_frac: float,
    daily_loss_cap: float = 3000.0,
) -> SizedOrder | None:
    lots, cash, risk = size_lots(
        capital, risk_pct, contract.ltp, contract.lot_size, stop_frac, daily_loss_cap
    )
    if lots < 1:
        return None
    return SizedOrder(
        contract=contract,
        lots=lots,
        quantity=lots * contract.lot_size,
        premium=round(contract.ltp, 2),
        stop_premium=round(contract.ltp * (1.0 - stop_frac), 2),
        target_premium=round(contract.ltp * (1.0 + target_frac), 2),
        risk_inr=risk,
        cash_required=cash,
    )


def entry_allowed(now: datetime, index_side: str) -> tuple[bool, str]:
    if now.weekday() >= 5:
        return False, "Cash and derivatives market is closed today."
    clock = _clock(now)
    if clock < time(9, 30):
        return False, "Opening range is still building. No option until 09:30 IST."
    if clock >= ENTRY_END:
        return False, "Entry window is closed (14:30 IST). Open options flatten at 15:00."
    if index_side not in {"BUY", "SELL"}:
        return False, "Nifty is still inside the opening range. No option yet."
    return True, "ok"


def manage_exit(entry_premium: float, ltp: float, stop_premium: float, target_premium: float, now: datetime) -> str | None:
    """Why a long option should be sold, or None to hold."""

    if _clock(now) >= FLATTEN_AT:
        return "SQUARE_OFF_1500"
    if ltp > 0 and stop_premium > 0 and ltp <= stop_premium:
        return "PREMIUM_STOP"
    if ltp > 0 and target_premium > 0 and ltp >= target_premium:
        return "PREMIUM_TARGET"
    return None


def _clock(now: datetime) -> time:
    stamp = now
    if stamp.tzinfo is not None:
        from config.settings import IST

        stamp = stamp.astimezone(IST)
    return stamp.time().replace(tzinfo=None)
