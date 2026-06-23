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


def _side_probability(predicted_side: str, calibrated_yes: float | None,
                      fallback: float | None) -> float:
    """Calibrated probability of the CHOSEN side, clamped to [0.5, 0.99].

    Prefers the calibrated YES probability (the champion's raw confidence is
    under-stated on the live record, and calibration is the lever that corrects
    it); falls back to the already-side-oriented selected probability, then to
    0.5 (no directional edge) when neither is present."""
    if calibrated_yes is not None:
        yes = _clamp(float(calibrated_yes))
        side = yes if predicted_side == "YES" else 1.0 - yes
    elif fallback is not None:
        side = _clamp(float(fallback))
    else:
        side = 0.5
    return _clamp(side, 0.5, 0.99)


def agreement_probs(pick: Mapping[str, Any], cfg: UltoimConfig) -> list[float | None]:
    """The YES probabilities compared for the model-agreement term. The
    observational challenger is EXCLUDED by default: it is unvalidated and, per
    the live record, poorly calibrated, so its divergence must not drag the
    grade. ``Q15_ULTOIM_GRADE_INCLUDES_CHALLENGER=true`` folds it back in."""
    probs: list[float | None] = [
        pick.get("calibrated_yes_probability"),
        pick.get("baseline_yes_probability"),
    ]
    if cfg.grade_includes_challenger:
        probs.append(pick.get("challenger_yes_probability"))
    return probs


def quality_score(pick: Mapping[str, Any], flip_probability: float | None,
                  cfg: UltoimConfig) -> float:
    """0..1 multi-factor setup quality for the chosen side.

    A *weighted average* of independent positive signals — directional
    confidence, data quality, evidence quality, model agreement — NOT a product
    of sub-1.0 factors. Averaging (not multiplying) keeps a strong setup high
    instead of compounding it toward zero, so the A/B/C thresholds are reachable.
    Validated anti-signals (the YES-side veto, flip risk, manipulation) then
    apply as bounded multiplicative penalties on top."""
    side = str(pick.get("predicted_side") or "").upper()
    confidence = (_side_probability(
        side, pick.get("calibrated_yes_probability"),
        pick.get("selected_probability")) - 0.5) / 0.49      # 0..1 edge over 50/50
    data = _clamp(float(pick.get("data_quality") or 0.0))
    evidence = _clamp(float(pick.get("evidence_quality") or 0.0))
    agreement = model_agreement(agreement_probs(pick, cfg))

    weighted = (
        (cfg.grade_weight_confidence, confidence),
        (cfg.grade_weight_data, data),
        (cfg.grade_weight_evidence, evidence),
        (cfg.grade_weight_agreement, agreement),
    )
    wsum = sum(w for w, _ in weighted)
    score = sum(w * v for w, v in weighted) / wsum if wsum > 0 else 0.0

    # Bounded anti-signal penalties (validated vetoes), applied multiplicatively.
    score *= _yes_quality_factor(
        side, pick.get("order_flow_persistence"), pick.get("book_resiliency"))
    if flip_probability is not None:
        score *= 1.0 - cfg.flip_grade_weight * _clamp(flip_probability)
    if pick.get("manipulation_suspected"):
        score *= cfg.manip_grade_penalty
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
