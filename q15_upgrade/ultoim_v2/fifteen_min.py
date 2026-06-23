"""Ultoim V2 — 15M selective-entry research SCREEN (PURE, record-only, ZERO decision effect).

Scoped to the 15-minute checkpoint NO picks ONLY. For every 15M NO candidate the
runner stamps whether a SELECTIVE entry gate — derived from a five-agent study of the
real settled ledgers — *would* have fired (``s15_pass``), plus the status of two
orthogonal "tilt" signals, so the rule accrues prospective, gradeable data. It is
RECORD-ONLY: NOTHING here ever changes ``fired``, sizing, the alert, delivery, or v2's
behaviour at the 10M / 7M marks — it returns all-None for those (inert outside 15M-NO).

THE RULE (research hypothesis, NOT promoted). At 15M the contract is a near-the-money
coin flip, so realised P&L comes from SELECTION, not forecasting. On the v95 ledger
(147 settled 15M NO picks, one ~11h session) the unfiltered 15M NO book was
-2.05c/pick; the conditions below isolated the profitable subset and each survived a
time-split:
  * LUKEWARM   selected_probability < ~0.55       — confident 15M NO picks LOSE (inverse).
  * CHEAP      ask in [~47, 60)                    — expensive NOs (>=60c) are ~-13c/pick.
  * CAL_DRIFT  calibrated_yes - raw_yes <= -0.03   — recalibration independently leaning
               NO; orthogonal to LUKEWARM/CHEAP (tilt, not a hard core condition).
  * FRESH      seconds_remaining >= 875            — a fresh 15M quote (not a late
               snapshot) settles far better; also raised TOTAL P&L (tilt).

``s15_pass`` is the CORE verdict (LUKEWARM & CHEAP) — the believable, higher-coverage
workhorse. The two tilts are recorded as codes (and ``s15_cal_drift`` as a number) so
the stacked variants can be measured later WITHOUT collapsing sample size now.

WHY RECORD-ONLY — the rule is a SINGLE-SESSION fit on a sister ledger (~147 picks).
The direction is consistent and mechanistic, but the exact thresholds are not bankable.
Promotion requires clearing ``validate.py``'s bar (n >= min_promote_n, Wilson-lower >
base rate, exact-binomial p < 0.05) on the PROSPECTIVE, cross-session record before any
of this could ever gate a real decision.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

# Bump when the conditions or their defaults change, so a later analysis never mixes
# rows scored under different definitions.
S15_VERSION = "lukewarm-cheap-1"

# The codes a 15M-NO candidate can carry. Positive framing: a code is present when the
# condition HELD. ``s15_pass`` requires the two CORE codes (LUKEWARM and CHEAP).
CORE_CODES = ("LUKEWARM", "CHEAP")
TILT_CODES = ("CAL_DRIFT", "FRESH")


def _num(value: Any) -> float | None:
    """Coerce to a finite float, rejecting bool and non-finite — these feed a gate, so
    a stray True/NaN must become None (skipped), never silently 1.0 / nan."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def cal_drift(calibrated_yes: Any, raw_yes: Any) -> float | None:
    """Calibration drift = calibrated_yes - raw_yes. Negative means the calibrator
    independently pulled the YES estimate DOWN (i.e. toward NO). Decision-time only;
    None if either input is missing/non-finite."""
    cy = _num(calibrated_yes)
    ry = _num(raw_yes)
    if cy is None or ry is None:
        return None
    return cy - ry


def applies(interval: Any, predicted_side: Any) -> bool:
    """The screen is scoped to the 15M checkpoint NO side ONLY. Everything else is
    inert (10M/7M behaviour and YES rows are never touched)."""
    return str(interval) == "15M" and str(predicted_side or "").upper() == "NO"


def evaluate_15m(row: Mapping[str, Any], interval: Any, cfg: Any) -> dict[str, Any]:
    """Evaluate the 15M selective research gate for one candidate. PURE.

    Returns ``{applicable, passed, codes, cal_drift}``. For non-15M or non-NO
    candidates ``applicable`` is False and the verdict is None (inert). A missing
    selected-probability or ask short-circuits to a single MISSING_DATA code with
    ``passed`` False — exactly like the live gate's NULL-SKIP.
    """
    if not applies(interval, row.get("predicted_side")):
        return {"applicable": False, "passed": None, "codes": None, "cal_drift": None}

    sel = _num(row.get("selected_probability"))
    ask = _num(row.get("entry_ask_cents"))
    drift = cal_drift(row.get("calibrated_yes_probability"), row.get("raw_yes_probability"))
    secs = _num(row.get("seconds_remaining"))

    if sel is None or ask is None:
        return {"applicable": True, "passed": False,
                "codes": ["MISSING_DATA"], "cal_drift": drift}

    codes: list[str] = []
    if sel < cfg.s15_sel_max:                                   # LUKEWARM (strict <)
        codes.append("LUKEWARM")
    if cfg.s15_ask_lo <= ask < cfg.s15_ask_hi:                  # CHEAP band [lo, hi)
        codes.append("CHEAP")
    if drift is not None and drift <= cfg.s15_cal_drift_max:    # CAL_DRIFT tilt
        codes.append("CAL_DRIFT")
    if secs is not None and secs >= cfg.s15_fresh_min_seconds:  # FRESH tilt
        codes.append("FRESH")

    passed = all(c in codes for c in CORE_CODES)
    return {"applicable": True, "passed": passed, "codes": codes, "cal_drift": drift}


def features(row: Mapping[str, Any], interval: Any, cfg: Any) -> dict[str, Any]:
    """Derive the ledger columns for one candidate row. Returns the exact dict the
    ledger stores: ``s15_pass`` (1/0/None), ``s15_codes`` (csv/None), ``s15_cal_drift``
    (float/None), ``s15_version`` (stamp/None). Pure; all-None when not applicable."""
    v = evaluate_15m(row, interval, cfg)
    if not v["applicable"]:
        return {"s15_pass": None, "s15_codes": None, "s15_cal_drift": None,
                "s15_version": None}
    return {
        "s15_pass": 1 if v["passed"] else 0,
        "s15_codes": ",".join(v["codes"]) if v["codes"] else None,
        "s15_cal_drift": v["cal_drift"],
        "s15_version": S15_VERSION,
    }
