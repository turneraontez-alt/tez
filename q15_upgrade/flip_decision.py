"""Strict flip-decision engine (OBSERVATIONAL / background).

Answers ONE question per interval, for the headline (#1) pick: will the current
predicted final-settlement side genuinely FLIP to the opposite side before close?

A *true* flip = the opposite side becomes the FINAL SETTLED outcome. It is graded
only against official settlement (``flipped = predicted_side != official_result``),
so a temporary crossover, wick, sweep, or brief manipulation is NOT a flip unless
the contract actually settles the other way.

Design guarantees (see tests):
  * Flip PROBABILITY is a deterministic, bounded blend — primarily the
    manipulation score (oriented: manipulation OPPOSING the pick raises it,
    supporting it lowers it), corroborated by instability/strike/flow/book/
    noise/regime/spot-vs-contract/time signals. A high manipulation score does
    NOT by itself force a YES.
  * The DECISION threshold is LEARNED per interval from completed, post-reset
    history via chronological out-of-sample testing — never a fixed guess, never
    tuned on the same rows it is scored on.
  * Decision = YES only when the interval threshold is VALIDATED (YES-precision
    >= target on an unseen test fold with a meaningful sample) AND the pick's
    flip probability exceeds it. Otherwise NO. Until all three intervals validate
    independently, the engine stays background (Decision NO).
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

INTERVALS = ("15M", "10M", "7M")
TARGET_PRECISION = 0.80


# --------------------------------------------------------------------------- #
# env helpers
# --------------------------------------------------------------------------- #
def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return max(low, min(high, value))


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return default
        f = float(value)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


@dataclass(frozen=True)
class FlipConfig:
    enabled: bool
    target_precision: float
    train_fraction: float
    min_total: int          # min resolved rows for the interval before any calibration
    min_yes_train: int      # min YES decisions in train to trust a candidate threshold
    min_yes_test: int       # min YES decisions in test to declare validated
    time_seconds: float     # the nominal interval length (for the time signal)

    @classmethod
    def from_env(cls, checkpoint: str | None = None) -> "FlipConfig":
        nominal = {"15M": 900.0, "10M": 600.0, "7M": 420.0}.get(str(checkpoint or "").upper(), 600.0)
        return cls(
            enabled=_env_bool("Q15_V95_FLIP_DECISION_ENABLED", True),
            target_precision=_env_float("Q15_V95_FLIP_TARGET_PRECISION", TARGET_PRECISION, 0.5, 0.99),
            train_fraction=_env_float("Q15_V95_FLIP_TRAIN_FRACTION", 0.70, 0.3, 0.9),
            min_total=_env_int("Q15_V95_FLIP_MIN_ROWS", 60, 10, 100000),
            min_yes_train=_env_int("Q15_V95_FLIP_MIN_YES_TRAIN", 10, 1, 100000),
            min_yes_test=_env_int("Q15_V95_FLIP_MIN_YES_TEST", 10, 1, 100000),
            time_seconds=nominal,
        )


# --------------------------------------------------------------------------- #
# flip PROBABILITY — deterministic, bounded blend of oriented signals
# --------------------------------------------------------------------------- #
# Weights are intentionally modest; the LEARNED threshold (not these weights)
# decides reliability. Manipulation + flip-risk are the primary basis.
_WEIGHTS = {
    "manip_opposing": 1.30,      # manipulation that OPPOSES the pick (primary basis)
    "flip_risk": 1.10,           # learned 0..100 flip-risk score
    "instability": 0.70,         # prediction-stability shadow signal (inverted)
    "strike_proximity": 0.60,    # near the strike -> easier to flip
    "crossings": 0.40,           # repeated strike crossings
    "wick_against": 0.45,        # wick rejecting the pick's side
    "momentum_against": 0.55,    # momentum reversing against the pick
    "flow_against": 0.50,        # order-flow persistence against the pick
    "book_fragility": 0.35,      # low book resiliency for the side
    "entropy": 0.30,             # directionless / noisy returns
    "regime_transition": 0.35,   # regime boundary
    "spot_contract_disagree": 0.45,  # market price disagrees with the model side
    "time_room": 0.35,           # more time left -> more room to flip
}


def _side_sign(side: str | None) -> float:
    """+1 if the pick is YES (supporting = up in YES space), -1 if NO."""
    return 1.0 if str(side or "").upper() == "YES" else -1.0


def flip_probability(analysis: Mapping[str, Any], checkpoint: str,
                     config: FlipConfig | None = None) -> dict[str, Any]:
    """Probability (0..1) that the pick's side flips by settlement, plus the
    oriented component contributions (for diagnostics, never shown to the owner).

    Missing inputs degrade a component to neutral (0.5) — never an exception.
    """
    cfg = config or FlipConfig.from_env(checkpoint)
    side = str(analysis.get("prediction_side") or "").upper()
    sign = _side_sign(side)  # +1 YES, -1 NO

    fr = analysis.get("flip_risk") if isinstance(analysis.get("flip_risk"), Mapping) else {}
    manip = analysis.get("manipulation") if isinstance(analysis.get("manipulation"), Mapping) else {}
    fv = analysis.get("feature_values") if isinstance(analysis.get("feature_values"), Mapping) else {}
    details = analysis.get("feature_details") if isinstance(analysis.get("feature_details"), Mapping) else {}
    sigs = analysis.get("shadow_signals") if isinstance(analysis.get("shadow_signals"), Mapping) else {}
    regime = analysis.get("regime") if isinstance(analysis.get("regime"), Mapping) else {}
    quote = analysis.get("quote") if isinstance(analysis.get("quote"), Mapping) else {}

    comps: dict[str, float] = {}

    # --- manipulation, ORIENTED. A high score is neutral unless it OPPOSES the
    # pick; manipulation that supports the pick lowers flip pressure. ----------
    manip_score = _num(fr.get("score"))
    if manip_score is not None:
        manip_score = manip_score / 100.0           # flip_risk score is 0..100
    if manip_score is None:
        manip_score = _num(manip.get("score"), 0.0)  # fallback already 0..1
    manip_score = _clamp(manip_score or 0.0, 0.0, 1.0)
    # direction "after" the manipulation, if known: YES/NO of the likely side.
    dir_after = str(manip.get("direction_after") or fr.get("direction_after") or "").upper()
    if dir_after in {"YES", "NO"}:
        opposing = 1.0 if dir_after != side else -1.0
    else:
        opposing = 0.0  # unknown direction -> mild, symmetric uncertainty
    # opposing manipulation pushes up; supporting pushes down; unknown adds a
    # small uncertainty bump scaled by the score.
    if opposing > 0:
        comps["manip_opposing"] = _clamp(0.5 + 0.5 * manip_score, 0.5, 1.0)
    elif opposing < 0:
        comps["manip_opposing"] = _clamp(0.5 - 0.5 * manip_score, 0.0, 0.5)
    else:
        comps["manip_opposing"] = _clamp(0.5 + 0.2 * manip_score, 0.5, 0.7)

    # --- learned flip-risk score (0..100) -------------------------------------
    fr_score = _num(fr.get("score"))
    comps["flip_risk"] = _clamp((fr_score / 100.0) if fr_score is not None else 0.5, 0.0, 1.0)

    # --- prediction stability (shadow signal, YES-space [-1,1]) ---------------
    stab = _num(sigs.get("prediction_stability"))
    if stab is None:
        comps["instability"] = 0.5
    else:
        # stability agreeing with the side (sign*stab>0) => stable => low flip.
        agree = sign * stab               # >0 supports the pick
        comps["instability"] = _clamp(0.5 - 0.5 * agree, 0.0, 1.0)

    # --- strike proximity & crossings -----------------------------------------
    dsig = _num(analysis.get("distance_in_sigma"))
    if dsig is None:
        ti = details.get("threshold_interaction") if isinstance(details.get("threshold_interaction"), Mapping) else {}
        dsig = _num(ti.get("distance_in_sigma"))
    comps["strike_proximity"] = _clamp(1.0 / (1.0 + abs(dsig)), 0.0, 1.0) if dsig is not None else 0.5
    ti = details.get("threshold_interaction") if isinstance(details.get("threshold_interaction"), Mapping) else {}
    crossings = _num(ti.get("crossings"))
    comps["crossings"] = _clamp((crossings or 0.0) / 6.0, 0.0, 1.0) if crossings is not None else 0.5

    # --- wick / momentum / flow, ORIENTED against the pick --------------------
    def _against(feature_key: str) -> float:
        val = _num(fv.get(feature_key))
        if val is None:
            return 0.5
        # feature is YES-oriented; sign*val>0 supports the pick. Against => up.
        return _clamp(0.5 - 0.5 * _clamp(sign * val, -1.0, 1.0), 0.0, 1.0)

    comps["wick_against"] = _against("wick")
    comps["momentum_against"] = _against("momentum")
    comps["flow_against"] = _against("flow")

    # --- book resiliency: low/opposing resiliency => fragile => up ------------
    book = _num(sigs.get("book_resiliency"))
    if book is None:
        book = _num(fv.get("book"))
    comps["book_fragility"] = _clamp(0.5 - 0.5 * _clamp(sign * (book or 0.0), -1.0, 1.0), 0.0, 1.0) \
        if book is not None else 0.5

    # --- entropy / regime transition (already YES-magnitude-ish) --------------
    ent = _num(sigs.get("entropy_noise"))
    # entropy_noise = lean*(1-noise): small magnitude => noisy/directionless => up
    comps["entropy"] = _clamp(1.0 - abs(ent), 0.0, 1.0) if ent is not None else 0.5
    reg = _num(sigs.get("regime_transition"))
    comps["regime_transition"] = _clamp(1.0 - abs(reg), 0.0, 1.0) if reg is not None else 0.5

    # --- spot vs contract disagreement ----------------------------------------
    model_yes = _num(analysis.get("yes_probability"))
    mid = None
    yb, ya = _num(quote.get("yes_bid_cents")), _num(quote.get("yes_ask_cents"))
    if yb is not None and ya is not None:
        mid = (yb + ya) / 200.0  # cents -> 0..1
    if model_yes is not None and mid is not None:
        # market-implied YES vs model YES: market on the OPPOSITE side of 0.5
        # from the model => disagreement => flip pressure.
        disagree = _clamp(abs(model_yes - mid), 0.0, 1.0)
        # only count it as flip pressure when the market leans the OTHER way.
        market_side_yes = mid >= 0.5
        model_side_yes = side == "YES"
        comps["spot_contract_disagree"] = _clamp(0.5 + (disagree if market_side_yes != model_side_yes else -disagree), 0.0, 1.0)
    else:
        comps["spot_contract_disagree"] = 0.5

    # --- time room: more seconds left => more room to flip --------------------
    secs = _num(analysis.get("seconds_remaining"))
    comps["time_room"] = _clamp((secs / cfg.time_seconds), 0.0, 1.0) if (secs is not None and cfg.time_seconds > 0) else 0.5

    logit = sum(_WEIGHTS[k] * (comps[k] - 0.5) for k in _WEIGHTS)
    prob = _sigmoid(logit)
    return {"probability": round(prob, 6), "components": {k: round(v, 4) for k, v in comps.items()}}


# --------------------------------------------------------------------------- #
# threshold selection (chronological OOS) + metrics
# --------------------------------------------------------------------------- #
def _metrics_at(rows: Sequence[Mapping[str, Any]], threshold: float) -> dict[str, Any]:
    """Confusion-matrix metrics for 'decision YES iff flip_probability > thr'
    against the realised ``flipped`` label. ``rows`` must each carry
    ``flip_probability`` (float) and ``flipped`` (bool)."""
    tp = fp = tn = fn = 0
    for r in rows:
        p = _num(r.get("flip_probability"))
        if p is None:
            continue
        decided_yes = p > threshold
        flipped = bool(r.get("flipped"))
        if decided_yes and flipped:
            tp += 1
        elif decided_yes and not flipped:
            fp += 1
        elif not decided_yes and flipped:
            fn += 1
        else:
            tn += 1
    n = tp + fp + tn + fn
    yes_n = tp + fp
    pos = tp + fn
    return {
        "n": n, "yes_n": yes_n,
        "yes_precision": round(tp / yes_n, 4) if yes_n else None,
        "recall": round(tp / pos, 4) if pos else None,
        "false_positive_rate": round(fp / (fp + tn), 4) if (fp + tn) else None,
        "false_negative_rate": round(fn / pos, 4) if pos else None,
        "coverage": round(yes_n / n, 4) if n else None,
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
    }


def select_threshold(resolved_rows: Sequence[Mapping[str, Any]], config: FlipConfig) -> dict[str, Any]:
    """Pick the per-interval flip threshold by CHRONOLOGICAL out-of-sample test.

    ``resolved_rows`` are settled flip decisions for ONE interval, each with
    ``created_at`` (for ordering), ``flip_probability`` and ``flipped``. They are
    ordered oldest-first, split train/test by time, the lowest threshold meeting
    the precision target (with enough YES decisions) is chosen on TRAIN, then it
    is VALIDATED on the unseen TEST fold. The threshold is never tuned on the
    same rows it is finally judged on.
    """
    rows = sorted(resolved_rows, key=lambda r: _num(r.get("created_at"), 0.0) or 0.0)
    n = len(rows)
    base = {
        "validated": False, "threshold": 1.01, "n": n,
        "target_precision": config.target_precision, "reason": "insufficient_rows",
        "train": {}, "test": {},
    }
    if n < config.min_total:
        return base
    split = max(1, min(n - 1, int(round(config.train_fraction * n))))
    train, test = rows[:split], rows[split:]
    # Candidate thresholds: the distinct probabilities seen in TRAIN (a YES is
    # "p > thr", so use values just below each observed prob).
    cand = sorted({_num(r.get("flip_probability"), 0.0) or 0.0 for r in train})
    best = None
    for c in cand:
        thr = max(0.0, c - 1e-9)
        m = _metrics_at(train, thr)
        if (m["yes_n"] or 0) >= config.min_yes_train and m["yes_precision"] is not None \
                and m["yes_precision"] >= config.target_precision:
            # lowest qualifying threshold = highest coverage at target precision
            best = thr
            break
    if best is None:
        base["reason"] = "no_train_threshold_meets_precision"
        base["train"] = _metrics_at(train, 0.5)
        return base
    test_m = _metrics_at(test, best)
    train_m = _metrics_at(train, best)
    validated = bool(
        (test_m["yes_n"] or 0) >= config.min_yes_test
        and test_m["yes_precision"] is not None
        and test_m["yes_precision"] >= config.target_precision
    )
    return {
        "validated": validated,
        "threshold": round(best, 6) if validated else 1.01,
        "candidate_threshold": round(best, 6),
        "n": n, "target_precision": config.target_precision,
        "reason": "validated" if validated else "test_below_precision",
        "train": train_m, "test": test_m,
    }


def evaluate(resolved_rows: Sequence[Mapping[str, Any]], config: FlipConfig) -> dict[str, Any]:
    """Full per-interval picture: the chosen threshold + the metrics of the
    decisions ACTUALLY made (graded by each row's stored ``decision``)."""
    sel = select_threshold(resolved_rows, config)
    # Metrics of the decisions as they were actually recorded/served.
    tp = fp = tn = fn = 0
    for r in resolved_rows:
        decided_yes = str(r.get("decision") or "").upper() == "YES"
        flipped = bool(r.get("flipped"))
        if decided_yes and flipped:
            tp += 1
        elif decided_yes and not flipped:
            fp += 1
        elif not decided_yes and flipped:
            fn += 1
        else:
            tn += 1
    n = tp + fp + tn + fn
    yes_n = tp + fp
    pos = tp + fn
    served = {
        "n": n, "yes_n": yes_n,
        "yes_precision": round(tp / yes_n, 4) if yes_n else None,
        "recall": round(tp / pos, 4) if pos else None,
        "false_positive_rate": round(fp / (fp + tn), 4) if (fp + tn) else None,
        "false_negative_rate": round(fn / pos, 4) if pos else None,
        "coverage": round(yes_n / n, 4) if n else None,
    }
    return {"selection": sel, "served": served}
