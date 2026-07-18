"""Versioned Platt calibration — the Q15 reliability pattern, ported.

The blend's raw probability passes through the ACTIVE calibration transform
before edge math. At launch that transform is the identity; a Platt fit
(regularized, clamped, Newton with convergence judged on the applied step —
lifted from ``q15_upgrade/ledger_v95.py``) can be fitted once
>= TT_EDGE_CALIBRATION_MIN_ROWS graded predictions exist. Fitted versions
are stored INACTIVE and promoted manually (``jobs/grade.py
--activate-calibration``) — same manual, evidence-gated promotion discipline
as the Q15 champion/challenger split.

Float math is fine here (probabilities, not money); Decimal conversion
happens at the edge_calc boundary.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

# Clamps mirror the validated Q15 defaults.
SLOPE_MIN = 0.40
SLOPE_MAX = 3.00
INTERCEPT_CAP = 1.50
REGULARIZATION = 0.20
MAX_ITERS = 50

_LOGIT_EPS = 1e-6


class CalibrationError(ValueError):
    """Raised when a fit is requested on an inadequate dataset."""


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def logit(p: float) -> float:
    p = min(1.0 - _LOGIT_EPS, max(_LOGIT_EPS, p))
    return math.log(p / (1.0 - p))


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


@dataclass(frozen=True)
class CalibrationTransform:
    """p_calibrated = sigmoid(intercept + slope * logit(p_raw))."""

    intercept: float
    slope: float

    def apply(self, p_raw: float) -> float:
        return _sigmoid(self.intercept + self.slope * logit(p_raw))


IDENTITY = CalibrationTransform(intercept=0.0, slope=1.0)


@dataclass(frozen=True)
class CalibrationFit:
    """A fitted (not yet active) calibration candidate with its evidence."""

    transform: CalibrationTransform
    n_rows: int
    converged: bool
    iterations: int
    brier_identity: float
    brier_fitted: float


def fit_platt(pairs: Sequence[tuple[float, float]], *, min_rows: int,
              max_iters: int = MAX_ITERS, slope_min: float = SLOPE_MIN,
              slope_max: float = SLOPE_MAX,
              intercept_cap: float = INTERCEPT_CAP,
              reg: float = REGULARIZATION) -> CalibrationFit:
    """Penalized Newton Platt fit on (raw_probability, outcome) pairs.

    ``outcome`` is 1.0/0.0 (home won / lost). Convergence is judged on the
    APPLIED post-clamp step so a coefficient pinned at its clamp counts as
    converged rather than iterating forever (the ledger_v95 fix). Raises
    :class:`CalibrationError` below ``min_rows`` — do not overfit before
    there is data.
    """
    pairs = [(logit(p), y) for p, y in pairs]
    if len(pairs) < min_rows:
        raise CalibrationError(
            f"need >= {min_rows} graded rows to fit calibration; have {len(pairs)}")
    intercept, slope = 0.0, 1.0
    converged = False
    iterations = 0
    for _ in range(max_iters):
        iterations += 1
        g0 = -reg * intercept
        g1 = -reg * (slope - 1.0)
        h00 = reg
        h01 = 0.0
        h11 = reg
        for x, y in pairs:
            p = _sigmoid(intercept + slope * x)
            residual = y - p
            variance = max(1e-6, p * (1.0 - p))
            g0 += residual
            g1 += residual * x
            h00 += variance
            h01 += variance * x
            h11 += variance * x * x
        determinant = h00 * h11 - h01 * h01
        if determinant <= 1e-12:
            break
        d0 = (g0 * h11 - g1 * h01) / determinant
        d1 = (g1 * h00 - g0 * h01) / determinant
        new_intercept = _clamp(intercept + d0, -intercept_cap, intercept_cap)
        new_slope = _clamp(slope + d1, slope_min, slope_max)
        applied = abs(new_intercept - intercept) + abs(new_slope - slope)
        intercept, slope = new_intercept, new_slope
        if applied < 1e-6:
            converged = True
            break

    transform = CalibrationTransform(intercept=intercept, slope=slope)
    brier_identity = sum((_sigmoid(x) - y) ** 2 for x, y in pairs) / len(pairs)
    brier_fitted = sum((_sigmoid(intercept + slope * x) - y) ** 2
                       for x, y in pairs) / len(pairs)
    return CalibrationFit(transform=transform, n_rows=len(pairs),
                          converged=converged, iterations=iterations,
                          brier_identity=brier_identity,
                          brier_fitted=brier_fitted)
