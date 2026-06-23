"""Ultoim V2 — blowup-risk SHADOW screen (PURE functions, no I/O, ZERO decision effect).

This module computes a per-candidate ``blowup_risk`` from features that are ALREADY
recorded at decision time (the model's own internal uncertainty ``conf_gap`` and its
``evidence_quality``). It is **record-only**: the runner stamps the score onto every
recorded row so the idea can accrue prospective, gradeable data, but NOTHING here ever
changes ``fired``, sizing, the alert, or delivery. The promotion bar in ``validate.py``
(n>=min_promote_n, Wilson-lower > base, exact-binomial p<0.05) plus a demonstrated
OUT-OF-SAMPLE separation must be cleared before any of this could influence a decision.

WHY SHADOW-ONLY — the honest stress-test record (2026-06-23 07:20 UTC snapshot, a
four-agent adversarial review of the v95 ledger + the independent ultoim_v2 ledger):
  * NO LEAKAGE: the inputs are point-in-time (``checkpoint_v95.analyse_v95`` computes
    ``conservative`` from data-quality/regime/divergence, not from the outcome; the
    settlement UPDATE touches only result columns). Good — but that is the only thing
    that fully passed.
  * INERT OUT-OF-SAMPLE: a walk-forward over 102 prospective decisions skipped 0
    winners AND 0 losers (P&L delta exactly +0.0c). The 3 losers it "catches"
    in-sample all predate any deployable decision.
  * DID NOT TRANSPORT: blowup_risk AUC was 0.671 in-sample on v95 but 0.491 (chance)
    on the independent ultoim_v2 ledger; conf_gap alone 0.460 there. The constants
    below were fit on v95 and evaluated on the same rows (double-dipping).
  * EFFECTIVE n ~= 1: the 3 in-sample "saves" share one settlement window (one
    correlated crypto move), and the threshold sat exactly on the top winner's score
    (zero margin). The 0.30 evidence term added ~0.002 AUC over conf_gap alone.
  * The one signal that DID transport across ledgers was ``distance_sigma`` (distance
    to strike): AUC ~0.615 on ultoim_v2, with |sigma|>=0.15 -> ~0% loss. It is recorded
    independently on every row already and is reported alongside blowup_risk by
    ``validate.screen_shadow_report`` so the better candidate signal accrues data too.

So: these constants are retained ONLY to log a reproducible shadow score; they are
NOT validated and MUST NOT gate anything until re-derived and proven prospectively on
the delivered population with a held-out test.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

# Bump when the formula or its constants change — lets the ledger distinguish rows
# scored under different definitions so a later analysis never mixes them.
SCREEN_VERSION = "blowup-shadow-1"

# --- constants: IN-SAMPLE FIT on the v95 ledger; DID NOT TRANSPORT (see docstring) ---
# conf_gap is normalised over [floor, floor+span]; evidence_quality is inverted over
# [floor, floor+span] (low evidence => high risk). Weights blend the two (0.70/0.30).
_CG_FLOOR = 0.07
_CG_SPAN = 0.06
_EV_FLOOR = 0.76
_EV_SPAN = 0.20
_W_CONF_GAP = 0.70
_W_EVIDENCE = 0.30


def _num(value: Any) -> float | None:
    """Coerce to a finite float, rejecting bool and non-finite — these feed a score,
    so a stray True/NaN must become None (skipped), never silently 1.0 / nan."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def conf_gap(selected_probability: Any, conservative_probability: Any) -> float | None:
    """The model's own internal uncertainty: how far its SELECTED-side probability
    sits above its CONSERVATIVE (uncertainty-discounted) probability. Larger gap =>
    the model is leaning harder than its own worst-case supports. Decision-time only;
    None if either input is missing/non-finite."""
    sel = _num(selected_probability)
    con = _num(conservative_probability)
    if sel is None or con is None:
        return None
    return sel - con


def blowup_risk(selected_probability: Any, conservative_probability: Any,
                evidence_quality: Any) -> float | None:
    """A 0..1 shadow blowup-risk score. PURE; record-only; never gates a decision.
    None if conf_gap is uncomputable (a missing-evidence row still scores off the
    conf_gap term, treating absent evidence as worst-case for that 0.30 component)."""
    cg = conf_gap(selected_probability, conservative_probability)
    if cg is None:
        return None
    cg_n = _clip((cg - _CG_FLOOR) / _CG_SPAN, 0.0, 1.0)
    ev = _num(evidence_quality)
    ev_inv_n = 1.0 if ev is None else 1.0 - _clip((ev - _EV_FLOOR) / _EV_SPAN, 0.0, 1.0)
    return _W_CONF_GAP * cg_n + _W_EVIDENCE * ev_inv_n


def distance_risk(distance_sigma: Any) -> float | None:
    """The transportable companion signal: risk rises as spot sits ON the strike
    (|distance_sigma| -> 0, the THRESHOLD_PIN coin-flip). Returned as a monotone
    risk score (higher = riskier) so it can be AUC'd the same way as blowup_risk.
    None if distance is unavailable."""
    ds = _num(distance_sigma)
    if ds is None:
        return None
    return -abs(ds)


def size_fraction(risk: Any) -> float:
    """Confidence-aware sizing fraction (1 - risk)^2, clamped to [0, 1]. PURE and
    currently INERT — provided so a future, PROVEN screen can shrink dollar exposure
    on the un-screenable tail instead of a hard skip. Not stored, not wired to
    anything. A missing/invalid risk yields full size (1.0), never a surprise zero."""
    r = _num(risk)
    if r is None:
        return 1.0
    return _clip((1.0 - _clip(r, 0.0, 1.0)) ** 2, 0.0, 1.0)


def shadow_features(row: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the shadow columns for one candidate/row from its decision-time fields.
    Returns the exact dict the ledger stores: ``conf_gap``, ``blowup_risk``, and the
    ``screen_version`` stamp. Pure; safe on partial rows (missing inputs -> None)."""
    sel = row.get("selected_probability")
    con = row.get("conservative_probability")
    ev = row.get("evidence_quality")
    return {
        "conf_gap": conf_gap(sel, con),
        "blowup_risk": blowup_risk(sel, con, ev),
        "screen_version": SCREEN_VERSION,
    }
