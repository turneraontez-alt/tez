"""Entry-economics engine (entry-econ-v1) — orchestration.

Takes a decision-time view of one contract (the v9.5 analysis dict, or an
explicit input bundle for testing) and produces a single ``EntryEconomicsResult``
combining the conservative probability, the executable economics, and the
ENTER/WAIT/SKIP decision. Pure and side-effect free; the ledger/runner persist it
separately.

It is defensive by construction: missing non-essential inputs lower coverage and
capacity but never crash and never force a SKIP on their own. The ONLY thing that
makes an entry impossible is the absence of a usable executable price.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping

from .config import EntryEconConfig
from .conservative import ConservativeResult, conservative_probability
from .costs import all_in_costs, estimate_fill
from .decision import EntryDecision, SKIP, decide
from .economics import TradeEconomics, compute_economics


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _normalise_cents(value: Any) -> float | None:
    v = _num(value)
    if v is None:
        return None
    if 0.0 <= v <= 1.0:
        v *= 100.0
    if 0.0 <= v <= 100.0:
        return v
    return None


@dataclass
class EntryEconomicsResult:
    decision: str                         # ENTER | WAIT | SKIP
    side: str | None
    asset: str | None
    checkpoint: str | None
    conservative: ConservativeResult | None
    economics: TradeEconomics | None
    entry_decision: EntryDecision
    version: str = "entry-econ-v1"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def main_blocker(self) -> str | None:
        return self.entry_decision.main_blocker

    @property
    def main_blocker_text(self) -> str:
        return self.entry_decision.main_blocker_text

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "decision": self.decision,
            "side": self.side,
            "asset": self.asset,
            "checkpoint": self.checkpoint,
            "main_blocker": self.main_blocker,
            "main_blocker_text": self.main_blocker_text,
            "conditions": self.entry_decision.conditions,
            "conservative": None if self.conservative is None else {
                "raw_side_prob": self.conservative.raw_side_prob,
                "conservative_side_prob": self.conservative.conservative_side_prob,
                "shrink": self.conservative.shrink,
                "flip_penalty": self.conservative.flip_penalty,
                "manip_penalty": self.conservative.manip_penalty,
                "factors": self.conservative.factors,
                "notes": self.conservative.notes,
            },
            "economics": None if self.economics is None else self.economics.as_dict(),
        }


def _pick(mappings, keys):
    """First non-None value for any key across the given mappings."""
    for key in keys:
        for m in mappings:
            if isinstance(m, Mapping) and m.get(key) is not None:
                return m.get(key)
    return None


def _side_ask(analysis: Mapping[str, Any], side: str) -> tuple[float | None, float | None, Any]:
    """Return (ask_cents, spread_cents, ask_levels) for the chosen side."""
    quote = analysis.get("quote") if isinstance(analysis.get("quote"), Mapping) else {}
    sources = (quote, analysis)
    levels = _pick(sources, (f"{side.lower()}_ask_levels",))
    # Prefer an explicit selected-side quote already computed upstream.
    ask = _normalise_cents(quote.get("ask_cents"))
    spread = _num(quote.get("spread_cents"))
    if ask is not None:
        return ask, spread, levels

    # Otherwise derive ask/bid from the yes/no book (with cross-side fallback).
    bid = None
    if side == "YES":
        ask = _normalise_cents(_pick(sources, ("yes_ask", "yes_ask_cents")))
        bid = _normalise_cents(_pick(sources, ("yes_bid", "yes_bid_cents")))
    else:
        ask = _normalise_cents(_pick(sources, ("no_ask", "no_ask_cents")))
        bid = _normalise_cents(_pick(sources, ("no_bid", "no_bid_cents")))
        if ask is None:
            yes_bid = _normalise_cents(_pick(sources, ("yes_bid", "yes_bid_cents")))
            ask = None if yes_bid is None else 100.0 - yes_bid
        if bid is None:
            yes_ask = _normalise_cents(_pick(sources, ("yes_ask", "yes_ask_cents")))
            bid = None if yes_ask is None else 100.0 - yes_ask
    if spread is None and ask is not None and bid is not None:
        spread = max(0.0, ask - bid)
    return ask, spread, levels


def evaluate_analysis(analysis: Mapping[str, Any],
                      cfg: EntryEconConfig | None = None,
                      *,
                      resolved_sample_size: int | None = None,
                      asset_reliability: float | None = None,
                      interval_reliability: float | None = None,
                      regime_reliability: float | None = None,
                      prediction_instability: float | None = None,
                      calibration_reliability: float | None = None) -> EntryEconomicsResult:
    """Evaluate a v9.5-style analysis dict into an entry-economics result.

    Recognised keys (all optional except a usable side + ask):
      prediction_side, selected_probability, yes_probability/no_probability,
      conservative_probability, checkpoint/interval, quote{ask_cents, spread_cents,
      depth_contracts, quote_age_seconds, *_ask_levels}, data_quality,
      evidence_coverage, seconds_remaining, market_implied_yes_probability,
      flip_risk{score, confidence}, manipulation{suspected, lean}, regime{name}.
    """
    cfg = cfg or EntryEconConfig.from_env()
    asset = analysis.get("asset") or analysis.get("ticker")
    checkpoint = analysis.get("checkpoint") or analysis.get("interval")
    side = analysis.get("prediction_side") or analysis.get("selected_side")
    side = str(side).upper() if side in ("YES", "NO") else (
        "YES" if str(side).upper() == "YES" else "NO" if str(side).upper() == "NO" else None)

    # Model side probability (calibrated, side-aware).
    yes_p = _num(analysis.get("yes_probability"))
    selected = _num(analysis.get("selected_probability"))
    if side == "YES":
        model_side = selected if selected is not None else yes_p
    elif side == "NO":
        model_side = selected if selected is not None else (None if yes_p is None else 1.0 - yes_p)
    else:
        model_side = None

    # Inputs for the conservative probability.
    data_quality = _num(analysis.get("data_quality"))
    coverage = _num(analysis.get("evidence_coverage"))
    market_yes = _num(analysis.get("market_implied_yes_probability"))
    market_side = None
    if market_yes is not None and side in ("YES", "NO"):
        market_side = market_yes if side == "YES" else 1.0 - market_yes
    disagreement = None
    if market_side is not None and model_side is not None:
        disagreement = model_side - market_side

    flip = analysis.get("flip_risk") if isinstance(analysis.get("flip_risk"), Mapping) else {}
    flip_score = flip.get("score")
    flip_conf = flip.get("confidence")
    manip = analysis.get("manipulation") if isinstance(analysis.get("manipulation"), Mapping) else {}
    manip_suspected = bool(manip.get("suspected"))
    manip_lean = str(manip.get("lean") or "").upper()
    manip_against = None
    if manip_suspected and side in ("YES", "NO") and manip_lean in ("YES", "NO"):
        manip_against = (manip_lean != side)
    regime = analysis.get("regime") if isinstance(analysis.get("regime"), Mapping) else {}

    if side not in ("YES", "NO") or model_side is None:
        ed = EntryDecision(SKIP, "side_invalid", ["no usable side / model probability"],
                           {"side_valid": False})
        return EntryEconomicsResult(SKIP, side, asset, checkpoint, None, None, ed,
                                    version=cfg.model_version)

    cons = conservative_probability(
        side=side, calibrated_side_prob=model_side, cfg=cfg, checkpoint=checkpoint,
        data_coverage=coverage, data_quality=data_quality,
        resolved_sample_size=resolved_sample_size,
        model_market_disagreement=disagreement,
        prediction_instability=prediction_instability,
        regime_reliability=regime_reliability,
        asset_reliability=asset_reliability,
        interval_reliability=interval_reliability,
        calibration_reliability=calibration_reliability,
        flip_score=flip_score, flip_confidence=flip_conf,
        manipulation_suspected=manip_suspected, manipulation_against_side=manip_against,
    )

    quote = analysis.get("quote") if isinstance(analysis.get("quote"), Mapping) else {}
    ask, spread, levels = _side_ask(analysis, side)
    depth = _num(quote.get("depth_contracts"))
    if depth is None:
        depth = _num(quote.get("ask_depth"))
    seconds = _num(analysis.get("seconds_remaining"))
    if seconds is None:
        seconds = _num(quote.get("seconds_remaining"))
    quote_age = _num(quote.get("quote_age_seconds"))

    required = cfg.required_edge_for(checkpoint or "")
    flip_score_norm = _norm_score(flip_score)
    flip_conf_norm = _norm_score(flip_conf)
    flip_invalidates = bool(flip_score_norm is not None and flip_score_norm >= cfg.flip_invalidate_score)

    econ = None
    if ask is not None:
        max_entry_ref = cons.conservative_side_prob * 100.0  # generous reference for depth
        fill = estimate_fill(
            best_ask_cents=ask, ask_levels=levels, spread_cents=spread, depth_contracts=depth,
            sim_contracts=cfg.sim_contracts, slippage_fallback_cents=cfg.slippage_fallback_cents,
            slippage_spread_fraction=cfg.slippage_spread_fraction, max_acceptable_cents=max_entry_ref,
        )
        costs = all_in_costs(
            ask_cents=ask, fill=fill, fee_rate=cfg.fee_rate, fee_ceil=cfg.fee_ceil,
            latency_cost_cents=cfg.latency_cost_cents, quote_age_seconds=quote_age,
            stale_penalty_cents_per_s=cfg.stale_penalty_cents_per_s,
            stale_penalty_cap_cents=cfg.stale_penalty_cap_cents,
            stale_quote_age_seconds=cfg.stale_quote_age_seconds,
        )
        econ = compute_economics(
            side=side, model_side_prob=model_side,
            conservative_side_prob=cons.conservative_side_prob,
            executable_ask_cents=ask, cost_breakdown=costs, fill=fill,
            required_margin_cents=required, recommended_band_cents=cfg.recommended_band_cents,
            risk_aversion_k=cfg.risk_aversion_k,
        )

    confirmation_incomplete = bool(analysis.get("waiting_for_confirmation")
                                   or analysis.get("low_evidence") is True
                                   and _num(analysis.get("evidence_coverage"), 1.0) < 0.5)
    critical_missing = bool(analysis.get("critical_data_missing"))

    ed = decide(
        econ=econ, cfg=cfg, checkpoint=checkpoint, side=side,
        conservative_side_prob=cons.conservative_side_prob,
        seconds_remaining=seconds, spread_cents=spread, depth_contracts=depth,
        quote_age_seconds=quote_age, data_quality=data_quality,
        min_data_quality=None, critical_data_missing=critical_missing,
        flip_score=flip_score_norm, flip_invalidates_side=flip_invalidates,
        manipulation_suspected=manip_suspected, manipulation_against_side=manip_against,
        confirmation_incomplete=confirmation_incomplete,
    )

    return EntryEconomicsResult(
        decision=ed.decision, side=side, asset=asset, checkpoint=checkpoint,
        conservative=cons, economics=econ, entry_decision=ed, version=cfg.model_version,
        extra={
            "spread_cents": spread,
            "depth_contracts": depth,
            "quote_age_seconds": quote_age,
            "seconds_remaining": seconds,
            "market_side_prob": market_side,
            "flip_score": flip_score_norm,
            "flip_invalidates_side": flip_invalidates,
            "manipulation_against_side": manip_against,
        },
    )


def _norm_score(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    if v > 1.0:
        v = v / 100.0
    return max(0.0, min(1.0, v))
