"""Challenger system — configuration.

A SEPARATE, read-only shadow model that estimates the calibrated probability a
Kalshi 15-minute crypto binary resolves YES. It never places orders, never
touches the production champion, and writes only to its own SQLite ledger.

Everything here is env-driven and **default-OFF** so production output is
byte-identical until you opt in:

  Q15_CHALLENGER_ENABLED      run the shadow at all (record predictions)   [off]
  Q15_CHALLENGER_AS_PRIMARY   promote: let the challenger drive the live    [off]
                              displayed probability instead of the champion
                              (the ONE switch to "make it the primary learner")

Both default to False. With ENABLED=False the package is inert. With
ENABLED=True / AS_PRIMARY=False it runs purely observationally beside the
champion. With AS_PRIMARY=True the integration seam returns the challenger's
calibrated probability (see q15_upgrade/challenger/__init__.py: primary_probability).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _str(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class ChallengerConfig:
    # -- master switches (default OFF) --
    enabled: bool = field(default_factory=lambda: _bool("Q15_CHALLENGER_ENABLED", False))
    as_primary: bool = field(default_factory=lambda: _bool("Q15_CHALLENGER_AS_PRIMARY", False))

    # -- feature set (research lineage; default "v5" = byte-identical live shadow) --
    # "v5" is the frozen production challenger feature vector. "v6" is the RESEARCH
    # superset (v5 + appended leakage-safe microstructure features) evaluated by
    # challenger/research.py. Promotion to "v6" is deliberate: set this to "v6" AND
    # bump Q15_CHALLENGER_MODEL_VERSION to "challenger-v6" so the new lineage's
    # comparison starts empty and never mixes with the v5 record. Default OFF.
    feature_set: str = field(default_factory=lambda: _str("Q15_CHALLENGER_FEATURE_SET", "v5"))
    # Prediction-stability EMA half-life in cycles (research option; 0 = passthrough,
    # so the live shadow is unchanged unless opted into). Reduces flip-flop.
    stability_half_life: float = field(default_factory=lambda: _float("Q15_CHALLENGER_STABILITY_HALFLIFE", 0.0))

    # -- model --
    # "logistic" (pure-python, no deps), "xgboost", "lightgbm",
    # "market_only" / "volatility_only" (baselines).
    backend: str = field(default_factory=lambda: _str("Q15_CHALLENGER_BACKEND", "logistic"))
    # "none", "platt", "isotonic" — fit on a validation fold only.
    calibration: str = field(default_factory=lambda: _str("Q15_CHALLENGER_CALIBRATION", "platt"))

    # Logistic hyperparameters (used by the pure-python backend).
    l2: float = field(default_factory=lambda: _float("Q15_CHALLENGER_L2", 10.0))
    learning_rate: float = field(default_factory=lambda: _float("Q15_CHALLENGER_LR", 0.05))
    max_iter: int = field(default_factory=lambda: _int("Q15_CHALLENGER_MAX_ITER", 500))

    # GBM hyperparameters (your starting config; used only if backend is a GBM).
    gbm_max_depth: int = field(default_factory=lambda: _int("Q15_CHALLENGER_GBM_MAX_DEPTH", 3))
    gbm_learning_rate: float = field(default_factory=lambda: _float("Q15_CHALLENGER_GBM_LR", 0.03))
    gbm_n_estimators: int = field(default_factory=lambda: _int("Q15_CHALLENGER_GBM_N_EST", 1000))
    gbm_min_child_weight: float = field(default_factory=lambda: _float("Q15_CHALLENGER_GBM_MIN_CHILD", 20.0))
    gbm_subsample: float = field(default_factory=lambda: _float("Q15_CHALLENGER_GBM_SUBSAMPLE", 0.75))
    gbm_colsample: float = field(default_factory=lambda: _float("Q15_CHALLENGER_GBM_COLSAMPLE", 0.70))
    gbm_reg_alpha: float = field(default_factory=lambda: _float("Q15_CHALLENGER_GBM_ALPHA", 0.5))
    gbm_reg_lambda: float = field(default_factory=lambda: _float("Q15_CHALLENGER_GBM_LAMBDA", 10.0))

    # -- training / validation --
    min_train_rows: int = field(default_factory=lambda: _int("Q15_CHALLENGER_MIN_TRAIN_ROWS", 200))
    # Re-train the shadow from its own resolved rows every N resolutions
    # ("learns as it goes"). 0 disables online refit.
    refit_every: int = field(default_factory=lambda: _int("Q15_CHALLENGER_REFIT_EVERY", 10))
    # Emit the per-15-min accuracy comparison (challenger vs current system).
    report_enabled: bool = field(default_factory=lambda: _bool("Q15_CHALLENGER_REPORT", True))
    n_splits: int = field(default_factory=lambda: _int("Q15_CHALLENGER_WF_SPLITS", 4))
    embargo_seconds: float = field(default_factory=lambda: _float("Q15_CHALLENGER_EMBARGO_SECONDS", 900.0))
    horizon_seconds: float = field(default_factory=lambda: _float("Q15_CHALLENGER_HORIZON_SECONDS", 900.0))

    # -- cost model (mirrors the production assumptions) --
    fee_rate: float = field(default_factory=lambda: _float("Q15_CHALLENGER_FEE_RATE", 0.07))
    slippage_spread_fraction: float = field(default_factory=lambda: _float("Q15_CHALLENGER_SLIPPAGE_FRAC", 0.50))
    slippage_fallback_cents: float = field(default_factory=lambda: _float("Q15_CHALLENGER_SLIPPAGE_FALLBACK", 0.75))
    market_impact_cents: float = field(default_factory=lambda: _float("Q15_CHALLENGER_IMPACT_CENTS", 0.25))
    uncertainty_margin_cents: float = field(default_factory=lambda: _float("Q15_CHALLENGER_UNCERTAINTY_CENTS", 1.0))
    latency_cost_cents: float = field(default_factory=lambda: _float("Q15_CHALLENGER_LATENCY_CENTS", 0.0))
    adverse_selection_cents: float = field(default_factory=lambda: _float("Q15_CHALLENGER_ADVERSE_CENTS", 0.0))

    # -- trade decision / no-trade zone --
    min_probability: float = field(default_factory=lambda: _float("Q15_CHALLENGER_MIN_PROB", 0.60))
    min_net_edge_cents: float = field(default_factory=lambda: _float("Q15_CHALLENGER_MIN_NET_EDGE_CENTS", 6.0))

    # -- risk controls (applied even in hypothetical evaluation) --
    max_spread_cents: float = field(default_factory=lambda: _float("Q15_CHALLENGER_MAX_SPREAD_CENTS", 12.0))
    min_depth_contracts: float = field(default_factory=lambda: _float("Q15_CHALLENGER_MIN_DEPTH", 3.0))
    min_seconds_remaining: float = field(default_factory=lambda: _float("Q15_CHALLENGER_MIN_SECONDS", 20.0))
    min_data_quality: float = field(default_factory=lambda: _float("Q15_CHALLENGER_MIN_DATA_QUALITY", 0.55))
    max_uncertainty: float = field(default_factory=lambda: _float("Q15_CHALLENGER_MAX_UNCERTAINTY", 0.85))
    # Conservative fractional sizing — never full Kelly.
    max_risk_fraction_per_trade: float = field(default_factory=lambda: _float("Q15_CHALLENGER_MAX_RISK_FRAC", 0.02))
    # Out-of-distribution fail-safe: at/above this score the default action is NO TRADE.
    ood_severe_threshold: float = field(default_factory=lambda: _float("Q15_CHALLENGER_OOD_SEVERE", 0.5))
    ood_block_trade: bool = field(default_factory=lambda: _bool("Q15_CHALLENGER_OOD_BLOCK", True))

    # -- persistence --
    db_path: str = field(default_factory=lambda: _str("Q15_CHALLENGER_DB", "data/q15_challenger_shadow_v1.sqlite3"))
    # The model_version doubles as the RESET key for the visible
    # Shadow-vs-Your-System comparison: every scoring query filters on it, so
    # bumping the version starts the new comparison completely empty while every
    # prior version's rows survive untouched in the same SQLite file as an internal
    # PRE-RESET archive (debug only) — never deleted, never mixed into the new
    # visible stats. Bumped v1 -> v2 -> v3 (each bump is one reset-on-deploy); the
    # v3 comparison only grades predictions recorded after the new version goes
    # live (its reset_at is stamped on first record). Override with
    # Q15_CHALLENGER_MODEL_VERSION. Bumped v3 -> v4 for the SYNCHRONIZED reset, then
    # v4 -> v5 for an owner-requested fresh start (clears the sparse post-fix record).
    # v5 only grades predictions made simultaneously from one frozen snapshot under
    # Eastern-Time display. Every prior version (v1..v4) stays archived in the same
    # file as PRE-SYNCHRONIZED-RESET and is never mixed into the v5 visible record.
    model_version: str = field(default_factory=lambda: _str("Q15_CHALLENGER_MODEL_VERSION", "challenger-v5"))

    # -- reset / comparison window --
    # Optional explicit reset timestamp (epoch seconds, UTC). When unset (0) the
    # reset timestamp is stamped the first time this model_version records, so the
    # report can show "Comparison reset: <UTC>". Predictions created before the
    # reset are never mixed into the visible record.
    reset_at: float = field(default_factory=lambda: _float("Q15_CHALLENGER_RESET_AT", 0.0))

    # -- grading rule --
    # Whether Your System (native/control) is graded ONLY on picks actually
    # delivered to Telegram before contract close (mark_native_sent on a real
    # delivery), or on every prediction it generates.
    #
    # Default OFF: Your System is graded on the SAME generated predictions as the
    # Shadow (which is never delivered — read-only test), so the Shadow-vs-Yours
    # card is a true apples-to-apples model comparison and fills every window
    # regardless of Telegram delivery health. The delivery audit line
    # (delivery_audit) still surfaces send health separately.
    #
    # ON (Q15_CHALLENGER_NATIVE_SENT_ONLY=true): the legacy delivery-gated rule —
    # only picks delivered before close count in the visible Yours record; undelivered
    # picks stay as internal background rows (graded for learning) but never appear.
    # This makes the visible Yours record empty whenever delivery is failing, which is
    # why the default is OFF. Reversible.
    native_sent_only: bool = field(
        default_factory=lambda: _bool("Q15_CHALLENGER_NATIVE_SENT_ONLY", False))

    @classmethod
    def from_env(cls) -> "ChallengerConfig":
        return cls()

    def with_overrides(self, **kw) -> "ChallengerConfig":
        valid = {f.name for f in fields(self)}
        bad = set(kw) - valid
        if bad:
            raise ValueError(f"unknown config keys: {sorted(bad)}")
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data.update(kw)
        return ChallengerConfig(**data)
