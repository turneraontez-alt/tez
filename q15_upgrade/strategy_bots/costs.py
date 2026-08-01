"""Versioned execution economics for RTI paper simulations.

The Q15 series currently use Kalshi's general quadratic taker fee with a
multiplier of one.  The published fee is rounded so that fee plus position
cost lands on a centicent (``$0.0001``).  Paper slippage represents an adverse
fill price, so the fee must be evaluated at that simulated fill rather than at
the better, pre-slippage quote.

This module is deliberately independent of the strategy rules and ledger so
offline freeze tooling and the live scorer share exactly one implementation.
"""
from __future__ import annotations

import math
from decimal import Decimal, ROUND_CEILING
from typing import Any


KALSHI_GENERAL_TAKER_FEE_RATE = Decimal("0.07")
KALSHI_Q15_FEE_MULTIPLIER = Decimal("1")
KALSHI_Q15_FEE_TYPE = "quadratic"
KALSHI_Q15_FEE_SCHEDULE_VERSION = "kalshi-fee-schedule-20260707"
RTI_EXECUTION_COST_MODEL_VERSION = (
    "rti-quote-plus-slippage-fee-at-fill-20260721-v2"
)


def _finite(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def kalshi_order_fee_cents(
    entry_price_cents: float | int | None,
    contracts: int,
    *,
    multiplier: float | int = 1,
) -> float | None:
    """Return the general quadratic taker fee for one equivalent order.

    The result is the total fee for the whole order, denominated in cents.  It
    follows the July 7, 2026 Kalshi schedule: ``0.07*C*P*(1-P)*M``, rounded so
    fee plus position cost is on a ``$0.0001`` boundary.  Q15's live series
    metadata reports ``fee_type=quadratic`` and ``fee_multiplier=1``.
    """
    price_num = _finite(entry_price_cents)
    multiplier_num = _finite(multiplier)
    if price_num is None or multiplier_num is None or multiplier_num < 0.0:
        return None
    count = max(1, int(contracts))
    price = max(Decimal("0"), min(Decimal("100"), Decimal(str(price_num))))
    if price <= 0 or price >= 100 or multiplier_num == 0.0:
        return 0.0
    probability = price / Decimal("100")
    position_cost = Decimal(count) * probability
    raw_fee = (
        KALSHI_GENERAL_TAKER_FEE_RATE
        * Decimal(str(multiplier_num))
        * Decimal(count)
        * probability
        * (Decimal("1") - probability)
    )
    rounded_total = (position_cost + raw_fee).quantize(
        Decimal("0.0001"),
        rounding=ROUND_CEILING,
    )
    return float((rounded_total - position_cost) * Decimal("100"))


def rti_simulated_execution(
    quoted_ask_cents: float | int | None,
    contracts: int,
    slippage_cents_per_contract: float | int | None,
) -> dict[str, float | int | str] | None:
    """Build the frozen RTI paper fill and its official taker fee.

    Slippage is an adverse entry-price movement, not a detached bookkeeping
    penalty.  Applying it to the fill before calculating the fee keeps EV,
    break-even, grading, drawdown, and offline freeze reports consistent.
    """
    quote_num = _finite(quoted_ask_cents)
    slippage_num = _finite(slippage_cents_per_contract)
    if quote_num is None:
        return None
    count = max(1, int(contracts))
    quote = max(0.0, min(100.0, quote_num))
    requested_slippage = max(0.0, float(slippage_num or 0.0))
    fill = min(100.0, quote + requested_slippage)
    realized_slippage = fill - quote
    fee_order = kalshi_order_fee_cents(
        fill,
        count,
        multiplier=float(KALSHI_Q15_FEE_MULTIPLIER),
    )
    if fee_order is None:
        return None
    fee_per_contract = fee_order / count
    return {
        "execution_cost_model_version": RTI_EXECUTION_COST_MODEL_VERSION,
        "fee_schedule_version": KALSHI_Q15_FEE_SCHEDULE_VERSION,
        "fee_type": KALSHI_Q15_FEE_TYPE,
        "fee_multiplier": float(KALSHI_Q15_FEE_MULTIPLIER),
        "contracts": count,
        "quoted_ask_cents": quote,
        "requested_slippage_cents_per_contract": requested_slippage,
        "realized_slippage_cents_per_contract": realized_slippage,
        "simulated_fill_cents": fill,
        "fee_cents_per_order": fee_order,
        "fee_cents_per_contract": fee_per_contract,
        "fee_slippage_breakeven_rate": (fill + fee_per_contract) / 100.0,
    }


def rti_simulated_net_pnl_cents(
    quoted_ask_cents: float | int | None,
    correct: bool,
    contracts: int,
    slippage_cents_per_contract: float | int | None,
) -> float | None:
    """Return fee/slippage-adjusted paper P/L in cents per contract."""
    execution = rti_simulated_execution(
        quoted_ask_cents,
        contracts,
        slippage_cents_per_contract,
    )
    if execution is None:
        return None
    fill = float(execution["simulated_fill_cents"])
    fee = float(execution["fee_cents_per_contract"])
    gross = 100.0 - fill if bool(correct) else -fill
    return gross - fee
