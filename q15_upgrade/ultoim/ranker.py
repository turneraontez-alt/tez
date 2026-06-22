"""Ultoim Build — multi-factor quality grade + value ranking (pure functions).

Applies two findings from the live record:
  * Grading by probability alone is anti-profit (the most confident picks are
    overpriced), so the grade blends calibration, data/evidence quality, model
    agreement, flip risk, manipulation, and the validated YES-quality veto.
  * Ranking by pure confidence is flat and unprofitable, so #1/#2/#3 are ordered
    by quality x value (net edge), not raw probability.
Nothing here trades or mutates the champion; it reads a frozen analysis snapshot.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from .config import UltoimConfig


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def model_agreement(yes_probs: Sequence[float | None]) -> float:
    """1.0 when the calibrated / challenger / baseline YES probabilities agree,
    falling toward 0 as they diverge. A wide spread = conflicting evidence."""
    vals = [float(p) for p in yes_probs if p is not None]
    if len(vals) < 2:
        return 1.0
    spread = max(vals) - min(vals)
    # A 0.5 spread (e.g. 0.3 vs 0.8) is full disagreement.
    return _clamp(1.0 - spread / 0.5)


def _yes_quality_factor(predicted_side: str, ofp: float | None, book: float | None,
                        floor: float = 0.70) -> float:
    """The validated YES-side veto: low order-flow-persistence / book-resiliency
    flag the catastrophic YES losers. NO picks are unaffected. Uses 0 as the
    neutral point of the signed signals (no in-sample threshold)."""
    if predicted_side != "YES":
        return 1.0
    low = [s is not None and float(s) < 0.0 for s in (ofp, book)]
    if all(low) and low:                 # both signals present and weak
        return floor
    if any(low):
        return floor + (1.0 - floor) / 2.0
    return 1.0


def quality_score(pick: Mapping[str, Any], flip_probability: float | None,
                  cfg: UltoimConfig) -> float:
    """0..1 multi-factor setup quality for the chosen side."""
    p = _clamp(float(pick.get("selected_probability") or 0.5), 0.5, 0.99)
    score = (p - 0.5) / 0.49                                  # 0..1 from 0.50..0.99
    score *= 0.55 + 0.45 * _clamp(float(pick.get("data_quality") or 0.0))
    score *= 0.70 + 0.30 * _clamp(float(pick.get("evidence_quality") or 0.0))
    score *= 0.60 + 0.40 * model_agreement([
        pick.get("calibrated_yes_probability"),
        pick.get("challenger_yes_probability"),
        pick.get("baseline_yes_probability"),
    ])
    if flip_probability is not None:
        score *= 1.0 - cfg.flip_grade_weight * _clamp(flip_probability)
    if pick.get("manipulation_suspected"):
        score *= 0.85
    score *= _yes_quality_factor(
        str(pick.get("predicted_side") or "").upper(),
        pick.get("order_flow_persistence"), pick.get("book_resiliency"),
    )
    return _clamp(score)


def grade_for(score: float, cfg: UltoimConfig) -> str:
    if score >= cfg.grade_a_min:
        return "A"
    if score >= cfg.grade_b_min:
        return "B"
    return "C"


def rank_score(quality: float, net_edge_cents: float | None, cfg: UltoimConfig) -> float:
    """Quality lifted by value: cheap-but-good picks rank above overpriced
    favourites. Net edge is bounded so the (noisy) edge can't dominate quality."""
    if net_edge_cents is None:
        return quality
    edge_term = max(-1.0, min(1.0, float(net_edge_cents) / max(cfg.edge_scale_cents, 1.0)))
    return quality + cfg.edge_weight * edge_term


def value_rank(picks: Sequence[dict[str, Any]], cfg: UltoimConfig) -> list[dict[str, Any]]:
    """Score, grade, and order the candidates; return the top_k with rank set.
    Each input dict must already carry ``quality_score`` and ``flip_probability``;
    this function only orders and stamps rank/grade."""
    ranked = sorted(
        picks,
        key=lambda d: rank_score(d["quality_score"], d.get("net_edge_cents"), cfg),
        reverse=True,
    )
    top = ranked[: cfg.top_k]
    for i, pick in enumerate(top, start=1):
        pick["rank"] = i
        pick["confidence_grade"] = grade_for(pick["quality_score"], cfg)
    return top
