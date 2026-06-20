from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Iterable, Sequence, Tuple


D0 = Decimal("0")
D100 = Decimal("100")


def dec(value, default: Decimal | None = None) -> Decimal | None:
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def dollars_to_cents(value) -> float | None:
    d = dec(value)
    if d is None:
        return None
    return float(d * D100)


def cents_to_dollars(value) -> str | None:
    d = dec(value)
    if d is None:
        return None
    return format((d / D100).quantize(Decimal("0.0001")), "f")


def normalized_taker_side(record: dict) -> str | None:
    side = (
        record.get("taker_outcome_side")
        or record.get("taker_side")
    )
    if side in ("yes", "no"):
        return side
    book_side = record.get("taker_book_side")
    if book_side == "bid":
        return "yes"
    if book_side == "ask":
        return "no"
    return None


def estimated_fee_cents(price_cents: float, contracts: float = 1.0, rate: float = 0.07) -> float:
    """Configurable estimate of the standard quadratic taker fee.

    This is kept as an estimate because Kalshi may apply market-specific fee
    schedules and balance-alignment rounding.  Calculations retain sub-cent
    precision instead of rounding the contract price to an integer cent.
    """
    p = max(D0, min(Decimal("1"), dec(price_cents, D0) / D100))
    c = max(D0, dec(contracts, D0))
    r = max(D0, dec(rate, D0))
    cents = r * c * p * (Decimal("1") - p) * D100
    return float(cents.quantize(Decimal("0.01"), rounding=ROUND_CEILING))


def vwap_for_quantity(
    levels: Sequence[Sequence[float]] | None,
    quantity: float,
) -> Tuple[float | None, float]:
    """Return (VWAP cents, filled quantity) for ascending ask levels."""
    if not levels or quantity <= 0:
        return None, 0.0
    remaining = Decimal(str(quantity))
    cost = D0
    filled = D0
    for level in levels:
        if len(level) < 2:
            continue
        price = dec(level[0])
        qty = dec(level[1])
        if price is None or qty is None or qty <= 0:
            continue
        take = min(remaining, qty)
        cost += price * take
        filled += take
        remaining -= take
        if remaining <= 0:
            break
    if filled <= 0:
        return None, 0.0
    return float(cost / filled), float(filled)


def floor_to_tick(value_cents: float, tick_cents: float) -> float:
    v = dec(value_cents, D0)
    tick = max(Decimal("0.0001"), dec(tick_cents, Decimal("0.1")))
    units = (v / tick).to_integral_value(rounding=ROUND_FLOOR)
    return float(units * tick)


def max_entry_price_cents(
    fair_value_cents: float,
    required_edge_cents: float,
    contracts: float = 1.0,
    fee_rate: float = 0.07,
    tick_cents: float = 0.1,
) -> float | None:
    """Highest price that still preserves required edge after estimated fees."""
    fair = float(fair_value_cents)
    required = max(0.0, float(required_edge_cents))
    if not math.isfinite(fair) or fair <= 0:
        return None

    lo, hi = 0.0, min(99.9999, fair)
    for _ in range(60):
        mid = (lo + hi) / 2.0
        net_edge = fair - mid - estimated_fee_cents(mid, contracts, fee_rate) / max(contracts, 1e-9)
        if net_edge >= required:
            lo = mid
        else:
            hi = mid
    return floor_to_tick(lo, tick_cents)
