"""Flip-risk monitoring (read-only overlay on the frozen prediction).

This module measures whether the current *frozen* V9.5 prediction is at risk of
flipping (YES->NO or NO->YES). It is deliberately separate from the prediction
model and NEVER changes the call — it only watches it. Three values are kept
distinct, as required:

  * manipulation/flip RISK score   — 0..100, live evidence that price is being
    pushed around (large wicks, book imbalance, unsupported contract moves, ...).
    Missing data lowers CONFIDENCE, never raises the score.
  * estimated FLIP PROBABILITY     — historical flip rate of the current score's
    bucket, learned from resolved contracts (not the same as the risk score).
  * learned flip THRESHOLD         — the score level that has historically
    preceded flips, per checkpoint / direction / asset, with a sample-size gate.

The learning lives in the ledger; this module holds the pure scoring, bucketing,
threshold resolution, alert state machine, and message formatting so they can be
unit-tested without a database.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


# --- env helpers (mirror checkpoint_v95 so the module is standalone) ----------
def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        return min(high, max(low, float(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        return min(high, max(low, int(float(os.environ.get(name, default)))))
    except (TypeError, ValueError):
        return default


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        out = float(value)
        return out if out == out and out not in (float("inf"), float("-inf")) else default
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# Evidence categories (the "independent" categories the alert gate counts).
CATEGORY_LABELS = {
    "wicks": "rejection wick / candle structure",
    "orderbook": "order-book imbalance",
    "spread_liquidity": "spread expansion / thin liquidity",
    "executed_flow": "contract move unsupported by executed flow",
    "spot_momentum": "spot momentum against the prediction",
    "prediction_divergence": "prediction-market vs spot divergence",
    "pin": "strike pin / repeated failed breakouts",
}

# Relative weight of each category in the blended risk score.
CATEGORY_WEIGHTS = {
    "wicks": 1.0,
    "orderbook": 0.8,
    "spread_liquidity": 0.6,
    "executed_flow": 1.0,
    "spot_momentum": 0.9,
    "prediction_divergence": 1.0,
    "pin": 0.7,
}


@dataclass
class RiskAssessment:
    score: float                       # 0..100 manipulation/flip risk
    confidence: float                  # 0..100 (data coverage)
    evidence_categories: list[str]     # categories that actively support a warning
    primary_reason: str
    direction_monitored: str | None    # e.g. "NO → YES"
    components: dict[str, float] = field(default_factory=dict)  # category -> 0..1

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 1),
            "confidence": round(self.confidence, 1),
            "evidence_categories": list(self.evidence_categories),
            "evidence_count": len(self.evidence_categories),
            "primary_reason": self.primary_reason,
            "direction_monitored": self.direction_monitored,
            "components": {k: round(v, 3) for k, v in self.components.items()},
        }


def _opposes(value: float | None, side: str) -> float:
    """Magnitude of a YES-signed signal in the direction that would flip ``side``.

    A signal that pushes toward the *opposite* of the current prediction is what
    threatens a flip; same-direction pressure is not flip risk. Returns 0..1.
    """
    v = _num(value, 0.0) or 0.0
    if side == "YES":
        return _clamp(-v, 0.0, 1.0)   # bearish (negative) pressure threatens a YES call
    if side == "NO":
        return _clamp(v, 0.0, 1.0)    # bullish (positive) pressure threatens a NO call
    return _clamp(abs(v), 0.0, 1.0)


def compute_risk(analysis: Mapping[str, Any]) -> RiskAssessment:
    """Live flip-risk from already-computed features. Never reads the future.

    Each evidence category yields a 0..1 "manipulation-like" sub-score and an
    availability flag. The blended score is the weighted mean over *available*
    categories only, so a missing category cannot inflate the score — it only
    shrinks coverage (confidence). Returns a :class:`RiskAssessment`.
    """
    side = str(analysis.get("prediction_side") or "").upper()
    fvals = analysis.get("feature_values") or {}
    fqual = analysis.get("feature_quality") or {}
    details = analysis.get("feature_details") or {}
    regime = str((analysis.get("regime") or {}).get("name") or "")
    quote = analysis.get("quote") or {}
    returns = analysis.get("returns") or {}

    min_q = _env_float("Q15_V95_FLIP_RISK_MIN_FEATURE_QUALITY", 0.20, 0.0, 1.0)
    # (sub-score 0..1, available)
    comps: dict[str, tuple[float, bool]] = {}

    # 1) Wicks / candle structure — rejection wick opposing the side, amplified by
    #    repeated failed breakouts.
    wick_q = _num(fqual.get("wick"), 0.0) or 0.0
    thr = details.get("threshold_interaction") or {}
    wick_av = wick_q >= min_q
    wick_score = _opposes(fvals.get("wick"), side)
    failed = (side == "YES" and thr.get("failed_breakout")) or (side == "NO" and thr.get("failed_breakdown"))
    if failed:
        wick_score = _clamp(wick_score + 0.30, 0.0, 1.0)
    comps["wicks"] = (wick_score, wick_av)

    # 2) Order-book imbalance opposing the side.
    book_q = _num(fqual.get("book"), 0.0) or 0.0
    comps["orderbook"] = (_opposes(fvals.get("book"), side), book_q >= min_q)

    # 3) Spread expansion / thin liquidity.
    spread = _num(quote.get("spread_cents"))
    depth = _num(quote.get("ask_depth"))
    if spread is not None:
        wide = _env_float("Q15_V95_FLIP_RISK_WIDE_SPREAD_CENTS", 6.0, 1.0, 50.0)
        sl = _clamp(spread / wide, 0.0, 1.0)
        if depth is not None:
            thin = _env_float("Q15_V95_FLIP_RISK_THIN_DEPTH", 20.0, 1.0, 100000.0)
            sl = _clamp(sl + 0.4 * _clamp(1.0 - depth / thin, 0.0, 1.0), 0.0, 1.0)
        comps["spread_liquidity"] = (sl, True)
    else:
        comps["spread_liquidity"] = (0.0, False)

    # 4) Executed-flow: a contract/price move not supported by executed flow —
    #    absorption (aggressive flow eaten without follow-through) is the tell.
    absorption = details.get("absorption") or {}
    flow_q = _num(fqual.get("flow"), 0.0) or 0.0
    flow_av = flow_q >= min_q or bool(absorption.get("available"))
    ef = _opposes(fvals.get("flow"), side)
    if absorption.get("available") and absorption.get("absorbed"):
        ef = _clamp(ef + 0.5, 0.0, 1.0)
    comps["executed_flow"] = (ef, flow_av)

    # 5) Spot momentum disagreeing with the predicted direction.
    mom_q = _num(fqual.get("momentum"), 0.0) or 0.0
    comps["spot_momentum"] = (_opposes(fvals.get("momentum"), side), mom_q >= min_q)

    # 6) Prediction-market vs spot divergence: contract price disagreeing with the
    #    model's own probability, and cross-exchange spot divergence.
    exch = details.get("exchange_consensus") or {}
    src_count = _num(exch.get("source_count"), 0.0) or 0.0
    div_bps = _num(exch.get("divergence_bps"), 0.0) or 0.0
    market_yes = _num(analysis.get("market_implied_yes_probability"))
    model_yes = _num(analysis.get("selected_probability"))
    pd_av = src_count > 0 or market_yes is not None
    div_threshold = _env_float("Q15_V95_FLIP_RISK_DIVERGENCE_BPS", 35.0, 1.0, 500.0)
    pd = _clamp(div_bps / div_threshold, 0.0, 1.0)
    if market_yes is not None and model_yes is not None:
        # Market leaning opposite the model (relative to the side) adds divergence.
        market_for_side = market_yes if side == "YES" else 1.0 - market_yes
        model_for_side = model_yes if side == "YES" else 1.0 - model_yes
        gap = _clamp((model_for_side - market_for_side) * 2.0, 0.0, 1.0)
        pd = _clamp(max(pd, gap), 0.0, 1.0)
    comps["prediction_divergence"] = (pd, pd_av)

    # 7) Strike pin / repeated crossings.
    pin_av = bool(thr.get("available")) or regime in {"THRESHOLD_PIN", "RANGE_REVERSAL"}
    crossings = _num(thr.get("crossings"), 0.0) or 0.0
    pin = 0.0
    if regime == "THRESHOLD_PIN":
        pin = 0.8
    elif crossings >= 5:
        pin = 0.5
    elif crossings >= 3:
        pin = 0.3
    comps["pin"] = (pin, pin_av)

    # Blend: weighted mean over available categories (missing -> excluded from the
    # score but counted against confidence/coverage).
    total_weight = sum(CATEGORY_WEIGHTS.values())
    avail_weight = 0.0
    score_acc = 0.0
    components_out: dict[str, float] = {}
    for cat, (sub, available) in comps.items():
        w = CATEGORY_WEIGHTS[cat]
        components_out[cat] = sub if available else 0.0
        if available:
            avail_weight += w
            score_acc += w * sub
    score = (score_acc / avail_weight * 100.0) if avail_weight > 0 else 0.0
    confidence = (avail_weight / total_weight * 100.0) if total_weight > 0 else 0.0

    # Evidence categories: available AND meaningfully elevated (the "independent
    # evidence categories" the alert gate counts; a single one must not alert).
    evidence_min = _env_float("Q15_V95_FLIP_RISK_EVIDENCE_MIN", 0.45, 0.0, 1.0)
    evidence = [cat for cat, (sub, av) in comps.items() if av and sub >= evidence_min]
    # Primary reason = the available category with the largest weighted contribution.
    primary_cat = max(
        (c for c, (s, a) in comps.items() if a),
        key=lambda c: CATEGORY_WEIGHTS[c] * comps[c][0],
        default=None,
    )
    primary = CATEGORY_LABELS.get(primary_cat, "insufficient evidence") if (primary_cat and comps[primary_cat][0] > 0) else "no elevated evidence"
    direction = None
    if side in {"YES", "NO"}:
        direction = f"{side} → {'NO' if side == 'YES' else 'YES'}"

    return RiskAssessment(
        score=score, confidence=confidence, evidence_categories=evidence,
        primary_reason=primary, direction_monitored=direction, components=components_out,
    )


# --- score buckets ------------------------------------------------------------
def bucket_label(score: float) -> str:
    """10-point bucket label, e.g. 70 -> '70-79', 95 -> '90-100'."""
    s = _clamp(float(score), 0.0, 100.0)
    if s >= 90.0:
        return "90-100"
    low = int(s // 10) * 10
    return f"{low}-{low + 9}"


BUCKET_ORDER = ["0-9", "10-19", "20-29", "30-39", "40-49",
                "50-59", "60-69", "70-79", "80-89", "90-100"]


# --- learned threshold resolution --------------------------------------------
@dataclass
class ThresholdResolution:
    threshold: float            # 0..100; score at/above which flip risk is "high"
    source: str                 # human label, e.g. "BTC 10M NO → YES" or "Overall 10M NO → YES history"
    status: str                 # "Learned" | "Preliminary"
    samples: int
    required: int


def resolve_threshold(
    asset_stats: Mapping[str, Any] | None,
    overall_stats: Mapping[str, Any] | None,
    *, asset: str, checkpoint: str, direction: str,
) -> ThresholdResolution:
    """Pick the learned threshold for (asset, checkpoint, direction).

    Uses the asset-specific learned threshold only once it has >= the minimum
    sample count; otherwise falls back to the overall (all-asset) history for that
    checkpoint+direction. Never lowers a threshold just to alert more — an absent
    learned value falls back to the conservative default, not a looser one.
    """
    required = _env_int("Q15_V95_FLIP_MIN_SAMPLES", 30, 1, 100000)
    default_threshold = _env_float("Q15_V95_FLIP_DEFAULT_THRESHOLD", 80.0, 0.0, 100.0)
    a_n = int((asset_stats or {}).get("samples") or 0)
    a_thr = _num((asset_stats or {}).get("threshold"))
    if a_n >= required and a_thr is not None:
        return ThresholdResolution(a_thr, f"{asset} {checkpoint} {direction}", "Learned", a_n, required)
    o_n = int((overall_stats or {}).get("samples") or 0)
    o_thr = _num((overall_stats or {}).get("threshold"))
    if o_thr is not None and o_n > 0:
        return ThresholdResolution(o_thr, f"Overall {checkpoint} {direction} history", "Preliminary", a_n, required)
    return ThresholdResolution(default_threshold, f"Default {checkpoint}", "Preliminary", a_n, required)


def estimate_flip_probability(
    score: float, bucket_rates: Mapping[str, Any] | None,
) -> float | None:
    """Historical flip rate of the current score's bucket (0..1), or None if no
    data for that bucket yet. This is NOT the risk score — it is the learned
    chance of an actual flip given that risk level."""
    if not bucket_rates:
        return None
    row = bucket_rates.get(bucket_label(score))
    if not row:
        return None
    rate = _num(row.get("flip_rate"))
    return rate


# --- alert states -------------------------------------------------------------
NORMAL = "NORMAL"
WATCH = "WATCH"
HIGH_FLIP_RISK = "HIGH FLIP RISK"
CONFIRMED_FLIP = "CONFIRMED PREDICTION FLIP"


@dataclass
class AlertDecision:
    state: str
    should_send: bool
    reason: str
    persistence: int


def evaluate_alert(
    *,
    risk: RiskAssessment,
    threshold: ThresholdResolution,
    flip_probability: float | None,
    prior: Mapping[str, Any] | None,
    now: float,
) -> tuple[AlertDecision, dict[str, Any]]:
    """The gated alert state machine with persistence, hysteresis and cooldown.

    ``prior`` is this (asset, checkpoint, ticker)'s carried state from previous
    cycles; the returned dict is the new state to carry forward. A HIGH FLIP RISK
    Telegram send requires ALL of: score >= threshold, the condition held for
    >= N consecutive observations, >= 2 independent evidence categories, flip
    probability above the configured minimum, sufficient confidence, and no prior
    send for the same warning (cooldown + hysteresis + re-arm rules).
    """
    prior = dict(prior or {})
    persist_needed = _env_int("Q15_V95_FLIP_PERSISTENCE", 3, 1, 50)
    min_categories = _env_int("Q15_V95_FLIP_MIN_CATEGORIES", 2, 1, 7)
    min_flip_prob = _env_float("Q15_V95_FLIP_MIN_PROBABILITY", 0.0, 0.0, 1.0)
    min_confidence = _env_float("Q15_V95_FLIP_MIN_CONFIDENCE", 40.0, 0.0, 100.0)
    cooldown = _env_float("Q15_V95_FLIP_ALERT_COOLDOWN_SECONDS", 90.0, 0.0, 3600.0)
    hysteresis = _env_float("Q15_V95_FLIP_HYSTERESIS_POINTS", 10.0, 0.0, 100.0)
    re_arm_jump = _env_float("Q15_V95_FLIP_REARM_POINTS", 15.0, 0.0, 100.0)
    watch_margin = _env_float("Q15_V95_FLIP_WATCH_MARGIN_POINTS", 10.0, 0.0, 100.0)

    thr = threshold.threshold
    above = risk.score >= thr
    # Persistence: count consecutive observations at/above threshold.
    persistence = (int(prior.get("persistence") or 0) + 1) if above else 0

    # Hysteresis latch: once active, stays "armed" until score drops well below.
    latched = bool(prior.get("latched"))
    if latched and risk.score <= thr - hysteresis:
        latched = False

    new_state = dict(prior)
    new_state["persistence"] = persistence
    new_state["latched"] = latched

    # Determine display state first (no send for NORMAL/WATCH).
    if above:
        display = HIGH_FLIP_RISK
    elif risk.score >= thr - watch_margin:
        display = WATCH
    else:
        display = NORMAL

    gates_pass = (
        above
        and persistence >= persist_needed
        and len(risk.evidence_categories) >= min_categories
        and (flip_probability is not None and flip_probability >= min_flip_prob)
        and risk.confidence >= min_confidence
    )
    if not gates_pass:
        reason = "below_threshold" if not above else (
            "persistence" if persistence < persist_needed else
            "evidence_categories" if len(risk.evidence_categories) < min_categories else
            "flip_probability" if (flip_probability is None or flip_probability < min_flip_prob) else
            "confidence"
        )
        return AlertDecision(display, False, reason, persistence), new_state

    # Dedup / cooldown / hysteresis / re-arm: at most one warning per asset+contract
    # unless score jumped >= re_arm_jump since last alert, a new stronger evidence
    # category appeared, or the latch had reset (score fell below hysteresis floor).
    last_alert_at = _num(prior.get("last_alert_at"))
    last_alert_score = _num(prior.get("last_alert_score"))
    last_categories = set(prior.get("last_categories") or [])
    already = bool(prior.get("alerted")) and latched
    new_category = bool(set(risk.evidence_categories) - last_categories)
    big_jump = last_alert_score is not None and (risk.score - last_alert_score) >= re_arm_jump
    cooled = last_alert_at is None or (now - last_alert_at) >= cooldown

    if already and not (big_jump or new_category):
        return AlertDecision(display, False, "deduplicated", persistence), new_state
    if not cooled and not big_jump:
        return AlertDecision(display, False, "cooldown", persistence), new_state

    new_state.update({
        "alerted": True, "latched": True, "last_alert_at": now,
        "last_alert_score": risk.score, "last_categories": list(risk.evidence_categories),
    })
    return AlertDecision(HIGH_FLIP_RISK, True, "fire", persistence), new_state


# --- message + dashboard formatting ------------------------------------------
def _pct(value: float | None) -> str:
    return "—" if value is None else f"{round(float(value)):d}%"


def format_high_flip_risk(*, asset: str, checkpoint: str, current_side: str,
                          risk: RiskAssessment, threshold: ThresholdResolution,
                          flip_probability: float | None, persistence: int) -> str:
    evidence = " + ".join(CATEGORY_LABELS.get(c, c) for c in risk.evidence_categories) or risk.primary_reason
    persist_needed = _env_int("Q15_V95_FLIP_PERSISTENCE", 3, 1, 50)
    return (
        f"⚠️ <b>HIGH FLIP RISK — {asset} {checkpoint}</b>\n\n"
        f"Current prediction: {current_side}\n"
        f"Possible flip: {risk.direction_monitored or '—'}\n"
        f"Manipulation risk: {_pct(risk.score)}\n"
        f"Estimated flip probability: {_pct(None if flip_probability is None else flip_probability * 100)}\n"
        f"Learned threshold: {_pct(threshold.threshold)}\n"
        f"Confirmation: {min(persistence, persist_needed)}/{persist_needed} observations\n"
        f"Evidence: {evidence}\n"
        f"Confidence: {_pct(risk.confidence)}\n\n"
        "Action: Recheck the current prediction. This is a risk warning, not a confirmed flip."
    )


def format_confirmed_flip(*, asset: str, checkpoint: str, previous_side: str,
                          new_side: str, risk_before: float | None,
                          flip_prob_before: float | None, evidence: Sequence[str]) -> str:
    main = ", ".join(CATEGORY_LABELS.get(c, c) for c in evidence) or "—"
    return (
        f"🔄 <b>CONFIRMED PREDICTION FLIP — {asset} {checkpoint}</b>\n\n"
        f"Previous prediction: {previous_side}\n"
        f"New prediction: {new_side}\n"
        f"Manipulation risk before flip: {_pct(risk_before)}\n"
        f"Estimated flip probability before flip: {_pct(None if flip_prob_before is None else flip_prob_before * 100)}\n"
        f"Main evidence: {main}"
    )


def dashboard_block(*, risk: RiskAssessment, threshold: ThresholdResolution,
                    flip_probability: float | None, state: str,
                    persistence: int) -> list[str]:
    persist_needed = _env_int("Q15_V95_FLIP_PERSISTENCE", 3, 1, 50)
    tail = "no alert" if state in (NORMAL, WATCH) else "alert sent"
    return [
        "MANIPULATION / FLIP RISK",
        f"Risk score: {_pct(risk.score)}",
        f"Risk confidence: {_pct(risk.confidence)}",
        f"Learned flip threshold: {_pct(threshold.threshold)}",
        f"Estimated flip probability: {_pct(None if flip_probability is None else flip_probability * 100)}",
        f"Direction monitored: {risk.direction_monitored or '—'}",
        f"Evidence categories: {len(risk.evidence_categories)}",
        f"Confirmation: {min(persistence, persist_needed)}/{persist_needed} observations",
        f"Threshold source: {threshold.source}",
        f"Status: {state} — {tail}",
    ]
