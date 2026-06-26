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
    "NEAR_STRIKE_PIN",
    "EXPENSIVE_NO_ADMIT",
    "ASK_CAP_7M",
    "ASK_FLOOR_12M",
    "RESEARCH_ONLY_MARK",
    "BTC_UNCONFIRMED",
    "POSITIVE_EDGE_BLOCK",
    "RISK_LOW",
    "RISK_HIGH",
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


def best_entry_cents(selected: float, cost_cents: float, cfg: Any,
                     *, ask_hi: float | None = None) -> int:
    """The most we'd pay and still clear the edge bar, floored to a whole cent and
    clamped into the configured ask band. ``selected`` is the chosen-side
    probability (0..1); ``cost_cents`` is the round-trip cost estimate in cents.
    ``ask_hi`` overrides the band ceiling (the expensive-NO admit raises it to
    ``cfg.expensive_no_ask_hi``); default ``None`` uses ``cfg.ask_hi`` unchanged."""
    raw = math.floor(selected * 100.0 - cost_cents - cfg.min_edge_cents)
    lo = int(math.floor(cfg.ask_lo))
    hi = int(math.floor(cfg.ask_hi if ask_hi is None else ask_hi))
    if raw < lo:
        raw = lo
    if raw > hi:
        raw = hi
    return int(raw)


def display_entry(selected: float, cost_cents: float, ask: float, cfg: Any,
                  *, expensive_no: bool = False) -> int:
    """The displayed "best entry … or lower" price. Never above the current market
    ask (you can always pay the ask on a marketable buy), and never below the band
    floor — ``int(ask)`` truncates toward zero, so a fractional ask like 49.9 must
    be floored and re-clamped into ``[ask_lo, ask_hi]`` lest the card advertise a
    price outside the admitted band.

    ``expensive_no`` (the default-ON expensive-NO admit): the edge bar is
    intentionally waived for this band, so the entry IS the market ask (you are a
    price-taker at the elevated price), floored and clamped into
    ``[ask_lo, expensive_no_ask_hi]`` — NOT the edge-based cap, which would advertise
    a price below the ask that would never fill."""
    lo = int(math.floor(cfg.ask_lo))
    if expensive_no:
        hi = int(math.floor(getattr(cfg, "expensive_no_ask_hi", cfg.ask_hi)))
        capped = int(math.floor(ask))
    else:
        hi = int(math.floor(cfg.ask_hi))
        capped = min(best_entry_cents(selected, cost_cents, cfg), int(math.floor(ask)))
    if capped < lo:
        capped = lo
    if capped > hi:
        capped = hi
    return int(capped)


