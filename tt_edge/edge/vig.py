"""American-odds conversions and proportional vig removal.

All arithmetic is :class:`~decimal.Decimal` — prices are money. American
prices are integers with ``abs(price) >= 100`` (a book never posts -50).

The "cents scale" used for line-movement is the linear scale bettors mean by
"the line moved 15 cents": +100 is 0, longer prices are positive, shorter
prices negative, and the +100/-100 boundary is adjacent (there are no prices
between -100 and +100 exclusive). -120 -> -20, +115 -> +15; a move from -120
to -105 is +15 (the side lengthened by 15 cents).
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

_HUNDRED = Decimal(100)
_ONE = Decimal(1)


class OddsError(ValueError):
    """An American price outside the representable range."""


def validate_american(price: int) -> int:
    """American prices are integers with abs >= 100."""
    if not isinstance(price, int) or isinstance(price, bool):
        raise OddsError(f"American price must be an int; got {price!r}")
    if abs(price) < 100:
        raise OddsError(f"American price must have abs(price) >= 100; got {price}")
    return price


def american_to_implied(price: int) -> Decimal:
    """Implied (vig-inclusive) win probability of an American price."""
    validate_american(price)
    p = Decimal(price)
    if p > 0:
        return _HUNDRED / (p + _HUNDRED)
    return -p / (-p + _HUNDRED)


def american_to_decimal_odds(price: int) -> Decimal:
    """Decimal (European) odds: total return per unit staked."""
    validate_american(price)
    p = Decimal(price)
    if p > 0:
        return _ONE + p / _HUNDRED
    return _ONE + _HUNDRED / -p


def devig_proportional(implied_a: Decimal, implied_b: Decimal) -> tuple[Decimal, Decimal]:
    """Proportionally strip the overround from a two-way market.

    fair_a = implied_a / (implied_a + implied_b). Works whether the book sum
    is above 1 (normal) or below (suspect/arb — still normalized, caller may
    flag)."""
    if implied_a <= 0 or implied_b <= 0:
        raise OddsError(
            f"implied probabilities must be positive; got {implied_a}, {implied_b}")
    total = implied_a + implied_b
    return implied_a / total, implied_b / total


def probability_to_american(p: Decimal) -> int:
    """Fair probability -> the American price with that implied probability
    (for display: 'fair -138'). Favorites (p >= 0.5) get negative prices."""
    if not (Decimal(0) < p < Decimal(1)):
        raise OddsError(f"probability must be in (0, 1); got {p}")
    if p >= Decimal("0.5"):
        raw = -(p / (_ONE - p)) * _HUNDRED
    else:
        raw = ((_ONE - p) / p) * _HUNDRED
    price = int(raw.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    # Rounding can land inside the (-100, +100) gap (e.g. p just under 0.5);
    # snap to the nearest legal price.
    if -100 < price < 100:
        price = -100 if p >= Decimal("0.5") else 100
    return price


def american_cents_scale(price: int) -> int:
    """Position of a price on the linear cents scale (see module docstring)."""
    validate_american(price)
    if price > 0:
        return price - 100
    return -(-price - 100)


def movement_cents(old_price: int, new_price: int) -> int:
    """Signed line movement in cents. Positive = the price LENGTHENED (the
    market moved AGAINST this side); negative = shortened (moved toward it).
    Crossing the +100/-100 boundary counts continuously: -105 -> +110 is +15.
    """
    return american_cents_scale(new_price) - american_cents_scale(old_price)
