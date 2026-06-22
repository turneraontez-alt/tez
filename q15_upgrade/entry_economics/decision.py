"""Entry decision: ENTER / WAIT / SKIP (entry-econ-v1).

Three outcomes only, with an explicit, ordered rationale:

  SKIP  — the trade is not economically valid and waiting will not fix it:
          no executable price, an unstable/invalidated side, stale or missing
          critical data, no positive conservative edge at any acceptable price,
          or too little time to justify the risk.
  WAIT  — the prediction is still valid but the entry is not (yet) good: the
          contract is overpriced, the spread is too wide, liquidity is weak,
          confirmation is incomplete, manipulation may create a better entry, or
          the target entry range has not been reached.
  ENTER — all six conditions hold: the side is valid, the executable price is
          within the acceptable range, the conservative net edge clears the
          required margin (and EV-after-uncertainty is non-negative), liquidity
          is sufficient, flip/manipulation risk does not invalidate the EV, and
          the data is current and reliable.

This module never forces an ENTER to raise alert frequency, and never SKIPs
solely because one non-essential source (e.g. order-book depth) is missing — a
missing non-essential input lowers capacity/confidence and yields WAIT, not SKIP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import EntryEconConfig
from .economics import TradeEconomics

ENTER = "ENTER"
WAIT = "WAIT"
SKIP = "SKIP"

# Human-readable blocker phrases (compact, for the panel's "Main blocker" line).
_BLOCKER_TEXT = {
    "no_executable_price": "No reliable executable price",
    "side_invalid": "Side not valid",
    "side_unstable": "Side unstable (flip risk)",
    "data_stale": "Data stale",
    "data_incomplete": "Critical data missing",
    "too_little_time": "Too little time",
    "no_positive_edge": "No positive edge at any fair price",
    "price_too_high": "Price too high",
    "spread_too_wide": "Spread too wide",
    "insufficient_depth": "Liquidity too thin",
    "wait_confirmation": "Confirmation incomplete",
    "manipulation_better_entry": "Manipulation — wait for better entry",
    "ev_uncertainty_negative": "Edge too thin after uncertainty",
    "range_not_reached": "Target entry not reached",
}


def blocker_text(code: str | None) -> str:
    if not code:
        return "—"
    return _BLOCKER_TEXT.get(code, code.replace("_", " ").capitalize())


@dataclass
class EntryDecision:
    decision: str                      # ENTER | WAIT | SKIP
    main_blocker: str | None           # blocker code (None when ENTER)
    reasons: list[str] = field(default_factory=list)
    conditions: dict[str, bool] = field(default_factory=dict)

    @property
    def main_blocker_text(self) -> str:
        return blocker_text(self.main_blocker)


def decide(
    *,
    econ: TradeEconomics | None,
    cfg: EntryEconConfig,
    checkpoint: str | None,
    side: str | None,
    conservative_side_prob: float | None,
    seconds_remaining: float | None,
    spread_cents: float | None,
    depth_contracts: float | None,
    quote_age_seconds: float | None,
    data_quality: float | None,
    min_data_quality: float | None,
    critical_data_missing: bool,
    flip_score: float | None,
    flip_invalidates_side: bool,
    manipulation_suspected: bool,
    manipulation_against_side: bool | None,
    confirmation_incomplete: bool,
) -> EntryDecision:
    """Return the ENTER/WAIT/SKIP decision with its ordered rationale."""
    reasons: list[str] = []
    cond: dict[str, bool] = {}

    required = cfg.required_edge_for(checkpoint or "")

    # ---- hard SKIP conditions (waiting cannot fix these) ----
    if econ is None or econ.executable_ask_cents is None:
        return EntryDecision(SKIP, "no_executable_price",
                             ["no executable ask available"], {"executable_price": False})
    cond["executable_price"] = True

    # Side validity: the conservative view must still favour the side, and the
    # side must not be invalidated by flip risk.
    p_cons = econ.conservative_side_prob if conservative_side_prob is None else conservative_side_prob
    side_favoured = side in {"YES", "NO"} and (p_cons is not None and p_cons >= cfg.min_conservative_side_prob)
    cond["side_valid"] = bool(side_favoured)
    if not side_favoured:
        return EntryDecision(SKIP, "side_invalid",
                             ["conservative probability does not favour the side"], cond)
    if flip_invalidates_side:
        cond["side_stable"] = False
        # An unstable side with no positive edge is a SKIP; if the economics are
        # otherwise fine, a transient flip is a WAIT (a better, calmer entry may come).
        if econ.maximum_entry_price_cents <= 0.0:
            return EntryDecision(SKIP, "side_unstable",
                                 ["flip risk invalidates the side and there is no edge"], cond)
        return EntryDecision(WAIT, "side_unstable",
                             ["flip risk too high to enter now"], cond)
    cond["side_stable"] = True

    # Data currency / completeness.
    if critical_data_missing:
        cond["data_reliable"] = False
        return EntryDecision(SKIP, "data_incomplete",
                             ["critical decision-time data is missing"], cond)
    stale = (quote_age_seconds is not None and quote_age_seconds > cfg.stale_quote_age_seconds)
    if stale:
        cond["data_reliable"] = False
        return EntryDecision(SKIP, "data_stale",
                             [f"executable price is stale ({quote_age_seconds:.0f}s old)"], cond)
    cond["data_reliable"] = True

    # Time remaining.
    if seconds_remaining is not None and seconds_remaining < cfg.min_seconds_remaining:
        cond["time_ok"] = False
        return EntryDecision(SKIP, "too_little_time",
                             ["not enough time left to justify the risk"], cond)
    cond["time_ok"] = True

    # No positive conservative edge at ANY acceptable price -> SKIP.
    if econ.maximum_entry_price_cents <= 0.0:
        cond["positive_edge_possible"] = False
        return EntryDecision(SKIP, "no_positive_edge",
                             ["conservative edge is non-positive even at a fair price"], cond)
    cond["positive_edge_possible"] = True

    # ---- from here the prediction is valid; remaining issues are WAITs ----
    # Liquidity gates.
    spread_ok = not (spread_cents is not None and spread_cents > cfg.max_spread_cents)
    # Missing depth is non-essential: unknown depth does not block (capacity is
    # just lower); only a KNOWN-thin book trips the gate.
    depth_known_thin = (depth_contracts is not None and depth_contracts < cfg.min_depth_contracts)
    cond["liquidity_ok"] = bool(spread_ok and not depth_known_thin)

    # Manipulation: when suspected against the side, prefer to wait for a cleaner
    # quote rather than chase a possibly-spoofed price.
    manip_block = bool(cfg.manipulation_blocks_enter and manipulation_suspected
                       and (manipulation_against_side is not False))
    cond["manipulation_ok"] = not manip_block

    # Edge / range / EV checks.
    price_in_range = econ.executable_ask_cents <= econ.maximum_entry_price_cents + 1e-9
    edge_ok = econ.net_edge_cents is not None and econ.net_edge_cents >= required
    ev_ok = econ.ev_after_uncertainty_cents is not None and econ.ev_after_uncertainty_cents >= 0.0
    cond["price_in_range"] = bool(price_in_range)
    cond["edge_ok"] = bool(edge_ok)
    cond["ev_after_uncertainty_ok"] = bool(ev_ok)
    cond["confirmation_complete"] = not confirmation_incomplete

    # ENTER iff every condition holds.
    if all([
        cond["side_valid"], cond["side_stable"], cond["data_reliable"], cond["time_ok"],
        cond["positive_edge_possible"], cond["liquidity_ok"], cond["manipulation_ok"],
        cond["price_in_range"], cond["edge_ok"], cond["ev_after_uncertainty_ok"],
        cond["confirmation_complete"],
    ]):
        return EntryDecision(ENTER, None, ["all entry conditions satisfied"], cond)

    # Otherwise WAIT with the most actionable blocker first.
    if not spread_ok:
        return EntryDecision(WAIT, "spread_too_wide", ["spread exceeds the limit"], cond)
    if depth_known_thin:
        return EntryDecision(WAIT, "insufficient_depth", ["order-book depth is thin"], cond)
    if manip_block:
        return EntryDecision(WAIT, "manipulation_better_entry",
                             ["manipulation suspected; a better entry may appear"], cond)
    if confirmation_incomplete:
        return EntryDecision(WAIT, "wait_confirmation", ["confirmation not yet complete"], cond)
    if not price_in_range:
        return EntryDecision(WAIT, "price_too_high",
                             ["ask is above the maximum acceptable entry"], cond)
    if not edge_ok:
        return EntryDecision(WAIT, "range_not_reached",
                             ["edge below required margin at the current price"], cond)
    if not ev_ok:
        return EntryDecision(WAIT, "ev_uncertainty_negative",
                             ["edge too thin after the uncertainty penalty"], cond)
    # Fallback (should be unreachable): treat as WAIT, never a forced ENTER.
    return EntryDecision(WAIT, "price_too_high", ["entry not currently favourable"], cond)
