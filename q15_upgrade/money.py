"""Canonical money/price helpers for the Kalshi 15-minute monitor.

A single, shared rounding and validation point for cent-denominated values so
the prediction ledger, the settlement P&L accounting, and the performance store
never disagree on precision. Kalshi binary contracts trade in integer cents in
``[0, 100]``; fees and modelled edges can be fractional, so the canonical price
precision is **two decimal places**, rounded half-to-even (banker's rounding)
to avoid the cumulative upward bias of ``round(x, 2)`` on ``.5`` ties.

Everything here is pure and deterministic (no clock, no I/O), uses ``Decimal``
internally for the money-sensitive rounding, and returns plain ``float`` for
storage/serialisation compatibility with the existing SQLite/JSON schema.
"""

from __future__ import annotations

import logging
import math
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any

logger = logging.getLogger("q15.money")

# Canonical price precision (decimal places) for cent-denominated values.
CENT_PLACES: int = 2
# Valid absolute price range for a Kalshi binary, in cents.
MIN_PRICE_CENTS: float = 0.0
MAX_PRICE_CENTS: float = 100.0

_QUANT = Decimal(1).scaleb(-CENT_PLACES)  # Decimal('0.01')


def _coerce(value: Any) -> float | None:
    """Best-effort float coercion that rejects NaN/inf and non-numerics."""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def round_cents(value: Any, places: int = CENT_PLACES) -> float | None:
    """Round a cent value to ``places`` decimals using banker's rounding.

    Returns ``None`` when the input is missing or not a finite number, so callers
    can distinguish "no value" from "0.0". This is the single canonical rounding
    point shared by the ledger and the performance store.
    """
    coerced = _coerce(value)
    if coerced is None:
        return None
    quant = Decimal(1).scaleb(-int(places))
    rounded = Decimal(repr(coerced)).quantize(quant, rounding=ROUND_HALF_EVEN)
    return float(rounded)


def is_valid_price_cents(value: Any) -> bool:
    """True iff ``value`` is a finite cent price within ``[0, 100]``."""
    coerced = _coerce(value)
    return coerced is not None and MIN_PRICE_CENTS <= coerced <= MAX_PRICE_CENTS


def clamp_price_cents(value: Any, *, context: str = "") -> float | None:
    """Round an *absolute* price and clamp it into the valid ``[0, 100]`` range.

    A price that lands outside the range after rounding indicates a precision or
    upstream-feed bug, so it is logged (without secrets) before being clamped —
    the clamp keeps a single bad value from rendering an impossible "105¢" entry
    level, while the log makes the anomaly visible instead of silent.
    """
    rounded = round_cents(value)
    if rounded is None:
        return None
    if rounded < MIN_PRICE_CENTS or rounded > MAX_PRICE_CENTS:
        logger.warning(
            "price cents %.4f out of range [%.0f, %.0f]%s; clamping",
            rounded, MIN_PRICE_CENTS, MAX_PRICE_CENTS,
            f" ({context})" if context else "",
        )
        return max(MIN_PRICE_CENTS, min(MAX_PRICE_CENTS, rounded))
    return rounded


def round_edge_cents(value: Any) -> float | None:
    """Round a *signed edge* (a delta between prices) to canonical precision.

    Unlike an absolute price, an edge is legitimately negative and is not bounded
    to ``[0, 100]`` — so this rounds without clamping the range.
    """
    return round_cents(value)


__all__ = [
    "CENT_PLACES",
    "MAX_PRICE_CENTS",
    "MIN_PRICE_CENTS",
    "clamp_price_cents",
    "is_valid_price_cents",
    "round_cents",
    "round_edge_cents",
]
