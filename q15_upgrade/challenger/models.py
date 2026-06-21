"""Challenger model backends.

Default backend is a pure-python L2-regularised logistic regression (no external
dependencies — the deploy target has no numpy/sklearn/xgboost). Optional GBM
backends (xgboost / lightgbm) are used only if importable; otherwise the factory
raises a clear error and the caller falls back to logistic.

Two interpretable baselines are included per the mandate: a market-price-only
model and a volatility/structural model.

All models consume X as a list of feature rows aligned to
``features.FEATURE_NAMES`` and output P(Yes) in [0.01, 0.99].
"""

from __future__ import annotations

from typing import Sequence

from .features import FEATURE_NAMES
from .mathx import clamp, normal_cdf, sigmoid

_IDX = {name: i for i, name in enumerate(FEATURE_NAMES)}


class BaseModel:
    name = "base"
    fitted = False

    def fit(self, X, y, sample_weight=None):
        raise NotImplementedError

    def predict_proba_one(self, x: Sequence[float]) -> float:
        raise NotImplementedError

    def predict_proba(self, X) -> list[float]:
        return [self.predict_proba_one(x) for x in X]

    def feature_importance(self) -> dict[str, float]:
        return {}

    def contributions(self, x: Sequence[float]) -> dict[str, float]:
        return {}


class LogisticRegression(BaseModel):
    name = "logistic"

    def __init__(self, l2: float = 10.0, lr: float = 0.05, max_iter: int = 500):
        self.l2 = l2
        self.lr = lr
        self.max_iter = max_iter
        self.weights: list[float] = []
        self.bias = 0.0
        self.means: list[float] = []
        self.stds: list[float] = []

    def _standardize(self, x: Sequence[float]) -> list[float]:
        return [
            (float(x[i]) - self.means[i]) / self.stds[i] if self.stds[i] > 0 else 0.0
            for i in range(len(x))
        ]

    def fit(self, X, y, sample_weight=None):
        X = [list(map(float, row)) for row in X]
        y = [float(v) for v in y]
        n = len(X)
        if n == 0:
            return self
        d = len(X[0])
        # Standardize on TRAIN only.
        self.means = [sum(row[j] for row in X) / n for j in range(d)]
        self.stds = []
        for j in range(d):
            m = self.means[j]
            var = sum((row[j] - m) ** 2 for row in X) / n
            self.stds.append(var ** 0.5)
        Z = [self._standardize(row) for row in X]
        w = [0.0] * d
        b = 0.0
        sw = [1.0] * n if sample_weight is None else [float(s) for s in sample_weight]
        sw_total = sum(sw) or 1.0
        for _ in range(self.max_iter):
            gw = [0.0] * d
            gb = 0.0
            for zi, yi, wi in zip(Z, y, sw):
                pred = sigmoid(sum(w[j] * zi[j] for j in range(d)) + b)
                err = (pred - yi) * wi
                for j in range(d):
                    gw[j] += err * zi[j]
                gb += err
            for j in range(d):
                gw[j] = gw[j] / sw_total + (self.l2 / sw_total) * w[j]
                w[j] -= self.lr * gw[j]
            gb /= sw_total
            b -= self.lr * gb
        self.weights, self.bias, self.fitted = w, b, True
        return self

    def predict_proba_one(self, x: Sequence[float]) -> float:
        if not self.fitted:
            return 0.5
        z = self._standardize(x)
        return clamp(sigmoid(sum(self.weights[j] * z[j] for j in range(len(z))) + self.bias), 0.01, 0.99)

    def feature_importance(self) -> dict[str, float]:
        if not self.fitted:
            return {}
        return {FEATURE_NAMES[j]: abs(self.weights[j]) for j in range(len(self.weights))}

    def contributions(self, x: Sequence[float]) -> dict[str, float]:
        if not self.fitted:
            return {}
        z = self._standardize(x)
        return {FEATURE_NAMES[j]: self.weights[j] * z[j] for j in range(len(z))}


class MarketOnlyModel(BaseModel):
    """Baseline: the de-spread market-implied probability itself."""

    name = "market_only"

    def fit(self, X, y, sample_weight=None):
        self.fitted = True
        return self

    def predict_proba_one(self, x: Sequence[float]) -> float:
        return clamp(float(x[_IDX["market_implied_prob"]]), 0.01, 0.99)


class VolatilityModel(BaseModel):
    """Baseline: structural P(Yes) = Phi(-normalized_distance)."""

    name = "volatility_only"

    def fit(self, X, y, sample_weight=None):
        self.fitted = True
        return self

    def predict_proba_one(self, x: Sequence[float]) -> float:
        nd = float(x[_IDX["normalized_distance"]])
        return clamp(normal_cdf(-nd), 0.01, 0.99)


class GBMModel(BaseModel):
    """Gradient-boosted-tree backend (xgboost or lightgbm), used only if available."""

    def __init__(self, backend: str, config):
        self.backend = backend
        self.config = config
        self.name = backend
        self._model = None

    def fit(self, X, y, sample_weight=None):
        cfg = self.config
        if self.backend == "xgboost":
            import xgboost as xgb  # noqa: F401

            self._model = xgb.XGBClassifier(
                objective="binary:logistic",
                eval_metric="logloss",
                max_depth=cfg.gbm_max_depth,
                learning_rate=cfg.gbm_learning_rate,
                n_estimators=cfg.gbm_n_estimators,
                min_child_weight=cfg.gbm_min_child_weight,
                subsample=cfg.gbm_subsample,
                colsample_bytree=cfg.gbm_colsample,
                reg_alpha=cfg.gbm_reg_alpha,
                reg_lambda=cfg.gbm_reg_lambda,
            )
            self._model.fit(X, y, sample_weight=sample_weight)
        elif self.backend == "lightgbm":
            import lightgbm as lgb  # noqa: F401

            self._model = lgb.LGBMClassifier(
                objective="binary",
                max_depth=cfg.gbm_max_depth,
                learning_rate=cfg.gbm_learning_rate,
                n_estimators=cfg.gbm_n_estimators,
                min_child_samples=int(cfg.gbm_min_child_weight),
                subsample=cfg.gbm_subsample,
                colsample_bytree=cfg.gbm_colsample,
                reg_alpha=cfg.gbm_reg_alpha,
                reg_lambda=cfg.gbm_reg_lambda,
            )
            self._model.fit(X, y, sample_weight=sample_weight)
        else:
            raise ValueError(f"unknown GBM backend: {self.backend}")
        self.fitted = True
        return self

    def predict_proba_one(self, x: Sequence[float]) -> float:
        proba = self._model.predict_proba([list(x)])[0][1]
        return clamp(float(proba), 0.01, 0.99)

    def predict_proba(self, X) -> list[float]:
        return [clamp(float(p[1]), 0.01, 0.99) for p in self._model.predict_proba([list(r) for r in X])]

    def feature_importance(self) -> dict[str, float]:
        try:
            imp = self._model.feature_importances_
            return {FEATURE_NAMES[j]: float(imp[j]) for j in range(len(imp))}
        except Exception:
            return {}


def make_model(config):
    """Construct the configured backend. Raises ImportError for an unavailable GBM."""
    backend = (config.backend or "logistic").lower()
    if backend == "logistic":
        return LogisticRegression(l2=config.l2, lr=config.learning_rate, max_iter=config.max_iter)
    if backend == "market_only":
        return MarketOnlyModel()
    if backend == "volatility_only":
        return VolatilityModel()
    if backend in ("xgboost", "lightgbm"):
        return GBMModel(backend, config)
    raise ValueError(f"unknown backend: {backend}")
