"""Offline training & evaluation harness — purged walk-forward + calibration.

Turns stored (timestamp, feature_dict, label) samples into either:
  * an out-of-sample evaluation (per-fold + concatenated test metrics, vs the
    market-only and volatility baselines), or
  * a frozen ShadowPredictor ready for live shadow use (model fit on all rows,
    calibrator fit on a held-out tail slice).

All fitting/preprocessing/calibration happens on training/validation data only —
never on the test fold (Phase 7).
"""

from __future__ import annotations

from typing import Any, Sequence

from .calibration import make_calibrator
from .config import ChallengerConfig
from .features import FEATURE_NAMES
from .ledger import _prob_metrics
from .models import LogisticRegression, MarketOnlyModel, VolatilityModel, make_model
from .predictor import ShadowPredictor
from .validation import purged_walk_forward


def ordered_vector(feature_dict: dict) -> list[float]:
    """Map a stored feature_details dict to the frozen FEATURE_NAMES order."""
    return [float(feature_dict.get(name, 0.0)) for name in FEATURE_NAMES]


def _select_calibrator(kind: str, val_probs, val_y, l2: float):
    """Pick/fit a calibrator on validation only. ``auto`` chooses by val log-loss."""
    if kind != "auto":
        return make_calibrator(kind, l2=l2).fit(val_probs, val_y)
    best = None
    best_ll = float("inf")
    for k in ("identity", "platt", "isotonic"):
        cal = make_calibrator(k, l2=l2).fit(val_probs, val_y)
        if not val_probs:
            return cal
        tp = [cal.transform(p) for p in val_probs]
        m = _prob_metrics([min(max(p, 1e-6), 1 - 1e-6) for p in tp], val_y)
        if m["log_loss"] < best_ll:
            best_ll, best = m["log_loss"], cal
    return best


def walk_forward_evaluate(
    timestamps: Sequence[float],
    feature_dicts: Sequence[dict],
    y: Sequence[int],
    config: ChallengerConfig | None = None,
) -> dict[str, Any]:
    """Purged walk-forward OOS evaluation for the configured backend + baselines."""
    cfg = config or ChallengerConfig.from_env()
    X = [ordered_vector(f) for f in feature_dicts]
    y = [int(v) for v in y]
    folds = purged_walk_forward(
        timestamps, n_splits=cfg.n_splits,
        embargo_seconds=cfg.embargo_seconds, horizon_seconds=cfg.horizon_seconds,
    )
    if not folds:
        return {"ok": False, "reason": "insufficient data for walk-forward", "n": len(y)}

    oos = {"challenger": ([], []), "market_only": ([], []), "volatility_only": ([], [])}
    fold_reports = []
    for fold in folds:
        tr = fold.train_idx + fold.val_idx
        if len({y[i] for i in tr}) < 2:
            continue  # need both classes to fit
        Xtr = [X[i] for i in tr]
        ytr = [y[i] for i in tr]
        # challenger
        model = make_model(cfg)
        model.fit(Xtr, ytr)
        val_p = [model.predict_proba_one(X[i]) for i in fold.val_idx] if fold.val_idx else []
        val_y = [y[i] for i in fold.val_idx]
        cal = _select_calibrator(cfg.calibration if cfg.calibration != "none" else "identity",
                                 val_p, val_y, cfg.l2)
        for i in fold.test_idx:
            oos["challenger"][0].append(cal.transform(model.predict_proba_one(X[i])))
            oos["challenger"][1].append(y[i])
        # baselines (no calibration, no fit needed)
        for bname, bmodel in (("market_only", MarketOnlyModel()), ("volatility_only", VolatilityModel())):
            for i in fold.test_idx:
                oos[bname][0].append(bmodel.predict_proba_one(X[i]))
                oos[bname][1].append(y[i])
        fold_reports.append({
            "test_n": len(fold.test_idx),
            "train_n": len(tr),
        })

    result: dict[str, Any] = {"ok": True, "folds": len(fold_reports), "fold_reports": fold_reports}
    for name, (probs, ys) in oos.items():
        if probs:
            result[name] = _prob_metrics([min(max(p, 1e-6), 1 - 1e-6) for p in probs], ys)
    return result


def train_predictor(
    timestamps: Sequence[float],
    feature_dicts: Sequence[dict],
    y: Sequence[int],
    config: ChallengerConfig | None = None,
    calib_tail_fraction: float = 0.25,
) -> tuple[ShadowPredictor, dict[str, Any]]:
    """Fit a final ShadowPredictor for live shadow use.

    Model is fit on the earlier rows; the calibrator is fit on a held-out tail
    slice (most recent ``calib_tail_fraction``) so calibration is not in-sample.
    Returns (predictor, info). If there is too little data / one class only, the
    predictor is returned UNFITTED (cold-start: defers to market price).
    """
    cfg = config or ChallengerConfig.from_env()
    X = [ordered_vector(f) for f in feature_dicts]
    y = [int(v) for v in y]
    n = len(y)
    info: dict[str, Any] = {"n": n, "backend": cfg.backend}
    if n < cfg.min_train_rows or len(set(y)) < 2:
        info["fitted"] = False
        info["reason"] = "insufficient_data" if n < cfg.min_train_rows else "single_class"
        return ShadowPredictor(cfg, make_model(cfg), make_calibrator("identity")), info

    n_cal = max(5, int(n * calib_tail_fraction))
    fit_X, fit_y = X[: n - n_cal], y[: n - n_cal]
    cal_X, cal_y = X[n - n_cal:], y[n - n_cal:]
    if len(set(fit_y)) < 2:
        info["fitted"] = False
        info["reason"] = "single_class_after_split"
        return ShadowPredictor(cfg, make_model(cfg), make_calibrator("identity")), info

    model = make_model(cfg)
    model.fit(fit_X, fit_y)
    cal_p = [model.predict_proba_one(x) for x in cal_X]
    cal = _select_calibrator(cfg.calibration if cfg.calibration != "none" else "identity",
                             cal_p, cal_y, cfg.l2)
    info["fitted"] = True
    info["fit_rows"] = len(fit_y)
    info["calib_rows"] = len(cal_y)
    info["calibrator"] = cal.name
    return ShadowPredictor(cfg, model, cal), info
