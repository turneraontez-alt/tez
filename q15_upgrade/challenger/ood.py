"""Out-of-distribution detection & fail-safe (addendum Section F).

Scores how far a decision-time observation sits outside the model's supported
operating ranges and missing feature families. A SEVERE score forces NO TRADE.
The score + reasons are returned so they can be stored with every prediction.

This is range/coverage-based (no training-density model needed at cold start);
it can be tightened later with a learned density / conformal score.
"""

from __future__ import annotations

from dataclasses import dataclass

from .features import FeatureVector
from .mathx import clamp


@dataclass
class OODBounds:
    max_abs_normalized_distance: float = 6.0
    min_vol_per_min: float = 0.0005
    max_vol_per_min: float = 0.05
    min_minutes: float = 0.5
    max_minutes: float = 30.0
    max_spread_cents: float = 25.0
    min_feature_coverage: float = 0.5

    @classmethod
    def from_config(cls, cfg) -> "OODBounds":
        return cls(max_spread_cents=max(cls.max_spread_cents, cfg.max_spread_cents))


def ood_score(fv: FeatureVector, bounds: OODBounds | None = None) -> tuple[float, list[str]]:
    """Return (score in [0,1], reasons). Higher = more out-of-distribution."""
    b = bounds or OODBounds()
    reasons: list[str] = []
    hits = 0.0
    checks = 0.0

    nd = abs(fv.details.get("normalized_distance", 0.0))
    checks += 1
    if nd > b.max_abs_normalized_distance:
        reasons.append(f"normalized_distance_out_of_range({nd:.2f})")
        hits += clamp((nd - b.max_abs_normalized_distance) / b.max_abs_normalized_distance, 0.0, 1.0)

    vol = fv.details.get("volatility_per_min", 0.0)
    checks += 1
    if vol > b.max_vol_per_min:
        reasons.append(f"volatility_above_range({vol:.4f})")
        hits += 1.0
    elif 0 < vol < b.min_vol_per_min:
        reasons.append(f"volatility_below_range({vol:.4f})")
        hits += 0.5

    minutes = fv.details.get("minutes_remaining", 0.0)
    checks += 1
    if minutes and (minutes < b.min_minutes or minutes > b.max_minutes):
        reasons.append(f"time_to_expiry_out_of_range({minutes:.1f}m)")
        hits += 1.0

    checks += 1
    if fv.spread_cents is not None and fv.spread_cents > b.max_spread_cents:
        reasons.append(f"spread_out_of_range({fv.spread_cents:.1f}c)")
        hits += 1.0

    checks += 1
    if fv.coverage_fraction < b.min_feature_coverage:
        reasons.append(f"thin_feature_coverage({fv.coverage_fraction:.2f})")
        hits += (b.min_feature_coverage - fv.coverage_fraction) / b.min_feature_coverage

    checks += 1
    if fv.market_yes_prob is None:
        reasons.append("missing_market_quote")
        hits += 1.0

    score = clamp(hits / checks, 0.0, 1.0)
    return score, reasons
