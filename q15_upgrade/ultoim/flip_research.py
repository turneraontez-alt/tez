"""Ultoim Build — flip / stability research (pure functions).

Active-research only: this estimates the probability that a pick's predicted
final-outcome side genuinely flips before settlement, as an ENSEMBLE of the
champion's flip_risk_score and the stability/microstructure signals (which
individually each beat the lone flip_risk_score by only a few points, but point
the same way). It is NEVER claimed validated — the graduation bar is to beat
flip_risk_score's separation out-of-sample on enough settled samples.

A genuine flip is judged at settlement: the originally-locked side != the final
official result. Brief wicks / temporary crossovers / reversing sweeps are not
flips and are not counted here (the call is graded only against settlement).
"""
from __future__ import annotations

import math
from typing import Any, Mapping

from .config import UltoimConfig

# Squash factor for the signed stability signals (~±0.1 typical) into a flip
# lean. Modest by design; the absolute scale is irrelevant while the predictor
# is unvalidated and held to the OOS bar before it can weight a grade.
_SIGNAL_K = 5.0


def _logistic(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def ensemble_flip_probability(pick: Mapping[str, Any]) -> float | None:
    """Combine flip_risk_score (0..100) with the signed stability / book /
    order-flow signals (higher = steadier = less flip) into a 0..1 flip
    probability. Returns None if no component is available."""
    parts: list[tuple[float, float]] = []  # (value, weight)
    fr = pick.get("flip_risk_score")
    if fr is not None:
        parts.append((max(0.0, min(1.0, float(fr) / 100.0)), 1.0))
    for key in ("prediction_stability", "book_resiliency", "order_flow_persistence"):
        sig = pick.get(key)
        if sig is not None:
            # steadier (more positive) signal -> lower flip lean
            parts.append((_logistic(-_SIGNAL_K * float(sig)), 0.5))
    if not parts:
        return None
    total_w = sum(w for _, w in parts)
    return max(0.0, min(1.0, sum(v * w for v, w in parts) / total_w))


def flip_fields(predicted_side: str, flip_probability: float | None,
                cfg: UltoimConfig) -> dict[str, Any]:
    """The recorded flip-research fields for a pick."""
    side = str(predicted_side or "").upper()
    other = "NO" if side == "YES" else "YES"
    decision = "NO"
    if flip_probability is not None and flip_probability >= cfg.flip_decision_threshold:
        decision = "YES"
    return {
        "flip_probability": flip_probability,
        "flip_decision": decision,
        "original_predicted_side": side,
        "expected_flip_side": other if decision == "YES" else side,
    }
