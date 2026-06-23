"""Ultoim V2 — the entry gate (PURE, fully unit-testable, no I/O).

Three independent gates decide whether a candidate fires a paper entry card:

  * gate_a (side):   NO-only by default — a YES pick fails unless ``no_only`` is off.
  * gate_b (admit):  confidence >= min AND ask within [ask_lo, ask_hi] (inclusive).
  * gate_c (edge):   conservative net edge after costs >= min_edge_cents (inclusive).

All comparators are INCLUSIVE. A missing selected-probability or ask short-circuits
to a single MISSING_DATA reason (NULL-SKIP) rather than a cascade of failures. The
math is plain cents floats (the live money path already operates in cents); no
Decimal is introduced here.

The edge comparator (gate_c) tolerates a tiny float-representation slack
(``_EDGE_EPS``): ``selected*100`` is not exactly representable for most
probabilities (e.g. ``0.58*100 - 56 == 1.999999999999993``), so a strict ``>=``
would reject a mathematically-exact 2.0¢ edge depending only on the binary
repr of ``selected`` — a silent, asymmetric boundary error on the gate's one
fitted knob. The epsilon makes the inclusive bar mean what it says.

``research_fired`` is the side-agnostic verdict (gate_b AND gate_c, ignoring the
NO-only restriction). Delivery keys ONLY on ``fired`` (which stays NO-only), but
``research_fired`` lets the runner record YES-side candidates as research rows so
YES-prone windows finally produce gradeable data — without ever delivering a YES.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

# One distinct reason code per failing test (collected, not short-circuited, so a
# recorded abstain row carries every reason it failed). MISSING_DATA / STALE_FEED
# are the two data-side abstain reasons.
REASON_CODES = (
    "WRONG_SIDE_YES",
    "CONF_BELOW_MIN",
    "ASK_BELOW_BAND",
    "ASK_ABOVE_BAND",
    "EDGE_BELOW_MIN",
    "MISSING_DATA",
    "STALE_FEED",
)

# Tolerance for the inclusive edge comparator — absorbs float-repr slack only
# (≈1e-13 in practice), never a real sub-cent shortfall.
_EDGE_EPS = 1e-9


def _clean_num(value: Any) -> float | None:
    """Coerce to a finite float, or None. Rejects bool (``float(True)==1.0`` would
    sail through every gate) and non-finite values — the gate is the contract
    boundary and must not inherit upstream type bugs."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def best_entry_cents(selected: float, cost_cents: float, cfg: Any) -> int:
    """The most we'd pay and still clear the edge bar, floored to a whole cent and
    clamped into the configured ask band. ``selected`` is the chosen-side
    probability (0..1); ``cost_cents`` is the round-trip cost estimate in cents."""
    raw = math.floor(selected * 100.0 - cost_cents - cfg.min_edge_cents)
    lo = int(math.floor(cfg.ask_lo))
    hi = int(math.floor(cfg.ask_hi))
    if raw < lo:
        raw = lo
    if raw > hi:
        raw = hi
    return int(raw)


def display_entry(selected: float, cost_cents: float, ask: float, cfg: Any) -> int:
    """The displayed "best entry … or lower" price. Never above the current market
    ask (you can always pay the ask on a marketable buy), and never below the band
    floor — ``int(ask)`` truncates toward zero, so a fractional ask like 49.9 must
    be floored and re-clamped into ``[ask_lo, ask_hi]`` lest the card advertise a
    price outside the admitted band."""
    capped = min(best_entry_cents(selected, cost_cents, cfg), int(math.floor(ask)))
    lo = int(math.floor(cfg.ask_lo))
    hi = int(math.floor(cfg.ask_hi))
    if capped < lo:
        capped = lo
    if capped > hi:
        capped = hi
    return int(capped)


def evaluate(candidate: Mapping[str, Any], cfg: Any) -> dict[str, Any]:
    """Evaluate one candidate against the three gates.

    Returns a dict with: fired(bool), research_fired(bool), reason_codes(list[str]),
    gate_a/gate_b/gate_c(bool), net_edge_cents(float|None),
    best_entry_cents(int|None). NULL-SKIP: a missing/invalid side prob or ask
    returns a single MISSING_DATA reason with everything else False/None.

    ``fired`` is the DELIVERY verdict (NO-only when ``cfg.no_only``).
    ``research_fired`` is gate_b AND gate_c only (side-agnostic) — used to record
    YES-side research candidates without ever delivering them.
    """
    side = str(candidate.get("predicted_side") or "").upper()
    sel = _clean_num(candidate.get("selected_probability"))
    ask = _clean_num(candidate.get("entry_ask_cents"))
    cost = _clean_num(candidate.get("total_cost_cents"))
    cost = 0.0 if cost is None else cost

    if sel is None or ask is None:
        return {
            "fired": False,
            "research_fired": False,
            "reason_codes": ["MISSING_DATA"],
            "gate_a": False,
            "gate_b": False,
            "gate_c": False,
            "net_edge_cents": None,
            "best_entry_cents": None,
        }

    net_edge = sel * 100.0 - ask - cost

    reason_codes: list[str] = []

    # gate_a — side. NO-only by default.
    gate_a = (not cfg.no_only) or side == "NO"
    if not gate_a:
        reason_codes.append("WRONG_SIDE_YES")

    # gate_b — admit (confidence + ask band, inclusive). Collect each sub-failure.
    gate_b_conf = sel >= cfg.min_confidence
    if not gate_b_conf:
        reason_codes.append("CONF_BELOW_MIN")
    gate_b_ask_lo = ask >= cfg.ask_lo
    gate_b_ask_hi = ask <= cfg.ask_hi
    if not gate_b_ask_lo:
        reason_codes.append("ASK_BELOW_BAND")
    if not gate_b_ask_hi:
        reason_codes.append("ASK_ABOVE_BAND")
    gate_b = gate_b_conf and gate_b_ask_lo and gate_b_ask_hi

    # gate_c — edge (inclusive, float-repr tolerant).
    gate_c = net_edge >= cfg.min_edge_cents - _EDGE_EPS
    if not gate_c:
        reason_codes.append("EDGE_BELOW_MIN")

    research_fired = gate_b and gate_c
    fired = gate_a and research_fired
    return {
        "fired": bool(fired),
        "research_fired": bool(research_fired),
        "reason_codes": reason_codes,
        "gate_a": bool(gate_a),
        "gate_b": bool(gate_b),
        "gate_c": bool(gate_c),
        "net_edge_cents": net_edge,
        "best_entry_cents": best_entry_cents(sel, cost, cfg),
    }
