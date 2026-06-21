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
    n_splits: int = field(default_factory=lambda: _int("Q15_CHALLENGER_WF_SPLITS", 4))
    embargo_seconds: float = field(default_factory=lambda: _float("Q15_CHALLENGER_EMBARGO_SECONDS", 900.0))
    horizon_seconds: float = field(default_factory=lambda: _float("Q15_CHALLENGER_HORIZON_SECONDS", 900.0))

    # -- cost model (mirrors the production assumptions) --
    fee_rate: float = field(default_factory=lambda: _float("Q15_CHALLENGER_FEE_RATE", 0.07))
    slippage_spread_fraction: float = field(default_factory=lambda: _float("Q15_CHALLENGER_SLIPPAGE_FRAC", 0.50))
    slippage_fallback_cents: float = field(default_factory=lambda: _float("Q15_CHALLENGER_SLIPPAGE_FALLBACK", 0.75))
    market_impact_cents: float = field(default_factory=lambda: _float("Q15_CHALLENGER_IMPACT_CENTS", 0.25))
    uncertainty_margin_cents: float = field(default_factory=lambda: _float("Q15_CHALLENGER_UNCERTAINTY_CENTS", 1.0))

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

    # -- persistence --
    db_path: str = field(default_factory=lambda: _str("Q15_CHALLENGER_DB", "data/q15_challenger_shadow_v1.sqlite3"))
    model_version: str = field(default_factory=lambda: _str("Q15_CHALLENGER_MODEL_VERSION", "challenger-v1"))

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
