"""Conservative probability for entry economics (entry-econ-v1).

The entry value must NOT be computed from an overconfident raw probability. This
module turns the model's calibrated side-probability into a *conservative* one by
shrinking it toward 0.5 according to how trustworthy the estimate is, then
applying a directional penalty for flip and manipulation risk.

Design:
  * Each reliability factor maps an input in its natural units to a multiplier in
    ``[factor_floor, 1.0]``. The product of the factors is the overall shrink, so
    ANY weak factor pulls the probability toward 0.5 — and a missing input
    degrades to a cautious-but-not-collapsing default rather than blocking.
  * The shrink is applied to the *distance from 0.5* (symmetric, side-agnostic),
    then flip/manipulation risk subtracts additional distance in prob units,
    pushing the estimate further toward the coin-flip line (never past it).

Every component is returned for transparency, the panel's diagnostics, and the
entry-performance ledger.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .config import EntryEconConfig


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _normalise_score(value: Any) -> float | None:
    """Coerce a 0..1 or 0..100 score to 0..1 (defensive: feeds vary)."""
    v = _num(value)
    if v is None:
        return None
    if v > 1.0:
        v = v / 100.0
    return _clamp(v, 0.0, 1.0)


@dataclass
class ConservativeResult:
    raw_side_prob: float                 # the model's calibrated side probability
    conservative_side_prob: float        # after shrink + directional penalties
    shrink: float                        # product of reliability factors in (0,1]
    flip_penalty: float                  # prob units removed for flip risk
    manip_penalty: float                 # prob units removed for manipulation risk
    factors: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def total_directional_penalty(self) -> float:
        return self.flip_penalty + self.manip_penalty


def _factor(value: float | None, floor: float, default: float) -> float:
    """Clamp a reliability input to [floor, 1]; use ``default`` when missing."""
    v = default if value is None else value
    return _clamp(float(v), floor, 1.0)


def conservative_probability(
    *,
    side: str,
    calibrated_side_prob: float,
    cfg: EntryEconConfig,
    checkpoint: str | None = None,
    data_coverage: float | None = None,
    data_quality: float | None = None,
    resolved_sample_size: int | None = None,
    model_market_disagreement: float | None = None,
    prediction_instability: float | None = None,
    regime_reliability: float | None = None,
    asset_reliability: float | None = None,
    interval_reliability: float | None = None,
    calibration_reliability: float | None = None,
    flip_score: float | None = None,
    flip_confidence: float | None = None,
    manipulation_suspected: bool = False,
    manipulation_against_side: bool | None = None,
) -> ConservativeResult:
    """Return a conservative side probability and its component diagnostics.

    All optional inputs degrade to cautious defaults when absent (the system
    "reduces coverage and confidence appropriately, then uses the reliable
    evidence that remains" rather than refusing to produce a value).
    """
    floor = _clamp(cfg.factor_floor, 0.05, 1.0)
    p = _clamp(float(calibrated_side_prob), 0.001, 0.999)
    notes: list[str] = []

    # --- reliability factors (each in [floor, 1]) ---
    # Calibration: unknown -> mildly discounted.
    f_cal = _factor(_num(calibration_reliability), floor, default=0.90)

    # Sample size: reliability = n / (n + k); unknown -> conservative mid.
    n = resolved_sample_size
    if n is None:
        f_n = 0.70
        notes.append("sample_size_unknown")
    else:
        f_n = float(n) / (float(n) + max(1.0, cfg.sample_half_k))
    f_n = _clamp(f_n, floor, 1.0)

    # Data coverage / quality: fraction of evidence actually backed.
    cov = _num(data_coverage)
    dq = _num(data_quality)
    cov_dq = None
    if cov is not None and dq is not None:
        cov_dq = 0.5 * cov + 0.5 * dq
    elif cov is not None:
        cov_dq = cov
    elif dq is not None:
        cov_dq = dq
    f_cov = _factor(cov_dq, floor, default=0.80)
    if cov_dq is None:
        notes.append("coverage_unknown")

    # Model vs market disagreement: small disagreement is healthy signal; large
    # disagreement (model far from the executable market) is a reliability risk.
    dis = _num(model_market_disagreement)
    if dis is None:
        f_dis = 0.90
    else:
        # disagreement expressed in prob units; 0 -> 1.0, 0.5 -> floor.
        f_dis = _clamp(1.0 - 2.0 * abs(dis), floor, 1.0)

    # Prediction instability: 0 stable -> 1.0, 1 churny -> floor.
    inst = _normalise_score(prediction_instability)
    f_inst = _factor(None if inst is None else (1.0 - inst), floor, default=0.85)

    # Regime / asset / interval priors (default neutral-ish; interval has a prior).
    f_regime = _factor(_num(regime_reliability), floor, default=0.95)
    f_asset = _factor(_num(asset_reliability), floor, default=0.95)
    iv = interval_reliability if interval_reliability is not None else cfg.interval_reliability_for(checkpoint or "")
    f_interval = _factor(_num(iv), floor, default=0.90)

    # Combine factors with a GEOMETRIC MEAN, not a raw product: the geometric mean
    # is still sensitive to any weak factor (a low value pulls it down noticeably,
    # unlike an arithmetic mean) but does not compound eight "pretty good" factors
    # down to a pathologically small number that would make a genuinely strong pick
    # impossible to ever enter. All factors are > 0 (floored), so the log is safe.
    _factors = [f_cal, f_n, f_cov, f_dis, f_inst, f_regime, f_asset, f_interval]
    shrink = math.exp(sum(math.log(f) for f in _factors) / len(_factors))
    shrink = _clamp(shrink, 0.0, 1.0)

    # Apply shrink to the distance from 0.5 (symmetric).
    dist = p - 0.5
    p_shrunk = 0.5 + dist * shrink

    # --- directional penalties (always push toward 0.5, never past it) ---
    fscore = _normalise_score(flip_score) or 0.0
    fconf = _normalise_score(flip_confidence)
    fconf = 1.0 if fconf is None else fconf
    flip_penalty = _clamp(cfg.flip_penalty_max * fscore * (0.5 + 0.5 * fconf), 0.0, cfg.flip_penalty_max)

    manip_penalty = 0.0
    if manipulation_suspected:
        # If we know the manipulation pushes against our side, charge full; if the
        # direction is unknown, charge a half penalty (could cut either way).
        if manipulation_against_side is None:
            manip_penalty = 0.5 * cfg.manip_penalty
        elif manipulation_against_side:
            manip_penalty = cfg.manip_penalty
        else:
            manip_penalty = 0.0

    direction = 1.0 if dist >= 0 else -1.0
    new_dist = max(0.0, abs(p_shrunk - 0.5) - (flip_penalty + manip_penalty))
    p_cons = _clamp(0.5 + direction * new_dist, 0.001, 0.999)

    return ConservativeResult(
        raw_side_prob=round(p, 6),
        conservative_side_prob=round(p_cons, 6),
        shrink=round(shrink, 6),
        flip_penalty=round(flip_penalty, 6),
        manip_penalty=round(manip_penalty, 6),
        factors={
            "calibration": round(f_cal, 4),
            "sample_size": round(f_n, 4),
            "coverage": round(f_cov, 4),
            "disagreement": round(f_dis, 4),
            "instability": round(f_inst, 4),
            "regime": round(f_regime, 4),
            "asset": round(f_asset, 4),
            "interval": round(f_interval, 4),
        },
        notes=notes,
    )
