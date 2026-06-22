"""Executable cost model for entry economics (entry-econ-v1).

Every cost is denominated in cents per contract and computed from prices that
could realistically be filled — never a midpoint, a last trade, or a theoretical
price. Money-sensitive arithmetic uses ``Decimal`` (banker's rounding) via the
shared ``q15_upgrade.money`` helpers so displayed/stored cents never carry float
noise.

The headline correctness fix vs the legacy gate: the Kalshi trading fee is
rounded UP to the cent (``ceil``), matching the exchange, instead of charging the
un-rounded ``0.07·p·(1-p)`` which silently under-charges every entry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..money import round_cents


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def kalshi_fee_cents(price_cents: float, contracts: float = 1.0,
                     rate: float = 0.07, ceil: bool = True) -> float:
    """Kalshi general trading fee, in cents, for ``contracts`` at ``price_cents``.

    Kalshi charges ``round_up(rate · C · P · (1-P))`` dollars where ``P`` is the
    price in dollars; the per-contract fee returned here is that order fee divided
    by the contract count. With ``ceil`` the order fee is rounded UP to the cent
    exactly as the exchange does, so a 0.5¢-priced contract costs 2¢ in fee, not
    the 1.75¢ the un-rounded formula implies.
    """
    p = _num(price_cents)
    if p is None:
        return 0.0
    p = max(0.0, min(100.0, p)) / 100.0
    c = max(1.0, float(contracts))
    fee_cents_order = max(0.0, rate) * c * p * (1.0 - p) * 100.0
    if ceil:
        fee_cents_order = math.ceil(fee_cents_order - 1e-9)
    return fee_cents_order / c


@dataclass
class FillEstimate:
    """Result of walking the ask side of the book for ``sim_contracts``."""
    best_ask_cents: float | None
    vwap_cents: float | None          # volume-weighted fill price for what filled
    filled_contracts: float
    requested_contracts: float
    slippage_cents: float             # vwap - best_ask (>= 0); fallback when book absent
    partial_fill: bool                # requested more than the book could fill
    depth_at_or_below: float          # contracts fillable at <= a reference price
    book_known: bool                  # were real price levels available?


def _parse_levels(levels: Any) -> list[tuple[float, float]]:
    """Coerce assorted ask-level encodings into sorted [(price_cents, qty)]."""
    out: list[tuple[float, float]] = []
    if not isinstance(levels, (list, tuple)):
        return out
    for level in levels:
        price = qty = None
        if isinstance(level, Mapping):
            price = _num(level.get("price", level.get("price_cents")))
            qty = _num(level.get("quantity", level.get("qty", level.get("count", level.get("size")))))
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            price = _num(level[0])
            qty = _num(level[1])
        if price is None or qty is None or qty <= 0:
            continue
        if 0.0 <= price <= 1.0:           # dollars -> cents
            price *= 100.0
        if 0.0 <= price <= 100.0:
            out.append((price, qty))
    out.sort(key=lambda pq: pq[0])
    return out


def estimate_fill(
    *,
    best_ask_cents: float | None,
    ask_levels: Any = None,
    spread_cents: float | None = None,
    depth_contracts: float | None = None,
    sim_contracts: float = 5.0,
    slippage_fallback_cents: float = 0.75,
    slippage_spread_fraction: float = 0.50,
    max_acceptable_cents: float | None = None,
) -> FillEstimate:
    """Estimate the all-in fill by walking the book; degrade gracefully.

    Preference order for slippage:
      1. real ask levels  -> VWAP over the requested size (true partial-fill aware)
      2. top-of-book only -> a fraction of the spread
      3. nothing          -> a fixed conservative fallback
    A missing book reduces confidence and capacity but never hard-blocks — the
    caller treats absent depth as lower capacity, not an automatic SKIP.
    """
    best = _num(best_ask_cents)
    levels = _parse_levels(ask_levels)
    requested = max(1.0, float(sim_contracts))

    if best is None and levels:
        best = levels[0][0]

    if levels:
        remaining = requested
        cost = 0.0
        filled = 0.0
        for price, qty in levels:
            take = min(remaining, qty)
            cost += take * price
            filled += take
            remaining -= take
            if remaining <= 1e-9:
                break
        vwap = (cost / filled) if filled > 0 else best
        slip = max(0.0, (vwap - best)) if (vwap is not None and best is not None) else 0.0
        ref = max_acceptable_cents if max_acceptable_cents is not None else (best if best is not None else 100.0)
        depth_le = sum(qty for price, qty in levels if price <= ref + 1e-9)
        return FillEstimate(
            best_ask_cents=round_cents(best),
            vwap_cents=round_cents(vwap),
            filled_contracts=round(filled, 4),
            requested_contracts=requested,
            slippage_cents=round_cents(slip) or 0.0,
            partial_fill=filled + 1e-9 < requested,
            depth_at_or_below=round(depth_le, 4),
            book_known=True,
        )

    # No price levels: fall back to spread-fraction / fixed slippage.
    sp = _num(spread_cents)
    if sp is not None and sp > 0:
        slip = slippage_spread_fraction * sp
    else:
        slip = slippage_fallback_cents
    known_depth = _num(depth_contracts)
    # With only top-of-book depth, capacity is whatever the feed reports (0 if
    # unknown — the caller distinguishes "unknown" from "thin").
    return FillEstimate(
        best_ask_cents=round_cents(best),
        vwap_cents=round_cents((best + slip) if best is not None else None),
        filled_contracts=(min(requested, known_depth) if known_depth is not None else 0.0),
        requested_contracts=requested,
        slippage_cents=round_cents(slip) or 0.0,
        partial_fill=bool(known_depth is not None and known_depth + 1e-9 < requested),
        depth_at_or_below=(known_depth if known_depth is not None else 0.0),
        book_known=False,
    )


@dataclass
class CostBreakdown:
    fee_cents: float
    slippage_cents: float
    latency_cents: float
    stale_cents: float

    @property
    def total_cents(self) -> float:
        return round_cents(self.fee_cents + self.slippage_cents
                           + self.latency_cents + self.stale_cents) or 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "fee_cents": self.fee_cents,
            "slippage_cents": self.slippage_cents,
            "latency_cents": self.latency_cents,
            "stale_cents": self.stale_cents,
            "total_cents": self.total_cents,
        }


def all_in_costs(
    *,
    ask_cents: float,
    fill: FillEstimate,
    fee_rate: float,
    fee_ceil: bool,
    latency_cost_cents: float,
    quote_age_seconds: float | None,
    stale_penalty_cents_per_s: float,
    stale_penalty_cap_cents: float,
    stale_quote_age_seconds: float,
) -> CostBreakdown:
    """Sum the executable costs the spread does NOT already contain.

    The executable ask already embeds the spread, so spread is not charged again;
    slippage here is the *extra* cost of walking past top-of-book (partial fill),
    plus latency and a stale-quote surcharge.
    """
    fee = kalshi_fee_cents(ask_cents, contracts=1.0, rate=fee_rate, ceil=fee_ceil)
    slip = max(0.0, float(fill.slippage_cents or 0.0))
    stale = 0.0
    if quote_age_seconds is not None and quote_age_seconds > stale_quote_age_seconds:
        over = quote_age_seconds - stale_quote_age_seconds
        stale = min(stale_penalty_cap_cents, stale_penalty_cents_per_s * over)
    return CostBreakdown(
        fee_cents=round_cents(fee) or 0.0,
        slippage_cents=round_cents(slip) or 0.0,
        latency_cents=round_cents(max(0.0, latency_cost_cents)) or 0.0,
        stale_cents=round_cents(stale) or 0.0,
    )
