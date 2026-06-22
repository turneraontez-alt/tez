"""OUR model's probability for a Polymarket Up/Down contract.

The champion already computes P(spot_close >= strike) with a lognormal-diffusion
structural model (see ``checkpoint_v95._structural_probability``):

    distance      = log(spot / strike)                      # orientation applied
    horizon_sigma = sigma_per_sqrt_second * sqrt(seconds)
    z             = (distance + drift) / horizon_sigma
    P(yes)        = Phi(z)                                   # clamped to [0.01, 0.99]

A Polymarket Up/Down contract is the SAME machinery with the threshold set to the
window's OPEN price ("price to beat") and orientation fixed to +1 (UP = close >=
open). So OUR up probability is exactly the champion's structural call re-aimed at
the open price — no parallel model, no new calibration. Pure and deterministic.
"""

from __future__ import annotations

import math

from ..challenger.mathx import clamp, normal_cdf, num


def updown_probability(
    *,
    spot: float | None,
    open_price: float | None,
    seconds_remaining: float | None,
    sigma_per_sqrt_second: float | None,
    drift_per_second: float | None = 0.0,
    max_drift_fraction_of_sigma: float = 0.35,
) -> float | None:
    """P(close >= open) for a 15-minute Up/Down contract, or None if inputs are
    unusable (missing / non-positive prices, missing horizon / volatility).

    ``drift_per_second`` is the champion's per-second expected log-return (already
    blended from short/medium returns); it is capped at
    ``max_drift_fraction_of_sigma * horizon_sigma`` exactly as the champion caps
    it, so a noisy drift estimate can't dominate the distance term.
    """
    s = num(spot)
    o = num(open_price)
    secs = num(seconds_remaining)
    sigma = num(sigma_per_sqrt_second)
    if s is None or o is None or s <= 0.0 or o <= 0.0:
        return None
    if secs is None or secs <= 0.0 or sigma is None or sigma <= 0.0:
        return None

    distance = math.log(s / o)                                   # UP orientation = +1
    horizon_sigma = max(1e-8, sigma * math.sqrt(max(1.0, secs)))
    drift = num(drift_per_second, 0.0) or 0.0
    projected = drift * secs
    cap = max(0.0, float(max_drift_fraction_of_sigma)) * horizon_sigma
    projected = clamp(projected, -cap, cap)
    z = clamp((distance + projected) / horizon_sigma, -5.0, 5.0)
    return clamp(normal_cdf(z), 0.01, 0.99)
