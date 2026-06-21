"""ShadowPredictor — ties features → model → calibration → decision into the
eight required outputs for a single contract.

Produces, per the Primary Objective:
  1. P(Yes)  2. P(No)  3. confidence  4. edge vs market
  5. net edge after costs  6. trade/no-trade  7. top factors  8. warnings

Read-only and side-effect-free: it does not persist anything (that's the
ShadowLedger) and never executes a trade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from . import features as featmod
from .calibration import IdentityCalibrator
from .decision import Decision, evaluate
from .mathx import clamp


@dataclass
class ChallengerPrediction:
    prob_yes: float
    prob_no: float
    confidence: float
    edge_vs_market: float | None          # model_yes_prob - market_yes_prob (prob units)
    net_edge_cents: float | None          # after fees/spread/slippage/uncertainty
    recommendation: str                   # BUY_YES | BUY_NO | NO_TRADE
    top_factors: list[dict[str, Any]]
    warnings: list[str]
    # raw context for logging / scoring
    raw_prob_yes: float = 0.5
    market_yes_prob: float | None = None
    decision: Decision | None = None
    feature_details: dict[str, float] = field(default_factory=dict)
    feature_vector: Any = None            # the full FeatureVector (decision-time context)


def _confidence(fv: featmod.FeatureVector, p_yes: float, trained: bool) -> float:
    decisiveness = 2.0 * abs(p_yes - 0.5)
    coverage = fv.coverage_fraction
    dq = fv.data_quality if fv.data_quality is not None else coverage
    base = 0.4 * coverage + 0.3 * dq + 0.3 * decisiveness
    return clamp((1.0 if trained else 0.4) * base, 0.0, 1.0)


def _top_factors(model, fv: featmod.FeatureVector, k: int = 5) -> list[dict[str, Any]]:
    contribs = model.contributions(fv.values) if hasattr(model, "contributions") else {}
    if contribs:
        ranked = sorted(contribs.items(), key=lambda kv: abs(kv[1]), reverse=True)[:k]
        return [
            {"factor": name, "value": round(fv.details.get(name, 0.0), 4),
             "effect": round(effect, 4),
             "direction": "supports_yes" if effect > 0 else "supports_no"}
            for name, effect in ranked
        ]
    imp = model.feature_importance() if hasattr(model, "feature_importance") else {}
    if imp:
        ranked = sorted(imp.items(), key=lambda kv: kv[1], reverse=True)[:k]
        return [{"factor": name, "value": round(fv.details.get(name, 0.0), 4),
                 "importance": round(score, 4)} for name, score in ranked]
    return []


class ShadowPredictor:
    def __init__(self, config, model, calibrator=None):
        self.config = config
        self.model = model
        self.calibrator = calibrator or IdentityCalibrator()

    def predict(self, snapshot: Mapping[str, Any]) -> ChallengerPrediction:
        cfg = self.config
        fv = featmod.extract(snapshot)
        trained = bool(getattr(self.model, "fitted", False))
        warnings: list[str] = []

        if trained:
            raw = self.model.predict_proba_one(fv.values)
        else:
            # Cold start before any training: defer to the market-implied price.
            raw = fv.market_yes_prob if fv.market_yes_prob is not None else 0.5
            warnings.append("model_untrained_cold_start")

        cal = self.calibrator.transform(raw)
        p_yes = clamp(cal, 0.01, 0.99)
        p_no = 1.0 - p_yes

        confidence = _confidence(fv, p_yes, trained)
        uncertainty = 1.0 - confidence

        dec = evaluate(cfg, p_yes, fv, uncertainty)
        edge_vs_market = (p_yes - fv.market_yes_prob) if fv.market_yes_prob is not None else None

        return ChallengerPrediction(
            prob_yes=round(p_yes, 6),
            prob_no=round(p_no, 6),
            confidence=round(confidence, 4),
            edge_vs_market=round(edge_vs_market, 6) if edge_vs_market is not None else None,
            net_edge_cents=round(dec.net_edge_cents, 4) if dec.net_edge_cents is not None else None,
            recommendation=dec.action,
            top_factors=_top_factors(self.model, fv),
            warnings=warnings + dec.warnings,
            raw_prob_yes=round(raw, 6),
            market_yes_prob=fv.market_yes_prob,
            decision=dec,
            feature_details=fv.details,
            feature_vector=fv,
        )
