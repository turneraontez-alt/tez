"""Trade-decision logic for the challenger (hypothetical only — never executes).

Computes gross/net edge against the EXECUTABLE quote (ask for the side being
bought, never the midpoint), applies a no-trade zone, and enforces risk gates.
Mirrors the production cost assumptions so the comparison is apples-to-apples.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .features import FeatureVector
from .mathx import clamp


@dataclass
class CostBreakdown:
    fee_cents: float
    spread_cost_cents: float
    slippage_cents: float
    impact_cents: float
    uncertainty_margin_cents: float

    @property
    def total_cents(self) -> float:
        return (
            self.fee_cents + self.spread_cost_cents + self.slippage_cents
            + self.impact_cents + self.uncertainty_margin_cents
        )


@dataclass
class Decision:
    action: str                      # "BUY_YES" | "BUY_NO" | "NO_TRADE"
    side: str | None                 # "YES" | "NO" | None
    market_yes_prob: float | None
    model_side_prob: float | None
    executable_ask_cents: float | None
    gross_edge_cents: float | None
    net_edge_cents: float | None
    costs: CostBreakdown | None
    warnings: list[str] = field(default_factory=list)
    hypothetical_size_fraction: float = 0.0


def _fee_cents(cfg, ask_cents: float) -> float:
    # Mirrors production: fee on the notional at the entry ask.
    return cfg.fee_rate * ask_cents * (100.0 - ask_cents) / 100.0


def _costs_for_side(cfg, ask_cents: float, spread_cents: float | None, uncertainty: float) -> CostBreakdown:
    slip = (
        cfg.slippage_spread_fraction * spread_cents
        if spread_cents is not None and spread_cents > 0
        else cfg.slippage_fallback_cents
    )
    margin = cfg.uncertainty_margin_cents * (1.0 + clamp(uncertainty, 0.0, 1.0))
    return CostBreakdown(
        fee_cents=_fee_cents(cfg, ask_cents),
        spread_cost_cents=0.0,  # spread is already paid via executing at the ask
        slippage_cents=slip,
        impact_cents=cfg.market_impact_cents,
        uncertainty_margin_cents=margin,
    )


def _risk_warnings(cfg, fv: FeatureVector, uncertainty: float) -> list[str]:
    w: list[str] = []
    if fv.market_yes_prob is None:
        w.append("missing_market_quote")
    if fv.spread_cents is not None and fv.spread_cents > cfg.max_spread_cents:
        w.append("spread_too_wide")
    if fv.depth_contracts is not None and fv.depth_contracts < cfg.min_depth_contracts:
        w.append("insufficient_depth")
    if fv.seconds_remaining is not None and fv.seconds_remaining < cfg.min_seconds_remaining:
        w.append("too_little_time")
    if fv.data_quality is not None and fv.data_quality < cfg.min_data_quality:
        w.append("low_data_quality")
    if uncertainty > cfg.max_uncertainty:
        w.append("excessive_uncertainty")
    if fv.coverage_fraction < 0.5:
        w.append("thin_feature_coverage")
    return w


def evaluate(cfg, p_yes: float, fv: FeatureVector, uncertainty: float) -> Decision:
    p_yes = clamp(float(p_yes), 0.01, 0.99)
    p_no = 1.0 - p_yes
    warnings = _risk_warnings(cfg, fv, uncertainty)
    blocking = {
        "missing_market_quote", "spread_too_wide", "insufficient_depth",
        "too_little_time", "low_data_quality", "excessive_uncertainty",
    }
    hard_block = bool(blocking & set(warnings))

    candidates = []
    if fv.yes_ask_cents is not None:
        c = _costs_for_side(cfg, fv.yes_ask_cents, fv.spread_cents, uncertainty)
        gross = p_yes * 100.0 - fv.yes_ask_cents
        candidates.append(("YES", "BUY_YES", p_yes, fv.yes_ask_cents, gross, gross - c.total_cents, c))
    if fv.no_ask_cents is not None:
        c = _costs_for_side(cfg, fv.no_ask_cents, fv.spread_cents, uncertainty)
        gross = p_no * 100.0 - fv.no_ask_cents
        candidates.append(("NO", "BUY_NO", p_no, fv.no_ask_cents, gross, gross - c.total_cents, c))

    if not candidates:
        return Decision(
            action="NO_TRADE", side=None, market_yes_prob=fv.market_yes_prob,
            model_side_prob=None, executable_ask_cents=None, gross_edge_cents=None,
            net_edge_cents=None, costs=None,
            warnings=warnings + ["no_executable_quote"],
        )

    best = max(candidates, key=lambda t: t[5])  # highest net edge
    side, action, side_prob, ask, gross, net, costs = best

    tradeable = (
        not hard_block
        and side_prob >= cfg.min_probability
        and net >= cfg.min_net_edge_cents
    )
    if not tradeable:
        action = "NO_TRADE"
        size = 0.0
    else:
        # Conservative fractional sizing scaled by net edge; never full Kelly.
        size = clamp(cfg.max_risk_fraction_per_trade * (net / max(1.0, cfg.min_net_edge_cents)),
                     0.0, cfg.max_risk_fraction_per_trade)

    return Decision(
        action=action,
        side=side if action != "NO_TRADE" else None,
        market_yes_prob=fv.market_yes_prob,
        model_side_prob=side_prob,
        executable_ask_cents=ask,
        gross_edge_cents=gross,
        net_edge_cents=net,
        costs=costs,
        warnings=warnings,
        hypothetical_size_fraction=size,
    )
