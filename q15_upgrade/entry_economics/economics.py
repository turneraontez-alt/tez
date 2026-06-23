"""Executable trade economics for a single contract (entry-econ-v1).

Given a side, a model/conservative probability, the executable ask, and a fill
estimate, this computes every quantity the entry decision needs — all in cents
per contract, all from prices that could realistically be filled.

Separation of concerns: this module answers "is the available entry favourable?"
It is deliberately independent of "which side is most likely to settle?" (the
upstream directional model) and "enter now or wait?" (``decision.py``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ..money import clamp_price_cents, round_cents, round_edge_cents
from .costs import CostBreakdown, FillEstimate


def _sigma_cents(prob: float) -> float:
    """Cent-stdev of the binary payoff at ``prob`` (max 50 at p=0.5)."""
    p = min(max(float(prob), 0.0), 1.0)
    return 100.0 * math.sqrt(p * (1.0 - p))


@dataclass
class TradeEconomics:
    side: str
    model_side_prob: float
    conservative_side_prob: float
    executable_ask_cents: float
    # fair values
    model_fair_value_cents: float
    conservative_fair_value_cents: float
    # cost / all-in
    costs: dict[str, float]
    all_in_entry_cost_cents: float            # ask + frictional costs
    # edges
    gross_edge_cents: float                   # model_fair - ask (before costs)
    net_edge_cents: float                     # conservative_fair - ask - costs (gate metric)
    model_net_edge_cents: float               # model_fair - ask - costs
    break_even_prob: float                    # (ask + costs) / 100
    # acceptable price band
    required_margin_cents: float
    maximum_entry_price_cents: float
    recommended_entry_low_cents: float
    recommended_entry_high_cents: float
    # value / risk
    ev_per_contract_cents: float              # conservative net edge (= net_edge_cents)
    ev_after_uncertainty_cents: float         # net_edge - k*sigma
    risk_reward_ratio: float | None           # (100-ask) / ask
    liquidity_capacity_contracts: float       # fillable at <= maximum_entry
    partial_fill_risk: bool
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "model_side_prob": self.model_side_prob,
            "conservative_side_prob": self.conservative_side_prob,
            "executable_ask_cents": self.executable_ask_cents,
            "model_fair_value_cents": self.model_fair_value_cents,
            "conservative_fair_value_cents": self.conservative_fair_value_cents,
            "costs": self.costs,
            "all_in_entry_cost_cents": self.all_in_entry_cost_cents,
            "gross_edge_cents": self.gross_edge_cents,
            "net_edge_cents": self.net_edge_cents,
            "model_net_edge_cents": self.model_net_edge_cents,
            "break_even_prob": self.break_even_prob,
            "required_margin_cents": self.required_margin_cents,
            "maximum_entry_price_cents": self.maximum_entry_price_cents,
            "recommended_entry_low_cents": self.recommended_entry_low_cents,
            "recommended_entry_high_cents": self.recommended_entry_high_cents,
            "ev_per_contract_cents": self.ev_per_contract_cents,
            "ev_after_uncertainty_cents": self.ev_after_uncertainty_cents,
            "risk_reward_ratio": self.risk_reward_ratio,
            "liquidity_capacity_contracts": self.liquidity_capacity_contracts,
            "partial_fill_risk": self.partial_fill_risk,
        }


def compute_economics(
    *,
    side: str,
    model_side_prob: float,
    conservative_side_prob: float,
    executable_ask_cents: float,
    cost_breakdown: CostBreakdown,
    fill: FillEstimate,
    required_margin_cents: float,
    recommended_band_cents: float,
    risk_aversion_k: float,
) -> TradeEconomics:
    """Compute the full executable economics for buying ``side`` at the ask.

    The executable ask already embeds the spread; ``cost_breakdown`` carries the
    *additional* frictional cents (fee, walk-the-book slippage, latency, stale
    surcharge). All edges are conservative-first so the gate cannot be fooled by
    an overconfident raw probability.
    """
    if side not in {"YES", "NO"}:
        raise ValueError("side must be YES or NO")
    p_model = min(max(float(model_side_prob), 0.001), 0.999)
    p_cons = min(max(float(conservative_side_prob), 0.001), 0.999)
    ask = float(executable_ask_cents)
    costs = cost_breakdown.total_cents

    model_fair = p_model * 100.0
    cons_fair = p_cons * 100.0
    all_in = ask + costs

    gross = model_fair - ask
    net = cons_fair - ask - costs                 # the conservative gate metric
    model_net = model_fair - ask - costs
    break_even = (ask + costs) / 100.0

    max_entry = clamp_price_cents(cons_fair - costs - max(0.0, required_margin_cents),
                                  context="entry_econ.max_entry")
    if max_entry is None:
        max_entry = 0.0
    rec_high = max(0.0, max_entry - 1.0)
    rec_low = max(0.0, rec_high - max(0.0, recommended_band_cents))

    ev = net                                       # conservative EV per contract
    ev_after = net - risk_aversion_k * _sigma_cents(p_cons)
    rr = ((100.0 - ask) / ask) if ask > 0 else None

    # Liquidity capacity = contracts fillable at or below the maximum acceptable
    # price (the FillEstimate already counted depth against that reference price).
    capacity = float(fill.depth_at_or_below or 0.0)

    notes: list[str] = []
    if fill.partial_fill:
        notes.append("partial_fill_risk")
    if not fill.book_known:
        notes.append("book_depth_unverified")

    return TradeEconomics(
        side=side,
        model_side_prob=round(p_model, 6),
        conservative_side_prob=round(p_cons, 6),
        executable_ask_cents=round_cents(ask) or 0.0,
        model_fair_value_cents=round_cents(model_fair) or 0.0,
        conservative_fair_value_cents=round_cents(cons_fair) or 0.0,
        costs=cost_breakdown.as_dict(),
        all_in_entry_cost_cents=round_cents(all_in) or 0.0,
        gross_edge_cents=round_edge_cents(gross) or 0.0,
        net_edge_cents=round_edge_cents(net) or 0.0,
        model_net_edge_cents=round_edge_cents(model_net) or 0.0,
        break_even_prob=round(break_even, 6),
        required_margin_cents=round_cents(required_margin_cents) or 0.0,
        maximum_entry_price_cents=round_cents(max_entry) or 0.0,
        recommended_entry_low_cents=round_cents(rec_low) or 0.0,
        recommended_entry_high_cents=round_cents(rec_high) or 0.0,
        ev_per_contract_cents=round_edge_cents(ev) or 0.0,
        ev_after_uncertainty_cents=round_edge_cents(ev_after) or 0.0,
        risk_reward_ratio=None if rr is None else round(rr, 4),
        liquidity_capacity_contracts=round(capacity, 4),
        partial_fill_risk=bool(fill.partial_fill),
        notes=notes,
    )
