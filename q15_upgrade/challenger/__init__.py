"""Challenger system — a separate, read-only shadow model for Kalshi 15-minute
crypto binaries, built to be promoted to the primary learner with one switch.

It is COMPLETELY decoupled from the production champion: its own config, its own
SQLite ledger, no order execution, no writes to production tables. With
``Q15_CHALLENGER_ENABLED=false`` (default) nothing here runs.

Public surface
--------------
- ``ChallengerConfig``         env-driven config (all switches default OFF)
- ``ShadowPredictor``          features -> model -> calibration -> 8 outputs
- ``ShadowLedger``             immutable shadow persistence + scoreboard
- ``walk_forward_evaluate``    purged walk-forward OOS evaluation vs baselines
- ``train_predictor``          fit a frozen ShadowPredictor for live shadow use
- ``build_live_predictor``     train one straight from the shadow ledger's history
- ``primary_probability``      THE promotion seam (see below)
- ``run_shadow``               predict + record one contract in shadow

Promoting the challenger to primary (the "easy switch")
-------------------------------------------------------
1. Let it bake in shadow until ``ShadowLedger.scoreboard()`` shows it beats the
   control on out-of-sample log-loss / Brier / calibration / net cents (Phase 17).
2. Set ``Q15_CHALLENGER_AS_PRIMARY=true``.
3. At the ONE place production picks the live probability, call:

       from q15_upgrade.challenger import primary_probability
       p_yes = primary_probability(snapshot, champion_p_yes, predictor=PREDICTOR)

   With ``as_primary`` False this returns ``champion_p_yes`` unchanged (production
   is byte-identical). With it True it returns the challenger's calibrated P(Yes),
   falling back to the champion if the challenger isn't trained yet. No other code
   path changes — that single call is the entire integration.
"""

from __future__ import annotations

from typing import Any, Mapping

from .calibration import make_calibrator
from .config import ChallengerConfig
from .features import FEATURE_NAMES, FeatureVector, extract
from .harness import ordered_vector, train_predictor, walk_forward_evaluate
from .ledger import ShadowLedger
from .models import make_model
from .predictor import ChallengerPrediction, ShadowPredictor

__all__ = [
    "ChallengerConfig", "ShadowPredictor", "ShadowLedger", "ChallengerPrediction",
    "FeatureVector", "FEATURE_NAMES", "extract", "make_model", "make_calibrator",
    "walk_forward_evaluate", "train_predictor", "ordered_vector",
    "build_live_predictor", "primary_probability", "run_shadow",
]


def build_live_predictor(config: ChallengerConfig | None = None,
                         ledger: ShadowLedger | None = None,
                         checkpoint: str | None = None) -> tuple[ShadowPredictor, dict]:
    """Train a ShadowPredictor from the shadow ledger's resolved history.

    Returns (predictor, info). Predictor is UNFITTED (cold-start) until enough
    resolved rows accrue — it then defers to the market price, which is safe.
    """
    cfg = config or ChallengerConfig.from_env()
    owns = ledger is None
    led = ledger or ShadowLedger(cfg.db_path)
    try:
        ts, feats, y = led.training_samples(checkpoint=checkpoint, model_version=cfg.model_version)
        return train_predictor(ts, feats, y, cfg)
    finally:
        if owns:
            led.close()


def primary_probability(snapshot: Mapping[str, Any], champion_prob_yes: float,
                        *, predictor: ShadowPredictor | None = None,
                        config: ChallengerConfig | None = None) -> float:
    """The promotion seam. Returns the champion's probability unless the
    challenger is promoted AND trained, in which case it returns the
    challenger's calibrated P(Yes). Safe to call from the hot path.
    """
    cfg = config or ChallengerConfig.from_env()
    if not cfg.as_primary or predictor is None:
        return champion_prob_yes
    if not getattr(predictor.model, "fitted", False):
        return champion_prob_yes  # not trained yet -> stay on champion
    try:
        return predictor.predict(snapshot).prob_yes
    except Exception:
        return champion_prob_yes  # never let the challenger break the live path


def run_shadow(snapshot: Mapping[str, Any], *, predictor: ShadowPredictor,
               ledger: ShadowLedger, champion_prob_yes: float | None,
               asset: str | None, contract: str | None, checkpoint: str | None,
               settlement_source: str | None = None,
               config: ChallengerConfig | None = None) -> ChallengerPrediction:
    """Predict one contract and record it in the shadow ledger (observational)."""
    cfg = config or ChallengerConfig.from_env()
    pred = predictor.predict(snapshot)
    ledger.record(
        pred, asset=asset, contract=contract, checkpoint=checkpoint,
        control_prob_yes=champion_prob_yes, settlement_source=settlement_source,
        model_version=cfg.model_version,
    )
    return pred
