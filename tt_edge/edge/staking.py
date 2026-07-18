"""Fractional-Kelly staking, capped and floored. Decimal throughout.

Quarter-Kelly (default) because launch-time model probabilities are
uncalibrated — full Kelly on a miscalibrated model is how bankrolls die.
Bankroll is a DB value (cents) owned by ``jobs/grade.py``; it is never a
constant here.

Precedence when the knobs conflict: the CAP always wins. The floor only
lifts a positive-Kelly stake that rounded below the minimum ticket, and never
above the cap (at a $15 bankroll a $1 floor would otherwise breach 5%).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from tt_edge.edge.vig import american_to_decimal_odds

_ZERO = Decimal(0)
_ONE = Decimal(1)


class StakingError(ValueError):
    """Invalid staking inputs (bad probability or bankroll)."""


def kelly_fraction(p: Decimal, american_price: int) -> Decimal:
    """Full-Kelly fraction f* = (b*p - q) / b for win probability ``p`` at
    ``american_price``, where b = decimal_odds - 1. Negative-EV inputs clamp
    to 0 (never a short recommendation)."""
    if not (_ZERO < p < _ONE):
        raise StakingError(f"win probability must be in (0, 1); got {p}")
    b = american_to_decimal_odds(american_price) - _ONE
    f = (b * p - (_ONE - p)) / b
    return f if f > _ZERO else _ZERO


@dataclass(frozen=True)
class StakePlan:
    """A sizing decision with its inputs preserved for the log/alert."""

    stake_cents: int
    kelly: Decimal            # full-Kelly fraction before scaling
    fraction: Decimal         # the fractional-Kelly multiplier used
    capped: bool              # True when the bankroll cap bound
    floored: bool             # True when the minimum ticket lifted the stake


def plan_stake(bankroll_cents: int, p: Decimal, american_price: int, *,
               fraction: Decimal, cap_pct: Decimal,
               floor_cents: int) -> StakePlan:
    """Size a recommendation. Returns stake 0 when Kelly is non-positive.

    stake = bankroll * fraction * kelly, rounded DOWN to whole cents
    (conservative), then capped at ``cap_pct`` of bankroll, then floored at
    ``floor_cents`` — floor never exceeding the cap.
    """
    if bankroll_cents <= 0:
        raise StakingError(f"bankroll_cents must be positive; got {bankroll_cents}")
    kelly = kelly_fraction(p, american_price)
    if kelly == _ZERO:
        return StakePlan(0, kelly, fraction, capped=False, floored=False)

    bankroll = Decimal(bankroll_cents)
    raw = (bankroll * fraction * kelly).to_integral_value(rounding=ROUND_FLOOR)
    cap = (bankroll * cap_pct).to_integral_value(rounding=ROUND_FLOOR)

    stake = raw
    capped = stake > cap
    if capped:
        stake = cap
    floored = _ZERO < stake < floor_cents
    if floored:
        stake = min(Decimal(floor_cents), cap)
        floored = stake > raw
        capped = capped or stake == cap
    return StakePlan(int(stake), kelly, fraction, capped=capped, floored=floored)
