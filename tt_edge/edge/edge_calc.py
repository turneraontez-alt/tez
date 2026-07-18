"""THE edge calculation and the recommend/abstain decision.

This module is the single source of truth for "edge" — the Q15 audit found
three inconsistent edge calculations and this pipeline does not repeat that.
Nothing else in tt_edge subtracts model and market probabilities.

Deliberate Phase-0 conservatism: only the MODEL-FAVORED side is ever
evaluated. A price-only "edge" on a side the model gives < 50% (market even
more pessimistic than the model) is exactly the trade an uncalibrated launch
model should not take; revisit once calibration is fitted and validated.

Decision order (first failure wins, but every failed guard is logged):

1. Freshness — any stale board/H2H/form/odds payload is a hard NO BET.
2. Insufficient data — fewer than ``min_h2h_meetings`` H2H meetings AND fewer
   than ``min_common_opps`` common opponents.
3. Fix-risk — the line moved >= ``fix_risk_cents`` AGAINST the model-favored
   side with no schedule explanation: movement is the news, hard pass. This
   outranks a big edge on purpose — a huge "edge" against a steamed line is
   exactly the trap.
4. No edge — model p within ``no_edge_band`` of the de-vig market p.
5. Threshold — edge below ``min_edge``.
6. Bankroll/exposure — no bankroll set, or max concurrent open picks reached.
7. Staking — a non-positive stake (Kelly <= 0 cannot happen with a real
   edge; rounding to zero cents can, on a dust bankroll).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from tt_edge.config import TTEdgeConfig
from tt_edge.edge.staking import StakePlan, plan_stake
from tt_edge.edge.vig import american_to_implied, devig_proportional
from tt_edge.freshness import FreshnessCheck

_ONE = Decimal(1)

RECOMMEND = "RECOMMEND"
NO_BET = "NO_BET"


def compute_edge(model_p: Decimal, fair_p: Decimal) -> Decimal:
    """Edge in probability points: model_p - fair_p. The ONLY edge formula."""
    return model_p - fair_p


def dedupe_reasons(reasons: list[str]) -> tuple[str, ...]:
    """Order-preserving dedupe (both players' form stale = one reason)."""
    return tuple(dict.fromkeys(reasons))


@dataclass(frozen=True)
class MarketView:
    """The two-way market being evaluated (American prices)."""

    home_price: int
    away_price: int
    # Signed movement of the model-favored side's price in cents since the
    # earliest snapshot (positive = lengthened = moved against that side);
    # None when only one snapshot exists.
    favored_side_movement_cents: int | None = None


@dataclass(frozen=True)
class Decision:
    """The full evaluation of one match: recommend or abstain, with the
    numbers the alert / prediction row needs either way."""

    status: str                       # RECOMMEND | NO_BET
    side: str | None                  # 'home' | 'away' (model-favored side)
    model_p: Decimal | None           # model probability of the picked side
    fair_p: Decimal | None            # de-vig market probability of that side
    edge: Decimal | None
    price_american: int | None        # book price of the picked side
    stake: StakePlan | None
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_recommendation(self) -> bool:
        return self.status == RECOMMEND


def evaluate(*, model_p_home: Decimal, market: MarketView,
             freshness_checks: list[FreshnessCheck],
             h2h_meetings: int, common_opponents: int,
             open_recommendations: int, bankroll_cents: int | None,
             config: TTEdgeConfig) -> Decision:
    """Evaluate one match end to end. Pure — no I/O, no clock."""
    if not (Decimal(0) < model_p_home < _ONE):
        raise ValueError(f"model_p_home must be in (0, 1); got {model_p_home}")

    side = "home" if model_p_home >= Decimal("0.5") else "away"
    model_p = model_p_home if side == "home" else _ONE - model_p_home
    price = market.home_price if side == "home" else market.away_price

    implied_home = american_to_implied(market.home_price)
    implied_away = american_to_implied(market.away_price)
    fair_home, fair_away = devig_proportional(implied_home, implied_away)
    fair_p = fair_home if side == "home" else fair_away
    edge = compute_edge(model_p, fair_p)

    reasons: list[str] = []
    for check_result in freshness_checks:
        if not check_result.ok:
            reasons.append(check_result.reason)
    if h2h_meetings < config.min_h2h_meetings and \
            common_opponents < config.min_common_opps:
        reasons.append("insufficient_data")
    movement = market.favored_side_movement_cents
    if movement is not None and movement >= config.fix_risk_cents:
        reasons.append("fix_risk_line_move")
    if abs(edge) < config.no_edge_band:
        reasons.append("no_edge")
    elif edge < config.min_edge:
        reasons.append("edge_below_threshold")
    if not reasons:
        # Exposure/bankroll guards only matter once the pick itself qualifies.
        if bankroll_cents is None or bankroll_cents <= 0:
            reasons.append("no_bankroll_set")
        elif open_recommendations >= config.max_open_recommendations:
            reasons.append("max_concurrent_open")

    if reasons:
        return Decision(status=NO_BET, side=side, model_p=model_p,
                        fair_p=fair_p, edge=edge, price_american=price,
                        stake=None, reasons=dedupe_reasons(reasons))

    stake = plan_stake(bankroll_cents, model_p, price,
                       fraction=config.kelly_fraction,
                       cap_pct=config.kelly_cap_pct,
                       floor_cents=config.stake_floor_cents)
    if stake.stake_cents <= 0:
        return Decision(status=NO_BET, side=side, model_p=model_p,
                        fair_p=fair_p, edge=edge, price_american=price,
                        stake=stake, reasons=("non_positive_stake",))
    return Decision(status=RECOMMEND, side=side, model_p=model_p,
                    fair_p=fair_p, edge=edge, price_american=price,
                    stake=stake, reasons=())