def evaluate(candidate: Mapping[str, Any], cfg: Any, *, interval: str | None = None,
             btc_yes_prob: float | None = None) -> dict[str, Any]:
    """Evaluate one candidate against the three gates.

    Returns a dict with: fired(bool), research_fired(bool), reason_codes(list[str]),
    gate_a/gate_b/gate_c(bool), expensive_no(bool), net_edge_cents(float|None),
    best_entry_cents(int|None). NULL-SKIP: a missing/invalid side prob or ask
    returns a single MISSING_DATA reason with everything else False/None.

    ``fired`` is the DELIVERY verdict (NO-only when ``cfg.no_only``).
    ``research_fired`` is gate_b AND gate_c only (side-agnostic) — used to record
    YES-side research candidates without ever delivering them.

    ``interval`` (keyword-only, default None) scopes two interval-aware rules:

      * the distance gate (``cfg.distance_gate_enabled``, default ON) ABSTAINS
        (suppresses delivery via ``fired``, NEVER ``research_fired`` — measurement
        keeps accruing) on a 15M NO candidate too close to the strike
        (``|distance_sigma| < distance_pin_sigma``), tagging ``NEAR_STRIKE_PIN``;

      * the expensive-NO admit (``cfg.expensive_no_enabled``, default ON) ADMITS a
        NON-15M NO candidate whose ask is in ``(ask_hi, expensive_no_ask_hi]`` even
        with sub-min stated edge — lifting the ceiling and waiving gate_c for that
        band only (``expensive_no`` true, tagged ``EXPENSIVE_NO_ADMIT``); the
        confidence floor and ask_lo still apply.

    The two are mutually exclusive (one is 15M-only, the other non-15M). The YES side
    is never affected by either. With both flags off behaviour is byte-identical to
    the plain three-gate verdict.

    ``btc_yes_prob`` (keyword-only, default None) feeds two further DEFAULT-OFF DELIVERY
    gates (both suppress ``fired`` only; ``research_fired`` is UNCHANGED):

      * the BTC-confirmation gate (``cfg.btc_confirm_enabled``): deliver only when BTC's
        contemporaneous calibrated P(YES) decisively agrees with the side (NO <= 0.5 -
        ``btc_confirm_margin``; YES >= 0.5 + margin), tagging ``BTC_UNCONFIRMED``;
        FAIL-OPEN when ``btc_yes_prob`` is None;

      * the inverse-edge gate (``cfg.require_inverse_edge``): suppress delivery on a
        non-negative stated edge, tagging ``POSITIVE_EDGE_BLOCK``.

    Both are off by default -> byte-identical. The BTC-confirmation gate is what makes the
    YES harvest safe (pair with ``no_only=false`` to admit YES only on a decisive BTC YES).
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
            "expensive_no": False,
            "net_edge_cents": None,
            "best_entry_cents": None,
        }

    net_edge = sel * 100.0 - ask - cost

    reason_codes: list[str] = []

    # gate_a — side. NO-only by default.
    gate_a = (not cfg.no_only) or side == "NO"
    if not gate_a:
        reason_codes.append("WRONG_SIDE_YES")

    # Expensive-NO ADMIT band (DEFAULT ON). For NO on a non-15M interval, an ask in
    # (ask_hi, expensive_no_ask_hi] is admitted DESPITE a sub-min stated edge: for NO
    # the model's edge is inverse and this band wins ~84% cross-ledger (n=893). It
    # lifts the ask ceiling AND waives the edge gate for THIS band only — the confidence
    # floor and ask_lo still apply, and 15M / the YES side are never touched. Mutually
    # exclusive with the 15M distance gate below (that one is 15M-only). With the flag
    # off, or any other side/interval/ask, behaviour is byte-identical.
    exp_ask_hi = getattr(cfg, "expensive_no_ask_hi", cfg.ask_hi)
    expensive_no = bool(
        getattr(cfg, "expensive_no_enabled", False)
        and side == "NO"
        and interval != "15M"
        and ask > cfg.ask_hi
        and ask <= exp_ask_hi
    )

    # 7M ASK CAP (DEFAULT OFF — see config.py). At the 7M mark only, a NO whose ask exceeds
    # cap_7m_ask_max is vetoed. NON-STATIONARY: an earlier small sample showed the >72c 7M slice
    # net-negative, but the fuller 189-fired sample reversed it (>72c is +98c/40, the cap vetoed
    # winners), so the default was flipped to OFF. When ON it OVERRIDES the expensive-NO admit at
    # 7M and suppresses BOTH delivery and research (a delivery-quality cap, not a research mark —
    # see research_fired below). 10M and the YES side are never affected. Off => byte-identical.
    ask_cap_7m = bool(
        getattr(cfg, "cap_7m_ask", False)
        and side == "NO"
        and interval == "7M"
        and ask > getattr(cfg, "cap_7m_ask_max", 72)
    )

    # gate_b — admit (confidence + ask band, inclusive). Collect each sub-failure.
    gate_b_conf = sel >= cfg.min_confidence
    if not gate_b_conf:
        reason_codes.append("CONF_BELOW_MIN")
    gate_b_ask_lo = ask >= cfg.ask_lo
    # The expensive-NO band lifts the ceiling to expensive_no_ask_hi for that band only.
    gate_b_ask_hi = ask <= cfg.ask_hi or expensive_no
    if not gate_b_ask_lo:
        reason_codes.append("ASK_BELOW_BAND")
    if not gate_b_ask_hi:
        reason_codes.append("ASK_ABOVE_BAND")
    gate_b = gate_b_conf and gate_b_ask_lo and gate_b_ask_hi

    # gate_c — edge (inclusive, float-repr tolerant). WAIVED for (a) the expensive-NO
    # band, and (b) the whole NO band when no_edge_waive is on (DEFAULT ON, owner-enabled):
    # for NO the model's stated edge is INVERSE, so requiring edge>=min cut the cheapest,
    # highest reward:risk NO WINNERS (settled record: the edge gate removed ~+$35/bet
    # picks). Confidence floor + ask band still apply; min_edge_cents is untouched so the
    # best-entry price is unchanged. Set Q15_ULTOIM_V2_NO_EDGE_WAIVE=false to restore it.
    no_edge_waive = bool(getattr(cfg, "no_edge_waive", False) and side == "NO")
    gate_c = (net_edge >= cfg.min_edge_cents - _EDGE_EPS) or expensive_no or no_edge_waive
    if not gate_c:
        reason_codes.append("EDGE_BELOW_MIN")
    if expensive_no:
        reason_codes.append("EXPENSIVE_NO_ADMIT")
    elif no_edge_waive and net_edge < cfg.min_edge_cents - _EDGE_EPS:
        reason_codes.append("NO_EDGE_WAIVE")

    # The 7M cap is a hard veto on both delivery and research for the over-cap 7M NO.
    if ask_cap_7m:
        reason_codes.append("ASK_CAP_7M")
    research_fired = gate_b and gate_c and not ask_cap_7m

    # Distance GATE (DEFAULT OFF). Scoped to 15M NO only: ABSTAIN on the near-strike
    # "pin" where cross-ledger evidence shows the NO side loses. Suppresses DELIVERY
    # (``fired``) only — ``research_fired`` is UNCHANGED so the recap's distance
    # scoreboard keeps measuring these candidates. fail-open on a missing distance.
    dist = _clean_num(candidate.get("distance_sigma"))
    near_block = bool(
        getattr(cfg, "distance_gate_enabled", False)
        and interval == "15M"
        and side == "NO"
        and dist is not None
        and abs(dist) < cfg.distance_pin_sigma
    )
    if near_block:
        reason_codes.append("NEAR_STRIKE_PIN")

    # 12M EXPENSIVE-NO delivery floor. Only active when 12M is promoted to live delivery
    # (cfg.deliver_12m): a 12M NO with ask < floor_12m_ask is the cheap near-strike LOSS zone
    # (33%/-17c/bet) -> suppress DELIVERY (``fired``) only; ``research_fired`` is unchanged so the
    # cheap rows keep accruing data. With deliver_12m off (default) 12M is research-only at the
    # runner anyway, so this is inert -> byte-identical.
    floor_12m_block = bool(
        getattr(cfg, "deliver_12m", False)
        and interval == "12M"
        and side == "NO"
        and ask < getattr(cfg, "floor_12m_ask", 65.0)
    )
    if floor_12m_block:
        reason_codes.append("ASK_FLOOR_12M")

    # Cross-asset BTC-CONFIRMATION gate (DEFAULT OFF). Suppresses DELIVERY (``fired``)
    # only -- ``research_fired`` is UNCHANGED so the recap keeps measuring. A candidate
    # delivers only when BTC's contemporaneous calibrated P(YES) (``btc_yes_prob``)
    # DECISIVELY agrees with its side: NO requires btc_yes_prob <= 0.5 - margin, YES
    # requires >= 0.5 + margin. The mushy middle (BTC pinned) and the disagreeing side are
    # blocked. FAIL-OPEN when ``btc_yes_prob`` is None (no cross-asset context) -> unchanged.
    # For a BTC candidate the runner passes BTC's own calibrated-yes -> a self-decisiveness
    # check (decisive BTC reads are the reliable ones; the mushy middle is a coin-flip).
    btc_unconfirmed = False
    risk_tier: str | None = None
    if getattr(cfg, "btc_confirm_enabled", False) and btc_yes_prob is not None:
        past = None
        if side == "NO":
            cutoff = 0.5 - getattr(cfg, "btc_confirm_margin", 0.15)
            btc_unconfirmed = not (btc_yes_prob <= cutoff)
            past = cutoff - btc_yes_prob                # how far PAST the NO cutoff (decisiveness)
        elif side == "YES":
            cutoff = 0.5 + getattr(cfg, "btc_confirm_margin_yes",
                                   getattr(cfg, "btc_confirm_margin", 0.15))
            btc_unconfirmed = not (btc_yes_prob >= cutoff)
            past = btc_yes_prob - cutoff                # how far PAST the YES cutoff
        # RISK TIER (record-only) for confirmed entries: LOW (~95% bracket) iff BTC clears the
        # cutoff by >= risk_low_btc_margin AND the book is tight (spread < risk_low_max_spread);
        # else HIGH (~81% bracket — borderline BTC or wide spread, the prime turn/early-exit set).
        if past is not None and not btc_unconfirmed:
            spread = _clean_num(candidate.get("spread_cents"))
            decisive = past >= getattr(cfg, "risk_low_btc_margin", 0.10)
            tight = spread is None or spread < getattr(cfg, "risk_low_max_spread", 3.0)
            risk_tier = "LOW" if (decisive and tight) else "HIGH"
    if btc_unconfirmed:
        reason_codes.append("BTC_UNCONFIRMED")
    if risk_tier is not None:
        reason_codes.append("RISK_LOW" if risk_tier == "LOW" else "RISK_HIGH")

    # Inverse-edge DELIVERY gate (DEFAULT OFF). Suppress DELIVERY (``fired``) for a
    # non-negative stated edge; ``research_fired`` UNCHANGED. The model's edge is INVERSE
    # near the strike -- positive-edge entries are the cheap near-strike coin-flips that
    # lose, while negative-edge (expensive, market-confident) entries win. A missing edge
    # already short-circuits to MISSING_DATA above, so this only fires on a real >= 0 edge.
    positive_edge_block = bool(
        getattr(cfg, "require_inverse_edge", False)
        and net_edge >= 0.0
    )
    if positive_edge_block:
        reason_codes.append("POSITIVE_EDGE_BLOCK")

    fired = (gate_a and research_fired and not near_block and not floor_12m_block
             and not btc_unconfirmed and not positive_edge_block)
    return {
        "fired": bool(fired),
        "research_fired": bool(research_fired),
        "reason_codes": reason_codes,
        "gate_a": bool(gate_a),
        "gate_b": bool(gate_b),
        "gate_c": bool(gate_c),
        "expensive_no": bool(expensive_no),
        "net_edge_cents": net_edge,
        "risk_tier": risk_tier,
        "best_entry_cents": best_entry_cents(sel, cost, cfg,
                                             ask_hi=(exp_ask_hi if expensive_no else None)),
    }
