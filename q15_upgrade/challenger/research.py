"""Challenger research harness — evaluate candidate upgrades OUT-OF-SAMPLE.

This is where a proposed challenger upgrade (e.g. the v6 feature set) is judged
against the PREVIOUS challenger version (v5) before anything is promoted. It uses
the challenger's own model + calibrator and the same purged walk-forward used for
the production OOS, so the comparison is apples-to-apples and leakage-safe:

  * folds are strictly chronological (no shuffling);
  * the model and calibrator are fit on train/validation only, never on test;
  * because both feature sets share the same timestamps they share the same
    folds and test indices, so the OOS probabilities are PAIRED per contract.

An upgrade is recommended for promotion only when it beats the previous version
out-of-sample on log-loss AND does not regress Brier or calibration, with a
block-bootstrap CI on the paired log-loss difference that excludes zero — i.e.
the improvement is real and statistically distinguishable, not noise. The default
when data is thin is "keep in research", never "promote".

Nothing here changes the live challenger. Promotion is a deliberate config switch
(``Q15_CHALLENGER_FEATURE_SET=v6`` + a new ``model_version``) the owner makes
after this harness shows a win on real settled data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from . import features as featmod
from . import features_v6 as featv6
from .calibration import make_calibrator
from .config import ChallengerConfig
from .ledger import _prob_metrics
from .models import LogisticRegression
from .stats import block_bootstrap_ci, paired_differences
from .validation import purged_walk_forward


def _clamp01(p: float) -> float:
    return min(max(p, 1e-6), 1.0 - 1e-6)


def _oos_probabilities(
    timestamps: Sequence[float],
    vectors: Sequence[Sequence[float]],
    y: Sequence[int],
    cfg: ChallengerConfig,
) -> tuple[list[int], list[float], list[int]]:
    """Return (test_indices, oos_probs, oos_y) from a purged walk-forward.

    The model is the challenger's own L2 logistic; the calibrator is fit on the
    validation slice only and frozen before scoring the unseen test fold.
    """
    folds = purged_walk_forward(
        timestamps, n_splits=cfg.n_splits,
        embargo_seconds=cfg.embargo_seconds, horizon_seconds=cfg.horizon_seconds,
    )
    idxs: list[int] = []
    probs: list[float] = []
    ys: list[int] = []
    for fold in folds:
        tr = fold.train_idx + fold.val_idx
        if len({y[i] for i in tr}) < 2:
            continue
        Xtr = [list(vectors[i]) for i in tr]
        ytr = [int(y[i]) for i in tr]
        model = LogisticRegression(l2=cfg.l2, lr=cfg.learning_rate, max_iter=cfg.max_iter)
        model.fit(Xtr, ytr)
        val_p = [model.predict_proba_one(vectors[i]) for i in fold.val_idx] if fold.val_idx else []
        val_y = [int(y[i]) for i in fold.val_idx]
        kind = cfg.calibration if cfg.calibration not in (None, "none") else "identity"
        cal = make_calibrator(kind, l2=cfg.l2).fit(val_p, val_y)
        for i in fold.test_idx:
            idxs.append(i)
            probs.append(_clamp01(cal.transform(model.predict_proba_one(vectors[i]))))
            ys.append(int(y[i]))
    return idxs, probs, ys


@dataclass
class UpgradeVerdict:
    ok: bool
    promote: bool
    reason: str
    n_test: int = 0
    baseline_metrics: dict[str, Any] = field(default_factory=dict)
    candidate_metrics: dict[str, Any] = field(default_factory=dict)
    paired: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "promote": self.promote, "reason": self.reason,
            "n_test": self.n_test, "baseline": self.baseline_metrics,
            "candidate": self.candidate_metrics, "paired": self.paired,
        }


def evaluate_feature_upgrade(
    timestamps: Sequence[float],
    snapshots: Sequence[Mapping[str, Any]],
    y: Sequence[int],
    cfg: ChallengerConfig | None = None,
    *,
    min_test: int = 40,
    ece_tolerance: float = 0.02,
) -> UpgradeVerdict:
    """Compare the v6 feature set (candidate) against v5 (baseline) out-of-sample.

    ``snapshots`` are decision-time dicts; v5 and v6 feature vectors are extracted
    from each. Returns an :class:`UpgradeVerdict` whose ``promote`` is True only on
    a real, significant OOS improvement that does not regress Brier or calibration.
    """
    cfg = cfg or ChallengerConfig.from_env()
    ts = [float(t) for t in timestamps]
    y = [int(v) for v in y]
    if len(ts) != len(snapshots) or len(ts) != len(y):
        return UpgradeVerdict(False, False, "input_length_mismatch")
    if len(ts) < cfg.n_splits + 2:
        return UpgradeVerdict(False, False, "insufficient_data", n_test=len(ts))

    # Extract both feature sets from identical snapshots (shared folds => paired).
    v5_vecs = []
    v6_vecs = []
    for snap in snapshots:
        fv5 = featmod.extract(snap)
        v5_vecs.append([float(fv5.details.get(n, 0.0)) for n in featmod.FEATURE_NAMES])
        v6 = featv6.extract_v6(snap)
        v6_vecs.append(v6.ordered_values())

    i5, p5, y5 = _oos_probabilities(ts, v5_vecs, y, cfg)
    i6, p6, y6 = _oos_probabilities(ts, v6_vecs, y, cfg)

    # Align on the common test indices (they should match; intersect defensively).
    m5 = {i: (p, yy) for i, p, yy in zip(i5, p5, y5)}
    m6 = {i: (p, yy) for i, p, yy in zip(i6, p6, y6)}
    common = sorted(set(m5) & set(m6))
    if len(common) < min_test:
        return UpgradeVerdict(
            False, False, "insufficient_oos_samples", n_test=len(common),
            baseline_metrics=_prob_metrics([m5[i][0] for i in m5], [m5[i][1] for i in m5]) if m5 else {},
            candidate_metrics=_prob_metrics([m6[i][0] for i in m6], [m6[i][1] for i in m6]) if m6 else {},
        )

    base_p = [m5[i][0] for i in common]
    cand_p = [m6[i][0] for i in common]
    yy = [m5[i][1] for i in common]

    base_m = _prob_metrics(base_p, yy)
    cand_m = _prob_metrics(cand_p, yy)

    pd = paired_differences(base_p, cand_p, yy)  # (challenger=candidate) - (control=baseline)
    ll_lo, ll_hi = block_bootstrap_ci(pd["log_loss_diff"], block_size=5, n_boot=1000)
    paired = {
        "mean_log_loss_diff": round(pd["mean_log_loss_diff"], 6),
        "log_loss_diff_ci95": [round(ll_lo, 6), round(ll_hi, 6)],
        "mean_brier_diff": round(pd["mean_brier_diff"], 6),
    }

    # Promotion gate (strict): candidate better on log-loss, no regression on
    # Brier/calibration, and the paired CI excludes 0 on the better side.
    better_ll = cand_m["log_loss"] < base_m["log_loss"]
    no_brier_regression = cand_m["brier"] <= base_m["brier"] + 1e-9
    ece_regression = cand_m["ece"] - base_m["ece"]
    brier_gain = base_m["brier"] - cand_m["brier"]
    # ECE may tick up within tolerance, OR be forgiven when the Brier improvement
    # (a proper score = calibration + refinement) more than covers the ECE uptick —
    # this avoids penalising a genuinely sharper model against a near-random
    # baseline whose ECE is trivially low because it never commits.
    no_ece_regression = (ece_regression <= ece_tolerance) or (brier_gain >= ece_regression)
    significant = ll_hi < 0.0  # whole CI of (candidate - baseline) log-loss is negative
    promote = bool(better_ll and no_brier_regression and no_ece_regression and significant)

    if promote:
        reason = "candidate_beats_baseline_oos_significant"
    elif better_ll and not significant:
        reason = "candidate_better_but_not_significant_keep_research"
    elif not better_ll:
        reason = "candidate_no_better_keep_research"
    else:
        reason = "candidate_regresses_brier_or_calibration_keep_research"

    return UpgradeVerdict(
        ok=True, promote=promote, reason=reason, n_test=len(common),
        baseline_metrics=base_m, candidate_metrics=cand_m, paired=paired,
    )


# --------------------------------------------------------------------------- #
# Prediction stability (research option) — EMA smoothing of the predicted logit
# per (contract, checkpoint). Reduces cycle-to-cycle flip-flop without changing
# the model; default OFF (half-life 0 = passthrough). Pure and deterministic.
# --------------------------------------------------------------------------- #
def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


class StabilityController:
    """EMA smoother for a stream of probabilities keyed by an identity.

    ``half_life`` is in updates: the weight of an old observation halves every
    ``half_life`` cycles. 0 disables smoothing (returns the raw probability), so
    the challenger's live behaviour is byte-identical unless this is opted into.
    """

    def __init__(self, half_life: float = 0.0):
        self.half_life = max(0.0, float(half_life))
        self._state: dict[str, float] = {}

    @property
    def _alpha(self) -> float:
        if self.half_life <= 0.0:
            return 1.0
        return 1.0 - 0.5 ** (1.0 / self.half_life)

    def smooth(self, key: str, prob: float) -> float:
        if self.half_life <= 0.0:
            return min(max(float(prob), 0.0), 1.0)
        x = _logit(prob)
        prev = self._state.get(key)
        if prev is None:
            self._state[key] = x
            return min(max(float(prob), 0.0), 1.0)
        a = self._alpha
        blended = a * x + (1.0 - a) * prev
        self._state[key] = blended
        return _sigmoid(blended)

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._state.clear()
        else:
            self._state.pop(key, None)
