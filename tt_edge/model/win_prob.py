"""Logistic blend of the three features -> P(home player wins).

The weights are HAND-SET at launch (0.45 / 0.35 / 0.20 by default) and stay
that way until >= TT_EDGE_CALIBRATION_MIN_ROWS graded matches exist — the
first ~300 matches are paper data collection, same discipline as Q15 paper
mode. Do not fit weights before there is data.

Missing-feature contract (explicit, per the engineering guidelines): a
feature with no supporting data contributes nothing and its weight is
removed from the blend (the remaining weights renormalize). If EVERY feature
is missing the blend is a coin flip (0.5) — the abstain guards upstream make
sure such a match never reaches an alert.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from tt_edge.model.features import (CommonOpponentFeature, FormFeature,
                                    H2HFeature)

# The model never claims certainty; clamping also keeps logit() finite for
# the calibration layer.
PROB_FLOOR = 0.02
PROB_CEIL = 0.98


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


@dataclass(frozen=True)
class BlendResult:
    """The blended probability plus the per-feature contributions (logged so
    every prediction is auditable after the fact)."""

    p_home: float
    score: float                     # pre-sigmoid blended score in [-1, 1]
    h2h_component: float | None      # None = feature missing, weight dropped
    form_component: float | None
    common_component: float | None


def blend_probability(h2h: H2HFeature | None, subject_form: FormFeature,
                      opponent_form: FormFeature,
                      common: CommonOpponentFeature | None, *,
                      w_h2h: float, w_form: float, w_common: float,
                      scale: float) -> BlendResult:
    """P(subject/home wins) from the three features.

    Each feature is centered to [-1, 1] (positive favors the subject):
    H2H rate r -> 2r-1; form differential is already centered; common-opponent
    differential is already centered. score = sum(w_i * x_i) / sum(w_i) over
    the PRESENT features, p = sigmoid(scale * score), clamped.
    """
    terms: list[tuple[float, float]] = []   # (weight, centered value)
    h2h_component = form_component = common_component = None

    if h2h is not None and h2h.n_meetings > 0:
        h2h_component = 2.0 * h2h.rate - 1.0
        terms.append((w_h2h, h2h_component))
    if subject_form.n_results > 0 or opponent_form.n_results > 0:
        form_component = subject_form.rate - opponent_form.rate
        terms.append((w_form, form_component))
    if common is not None and common.n_common > 0:
        common_component = common.differential
        terms.append((w_common, common_component))

    total_weight = sum(weight for weight, _ in terms)
    if total_weight <= 0:
        return BlendResult(p_home=0.5, score=0.0, h2h_component=None,
                           form_component=None, common_component=None)
    score = sum(weight * value for weight, value in terms) / total_weight
    p = _sigmoid(scale * score)
    p = min(PROB_CEIL, max(PROB_FLOOR, p))
    return BlendResult(p_home=p, score=score, h2h_component=h2h_component,
                       form_component=form_component,
                       common_component=common_component)
