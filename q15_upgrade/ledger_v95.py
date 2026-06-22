"""Version-scoped prediction ledger, calibration, and shadow learning for Q15 V9.5.

The production champion is intentionally frozen. Separate bounded challengers
learn by checkpoint. The 10-minute challenger is primary by default; the
15-minute challenger is disabled by default and can be enabled independently.
Neither challenger can promote itself or change live thresholds.  Every stored prediction is tagged by model
and feature-schema version so historical rows cannot silently contaminate the
current release.
"""
from __future__ import annotations

from contextlib import closing
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from q15_upgrade import flip_risk
from q15_upgrade.money import round_cents

logger = logging.getLogger(__name__)

VERSION = "q15-v9.5.1-ledger-v2-10m-primary"
MODEL_VERSION = "q15-v9.5.2-champion-ensemble-10m-primary-v2"
FEATURE_SCHEMA_VERSION = "q15-v9.5.1-canonical-snapshot-v1"
READ_ONLY = True


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return max(low, min(high, value))


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _wilson_interval(right: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    """95% Wilson score interval for a binomial proportion (right/n)."""
    if n <= 0:
        return (None, None)
    p = right / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    margin = (z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))) / denom
    return (round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4))


def _logit(probability: float) -> float:
    p = _clamp(float(probability), 1e-6, 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _betacf(a: float, b: float, x: float) -> float:
    """Continued-fraction expansion for the incomplete beta (Numerical Recipes)."""
    MAXIT, EPS, FPMIN = 200, 3.0e-12, 1.0e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    bt = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log(1.0 - x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _two_sided_p(t: float, df: float | None = None) -> float:
    """Two-sided p-value from a t statistic.

    With ``df`` (degrees of freedom) the exact Student-t distribution is used
    via the regularized incomplete beta; this is correct for the small samples
    (n~50) at which promotion is screened, where the normal approximation is
    slightly anti-conservative. With ``df`` omitted it falls back to the normal
    approximation (preserved for callers that pass a pure z statistic)."""
    if not math.isfinite(t):
        # A non-finite statistic (NaN, or ±inf from a degenerate zero-variance
        # sample) carries no usable evidence of a real difference, so the
        # conservative p-value is 1.0 — never treat it as maximal significance.
        return 1.0
    if df is None or df <= 0:
        return _clamp(2.0 * (1.0 - _normal_cdf(abs(t))), 0.0, 1.0)
    df = float(df)
    return _clamp(_betai(0.5 * df, 0.5, df / (df + t * t)), 0.0, 1.0)


def _round_p(p: float | None) -> float | None:
    """Round a p-value without flattening very strong results to 0.0.

    Fixed 6-dp rounding collapses any p < 5e-7 to 0.0, so two distinct
    extremely-significant tests become indistinguishable. Keep ~3 significant
    figures for small p-values (observability only — promotion still compares
    against alpha, not the rounded display)."""
    if p is None:
        return None
    if p <= 0.0:
        return 0.0
    if p >= 1e-4:
        return round(p, 6)
    digits = 2 - int(math.floor(math.log10(p)))
    return round(p, min(digits, 15))


def _isotonic_fit(pairs: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """Pool-Adjacent-Violators isotonic regression.

    ``pairs`` is (raw_probability, outcome∈{0,1}). Returns monotonically
    non-decreasing (x, y) anchors (block mean x → block mean y), x ascending —
    the calibrated map. Pure and deterministic; empty in → empty out."""
    pts = sorted((float(x), float(y)) for x, y in pairs)
    if not pts:
        return []
    blocks: list[list[float]] = []  # [sum_y, count, sum_x]
    for x, y in pts:
        blocks.append([y, 1.0, x])
        # Merge while the previous block's mean would violate monotonicity.
        while len(blocks) >= 2 and blocks[-2][0] / blocks[-2][1] >= blocks[-1][0] / blocks[-1][1]:
            sy2, c2, sx2 = blocks.pop()
            sy1, c1, sx1 = blocks.pop()
            blocks.append([sy1 + sy2, c1 + c2, sx1 + sx2])
    return [(sx / c, sy / c) for sy, c, sx in blocks]


def _isotonic_predict(anchors: Sequence[tuple[float, float]], x: float) -> float | None:
    """Linear interpolation over isotonic ``anchors`` (clamped at the ends)."""
    if not anchors:
        return None
    if x <= anchors[0][0]:
        return anchors[0][1]
    if x >= anchors[-1][0]:
        return anchors[-1][1]
    for i in range(1, len(anchors)):
        x0, y0 = anchors[i - 1]
        x1, y1 = anchors[i]
        if x <= x1:
            if x1 == x0:
                return y1
            t = (x - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return anchors[-1][1]


def _fit_platt(xy: Sequence[tuple[float, float]], max_iters: int, slope_min: float,
               slope_max: float, intercept_cap: float, reg: float = 0.20) -> tuple[float, float, bool, int]:
    """Penalised logistic (Platt) fit y ~ sigmoid(intercept + slope*x).

    Convergence is judged on the APPLIED (post-clamp) step, not the raw Newton
    step, so a coefficient pinned at its clamp (a constrained optimum — e.g. an
    under-confident model whose slope wants to exceed the ceiling) counts as
    converged instead of looping forever and falling back to identity."""
    intercept, slope = 0.0, 1.0
    converged = False
    iterations = 0
    for _ in range(max_iters):
        iterations += 1
        g0 = -reg * intercept
        g1 = -reg * (slope - 1.0)
        h00 = reg
        h01 = 0.0
        h11 = reg
        for x, y in xy:
            p = _sigmoid(intercept + slope * x)
            residual = y - p
            variance = max(1e-6, p * (1.0 - p))
            g0 += residual
            g1 += residual * x
            h00 += variance
            h01 += variance * x
            h11 += variance * x * x
        determinant = h00 * h11 - h01 * h01
        if determinant <= 1e-12:
            break
        d0 = (g0 * h11 - g1 * h01) / determinant
        d1 = (g1 * h00 - g0 * h01) / determinant
        new_intercept = _clamp(intercept + d0, -intercept_cap, intercept_cap)
        new_slope = _clamp(slope + d1, slope_min, slope_max)
        applied = abs(new_intercept - intercept) + abs(new_slope - slope)
        intercept, slope = new_intercept, new_slope
        if applied < 1e-6:
            converged = True
            break
    return intercept, slope, converged, iterations


def _brier(pairs: Sequence[tuple[float, float]]) -> float | None:
    """Mean squared error of predicted probability vs binary outcome."""
    pairs = list(pairs)
    if not pairs:
        return None
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return default
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def _row_get(row: Any, key: str) -> Any:
    """Read a column from a sqlite3.Row, tolerating its absence in the SELECT."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _parse_ts(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        parsed = float(value)
        if parsed > 10_000_000_000:
            parsed /= 1000.0
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


# Checkpoints whose predictions are recorded and graded for accuracy. 10M/15M
# also carry shadow weight-learning; 7M is accuracy-tracked only by default.
TRACKED_CHECKPOINTS = ("15M", "10M", "7M")
LEARNING_CHECKPOINTS = ("10M", "15M")


# Frozen champion logit weights. Each is the maximum logit swing a feature can
# add to the structural base, applied as ``weight * value * quality`` where value
# is in [-1, 1] and quality in [0, 1] (see _model_probability). The ranking
# encodes the model's priors at these horizons:
#   momentum (0.34)              — strongest single directional signal short-term.
#   threshold_interaction (0.30) — distance/crossings vs the strike; second only
#                                  to momentum because it is what actually settles.
#   flow (0.26)                  — aggressive taker imbalance.
#   absorption (0.20)            — flow that fails to move price (mean-reversion warning).
#   book / context / exchange    — confirmation signals (0.18 each), weaker alone.
#   wick (0.12), derivatives (0.10) — weakest / sparsest, kept low on purpose.
# These are FROZEN: only the shadow challenger learns; changing them is a manual,
# significance-tested promotion. test_q15_v95_weights.py pins these values so an
# accidental edit fails loudly rather than silently shifting every prediction.
CHAMPION_WEIGHTS: dict[str, float] = {
    "intercept": 0.0,
    "momentum": 0.34,
    "flow": 0.26,
    "book": 0.18,
    "wick": 0.12,
    "context": 0.18,
    "threshold_interaction": 0.30,
    "exchange_consensus": 0.18,
    "derivatives": 0.10,
    "absorption": 0.20,
}


class _PersistentConnection(sqlite3.Connection):
    """SQLite connection whose ``close()`` is a no-op so a single connection can
    be reused across the ledger's many ``with closing(self._connect())`` call
    sites. Re-opening the file every call is expensive on Replit's networked
    ``data/`` disk, and the ledger is read several times per asset per cycle
    inside ``v95_analysis``; the connection lives for the process lifetime and
    is serialized by the ledger's lock. ``with conn:`` transactions still
    commit/roll back normally."""

    def close(self) -> None:  # noqa: D401 - intentional no-op for connection reuse
        pass


class V95Ledger:
    """Atomic SQLite ledger with frozen champion and shadow challenger."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path or os.environ.get("Q15_V95_LEDGER_DB") or "data/q15_v95_ledger_v1.sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._shared_connection: sqlite3.Connection | None = None
        primary = str(os.environ.get("Q15_V95_PRIMARY_LEARNING_CHECKPOINT", "10M")).strip().upper()
        self.primary_learning_checkpoint = primary if primary in {"10M", "15M"} else "10M"
        # How much more the primary (10M) checkpoint's resolved results count in
        # shadow training vs the secondary intervals. 10M is the best performer,
        # so it gets the heaviest sample weight; 7M/15M still learn, just slower.
        self.primary_learning_weight = _env_float("Q15_V95_PRIMARY_LEARNING_WEIGHT", 1.25, 1.0, 3.0)
        # The primary learner's heavier sample weight must actually exceed 1.0 to
        # accelerate learning. A legacy ``min(1.0, ...)`` clamp silently erased the
        # boost for high-quality rows (the only rows whose base weight already hit
        # 1.0), so 10M learned at the same rate as 15M despite the 1.25x knob. With
        # the boost ON (default) the weight scales fully (bounded by the per-result
        # and total-drift caps downstream, so a >1.0 weight is safe). OFF restores
        # the legacy clamp. Shadow-only: production champion weights stay frozen.
        self.primary_learning_boost = _env_bool("Q15_V95_PRIMARY_LEARNING_BOOST", True)
        legacy_enabled = _env_bool("Q15_V95_SHADOW_LEARNING", True)
        # 7M is tracked and graded for accuracy alongside 15M/10M, but its weight
        # learning is observational by default (like 15M). Enabling it requires the
        # challenger weight tables to admit '7M' — see _initialize.
        self.learning_enabled_by_checkpoint = {
            "10M": _env_bool("Q15_V95_10M_SHADOW_LEARNING", legacy_enabled),
            "15M": _env_bool("Q15_V95_15M_SHADOW_LEARNING", True),
            # 7M stays OFF by default: the challenger tables' CHECK constraint
            # only admits '10M'/'15M', so enabling 7M learning raises an
            # IntegrityError on every 7M resolution. Needs a schema migration to
            # admit '7M' before it can be turned on (see _initialize).
            "7M": _env_bool("Q15_V95_7M_SHADOW_LEARNING", False),
        }
        self.learning_rate_by_checkpoint = {
            "10M": _env_float("Q15_V95_10M_SHADOW_LEARNING_RATE", 0.05, 0.001, 0.20),
            "15M": _env_float("Q15_V95_15M_SHADOW_LEARNING_RATE", 0.02, 0.001, 0.20),
            "7M": _env_float("Q15_V95_7M_SHADOW_LEARNING_RATE", 0.05, 0.001, 0.20),
        }
        self.per_result_cap_by_checkpoint = {
            "10M": _env_float("Q15_V95_10M_SHADOW_MAX_DELTA", 0.015, 0.001, 0.05),
            "15M": _env_float("Q15_V95_15M_SHADOW_MAX_DELTA", 0.008, 0.001, 0.05),
            "7M": _env_float("Q15_V95_7M_SHADOW_MAX_DELTA", 0.015, 0.001, 0.05),
        }
        self.total_drift_cap_by_checkpoint = {
            "10M": _env_float("Q15_V95_10M_SHADOW_MAX_DRIFT", 0.35, 0.05, 1.0),
            "15M": _env_float("Q15_V95_15M_SHADOW_MAX_DRIFT", 0.20, 0.05, 1.0),
            "7M": _env_float("Q15_V95_7M_SHADOW_MAX_DRIFT", 0.35, 0.05, 1.0),
        }
        self.minimum_learning_quality_by_checkpoint = {
            "10M": _env_float("Q15_V95_10M_SHADOW_MIN_QUALITY", 0.55, 0.0, 1.0),
            "15M": _env_float("Q15_V95_15M_SHADOW_MIN_QUALITY", 0.65, 0.0, 1.0),
            "7M": _env_float("Q15_V95_7M_SHADOW_MIN_QUALITY", 0.55, 0.0, 1.0),
        }
        # Backward-compatible public attributes now describe the primary learner.
        self.shadow_learning_enabled = self.learning_enabled_by_checkpoint[self.primary_learning_checkpoint]
        self.learning_rate = self.learning_rate_by_checkpoint[self.primary_learning_checkpoint]
        self.per_result_cap = self.per_result_cap_by_checkpoint[self.primary_learning_checkpoint]
        self.total_drift_cap = self.total_drift_cap_by_checkpoint[self.primary_learning_checkpoint]
        self.minimum_learning_quality = self.minimum_learning_quality_by_checkpoint[self.primary_learning_checkpoint]
        self.minimum_calibration_rows = _env_int("Q15_V95_CALIBRATION_MIN_ROWS", 30, 10, 1000)
        # When a Platt fit fails to converge (or hits a near-singular Hessian) the
        # bounded-but-untrusted intercept/slope it leaves behind should not silently
        # transform live probabilities. With this ON (default) an unconverged fit
        # falls back to the identity transform (raw probability passes through),
        # surfaced via the fit's ``reason`` + ``fallback`` and the stats() counter.
        self.calibration_require_converged = _env_bool("Q15_V95_CALIBRATION_REQUIRE_CONVERGED", True)
        # Newton-step budget for the Platt fit. Raised to 50 (was 12): an
        # UNDER-confident model needs a slope > 1 and more steps to reach it.
        self._calibration_max_iters = _env_int("Q15_V95_CALIBRATION_MAX_ITERS", 50, 1, 500)
        # Platt coefficient clamps. The slope CEILING was 1.50 — too low for an
        # under-confident model (it wants slope ~2 to sharpen 0.78 toward 0.95),
        # so the Newton step pinned at the clamp and the fit reported "unconverged"
        # forever -> identity. Widened, and convergence is now judged on the
        # APPLIED (post-clamp) step so a constrained optimum counts as converged.
        self._calibration_slope_min = _env_float("Q15_V95_CALIBRATION_SLOPE_MIN", 0.40, 0.05, 1.0)
        self._calibration_slope_max = _env_float("Q15_V95_CALIBRATION_SLOPE_MAX", 3.00, 1.0, 10.0)
        self._calibration_intercept_cap = _env_float("Q15_V95_CALIBRATION_INTERCEPT_CAP", 1.50, 0.25, 5.0)
        self._calibration_isotonic = _env_bool("Q15_V95_CALIBRATION_ISOTONIC", False)
        self._calibration_isotonic_fallback = _env_bool("Q15_V95_CALIBRATION_ISOTONIC_FALLBACK", True)
        # OOS-SELF-SELECTING calibration (DEFAULT ON, safe-by-construction). Split
        # resolved rows chronologically (train older / test newer), measure the
        # held-out Brier of identity vs Platt vs isotonic, and apply the BEST method
        # only if it beats identity by the floor — otherwise identity. So enabling
        # calibration can only help or no-op out of sample, never silently ship a
        # worse transform. This is the validated "turn calibration on" path.
        self._calibration_oos_select = _env_bool("Q15_V95_CALIBRATION_OOS_SELECT", True)
        self._calibration_oos_floor = _env_float("Q15_V95_CALIBRATION_OOS_FLOOR", 0.0005, 0.0, 0.5)
        self._calibration_oos_test_fraction = _env_float("Q15_V95_CALIBRATION_OOS_TEST_FRACTION", 0.30, 0.1, 0.5)
        self.minimum_promotion_rows = _env_int("Q15_V95_PROMOTION_MIN_ROWS", 50, 20, 5000)
        # Regime-aware challenger: a per-(checkpoint, regime) weight set that
        # specializes once a regime has enough of its own resolved results,
        # falling back to the global challenger until then.
        self.regime_learning_enabled = _env_bool("Q15_V95_REGIME_CHALLENGER", True)
        self.minimum_regime_updates = _env_int("Q15_V95_REGIME_MIN_UPDATES", 30, 5, 5000)
        self._available = True
        self._last_error: str | None = None
        # Behaviour-identical hot-path caches for the per-asset ledger reads that
        # dominate run_cycle's `analyse` bucket but only change when results are
        # resolved or challenger weights are written. Each entry stores the data
        # version it was computed at; a monotonic counter is bumped on the only
        # two mutations that matter (resolve_ticker / _apply_shadow_update), so a
        # cache hit returns the identical fit/centroids/weights. Kill-switch:
        # Q15_V95_LEDGER_CACHE=false reverts to recompute-every-call.
        self._cache_enabled = _env_bool("Q15_V95_LEDGER_CACHE", True)
        self._data_version = 0
        # Observability: rows silently dropped because their stored feature JSON
        # was unparseable. Surfaced via stats() so a corrupt data pipeline shows
        # up instead of quietly thinning calibration/learning inputs. Tracked both
        # in aggregate (backward-compatible) and per checkpoint, so a corruption
        # confined to one interval (e.g. only 10M) is not masked as system-wide.
        self._dropped_feature_rows = 0
        self._dropped_feature_rows_by_checkpoint: dict[str, int] = {cp: 0 for cp in TRACKED_CHECKPOINTS}
        # Observability: shadow-learning steps. The five learning knobs (rate,
        # per-result cap, drift cap, min-quality, primary weight) interact, and a
        # bad combination can silently collapse every step to ~0 — "learning is on"
        # but nothing moves. We surface the last effective step magnitude and a
        # counter of applied-but-frozen results so that state is visible, not a
        # guess. Below this magnitude an update counts as effectively frozen.
        self._last_learning_step_magnitude = 0.0
        self._learning_frozen_results = 0
        self._learning_frozen_epsilon = _env_float("Q15_V95_LEARNING_FROZEN_EPSILON", 1e-9, 0.0, 1.0)
        self._last_learning_frozen_log = 0.0
        # Observability: Platt fits that did not converge and were replaced by the
        # identity transform (see calibration_require_converged). Surfaced in stats().
        self._calibration_unconverged_fallbacks = 0
        # Observability: read-only shadow-challenger calls that raised and were
        # swallowed (observe/mark_sent/resolve). Production is never affected, but a
        # misconfigured or broken shadow would otherwise be invisible — these surface
        # in stats() (and /api/health) so "the shadow silently stopped learning" is
        # detectable instead of being a guess.
        self._shadow_errors = 0
        self._last_shadow_error: str | None = None
        self._calibration_fit_cache: dict[tuple[str, str | None], tuple[int, dict[str, Any]]] = {}
        self._pattern_centroid_cache: dict[str, tuple[int, dict[str, Any]]] = {}
        self._challenger_weights_cache: dict[tuple[str, str | None], tuple[int, dict[str, float]]] = {}
        try:
            self._initialize()
        except Exception as exc:  # fail closed but keep monitor alive
            self._available = False
            self._last_error = f"{type(exc).__name__}: {exc}"

    def _connect(self) -> sqlite3.Connection:
        # Reuse one persistent connection (close() is a no-op) instead of opening
        # the SQLite file on every call — file open is the dominant cost on
        # Replit's networked disk and this method is hit several times per asset
        # per cycle. Every call site serializes via self._lock.
        connection = self._shared_connection
        if connection is not None:
            return connection
        connection = sqlite3.connect(
            self.path, timeout=15.0, check_same_thread=False,
            factory=_PersistentConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA foreign_keys=ON")
        self._shared_connection = connection
        return connection

    def _initialize(self) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS predictions (
                    prediction_id TEXT PRIMARY KEY,
                    model_version TEXT NOT NULL,
                    feature_schema_version TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    checkpoint TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    close_time REAL,
                    predicted_side TEXT NOT NULL CHECK(predicted_side IN ('YES','NO')),
                    raw_yes_probability REAL NOT NULL,
                    calibrated_yes_probability REAL NOT NULL,
                    challenger_yes_probability REAL NOT NULL,
                    baseline_yes_probability REAL NOT NULL,
                    selected_probability REAL NOT NULL,
                    conservative_probability REAL NOT NULL,
                    data_quality REAL NOT NULL,
                    evidence_quality REAL NOT NULL,
                    trade_quality REAL NOT NULL,
                    trade_decision TEXT NOT NULL,
                    regime TEXT NOT NULL,
                    rank INTEGER,
                    entry_ask_cents REAL,
                    entry_cost_cents REAL,
                    realized_cents REAL,
                    feature_json TEXT NOT NULL,
                    contribution_json TEXT NOT NULL,
                    quote_json TEXT NOT NULL,
                    official_result TEXT CHECK(official_result IN ('YES','NO')),
                    resolved_at REAL,
                    correct INTEGER,
                    champion_brier REAL,
                    challenger_brier REAL,
                    baseline_brier REAL,
                    champion_logloss REAL,
                    challenger_logloss REAL,
                    baseline_logloss REAL,
                    learning_applied INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(model_version, ticker, checkpoint)
                );
                CREATE INDEX IF NOT EXISTS idx_v95_predictions_resolved
                    ON predictions(model_version, checkpoint, resolved_at);
                CREATE INDEX IF NOT EXISTS idx_v95_predictions_asset
                    ON predictions(model_version, checkpoint, asset, resolved_at);

                CREATE TABLE IF NOT EXISTS checkpoint_challenger_weights (
                    checkpoint TEXT NOT NULL CHECK(checkpoint IN ('10M','15M')),
                    name TEXT NOT NULL,
                    base_value REAL NOT NULL,
                    value REAL NOT NULL,
                    grad_sq REAL NOT NULL DEFAULT 0,
                    updates INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(checkpoint, name)
                );
                CREATE TABLE IF NOT EXISTS regime_challenger_weights (
                    checkpoint TEXT NOT NULL CHECK(checkpoint IN ('10M','15M')),
                    regime TEXT NOT NULL,
                    name TEXT NOT NULL,
                    base_value REAL NOT NULL,
                    value REAL NOT NULL,
                    grad_sq REAL NOT NULL DEFAULT 0,
                    updates INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(checkpoint, regime, name)
                );
                CREATE TABLE IF NOT EXISTS checkpoint_challenger_updates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    checkpoint TEXT NOT NULL CHECK(checkpoint IN ('10M','15M')),
                    prediction_id TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    error REAL NOT NULL,
                    sample_weight REAL NOT NULL,
                    delta_json TEXT NOT NULL,
                    before_json TEXT NOT NULL,
                    after_json TEXT NOT NULL,
                    FOREIGN KEY(prediction_id) REFERENCES predictions(prediction_id)
                );
                CREATE TABLE IF NOT EXISTS notification_state (
                    event_key TEXT PRIMARY KEY,
                    checkpoint TEXT NOT NULL,
                    state TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    sent_at REAL,
                    reserved_until REAL,
                    failures INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            # Migrate older ledgers that predate the rank / P&L columns.
            self._ensure_column(connection, "predictions", "rank", "rank INTEGER")
            self._ensure_column(connection, "predictions", "entry_ask_cents", "entry_ask_cents REAL")
            self._ensure_column(connection, "predictions", "entry_cost_cents", "entry_cost_cents REAL")
            self._ensure_column(connection, "predictions", "realized_cents", "realized_cents REAL")
            # Per-interval performance breakdowns: store the confidence grade the
            # alert showed, the side first predicted at checkpoint fire, and a flag
            # set when the live side later drifts from that locked prediction
            # before the market closed (a prediction-stability signal). All
            # additive; old rows read as NULL/0.
            self._ensure_column(connection, "predictions", "confidence_grade", "confidence_grade TEXT")
            self._ensure_column(connection, "predictions", "original_predicted_side", "original_predicted_side TEXT")
            self._ensure_column(connection, "predictions", "changed_before_close", "changed_before_close INTEGER DEFAULT 0")
            # Suspected price-manipulation tracking: a read-only flag (1/0) set when
            # large-player signals (strike pin / order-wall absorption / cross-
            # exchange divergence) fired at prediction time, plus the comma-joined
            # reason(s). Lets the scoreboard show whether the model is less reliable
            # when manipulation is suspected. Additive; old rows read NULL/0.
            self._ensure_column(connection, "predictions", "manipulation_suspected", "manipulation_suspected INTEGER DEFAULT 0")
            self._ensure_column(connection, "predictions", "manipulation_reason", "manipulation_reason TEXT")
            # Flip-risk overlay: the point-in-time manipulation/flip-risk score and
            # confidence observed AT this checkpoint (used to learn what score
            # precedes a prediction flip). Recorded live; never back-filled.
            self._ensure_column(connection, "predictions", "flip_risk_score", "flip_risk_score REAL")
            self._ensure_column(connection, "predictions", "flip_risk_confidence", "flip_risk_confidence REAL")
            self._ensure_column(connection, "predictions", "flip_evidence_count", "flip_evidence_count INTEGER")
            # Whether this prediction was actually PUSHED to the user (an entry was
            # recommended and the alert was delivered) vs only observed in the
            # background. Lets the scoreboard keep pushed-only accuracy separate
            # from the full background record. Default 0 = background.
            self._ensure_column(connection, "predictions", "pushed", "pushed INTEGER NOT NULL DEFAULT 0")
            # SHADOW FACTORS: extra decision-time signals (e.g. cross-asset /
            # correlated-movement) recorded for the read-only factor lab ONLY. Kept
            # in a SEPARATE column so they never touch feature_json — the frozen
            # champion, challenger, calibration, and pattern overlay all read only
            # their known feature names, so this can never perturb a live decision.
            # Additive; old rows read NULL.
            self._ensure_column(connection, "predictions", "shadow_factor_json", "shadow_factor_json TEXT")
            # SHADOW SIGNALS: the five experimental, YES-oriented signals graded by
            # the background A/B (see q15_upgrade/shadow_signals.py). Stored in their
            # own column, never in feature_json, so they can never reach the frozen
            # champion/challenger/calibration; they only exist to be graded against
            # the settled outcome. Additive; old rows read NULL.
            self._ensure_column(connection, "predictions", "shadow_signal_json", "shadow_signal_json TEXT")
            # One active pushed prediction per timeframe: the contract currently
            # occupying each checkpoint's slot, held until it closes so a second
            # prediction for the same time frame is never pushed while one is live.
            connection.execute(
                """CREATE TABLE IF NOT EXISTS pushed_slots(
                    model_version TEXT NOT NULL, checkpoint TEXT NOT NULL,
                    ticker TEXT NOT NULL, close_time REAL, pushed_at REAL NOT NULL,
                    PRIMARY KEY(model_version, checkpoint)
                )"""
            )
            # Exactly one follow-up check per (contract, interval) after an entry was
            # recommended: armed when the entry alert is delivered, fired once at
            # due_at, then never again. Keyed per (ticker, checkpoint) so a 15M
            # follow-up never blocks the same contract's 10M follow-up.
            connection.execute(
                """CREATE TABLE IF NOT EXISTS entry_followups(
                    model_version TEXT NOT NULL, ticker TEXT NOT NULL, checkpoint TEXT NOT NULL,
                    asset TEXT, side TEXT, recommended_at REAL NOT NULL, due_at REAL NOT NULL,
                    sent_at REAL,
                    PRIMARY KEY(model_version, ticker, checkpoint)
                )"""
            )
            # Warning-performance log: one row per HIGH FLIP RISK alert that fired,
            # reconciled against whether the frozen prediction actually flipped.
            connection.execute(
                """CREATE TABLE IF NOT EXISTS flip_warnings(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_version TEXT NOT NULL, asset TEXT NOT NULL, checkpoint TEXT NOT NULL,
                    ticker TEXT NOT NULL, direction TEXT NOT NULL,
                    risk_score REAL, flip_probability REAL, confidence REAL, created_at REAL NOT NULL,
                    resolved INTEGER DEFAULT 0, flip_occurred INTEGER, advance_seconds REAL, realized_cents REAL,
                    UNIQUE(model_version, ticker, checkpoint, direction)
                )"""
            )
            # OFFICIAL RECORD — immutable snapshot of every prediction that was
            # successfully DELIVERED to the owner before the contract closed. This
            # is the source of truth for the public win/loss record: a prediction
            # only counts here if it was actually sent (has a Telegram message_id)
            # and sent_at < close_time. Rows are insert-only and NEVER edited or
            # retroactively regraded — grading joins to predictions.official_result
            # at read time, so the stored call stays exactly as it was sent.
            connection.execute(
                """CREATE TABLE IF NOT EXISTS sent_predictions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_version TEXT NOT NULL,
                    contract_id TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    record_type TEXT NOT NULL,
                    predicted_side TEXT,
                    probability REAL,
                    manipulation_probability REAL,
                    entry_decision TEXT,
                    sent_at REAL NOT NULL,
                    close_time REAL,
                    message_id INTEGER,
                    UNIQUE(model_version, contract_id, interval, record_type, message_id)
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_v95_sent_predictions "
                "ON sent_predictions(model_version, record_type, interval)"
            )
            connection.execute(
                # One official interval report per (interval, 15-min window). Once a
                # report is delivered the row exists and the lock holds — later
                # analysis never resends or replaces that interval's report.
                """CREATE TABLE IF NOT EXISTS official_report_lock(
                    model_version TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    window_key INTEGER NOT NULL,
                    locked_at REAL NOT NULL,
                    message_id INTEGER,
                    PRIMARY KEY(model_version, interval, window_key)
                )"""
            )
            connection.execute(
                # OBSERVATIONAL ONLY — the entry-timing experiment. Records what the
                # model would have predicted at extra time-marks (e.g. 13/12/11 min
                # remaining, in seconds) so we can MEASURE where accuracy crosses
                # into an edge instead of guessing. Never delivered, never an
                # official record, isolated from the 15M/10M/7M interval semantics
                # (free-form mark_seconds, no CHECK constraint). Graded on
                # settlement alongside the real predictions.
                """CREATE TABLE IF NOT EXISTS timing_experiment(
                    model_version TEXT NOT NULL,
                    contract TEXT NOT NULL,
                    mark_seconds INTEGER NOT NULL,
                    asset TEXT,
                    created_at REAL NOT NULL,
                    predicted_side TEXT,
                    yes_probability REAL,
                    selected_probability REAL,
                    confidence_grade TEXT,
                    close_time REAL,
                    snapshot_id TEXT,
                    official_result TEXT,
                    resolved_at REAL,
                    PRIMARY KEY(model_version, contract, mark_seconds)
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_v95_timing_experiment "
                "ON timing_experiment(model_version, mark_seconds, official_result)"
            )
            # Strict flip-decision engine (OBSERVATIONAL). One immutable decision
            # per (contract, checkpoint) — UNIQUE prevents repeated predictions
            # for the same interval. Stored BEFORE the outcome with the threshold
            # in force at decision time, graded on settlement (flipped =
            # predicted_side != official_result). Archived + cleared by a reset;
            # active rows are post-reset only. Never delivered as its own report.
            for _tbl in ("flip_decisions", "flip_decisions_archive"):
                connection.execute(
                    f"""CREATE TABLE IF NOT EXISTS {_tbl}(
                        model_version TEXT NOT NULL,
                        contract TEXT NOT NULL,
                        checkpoint TEXT NOT NULL,
                        asset TEXT,
                        created_at REAL NOT NULL,
                        predicted_side TEXT,
                        flip_probability REAL,
                        threshold REAL,
                        decision TEXT,
                        validated INTEGER NOT NULL DEFAULT 0,
                        close_time REAL,
                        snapshot_id TEXT,
                        official_result TEXT,
                        flipped INTEGER,
                        correct INTEGER,
                        resolved_at REAL,
                        PRIMARY KEY(model_version, contract, checkpoint)
                    )"""
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_v95_flip_decisions "
                "ON flip_decisions(model_version, checkpoint, official_result)"
            )
            now = time.time()
            for checkpoint in LEARNING_CHECKPOINTS:
                for name, base in CHAMPION_WEIGHTS.items():
                    connection.execute(
                        "INSERT OR IGNORE INTO checkpoint_challenger_weights(checkpoint,name,base_value,value,updated_at) VALUES(?,?,?,?,?)",
                        (checkpoint, name, base, base, now),
                    )
            connection.commit()

    # SQLite identifiers (table/column names) cannot be passed as bound
    # parameters, so any name spliced into DDL must be proven to be a plain
    # identifier first. This whitelist closes the one parameterisation gap in the
    # file: even though every current caller passes a literal, a name that ever
    # came from config/env can no longer reach the SQL string unvalidated.
    _IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    @classmethod
    def _safe_identifier(cls, name: str) -> str:
        if not cls._IDENTIFIER_RE.match(str(name or "")):
            raise ValueError(f"unsafe SQL identifier: {name!r}")
        return str(name)

    @classmethod
    def _ensure_column(cls, connection: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        safe_table = cls._safe_identifier(table)
        safe_column = cls._safe_identifier(column)
        # The DDL fragment must begin with the (validated) column name; validating
        # it here keeps the ALTER statement's identifier provably safe while the
        # trailing type/constraint text stays a controlled, code-supplied literal.
        if not str(ddl).startswith(safe_column):
            raise ValueError(f"column DDL {ddl!r} does not begin with column name {column!r}")
        existing = {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({safe_table})")}
        if safe_column not in existing:
            connection.execute(f"ALTER TABLE {safe_table} ADD COLUMN {ddl}")

    @staticmethod
    def _checkpoint(value: Any) -> str:
        checkpoint = str(value or "").strip().upper()
        return checkpoint if checkpoint in TRACKED_CHECKPOINTS else "10M"

    def learning_enabled(self, checkpoint: str) -> bool:
        return bool(self.learning_enabled_by_checkpoint[self._checkpoint(checkpoint)])

    @staticmethod
    def _regime_key(regime: Any) -> str:
        # Bounded to 64 chars: regime labels are short codes (e.g. "BULL_TREND"),
        # but the value flows from upstream snapshots into the regime_challenger
        # table key, so cap it defensively to keep an anomalous/huge string from
        # bloating the table or the per-regime caches.
        key = (str(regime or "UNKNOWN").strip().upper() or "UNKNOWN")
        return key[:64]

    def challenger_weights(self, checkpoint: str | None = None, regime: str | None = None) -> dict[str, float]:
        """Weights for the shadow challenger.

        When a regime is supplied and its per-regime challenger has matured
        (>= minimum_regime_updates resolved results), the regime-specialized
        weights are returned; otherwise the global checkpoint challenger is used.
        """
        checkpoint = self._checkpoint(checkpoint or self.primary_learning_checkpoint)
        if not self._available:
            return dict(CHAMPION_WEIGHTS)
        use_regime = bool(self.regime_learning_enabled and regime)
        regime_key = self._regime_key(regime) if use_regime else None
        cache_key = (checkpoint, regime_key)
        with self._lock:
            version = self._data_version
            if self._cache_enabled:
                cached = self._challenger_weights_cache.get(cache_key)
                if cached is not None and cached[0] == version:
                    return dict(cached[1])  # fresh copy: callers may treat it as owned
            with closing(self._connect()) as connection:
                result: dict[str, float] | None = None
                if use_regime:
                    regime_weights = {
                        str(row["name"]): float(row["value"])
                        for row in connection.execute(
                            "SELECT name,value FROM regime_challenger_weights WHERE checkpoint=? AND regime=? ORDER BY name",
                            (checkpoint, regime_key),
                        )
                    }
                    maturity = connection.execute(
                        "SELECT updates FROM regime_challenger_weights WHERE checkpoint=? AND regime=? AND name='intercept'",
                        (checkpoint, regime_key),
                    ).fetchone()
                    if regime_weights and maturity is not None and int(maturity["updates"] or 0) >= self.minimum_regime_updates:
                        result = regime_weights
                if result is None:
                    rows = connection.execute(
                        "SELECT name,value FROM checkpoint_challenger_weights WHERE checkpoint=? ORDER BY name",
                        (checkpoint,),
                    )
                    weights = {str(row["name"]): float(row["value"]) for row in rows}
                    result = weights or dict(CHAMPION_WEIGHTS)
            if self._cache_enabled:
                self._challenger_weights_cache[cache_key] = (version, dict(result))
            return dict(result)

    def record_prediction(self, *, ticker: str, asset: str, checkpoint: str, created_at: float,
                          close_time: float | None, predicted_side: str, raw_yes_probability: float,
                          calibrated_yes_probability: float, challenger_yes_probability: float,
                          baseline_yes_probability: float, selected_probability: float,
                          conservative_probability: float, data_quality: float, evidence_quality: float,
                          trade_quality: float, trade_decision: str, regime: str,
                          features: Mapping[str, Any], contributions: Mapping[str, Any],
                          quote: Mapping[str, Any], rank: int | None = None,
                          costs: Mapping[str, Any] | None = None,
                          confidence_grade: str | None = None,
                          manipulation_suspected: bool = False,
                          manipulation_reason: str | None = None,
                          flip_risk_score: float | None = None,
                          flip_risk_confidence: float | None = None,
                          flip_evidence_count: int | None = None,
                          shadow_factors: Mapping[str, Any] | None = None,
                          shadow_signals: Mapping[str, Any] | None = None,
                          snapshot_id: str | None = None) -> tuple[str, bool]:
        checkpoint = self._checkpoint(checkpoint)
        prediction_id = f"{MODEL_VERSION}|{checkpoint}|{ticker}"
        if not self._available or not ticker:
            return prediction_id, False
        rank_value = None
        try:
            rank_value = int(rank) if rank is not None else None
        except (TypeError, ValueError):
            rank_value = None
        # Capture the paper entry price and estimated costs so realized P&L can be
        # computed at settlement: a win pays 100 minus ask and costs, a loss
        # forfeits ask plus costs. None when no executable ask was available.
        quote = dict(quote or {})
        costs = dict(costs or {})
        entry_ask = _num(quote.get("ask_cents"))
        entry_cost = _num(costs.get("total_cents"))
        if entry_cost is None:
            entry_cost = _num(costs.get("total_cost_cents"), 0.0)
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO predictions(
                       prediction_id,model_version,feature_schema_version,ticker,asset,checkpoint,
                       created_at,close_time,predicted_side,raw_yes_probability,
                       calibrated_yes_probability,challenger_yes_probability,baseline_yes_probability,
                       selected_probability,conservative_probability,data_quality,evidence_quality,
                       trade_quality,trade_decision,regime,rank,entry_ask_cents,entry_cost_cents,
                       feature_json,contribution_json,quote_json,
                       confidence_grade,original_predicted_side,changed_before_close,
                       manipulation_suspected,manipulation_reason,
                       flip_risk_score,flip_risk_confidence,flip_evidence_count)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)""",
                (
                    prediction_id, MODEL_VERSION, FEATURE_SCHEMA_VERSION, ticker, asset, checkpoint,
                    created_at, close_time, predicted_side, raw_yes_probability,
                    calibrated_yes_probability, challenger_yes_probability, baseline_yes_probability,
                    selected_probability, conservative_probability, data_quality, evidence_quality,
                    trade_quality, trade_decision, regime, rank_value, entry_ask, entry_cost,
                    _json(dict(features)), _json(dict(contributions)), _json(dict(quote)),
                    (str(confidence_grade).upper() if confidence_grade else None), predicted_side,
                    1 if manipulation_suspected else 0,
                    (str(manipulation_reason) or None) if manipulation_reason else None,
                    _num(flip_risk_score), _num(flip_risk_confidence),
                    int(flip_evidence_count) if flip_evidence_count is not None else None,
                ),
            )
            inserted = cursor.rowcount == 1
            # Shadow factors land in their isolated column in the SAME transaction,
            # only on a fresh insert (never overwrites a locked prediction).
            if inserted and shadow_factors:
                connection.execute(
                    "UPDATE predictions SET shadow_factor_json=? WHERE prediction_id=?",
                    (_json(dict(shadow_factors)), prediction_id),
                )
            # Experimental shadow signals land in their own isolated column in the
            # same transaction, only on a fresh insert (never overwrites a lock).
            if inserted and shadow_signals:
                connection.execute(
                    "UPDATE predictions SET shadow_signal_json=? WHERE prediction_id=?",
                    (_json(dict(shadow_signals)), prediction_id),
                )
            connection.commit()
        if inserted:
            # Pass production's ACTUAL decision (the sent ``predicted_side`` from the
            # CALIBRATED prob, the calibrated prob itself, and the cross-asset rank)
            # alongside the raw champion prob, so the Shadow-vs-Your-System report
            # grades/ranks "Your System" by what it really predicted — not by
            # re-thresholding the raw probability (which flips the side, and the
            # ✓/✗, on ~38% of rows).
            self._shadow_observe(
                ticker=ticker, asset=asset, checkpoint=checkpoint, created_at=created_at,
                close_time=close_time, control_prob_yes=raw_yes_probability,
                native_predicted_side=predicted_side,
                native_prob_yes=calibrated_yes_probability, native_rank=rank_value,
                features=features, quote=quote, snapshot_id=snapshot_id,
            )
        return prediction_id, inserted

    # -- read-only challenger shadow (default-OFF; never affects production) --
    def _shadow_observe(self, **kw) -> None:
        try:
            from q15_upgrade.challenger.runner import get_runner
            runner = get_runner()
            if runner is not None:
                runner.observe(**kw)
        except Exception as exc:
            self._note_shadow_error("observe", exc)

    def _shadow_mark_sent(self, ticker: str, checkpoint: str) -> None:
        """Tell the shadow that Your System's prediction for (ticker, checkpoint)
        was delivered before close, so it counts in the visible Shadow-vs-Yours
        record. Read-only wrt production; never raises into the alert path."""
        try:
            from q15_upgrade.challenger.runner import get_runner
            runner = get_runner()
            if runner is not None:
                runner.mark_native_sent(str(ticker), str(checkpoint))
        except Exception as exc:
            self._note_shadow_error("mark_sent", exc)

    def _shadow_mark_failed(self, ticker: str, checkpoint: str, error: str) -> None:
        """Tell the shadow that Your System's official send for (ticker, checkpoint)
        FAILED. The shadow keeps the generated pick as a background DELIVERY_FAILED
        row (out of the visible win/loss totals) with the exact error, so it is not
        silently lost. Read-only wrt production; never raises into the alert path."""
        try:
            from q15_upgrade.challenger.runner import get_runner
            runner = get_runner()
            if runner is not None:
                runner.mark_native_delivery_failed(str(ticker), str(checkpoint), str(error))
        except Exception as exc:
            self._note_shadow_error("mark_failed", exc)

    def _shadow_mark_pending(self, ticker: str, checkpoint: str, delivery_key: str) -> None:
        """Tell the shadow that Your System's official report for (ticker, checkpoint)
        was durably QUEUED in the outbox under ``delivery_key`` — outcome to be
        reconciled from the outbox's true status later (so a background-worker
        delivery is credited, not mis-scored as a failure). Read-only wrt
        production; never raises into the alert path."""
        try:
            from q15_upgrade.challenger.runner import get_runner
            runner = get_runner()
            if runner is not None:
                runner.mark_native_pending(str(ticker), str(checkpoint), str(delivery_key))
        except Exception as exc:
            self._note_shadow_error("mark_pending", exc)

    def _shadow_reconcile_delivery(self, status_lookup: Any) -> dict[str, int]:
        """Reconcile pending native picks against the outbox's true delivery status
        (``status_lookup(key) -> 'SENT'|'DEAD_LETTER'|...|None``). Promotes picks
        whose official report actually delivered (sync OR worker) and fails only
        those whose report dead-lettered. Read-only wrt production; never raises."""
        try:
            from q15_upgrade.challenger.runner import get_runner
            runner = get_runner()
            if runner is not None:
                return runner.reconcile_native_delivery(status_lookup)
        except Exception as exc:
            self._note_shadow_error("reconcile_delivery", exc)
        return {"promoted": 0, "failed": 0}

    def _shadow_resolve(self, events) -> None:
        try:
            from q15_upgrade.challenger.runner import get_runner
            runner = get_runner()
            if runner is None:
                return
            for ev in events:
                runner.resolve(str(ev.get("ticker")), str(ev.get("checkpoint")),
                               str(ev.get("official_result")))
        except Exception as exc:
            self._note_shadow_error("resolve", exc)

    def _note_shadow_error(self, op: str, exc: BaseException) -> None:
        """Record a swallowed shadow-challenger failure so it is observable.

        Read-only wrt production: the shadow runner is purely observational, so a
        failure here must never propagate into the alert path. We still count it
        and keep the last message so stats()/health can show the shadow is broken
        instead of silently degraded. The full traceback stays at DEBUG."""
        self._shadow_errors += 1
        self._last_shadow_error = f"{op}: {type(exc).__name__}: {exc}"
        logger.debug("challenger shadow %s skipped", op, exc_info=True)

    def frozen_prediction(self, ticker: str, checkpoint: str) -> dict[str, Any] | None:
        """The frozen (first-recorded) side + flip-risk score for (ticker, checkpoint).

        Used to detect a confirmed flip — when a later checkpoint's frozen side
        differs from an earlier one for the same contract. Returns None if that
        checkpoint was never recorded for the ticker."""
        if not self._available or not ticker:
            return None
        checkpoint = self._checkpoint(checkpoint)
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT predicted_side, flip_risk_score, flip_risk_confidence FROM predictions "
                "WHERE model_version=? AND checkpoint=? AND ticker=?",
                (MODEL_VERSION, checkpoint, str(ticker)),
            ).fetchone()
        if row is None:
            return None
        return {
            "side": str(row["predicted_side"] or "").upper() or None,
            "flip_risk_score": _num(row["flip_risk_score"]),
            "flip_risk_confidence": _num(row["flip_risk_confidence"]),
        }

    # -- pushed-prediction accounting + one-active-per-timeframe slot lock ------
    def mark_pushed(self, ticker: str, checkpoint: str) -> bool:
        """Flag the (ticker, checkpoint) prediction as actually pushed to the user.

        Idempotent. Lets the scoreboard report pushed-only accuracy without
        background observations inflating it."""
        if not self._available or not ticker:
            return False
        checkpoint = self._checkpoint(checkpoint)
        with self._lock, closing(self._connect()) as connection:
            cur = connection.execute(
                "UPDATE predictions SET pushed=1 WHERE model_version=? AND checkpoint=? AND ticker=?",
                (MODEL_VERSION, checkpoint, str(ticker)),
            )
            connection.commit()
            return cur.rowcount > 0

    # -- OFFICIAL RECORD: immutable sent-prediction snapshot + grading -----------
    def record_sent_prediction(self, *, contract_id: str, asset: str, interval: str,
                               record_type: str, predicted_side: str | None,
                               probability: float | None, manipulation_probability: float | None,
                               entry_decision: str | None, sent_at: float,
                               close_time: float | None, message_id: int | None) -> bool:
        """Append an immutable record of a prediction that was DELIVERED to the
        owner. A row only earns a place in the official record if it was actually
        sent before close: ``message_id`` is not None AND ``sent_at < close_time``.
        Anything else (muted, failed, or generated after close) is rejected here so
        it can never reach the public win/loss totals. Insert-only; never updated.

        ``record_type`` is one of 'interval' (the 15M/10M/7M YES/NO call), 'entry'
        (an ENTER NOW / ENTRY RECOMMENDED delivery), or 'manipulation'."""
        if not self._available or not contract_id:
            return False
        if message_id is None:
            return False  # not actually delivered to Telegram -> not official
        if close_time is not None and sent_at >= float(close_time):
            return False  # generated at/after close -> never official
        interval = self._checkpoint(interval)
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO sent_predictions(model_version,contract_id,asset,interval,"
                "record_type,predicted_side,probability,manipulation_probability,entry_decision,"
                "sent_at,close_time,message_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (MODEL_VERSION, str(contract_id), str(asset), interval, str(record_type),
                 (str(predicted_side).upper() if predicted_side else None),
                 (float(probability) if probability is not None else None),
                 (float(manipulation_probability) if manipulation_probability is not None else None),
                 (str(entry_decision) if entry_decision else None),
                 float(sent_at), (float(close_time) if close_time is not None else None),
                 int(message_id)),
            )
            connection.commit()
        return True

    # -- entry-timing experiment (OBSERVATIONAL ONLY) ---------------------------
    def record_timing_observation(self, *, contract: str, mark_seconds: int, asset: str | None,
                                  predicted_side: str | None, yes_probability: float | None,
                                  selected_probability: float | None, confidence_grade: str | None,
                                  created_at: float, close_time: float | None,
                                  snapshot_id: str | None = None) -> bool:
        """Record (insert-only) what the model predicted at an extra time-mark.

        Read-only/observational: never delivered, never an official record. One
        row per (contract, mark_seconds) — the first write wins; later cycles in
        the same band are ignored so the mark captures the model's call at that
        time-to-close. Graded later by :meth:`resolve_ticker`."""
        if not self._available or not contract:
            return False
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO timing_experiment(model_version,contract,mark_seconds,asset,"
                "created_at,predicted_side,yes_probability,selected_probability,confidence_grade,"
                "close_time,snapshot_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (MODEL_VERSION, str(contract), int(mark_seconds),
                 (str(asset) if asset else None), float(created_at),
                 (str(predicted_side).upper() if predicted_side else None),
                 (float(yes_probability) if yes_probability is not None else None),
                 (float(selected_probability) if selected_probability is not None else None),
                 (str(confidence_grade) if confidence_grade else None),
                 (float(close_time) if close_time is not None else None),
                 (str(snapshot_id) if snapshot_id else None)),
            )
            connection.commit()
        return True

    def timing_experiment_scoreboard(self) -> dict[str, Any]:
        """Per-mark accuracy of the observational timing experiment.

        Lets us SEE where (e.g. 13 vs 12 vs 11 vs the live 10M) accuracy crosses
        into a usable edge, with Wilson CIs so a thin mark reads ``low_n`` rather
        than as proven. Read-only."""
        empty = {"available": bool(self._available), "by_mark": {}}
        if not self._available:
            return {"available": False, "by_mark": {}}
        threshold = _env_int("Q15_V95_SCOREBOARD_MIN_N", 10, 1, 1000)
        with self._lock, closing(self._connect()) as connection:
            rows = list(connection.execute(
                "SELECT mark_seconds, "
                "SUM(CASE WHEN predicted_side=official_result THEN 1 ELSE 0 END) AS right, "
                "SUM(CASE WHEN official_result IS NOT NULL THEN 1 ELSE 0 END) AS n, "
                "SUM(CASE WHEN official_result IS NULL THEN 1 ELSE 0 END) AS pending "
                "FROM timing_experiment WHERE model_version=? "
                "GROUP BY mark_seconds ORDER BY mark_seconds DESC",
                (MODEL_VERSION,),
            ))
        by_mark: dict[str, Any] = {}
        for r in rows:
            mark = int(r["mark_seconds"])
            right = int(r["right"] or 0)
            n = int(r["n"] or 0)
            low, high = _wilson_interval(right, n)
            by_mark[str(mark)] = {
                "minutes": round(mark / 60.0, 1),
                "right": right, "wrong": n - right, "n": n,
                "pending": int(r["pending"] or 0),
                "accuracy": round(right / n, 4) if n else None,
                "ci_low": low, "ci_high": high,
                "low_n": bool(n and n < threshold),
                "ci_excludes_half": bool(
                    n and ((low is not None and low > 0.5) or (high is not None and high < 0.5))
                ),
            }
        empty["by_mark"] = by_mark
        return empty

    # -- strict flip-decision engine (OBSERVATIONAL) ----------------------------
    def flip_reset_at(self) -> float:
        """Timestamp of the last flip-data reset (0.0 if never). Active flip
        decisions are those created at/after this; pre-reset rows are archived."""
        if not self._available:
            return 0.0
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='flip_reset_at'"
            ).fetchone()
        try:
            return float(row["value"]) if row else 0.0
        except (TypeError, ValueError):
            return 0.0

    def reset_flip_data(self, archive: bool = True, now: float | None = None) -> dict[str, Any]:
        """Full reset of the flip dataset: archive every current decision, clear
        the active table, and stamp the reset time so thresholds/metrics are
        recalculated ONLY from post-reset data. Pre-reset rows are excluded from
        active scoring (kept in ``flip_decisions_archive`` when ``archive``)."""
        if not self._available:
            return {"reset": False, "archived": 0}
        now = now if now is not None else time.time()
        with self._lock, closing(self._connect()) as connection:
            archived = 0
            if archive:
                cur = connection.execute(
                    "INSERT OR IGNORE INTO flip_decisions_archive SELECT * FROM flip_decisions "
                    "WHERE model_version=?", (MODEL_VERSION,))
                archived = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            connection.execute("DELETE FROM flip_decisions WHERE model_version=?", (MODEL_VERSION,))
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES('flip_reset_at',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now),))
            connection.commit()
        return {"reset": True, "archived": archived, "reset_at": now}

    def flip_reset_status(self) -> dict[str, Any]:
        """Reset-status: how many active (post-reset) vs archived (pre-reset)
        flip decisions exist, and whether normal output is post-reset-only."""
        if not self._available:
            return {"available": False}
        with self._lock, closing(self._connect()) as connection:
            active = connection.execute(
                "SELECT COUNT(*) FROM flip_decisions WHERE model_version=?", (MODEL_VERSION,)).fetchone()[0]
            archived = connection.execute(
                "SELECT COUNT(*) FROM flip_decisions_archive WHERE model_version=?", (MODEL_VERSION,)).fetchone()[0]
        return {
            "available": True, "reset_at": self.flip_reset_at(),
            "active_decisions": int(active), "archived_decisions": int(archived),
            # The active table holds post-reset rows only, so normal output is
            # inherently post-reset; the flag governs whether diagnostics may also
            # surface the archived (pre-reset) totals.
            "show_post_reset_only": _env_bool("Q15_V95_FLIP_SHOW_POST_RESET_ONLY", True),
        }

    def record_flip_decision(self, *, contract: str, checkpoint: str, asset: str | None,
                             predicted_side: str | None, flip_probability: float | None,
                             threshold: float | None, decision: str, validated: bool,
                             created_at: float, close_time: float | None,
                             snapshot_id: str | None = None) -> bool:
        """Record (insert-only) one flip decision before the outcome is known.

        One row per (contract, checkpoint) — the first write wins, so an interval
        is never re-decided for the same contract. Stores the threshold actually
        in force, so later threshold drift can't retroactively change this graded
        decision."""
        if not self._available or not contract:
            return False
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO flip_decisions(model_version,contract,checkpoint,asset,"
                "created_at,predicted_side,flip_probability,threshold,decision,validated,close_time,"
                "snapshot_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (MODEL_VERSION, str(contract), self._checkpoint(checkpoint),
                 (str(asset) if asset else None), float(created_at),
                 (str(predicted_side).upper() if predicted_side else None),
                 (float(flip_probability) if flip_probability is not None else None),
                 (float(threshold) if threshold is not None else None),
                 str(decision).upper(), 1 if validated else 0,
                 (float(close_time) if close_time is not None else None),
                 (str(snapshot_id) if snapshot_id else None)),
            )
            connection.commit()
        return True

    def flip_decision_rows(self, checkpoint: str | None = None, resolved_only: bool = True,
                           post_reset_only: bool = True) -> list[dict[str, Any]]:
        """Active flip decisions for threshold selection / metrics. Post-reset
        rows only by default; chronological (oldest first) for OOS splitting."""
        if not self._available:
            return []
        clauses = ["model_version=?"]
        params: list[Any] = [MODEL_VERSION]
        if checkpoint is not None:
            clauses.append("checkpoint=?")
            params.append(self._checkpoint(checkpoint))
        if resolved_only:
            clauses.append("official_result IS NOT NULL")
        if post_reset_only:
            clauses.append("created_at >= ?")
            params.append(self.flip_reset_at())
        with self._lock, closing(self._connect()) as connection:
            rows = list(connection.execute(
                "SELECT created_at, checkpoint, asset, predicted_side, flip_probability, "
                "threshold, decision, official_result, flipped, correct "
                f"FROM flip_decisions WHERE {' AND '.join(clauses)} ORDER BY created_at ASC",
                params,
            ))
        return [{
            "created_at": r["created_at"], "checkpoint": r["checkpoint"], "asset": r["asset"],
            "predicted_side": r["predicted_side"], "flip_probability": r["flip_probability"],
            "threshold": r["threshold"], "decision": r["decision"],
            "official_result": r["official_result"],
            "flipped": bool(r["flipped"]) if r["flipped"] is not None else None,
            "correct": bool(r["correct"]) if r["correct"] is not None else None,
        } for r in rows]

    def flip_decision_report(self) -> dict[str, Any]:
        """Per-interval flip-decision summary: the learned threshold, whether it
        is validated to the target precision out of sample, and the served-decision
        metrics (YES-precision / recall / FPR / FNR / coverage / n) by interval and
        asset. Read-only; post-reset data only."""
        from q15_upgrade import flip_decision as fd
        out: dict[str, Any] = {"available": bool(self._available),
                               "reset_at": self.flip_reset_at(), "by_interval": {}}
        if not self._available:
            return {"available": False, "by_interval": {}}
        for cp in fd.INTERVALS:
            rows = self.flip_decision_rows(cp, resolved_only=True, post_reset_only=True)
            cfg = fd.FlipConfig.from_env(cp)
            block = fd.evaluate(rows, cfg)
            # by-asset served precision (diagnostic; hidden from the owner panel)
            by_asset: dict[str, dict[str, Any]] = {}
            for r in rows:
                a = str(r.get("asset") or "?")
                d = by_asset.setdefault(a, {"yes": 0, "yes_correct": 0, "n": 0})
                d["n"] += 1
                if str(r.get("decision") or "").upper() == "YES":
                    d["yes"] += 1
                    if r.get("flipped"):
                        d["yes_correct"] += 1
            for a, d in by_asset.items():
                d["yes_precision"] = round(d["yes_correct"] / d["yes"], 4) if d["yes"] else None
            block["by_asset"] = by_asset
            out["by_interval"][cp] = block
        out["all_intervals_validated"] = all(
            (out["by_interval"].get(cp, {}).get("selection", {}) or {}).get("validated")
            for cp in fd.INTERVALS
        )
        return out

    # -- ONE official report per interval per window (report-frequency lock) -----
    @staticmethod
    def _report_window_key(close_time: float | None, now: float) -> int:
        """Bucket a contract into its 15-minute window so all assets settling
        together share one lock. Falls back to ``now`` when close is unknown."""
        basis = float(close_time) if close_time is not None else float(now)
        return int(basis // 900)

    def report_locked(self, interval: str, close_time: float | None, now: float) -> bool:
        """True if an official report for this (interval, 15-min window) has already
        been delivered — so no duplicate/recheck/resend is sent for it."""
        if not self._available:
            return False
        interval = self._checkpoint(interval)
        window_key = self._report_window_key(close_time, now)
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM official_report_lock WHERE model_version=? AND interval=? "
                "AND window_key=?",
                (MODEL_VERSION, interval, window_key),
            ).fetchone()
        return row is not None

    def lock_official_report(self, interval: str, close_time: float | None, now: float,
                             message_id: int | None) -> bool:
        """Lock this (interval, window) on a real delivery. Returns True if this
        call created the lock (first delivery), False if it was already locked
        (idempotent — a concurrent/duplicate send is rejected)."""
        if not self._available:
            return False
        interval = self._checkpoint(interval)
        window_key = self._report_window_key(close_time, now)
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO official_report_lock(model_version,interval,window_key,"
                "locked_at,message_id) VALUES(?,?,?,?,?)",
                (MODEL_VERSION, interval, window_key, float(now),
                 (int(message_id) if message_id is not None else None)),
            )
            connection.commit()
            return cursor.rowcount == 1

    def unlock_official_report(self, interval: str, close_time: float | None, now: float) -> None:
        """Release a claimed lock when the send did NOT deliver (muted/failed), so
        the next cycle can retry. A delivered report keeps its lock permanently."""
        if not self._available:
            return
        interval = self._checkpoint(interval)
        window_key = self._report_window_key(close_time, now)
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "DELETE FROM official_report_lock WHERE model_version=? AND interval=? "
                "AND window_key=?",
                (MODEL_VERSION, interval, window_key),
            )
            connection.commit()

    def last_official_report_at(self, interval: str) -> float | None:
        """Most recent delivered-and-locked time for this interval across ALL
        windows, or None. Backs a per-interval minimum-gap guard that caps the
        same interval's report to once per window even if the window key is
        momentarily unstable (e.g. a contract-mapping flip near the boundary)."""
        if not self._available:
            return None
        interval = self._checkpoint(interval)
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT MAX(locked_at) AS last FROM official_report_lock "
                "WHERE model_version=? AND interval=?",
                (MODEL_VERSION, interval),
            ).fetchone()
        return None if row is None or row["last"] is None else float(row["last"])

    def official_scoreboard(self) -> dict[str, Any]:
        """Official win/loss record built ONLY from delivered predictions, graded
        against the settled outcome. Interval records (15M/10M/7M) and the entry
        record split YES vs NO vs Total; the manipulation record is correct/wrong.
        Each bucket reuses the Wilson-backed ``_win_loss`` so a thin sample is
        flagged ``low_n`` rather than read as proven skill."""
        empty = {"15M": {}, "10M": {}, "7M": {}, "entry": {}, "manipulation": {}}
        if not self._available:
            return {"available": False, **empty}
        with self._lock, closing(self._connect()) as connection:
            rows = list(connection.execute(
                """SELECT s.interval AS interval, s.record_type AS record_type,
                          s.predicted_side AS predicted_side, p.official_result AS official_result,
                          p.realized_cents AS realized_cents
                   FROM sent_predictions s
                   JOIN predictions p
                     ON p.model_version = s.model_version AND p.ticker = s.contract_id
                    AND p.checkpoint = s.interval
                   WHERE s.model_version = ? AND p.official_result IS NOT NULL""",
                (MODEL_VERSION,),
            ))

        def _graded(record_type: str, interval: str | None, side: str | None) -> dict[str, Any]:
            sel = [
                r for r in rows
                if str(r["record_type"]) == record_type
                and (interval is None or str(r["interval"]) == interval)
                and (side is None or str(r["predicted_side"] or "").upper() == side)
            ]
            # _win_loss keys on row["correct"]; compute it from the sent call vs
            # the settled result so a regrade can never alter the stored prediction.
            graded = [
                {"correct": 1 if str(r["predicted_side"] or "").upper() == str(r["official_result"]).upper() else 0,
                 "realized_cents": r["realized_cents"]}
                for r in sel
            ]
            return self._win_loss(graded)

        result: dict[str, Any] = {"available": True}
        for interval in ("15M", "10M", "7M"):
            result[interval] = {
                "yes": _graded("interval", interval, "YES"),
                "no": _graded("interval", interval, "NO"),
                "total": _graded("interval", interval, None),
            }
        result["entry"] = {
            "yes": _graded("entry", None, "YES"),
            "no": _graded("entry", None, "NO"),
            "total": _graded("entry", None, None),
        }
        result["manipulation"] = _graded("manipulation", None, None)
        return result

    def contract_recap(self, ticker: str) -> dict[str, Any] | None:
        """Per-contract close-out for a SETTLED contract, built only from what was
        actually delivered (the sent_predictions for this ticker) graded against
        the settled outcome. Returns the per-interval hit/miss, the side flips, the
        entry result, and the manipulation call — or None if nothing was sent for
        this contract or it has not settled."""
        if not self._available or not ticker:
            return None
        order = {"15M": 0, "10M": 1, "7M": 2}
        with self._lock, closing(self._connect()) as connection:
            rows = list(connection.execute(
                """SELECT s.interval AS interval, s.record_type AS record_type,
                          s.predicted_side AS side, s.asset AS asset,
                          p.official_result AS result, p.realized_cents AS realized,
                          p.close_time AS close_time
                   FROM sent_predictions s
                   JOIN predictions p
                     ON p.model_version = s.model_version AND p.ticker = s.contract_id
                    AND p.checkpoint = s.interval
                   WHERE s.model_version = ? AND s.contract_id = ?
                     AND p.official_result IS NOT NULL""",
                (MODEL_VERSION, str(ticker)),
            ))
        if not rows:
            return None
        result = str(rows[0]["result"]).upper()
        asset = str(rows[0]["asset"])
        close_time = _num(_row_get(rows[0], "close_time"))
        ordered = sorted(
            ({"interval": str(r["interval"]).upper(), "side": str(r["side"] or "").upper(),
              "hit": str(r["side"] or "").upper() == result}
             for r in rows if str(r["record_type"]) == "interval"),
            key=lambda iv: order.get(iv["interval"], 9),
        )
        # Side flips across the ordered intervals.
        changes = []
        seq = [(iv["interval"], iv["side"]) for iv in ordered if iv["side"] in ("YES", "NO")]
        for (_, a), (cp_b, b) in zip(seq, seq[1:]):
            if a != b:
                changes.append(f"{a} → {b} at {cp_b}")
        flips = (f"{'; '.join(changes)} ({len(changes)})") if changes else None
        entry = None
        manip = None
        for r in rows:
            rt = str(r["record_type"])
            side = str(r["side"] or "").upper()
            if rt == "entry" and entry is None:
                entry = {"checkpoint": str(r["interval"]).upper(), "decision": "ENTRY RECOMMENDED",
                         "outcome": "WIN" if side == result else "LOSS",
                         "cents": _num(_row_get(r, "realized"))}
            elif rt == "manipulation" and manip is None:
                manip = {"flagged": True, "type": None, "correct": side == result}
        return {"ticker": str(ticker), "asset": asset, "result": result, "close_time": close_time,
                "intervals": ordered, "flips": flips, "entry": entry, "manipulation": manip}

    def pushed_slot_blocks(self, checkpoint: str, ticker: str, now: float) -> bool:
        """True if a DIFFERENT, still-open contract already holds this timeframe's
        active slot — i.e. pushing now would be a second prediction for the same
        time frame. The slot frees on its own once the held contract closes."""
        if not self._available:
            return False
        checkpoint = self._checkpoint(checkpoint)
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT ticker, close_time FROM pushed_slots WHERE model_version=? AND checkpoint=?",
                (MODEL_VERSION, checkpoint),
            ).fetchone()
        if row is None or str(row["ticker"]) == str(ticker):
            return False
        close = _num(row["close_time"])
        return close is None or now < close  # held contract still open -> locked

    def claim_pushed_slot(self, checkpoint: str, ticker: str, close_time: float | None, now: float) -> bool:
        """Occupy this timeframe's active slot with ``ticker`` until it closes.

        Refuses if a different, still-open contract holds it; otherwise claims
        (or refreshes its own claim) and returns True."""
        if not self._available or not ticker:
            return False
        checkpoint = self._checkpoint(checkpoint)
        if self.pushed_slot_blocks(checkpoint, ticker, now):
            return False
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO pushed_slots(model_version,checkpoint,ticker,close_time,pushed_at) "
                "VALUES(?,?,?,?,?)",
                (MODEL_VERSION, checkpoint, str(ticker), _num(close_time), float(now)),
            )
            connection.commit()
            return True

    def active_pushed_slot(self, checkpoint: str, now: float) -> dict[str, Any] | None:
        """The contract currently holding this timeframe's slot, or None if free
        (never claimed, or the held contract has already closed)."""
        if not self._available:
            return None
        checkpoint = self._checkpoint(checkpoint)
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT ticker, close_time, pushed_at FROM pushed_slots WHERE model_version=? AND checkpoint=?",
                (MODEL_VERSION, checkpoint),
            ).fetchone()
        if row is None:
            return None
        close = _num(row["close_time"])
        if close is not None and now >= close:
            return None
        return {"ticker": str(row["ticker"]), "close_time": close, "pushed_at": _num(row["pushed_at"])}

    def arm_entry_followup(self, *, ticker: str, checkpoint: str, asset: str, side: str,
                           now: float, delay: float) -> bool:
        """Arm the single follow-up check for (ticker, checkpoint) on the FIRST
        delivered entry recommendation. INSERT OR IGNORE keeps the original arm
        time, so repeated entry alerts never re-arm or duplicate a follow-up."""
        if not self._available or not ticker:
            return False
        checkpoint = self._checkpoint(checkpoint)
        with self._lock, closing(self._connect()) as connection:
            cur = connection.execute(
                "INSERT OR IGNORE INTO entry_followups(model_version,ticker,checkpoint,asset,side,"
                "recommended_at,due_at,sent_at) VALUES(?,?,?,?,?,?,?,NULL)",
                (MODEL_VERSION, str(ticker), checkpoint, str(asset), str(side or ""),
                 float(now), float(now) + float(delay)),
            )
            connection.commit()
            return cur.rowcount == 1

    def followup_already_sent(self, ticker: str, checkpoint: str) -> bool:
        """True once this (ticker, checkpoint)'s one follow-up has been sent — so
        the live alert can show 'Follow-up check remaining: NO'."""
        if not self._available or not ticker:
            return False
        checkpoint = self._checkpoint(checkpoint)
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT sent_at FROM entry_followups WHERE model_version=? AND ticker=? AND checkpoint=?",
                (MODEL_VERSION, str(ticker), checkpoint),
            ).fetchone()
        return row is not None and row["sent_at"] is not None

    def due_followups(self, now: float) -> list[dict[str, Any]]:
        """Armed follow-ups whose due time has arrived and that have not fired."""
        if not self._available:
            return []
        with self._lock, closing(self._connect()) as connection:
            rows = list(connection.execute(
                "SELECT ticker,checkpoint,asset,side,recommended_at,due_at FROM entry_followups "
                "WHERE model_version=? AND sent_at IS NULL AND due_at<=?",
                (MODEL_VERSION, float(now)),
            ))
        return [
            {"ticker": str(r["ticker"]), "checkpoint": str(r["checkpoint"]),
             "asset": str(r["asset"] or ""), "side": str(r["side"] or ""),
             "recommended_at": _num(r["recommended_at"]), "due_at": _num(r["due_at"])}
            for r in rows
        ]

    def mark_followup_sent(self, ticker: str, checkpoint: str, now: float) -> bool:
        """Consume the one follow-up for (ticker, checkpoint); never fires again."""
        if not self._available or not ticker:
            return False
        checkpoint = self._checkpoint(checkpoint)
        with self._lock, closing(self._connect()) as connection:
            cur = connection.execute(
                "UPDATE entry_followups SET sent_at=? WHERE model_version=? AND ticker=? "
                "AND checkpoint=? AND sent_at IS NULL",
                (float(now), MODEL_VERSION, str(ticker), checkpoint),
            )
            connection.commit()
            return cur.rowcount > 0

    def note_prediction_revision(self, *, ticker: str, checkpoint: str, current_side: str) -> bool:
        """Flag that the live predicted side drifted from the locked prediction
        before the market closed. The recorded (graded) prediction is NOT mutated
        — only ``changed_before_close`` is set — so this measures prediction
        stability ("how often a prediction changes before the interval ends")
        without changing what gets scored. Idempotent; safe to call every cycle.
        """
        checkpoint = self._checkpoint(checkpoint)
        current = str(current_side or "").upper()
        if not self._available or not ticker or current not in {"YES", "NO"}:
            return False
        prediction_id = f"{MODEL_VERSION}|{checkpoint}|{ticker}"
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                "UPDATE predictions SET changed_before_close=1 "
                "WHERE prediction_id=? AND official_result IS NULL "
                "AND changed_before_close=0 AND original_predicted_side IS NOT NULL "
                "AND original_predicted_side<>?",
                (prediction_id, current),
            )
            connection.commit()
            return cursor.rowcount > 0

    @staticmethod
    def _official_result(row: Mapping[str, Any]) -> str | None:
        side = str(row.get("side") or "").upper()
        outcome = str(row.get("outcome") or "").lower()
        if side not in {"YES", "NO"} or outcome not in {"win", "loss"}:
            return None
        return side if outcome == "win" else ("NO" if side == "YES" else "YES")

    # DIAGNOSTIC (read-only): when a CLOSED market has no `result` yet, capture what
    # Kalshi DOES expose so we can wire instant settlement to the field that carries
    # the determined outcome (Kalshi sets `result` only at final settlement, which
    # lags the determination). Curated determination-relevant fields + the full key
    # list; scalar values only (market objects carry no secrets, but stay defensive).
    _MARKET_DIAG_KEYS = (
        "status", "result", "settlement_value", "expiration_value", "settled_time",
        "expiration_time", "close_time", "close_ts", "floor_strike", "cap_strike",
        "strike_type", "can_close_early", "last_price", "yes_bid", "yes_ask",
    )

    @classmethod
    def _undetermined_market_sample(cls, ticker: str, market: Mapping[str, Any]) -> dict[str, Any]:
        sample: dict[str, Any] = {"ticker": str(ticker)}
        for key in cls._MARKET_DIAG_KEYS:
            if key in market:
                value = market.get(key)
                sample[key] = value if isinstance(value, (str, int, float, bool)) or value is None else type(value).__name__
        try:
            sample["all_keys"] = sorted(str(k) for k in market.keys())
        except (AttributeError, TypeError):
            sample["all_keys"] = []
        return sample

    def reconcile_from_signal_store(self, signal_store: Any) -> dict[str, Any]:
        if not self._available:
            return {"available": False, "reason": self._last_error or "ledger_unavailable"}
        query = getattr(signal_store, "query", None)
        if not callable(query):
            return {"available": False, "reason": "signal_store_query_unavailable"}
        try:
            rows = query(
                "SELECT ticker, asset, side, outcome, settled_at FROM signals "
                "WHERE ticker IS NOT NULL AND side IN ('YES','NO') "
                "AND outcome IN ('win','loss') ORDER BY settled_at DESC LIMIT 5000"
            ) or []
        except Exception as exc:
            self._last_error = f"reconcile:{type(exc).__name__}:{exc}"
            return {"available": False, "reason": self._last_error}
        newest: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            ticker = str(row.get("ticker") or "").strip()
            if ticker and ticker not in newest:
                newest[ticker] = row
        resolved = learned = 0
        result_events: list[dict[str, Any]] = []
        for ticker, row in newest.items():
            official = self._official_result(row)
            if official is None:
                continue
            outcome_time = _parse_ts(row.get("settled_at")) or time.time()
            result = self.resolve_ticker(ticker, official, outcome_time)
            resolved += result["resolved"]
            learned += result["updates_applied"]
            result_events.extend(result.get("events", []))
        return {
            "available": True,
            "signal_rows_examined": len(rows),
            "unique_settled_tickers": len(newest),
            "new_predictions_resolved": resolved,
            "shadow_updates_applied": learned,
            "result_events": result_events[:20],
        }

    def reconcile_pending_from_market(self, get_market: Any, now: float | None = None,
                                      max_calls: int = 12) -> dict[str, Any]:
        """Resolve predictions straight from official Kalshi results.

        Unlike reconcile_from_signal_store, this does not depend on a signals-table
        row existing for the ticker: any prediction whose market has closed gets
        graded, so the learning corpus is complete. One REST call per ticker,
        bounded by max_calls per invocation AND a wall-clock budget
        (Q15_V95_RECONCILE_BUDGET_SECONDS) so a batch of slow Kalshi lookups for
        recently-closed-but-unsettled markets can never monopolise the refresh
        loop. Any tickers left over are retried on the next invocation."""
        if not self._available or not callable(get_market):
            return {"available": False, "reason": "unavailable"}
        now = now or time.time()
        budget = _env_float("Q15_V95_RECONCILE_BUDGET_SECONDS", 4.0, 0.5, 60.0)
        with self._lock, closing(self._connect()) as connection:
            rows = list(connection.execute(
                "SELECT DISTINCT ticker FROM predictions "
                "WHERE official_result IS NULL AND close_time IS NOT NULL AND close_time <= ? "
                "ORDER BY close_time LIMIT ?",
                (now, max(1, int(max_calls))),
            ))
        tickers = [str(row["ticker"]) for row in rows if row["ticker"]]
        resolved = learned = calls = 0
        events: list[dict[str, Any]] = []
        undetermined: list[dict[str, Any]] = []  # closed-but-no-result diagnostic samples
        undetermined_limit = _env_int("Q15_V95_UNDETERMINED_SAMPLE_LIMIT", 8, 0, 50)
        started = time.monotonic()
        budget_exceeded = False
        for ticker in tickers:
            # Stop before the next slow REST call would blow the time budget; the
            # remaining tickers are picked up on a later cycle.
            if time.monotonic() - started > budget:
                budget_exceeded = True
                break
            try:
                market = get_market(ticker)
            except Exception as exc:
                self._last_error = f"market_reconcile:{type(exc).__name__}:{exc}"
                continue
            calls += 1
            if not isinstance(market, Mapping):
                continue
            result = str(market.get("result") or "").upper()
            if result not in {"YES", "NO"}:
                # Closed but `result` not populated yet. Capture the raw fields once
                # so we can identify which field carries Kalshi's INSTANT determined
                # outcome (so settlement need not wait for the laggy `result`).
                if len(undetermined) < undetermined_limit:
                    undetermined.append(self._undetermined_market_sample(ticker, market))
                continue  # not officially resolved yet
            outcome_time = _parse_ts(market.get("close_time") or market.get("settled_at")) or now
            outcome = self.resolve_ticker(ticker, result, outcome_time)
            resolved += outcome["resolved"]
            learned += outcome["updates_applied"]
            events.extend(outcome.get("events", []))
        return {
            "available": True, "tickers_checked": len(tickers), "market_calls": calls,
            "new_predictions_resolved": resolved, "shadow_updates_applied": learned,
            "budget_seconds": budget, "budget_exceeded": budget_exceeded,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "result_events": events[:20],
            # Closed contracts still missing `result` this pass, with their raw Kalshi
            # fields — so /api/q15-v9-5/learning shows what the instant outcome field is.
            "undetermined_closed_count": len(undetermined),
            "undetermined_market_samples": undetermined,
        }

    @staticmethod
    def _loss(probability: float, actual_yes: float) -> tuple[float, float]:
        p = _clamp(probability, 1e-6, 1.0 - 1e-6)
        brier = (p - actual_yes) ** 2
        logloss = -(actual_yes * math.log(p) + (1.0 - actual_yes) * math.log(1.0 - p))
        return brier, logloss

    @staticmethod
    def _result_review(row: Mapping[str, Any], official: str) -> dict[str, Any]:
        try:
            contributions = json.loads(str(row.get("contribution_json") or "{}"))
            features = json.loads(str(row.get("feature_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            contributions, features = {}, {}
        predicted = str(row.get("predicted_side") or "").upper()
        direction = 1.0 if predicted == "YES" else -1.0
        ranked = []
        for name, raw in contributions.items():
            if name in {"structural_logit", "intercept", "evidence_total", "temperature"}:
                continue
            try:
                aligned = direction * float(raw)
            except (TypeError, ValueError):
                continue
            ranked.append({"name": name, "aligned_contribution": aligned, "feature": features.get(name)})
        supporters = sorted((item for item in ranked if item["aligned_contribution"] > 0), key=lambda x: x["aligned_contribution"], reverse=True)[:3]
        warnings = sorted((item for item in ranked if item["aligned_contribution"] < 0), key=lambda x: x["aligned_contribution"])[:3]
        correct = predicted == official
        return {
            "correct": correct,
            "top_supporters": supporters,
            "top_warnings": warnings,
            "interpretation": (
                "supporting factors were directionally consistent" if correct
                else "the strongest supporting factors failed; review regime, threshold interaction, and absorption"
            ),
        }

    def resolve_ticker(self, ticker: str, official_result: str, resolved_at: float | None = None) -> dict[str, Any]:
        official = str(official_result).upper()
        if official not in {"YES", "NO"} or not self._available:
            return {"resolved": 0, "updates_applied": 0, "events": []}
        resolved_at = resolved_at or time.time()
        rows: list[sqlite3.Row] = []
        events: list[dict[str, Any]] = []
        with self._lock, closing(self._connect()) as connection:
            # Grade the observational timing experiment for this contract too
            # (read-only; same settlement result, never affects the live record).
            connection.execute(
                "UPDATE timing_experiment SET official_result=?, resolved_at=? "
                "WHERE contract=? AND official_result IS NULL",
                (official, resolved_at, ticker),
            )
            # Grade the flip decisions for this contract: a TRUE flip is the
            # predicted side NOT matching the final settled side; a decision is
            # correct when its YES/NO call matches whether the flip happened.
            connection.execute(
                "UPDATE flip_decisions SET official_result=?, resolved_at=?, "
                "flipped=(CASE WHEN predicted_side IS NULL THEN NULL "
                "WHEN predicted_side<>? THEN 1 ELSE 0 END), "
                "correct=(CASE WHEN predicted_side IS NULL THEN NULL "
                "WHEN (decision='YES')=(predicted_side<>?) THEN 1 ELSE 0 END) "
                "WHERE contract=? AND official_result IS NULL",
                (official, resolved_at, official, official, ticker),
            )
            rows = list(connection.execute(
                "SELECT * FROM predictions WHERE ticker=? AND official_result IS NULL ORDER BY created_at",
                (ticker,),
            ))
            for row in rows:
                actual_yes = 1.0 if official == "YES" else 0.0
                cb, cl = self._loss(float(row["calibrated_yes_probability"]), actual_yes)
                hb, hl = self._loss(float(row["challenger_yes_probability"]), actual_yes)
                bb, bl = self._loss(float(row["baseline_yes_probability"]), actual_yes)
                correct = int(str(row["predicted_side"]) == official)
                ask = _num(_row_get(row, "entry_ask_cents"))
                cost = _num(_row_get(row, "entry_cost_cents"), 0.0) or 0.0
                realized = None
                if ask is not None:
                    # Canonical 2-dp cent precision shared with the performance
                    # store and the scoreboard so P&L never drifts between the
                    # value stored here and the value re-tallied elsewhere.
                    realized = round_cents((100.0 - ask - cost) if correct else -(ask + cost))
                connection.execute(
                    """UPDATE predictions SET official_result=?,resolved_at=?,correct=?,
                       realized_cents=?,champion_brier=?,challenger_brier=?,baseline_brier=?,
                       champion_logloss=?,challenger_logloss=?,baseline_logloss=?
                       WHERE prediction_id=?""",
                    (official, resolved_at, correct, realized, cb, hb, bb, cl, hl, bl, row["prediction_id"]),
                )
                review = self._result_review(dict(row), official)
                events.append({
                    "ticker": ticker, "asset": row["asset"], "checkpoint": row["checkpoint"],
                    "predicted_side": row["predicted_side"], "official_result": official,
                    "correct": bool(correct), "probability": row["calibrated_yes_probability"],
                    "review": review,
                })
            connection.commit()
            if rows:
                # Resolved rows changed -> invalidate calibrate/pattern caches.
                self._data_version += 1
        learned = 0
        for row in rows:
            if self._apply_shadow_update(str(row["prediction_id"])):
                learned += 1
        self._shadow_resolve(events)
        return {"resolved": len(rows), "updates_applied": learned, "events": events}

    def _apply_shadow_update(self, prediction_id: str) -> bool:
        if not self._available:
            return False
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM predictions WHERE prediction_id=?", (prediction_id,)).fetchone()
            if row is None or row["official_result"] not in {"YES", "NO"}:
                return False
            if str(row["model_version"] or "") != MODEL_VERSION:
                connection.execute("UPDATE predictions SET learning_applied=-4 WHERE prediction_id=?", (prediction_id,))
                connection.commit()
                return False
            checkpoint = self._checkpoint(row["checkpoint"])
            if not self.learning_enabled(checkpoint):
                connection.execute("UPDATE predictions SET learning_applied=-2 WHERE prediction_id=?", (prediction_id,))
                connection.commit()
                return False
            if connection.execute(
                "SELECT 1 FROM checkpoint_challenger_updates WHERE prediction_id=?", (prediction_id,)
            ).fetchone():
                connection.execute("UPDATE predictions SET learning_applied=1 WHERE prediction_id=?", (prediction_id,))
                connection.commit()
                return False
            quality = float(row["data_quality"] or 0.0)
            minimum_quality = self.minimum_learning_quality_by_checkpoint[checkpoint]
            if quality < minimum_quality:
                connection.execute("UPDATE predictions SET learning_applied=-1 WHERE prediction_id=?", (prediction_id,))
                connection.commit()
                return False
            try:
                features = json.loads(str(row["feature_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                self._dropped_feature_rows += 1
                self._dropped_feature_rows_by_checkpoint[checkpoint] = (
                    self._dropped_feature_rows_by_checkpoint.get(checkpoint, 0) + 1
                )
                logger.warning("Unparseable feature_json for prediction_id=%s (checkpoint=%s); skipping learning", prediction_id, checkpoint)
                connection.execute("UPDATE predictions SET learning_applied=-3 WHERE prediction_id=?", (prediction_id,))
                connection.commit()
                return False
            weight_rows = list(connection.execute(
                "SELECT * FROM checkpoint_challenger_weights WHERE checkpoint=? ORDER BY name", (checkpoint,)
            ))
            before = {str(item["name"]): float(item["value"]) for item in weight_rows}
            actual_yes = 1.0 if row["official_result"] == "YES" else 0.0
            probability = _clamp(float(row["challenger_yes_probability"]), 0.01, 0.99)
            error = actual_yes - probability
            quality_factor = _clamp((quality - minimum_quality) / max(1e-9, 1.0 - minimum_quality), 0.0, 1.0)
            # The primary 10M learner gets more useful weight, while low-quality
            # rows still have limited influence. The boosted weight is allowed to
            # exceed 1.0 (capped at primary_learning_weight, since the base is in
            # [0.25, 1.0]); the per-result and total-drift caps below bound the
            # actual step, so a >1.0 weight only speeds learning, never destabilizes.
            sample_weight = 0.25 + 0.75 * quality_factor
            if checkpoint == self.primary_learning_checkpoint:
                if self.primary_learning_boost:
                    sample_weight = sample_weight * self.primary_learning_weight
                else:  # legacy clamp: erased the boost for high-quality rows
                    sample_weight = min(1.0, sample_weight * self.primary_learning_weight)
            learning_rate = self.learning_rate_by_checkpoint[checkpoint]
            per_result_cap = self.per_result_cap_by_checkpoint[checkpoint]
            total_drift_cap = self.total_drift_cap_by_checkpoint[checkpoint]
            after = dict(before)
            deltas: dict[str, float] = {}
            now = time.time()
            for item in weight_rows:
                name = str(item["name"])
                base = float(item["base_value"])
                current = float(item["value"])
                x = 1.0 if name == "intercept" else float(features.get(name, 0.0) or 0.0)
                if name != "intercept" and abs(x) < 1e-12:
                    continue
                gradient = sample_weight * error * x - 0.002 * (current - base)
                grad_sq = float(item["grad_sq"] or 0.0) + gradient * gradient
                step = learning_rate * gradient / math.sqrt(1.0 + grad_sq)
                delta = _clamp(step, -per_result_cap, per_result_cap)
                next_value = _clamp(current + delta, base - total_drift_cap, base + total_drift_cap)
                actual_delta = next_value - current
                if abs(actual_delta) < 1e-15:
                    continue
                after[name] = next_value
                deltas[name] = actual_delta
                connection.execute(
                    "UPDATE checkpoint_challenger_weights SET value=?,grad_sq=?,updates=updates+1,updated_at=? WHERE checkpoint=? AND name=?",
                    (next_value, grad_sq, now, checkpoint, name),
                )
            # Observability: the effective step this result produced. If learning
            # is enabled but the net movement collapsed to ~0 (the per-result /
            # drift caps or a tiny learning rate cancelling the gradient), record
            # it as an effectively-frozen result and log it (throttled). This makes
            # "learning is on but nothing is moving" visible instead of silent.
            step_magnitude = sum(abs(d) for d in deltas.values())
            self._last_learning_step_magnitude = step_magnitude
            if step_magnitude <= self._learning_frozen_epsilon:
                self._learning_frozen_results += 1
                if now - self._last_learning_frozen_log >= 300.0:
                    self._last_learning_frozen_log = now
                    logger.warning(
                        "Shadow learning effectively frozen for checkpoint=%s: step magnitude %.3e <= epsilon %.3e "
                        "(rate=%.4f per_result_cap=%.4f drift_cap=%.4f) — check the learning knobs",
                        checkpoint, step_magnitude, self._learning_frozen_epsilon,
                        learning_rate, per_result_cap, total_drift_cap,
                    )
            else:
                logger.debug(
                    "Shadow learning step checkpoint=%s magnitude=%.3e features_moved=%d",
                    checkpoint, step_magnitude, len(deltas),
                )
            # Mirror the same gradient step into the per-regime challenger so it
            # specializes to the market condition this result occurred in.
            if self.regime_learning_enabled:
                self._update_regime_weights(
                    connection, checkpoint, row["regime"], features, error,
                    sample_weight, learning_rate, per_result_cap, total_drift_cap, now,
                )
            connection.execute(
                "INSERT INTO checkpoint_challenger_updates(checkpoint,prediction_id,created_at,error,sample_weight,delta_json,before_json,after_json) VALUES(?,?,?,?,?,?,?,?)",
                (checkpoint, prediction_id, now, error, sample_weight, _json(deltas), _json(before), _json(after)),
            )
            connection.execute("UPDATE predictions SET learning_applied=1 WHERE prediction_id=?", (prediction_id,))
            connection.commit()
            # Challenger weights changed -> invalidate the challenger_weights cache.
            self._data_version += 1
            return True

    def _update_regime_weights(self, connection: sqlite3.Connection, checkpoint: str, regime: Any,
                               features: Mapping[str, Any], error: float, sample_weight: float,
                               learning_rate: float, per_result_cap: float, total_drift_cap: float,
                               now: float) -> None:
        key = self._regime_key(regime)
        rows = list(connection.execute(
            "SELECT * FROM regime_challenger_weights WHERE checkpoint=? AND regime=? ORDER BY name",
            (checkpoint, key),
        ))
        if not rows:
            # Warm-start from the current global challenger; regularize toward the
            # frozen champion (base_value) just like the global challenger does.
            global_values = {
                str(r["name"]): float(r["value"])
                for r in connection.execute(
                    "SELECT name,value FROM checkpoint_challenger_weights WHERE checkpoint=?", (checkpoint,)
                )
            }
            for name, base in CHAMPION_WEIGHTS.items():
                connection.execute(
                    "INSERT OR IGNORE INTO regime_challenger_weights(checkpoint,regime,name,base_value,value,updated_at) VALUES(?,?,?,?,?,?)",
                    (checkpoint, key, name, base, global_values.get(name, base), now),
                )
            rows = list(connection.execute(
                "SELECT * FROM regime_challenger_weights WHERE checkpoint=? AND regime=? ORDER BY name",
                (checkpoint, key),
            ))
        for item in rows:
            name = str(item["name"])
            base = float(item["base_value"])
            current = float(item["value"])
            x = 1.0 if name == "intercept" else float(features.get(name, 0.0) or 0.0)
            if name != "intercept" and abs(x) < 1e-12:
                continue  # feature absent this row; don't touch its weight or count
            gradient = sample_weight * error * x - 0.002 * (current - base)
            grad_sq = float(item["grad_sq"] or 0.0) + gradient * gradient
            step = learning_rate * gradient / math.sqrt(1.0 + grad_sq)
            delta = _clamp(step, -per_result_cap, per_result_cap)
            next_value = _clamp(current + delta, base - total_drift_cap, base + total_drift_cap)
            # Always advance the update count (intercept's count = the regime's
            # resolved-sample count, which gates maturity) even on a zero step.
            connection.execute(
                "UPDATE regime_challenger_weights SET value=?,grad_sq=?,updates=updates+1,updated_at=? WHERE checkpoint=? AND regime=? AND name=?",
                (next_value, grad_sq, now, checkpoint, key, name),
            )

    def _pattern_centroids(self, checkpoint: str) -> dict[str, Any]:
        """Cached winner/loser feature centroids for a checkpoint. The 500-row
        fetch + per-row JSON parse is identical for every asset in a cycle and
        unchanged until a resolution bumps the data version, so it is computed
        once and reused. Returns {active, resolved} (insufficient) or
        {active, resolved, winners, losers, winner, loser, names}."""
        with self._lock:
            version = self._data_version
            if self._cache_enabled:
                cached = self._pattern_centroid_cache.get(checkpoint)
                if cached is not None and cached[0] == version:
                    return cached[1]
            with closing(self._connect()) as connection:
                rows = list(connection.execute(
                    "SELECT predicted_side,correct,data_quality,feature_json FROM predictions "
                    "WHERE model_version=? AND checkpoint=? AND official_result IS NOT NULL "
                    "ORDER BY resolved_at DESC LIMIT 500", (MODEL_VERSION, checkpoint),
                ))
        # Lock released; centroid build below is identical to the original.
        winners = [row for row in rows if int(row["correct"] or 0) == 1]
        losers = [row for row in rows if int(row["correct"] or 0) == 0]
        dropped_rows = 0  # accumulated unlocked, folded into the counter under lock
        if len(rows) < 10 or len(winners) < 3 or len(losers) < 3:
            result: dict[str, Any] = {"active": False, "resolved": len(rows)}
        else:
            names = [name for name in CHAMPION_WEIGHTS if name != "intercept"]
            def centroid(selected: Sequence[sqlite3.Row]) -> dict[str, float]:
                nonlocal dropped_rows
                sums = {name: 0.0 for name in names}
                weights = {name: 0.0 for name in names}
                for row in selected:
                    try:
                        values = json.loads(str(row["feature_json"]))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        dropped_rows += 1
                        continue
                    orientation = 1.0 if str(row["predicted_side"]).upper() == "YES" else -1.0
                    quality = _clamp(float(row["data_quality"] or 0.0), 0.10, 1.0)
                    for name in names:
                        try:
                            value = orientation * float(values.get(name, 0.0) or 0.0)
                        except (TypeError, ValueError):
                            continue
                        sums[name] += value * quality
                        weights[name] += quality
                return {name: sums[name] / weights[name] if weights[name] else 0.0 for name in names}
            result = {"active": True, "resolved": len(rows), "winners": len(winners), "losers": len(losers),
                      "winner": centroid(winners), "loser": centroid(losers), "names": names}
        if dropped_rows or self._cache_enabled:
            with self._lock:
                # Counter mutation belongs under the lock (it is read unlocked in
                # status()); fold in the rows the unlocked centroid build dropped.
                self._dropped_feature_rows += dropped_rows
                if dropped_rows:
                    self._dropped_feature_rows_by_checkpoint[checkpoint] = (
                        self._dropped_feature_rows_by_checkpoint.get(checkpoint, 0) + dropped_rows
                    )
                if self._cache_enabled and self._data_version == version:
                    self._pattern_centroid_cache[checkpoint] = (version, result)
        return result

    def pattern_similarity(self, features: Mapping[str, float], provisional_side: str, checkpoint: str = "10M") -> dict[str, Any]:
        """Checkpoint-specific winner/loser similarity; diagnostic at 10 rows and shadow-only at 30."""
        if not self._available:
            return {"active": False, "reason": "ledger_unavailable", "shadow_adjustment": 0.0}
        checkpoint = self._checkpoint(checkpoint)
        centroids = self._pattern_centroids(checkpoint)
        if not centroids["active"]:
            return {"active": False, "reason": "need_10_resolved_with_3_winners_and_3_losers", "resolved": centroids["resolved"], "shadow_adjustment": 0.0}
        names = centroids["names"]
        winner = centroids["winner"]
        loser = centroids["loser"]
        resolved = centroids["resolved"]
        orientation = 1.0 if str(provisional_side).upper() == "YES" else -1.0
        current = {name: orientation * float(features.get(name, 0.0) or 0.0) for name in names}
        def cosine(a: Mapping[str, float], b: Mapping[str, float]) -> float:
            dot = sum(a[name] * b[name] for name in names)
            an = math.sqrt(sum(a[name] ** 2 for name in names))
            bn = math.sqrt(sum(b[name] ** 2 for name in names))
            return dot / (an * bn) if an > 1e-12 and bn > 1e-12 else 0.0
        win_similarity = cosine(current, winner)
        loss_similarity = cosine(current, loser)
        diagnostic = _clamp(win_similarity - loss_similarity, -1.0, 1.0)
        shadow_adjustment = _clamp(diagnostic * 0.03, -0.03, 0.03) if resolved >= 30 else 0.0
        return {
            "active": True, "resolved": resolved, "winners": centroids["winners"], "losers": centroids["losers"],
            "winner_similarity": win_similarity, "loser_similarity": loss_similarity,
            "diagnostic_score": diagnostic, "shadow_adjustment": shadow_adjustment,
            "production_adjustment": 0.0, "shadow_influence_active": resolved >= 30,
            "checkpoint": checkpoint,
        }

    def calibrate(self, raw_probability: float, checkpoint: str, asset: str | None = None) -> dict[str, Any]:
        """Regularized Platt calibration fitted only to resolved current-version rows.

        The expensive Platt fit (intercept/slope) depends solely on the resolved-
        row set for (checkpoint, asset); only the cheap final transform uses
        ``raw_probability``. The fit is cached via ``_calibration_fit`` and reused
        across assets/cycles until a resolution bumps the data version.
        """
        raw = _clamp(raw_probability, 0.01, 0.99)
        if not self._available:
            return {"probability": raw, "active": False, "reason": "ledger_unavailable", "rows": 0}
        checkpoint = str(checkpoint).upper()
        fit = self._calibration_fit(checkpoint, asset)
        if not fit["active"]:
            return {"probability": raw, "active": False, "reason": fit["reason"], "rows": fit["rows"]}
        method, reason = self._select_calibration_method(fit)
        base = {"active": True, "rows": fit["rows"], "scope": fit["scope"], "method": method, "reason": reason}
        if method == "isotonic" and fit.get("isotonic"):
            iso = _isotonic_predict(fit["isotonic"], raw)
            if iso is not None:
                return {**base, "probability": _clamp(iso, 0.01, 0.99)}
        if method == "platt":
            calibrated = _clamp(_sigmoid(fit["intercept"] + fit["slope"] * _logit(raw)), 0.01, 0.99)
            result = {**base, "probability": calibrated,
                      "intercept": fit["intercept"], "slope": fit["slope"]}
            if fit.get("fallback"):
                result["fallback"] = fit["fallback"]
            return result
        # identity (incl. OOS-no-gain / unconverged): raw passes through unchanged.
        # The APPLIED transform is identity, hence intercept 0 / slope 1.
        result = {**base, "probability": raw, "intercept": 0.0, "slope": 1.0}
        if fit.get("fallback"):
            result["fallback"] = fit["fallback"]
        return result

    def _select_calibration_method(self, fit: Mapping[str, Any]) -> tuple[str, str]:
        """Decide which transform to APPLY for this fit. Priority:
        1. explicit isotonic override (Q15_V95_CALIBRATION_ISOTONIC);
        2. OOS self-selection (default): apply the held-out best (Platt/isotonic)
           only if it beats identity by the floor — else identity;
        3. legacy fallback: isotonic-on-unconverged, else Platt.
        Returns (method, reason)."""
        unconverged = fit.get("fallback") == "identity_unconverged"
        if self._calibration_isotonic and fit.get("isotonic"):
            return "isotonic", "isotonic_forced"
        oos = fit.get("oos") or {}
        if self._calibration_oos_select and oos.get("available"):
            if oos.get("improves") and oos.get("best_method") in {"platt", "isotonic"}:
                return oos["best_method"], f"oos_selected_{oos['best_method']}"
            return "identity", "oos_no_gain_identity"
        if self._calibration_isotonic_fallback and unconverged and fit.get("isotonic"):
            return "isotonic", "isotonic_unconverged_fallback"
        if unconverged:
            return "identity", "platt_unconverged_identity"
        return "platt", "platt_current_version"

    def _calibration_oos(self, rows_chrono: Sequence[sqlite3.Row]) -> dict[str, Any]:
        """Chronological out-of-sample Brier of identity vs Platt vs isotonic.

        Fit each method on the OLDER train fold, score the NEWER test fold. Picks
        the lowest test Brier and flags whether it beats identity by the floor, so
        calibrate() can apply a transform only when it is OOS-proven to help.
        ``rows_chrono`` is oldest-first."""
        n = len(rows_chrono)
        out: dict[str, Any] = {"available": False, "n": n}
        test_n = int(round(self._calibration_oos_test_fraction * n))
        split = n - test_n
        if split < self.minimum_calibration_rows or test_n < 20:
            out["reason"] = "insufficient_for_oos"
            return out
        train, test = rows_chrono[:split], rows_chrono[split:]
        xy_train = [(_clamp(_logit(float(r["raw_yes_probability"])), -4.0, 4.0),
                     1.0 if r["official_result"] == "YES" else 0.0) for r in train]
        i_tr, s_tr, conv_tr, _ = _fit_platt(
            xy_train, self._calibration_max_iters, self._calibration_slope_min,
            self._calibration_slope_max, self._calibration_intercept_cap)
        iso_tr = _isotonic_fit([(_clamp(float(r["raw_yes_probability"]), 0.01, 0.99),
                                 1.0 if r["official_result"] == "YES" else 0.0) for r in train])
        ident_pairs, platt_pairs, iso_pairs = [], [], []
        for r in test:
            raw = _clamp(float(r["raw_yes_probability"]), 0.01, 0.99)
            y = 1.0 if r["official_result"] == "YES" else 0.0
            ident_pairs.append((raw, y))
            platt_pairs.append((_clamp(_sigmoid(i_tr + s_tr * _clamp(_logit(raw), -4.0, 4.0)), 0.01, 0.99), y))
            iso = _isotonic_predict(iso_tr, raw)
            iso_pairs.append((_clamp(iso if iso is not None else raw, 0.01, 0.99), y))
        briers = {"identity": _brier(ident_pairs), "platt": _brier(platt_pairs),
                  "isotonic": _brier(iso_pairs)}
        identity_brier = briers["identity"]
        best_method = min(briers, key=lambda k: briers[k])
        best_brier = briers[best_method]
        improves = bool(best_method != "identity" and identity_brier is not None
                        and best_brier is not None
                        and (identity_brier - best_brier) >= self._calibration_oos_floor)
        return {
            "available": True, "n_train": split, "n_test": test_n,
            "identity_brier": round(identity_brier, 6) if identity_brier is not None else None,
            "platt_brier": round(briers["platt"], 6) if briers["platt"] is not None else None,
            "isotonic_brier": round(briers["isotonic"], 6) if briers["isotonic"] is not None else None,
            "best_method": best_method, "best_brier": round(best_brier, 6) if best_brier is not None else None,
            "improves": improves, "platt_converged": conv_tr,
        }

    def calibration_experiment(self, checkpoint: str | None = None) -> dict[str, Any]:
        """A/B the calibration methods out of sample, per checkpoint — the numbers
        the next review reads to confirm isotonic/Platt vs identity Brier. Read-only."""
        out: dict[str, Any] = {"available": bool(self._available), "by_checkpoint": {}}
        if not self._available:
            return {"available": False, "by_checkpoint": {}}
        cps = [str(checkpoint).upper()] if checkpoint else ["15M", "10M", "7M"]
        for cp in cps:
            fit = self._calibration_fit(cp, None)
            oos = (fit.get("oos") or {}) if fit.get("active") else {}
            method, reason = (self._select_calibration_method(fit) if fit.get("active")
                              else ("identity", fit.get("reason", "inactive")))
            out["by_checkpoint"][cp] = {
                "rows": fit.get("rows", 0), "applied_method": method, "reason": reason,
                "platt_converged": fit.get("converged"),
                "platt_slope": round(fit.get("fitted_slope"), 4) if fit.get("fitted_slope") is not None else None,
                "oos": oos,
            }
        return out

    def _calibration_fit(self, checkpoint: str, asset: str | None) -> dict[str, Any]:
        """Cached Platt fit for (checkpoint, asset). Pure function of the resolved
        rows; invalidated by the data version. Returns {active, reason, rows} and,
        when active, {intercept, slope, scope}. The Newton solve runs outside the
        lock (as the original did), so heavy compute never blocks other readers."""
        cache_key = (checkpoint, str(asset).upper() if asset else None)
        with self._lock:
            version = self._data_version
            if self._cache_enabled:
                cached = self._calibration_fit_cache.get(cache_key)
                if cached is not None and cached[0] == version:
                    return cached[1]
            with closing(self._connect()) as connection:
                asset_rows: list[sqlite3.Row] = []
                if asset:
                    asset_rows = list(connection.execute(
                        "SELECT raw_yes_probability,official_result FROM predictions WHERE model_version=? AND checkpoint=? AND asset=? AND official_result IS NOT NULL ORDER BY resolved_at DESC LIMIT 1000",
                        (MODEL_VERSION, checkpoint, str(asset).upper()),
                    ))
                rows = asset_rows if len(asset_rows) >= self.minimum_calibration_rows else list(connection.execute(
                    "SELECT raw_yes_probability,official_result FROM predictions WHERE model_version=? AND checkpoint=? AND official_result IS NOT NULL ORDER BY resolved_at DESC LIMIT 2500",
                    (MODEL_VERSION, checkpoint),
                ))
                scope = "asset" if asset_rows and len(asset_rows) >= self.minimum_calibration_rows else "checkpoint"
        # Lock released; the Platt solve below is identical to the original.
        if len(rows) < self.minimum_calibration_rows:
            fit: dict[str, Any] = {"active": False, "reason": "insufficient_resolved_rows", "rows": len(rows)}
        else:
            # Penalised logistic regression on ALL resolved rows, via the shared
            # solver (wider clamps + clamp-aware convergence).
            xy_all = [(_clamp(_logit(float(r["raw_yes_probability"])), -4.0, 4.0),
                       1.0 if r["official_result"] == "YES" else 0.0) for r in rows]
            intercept, slope, converged, iterations = _fit_platt(
                xy_all, self._calibration_max_iters, self._calibration_slope_min,
                self._calibration_slope_max, self._calibration_intercept_cap)
            reason = "platt_current_version"
            fallback = None
            if not converged and self.calibration_require_converged:
                logger.warning(
                    "Platt calibration did not converge for checkpoint=%s scope=%s rows=%d "
                    "after %d iterations (intercept=%.4f slope=%.4f)",
                    checkpoint, scope, len(rows), iterations, intercept, slope,
                )
                fallback = "identity_unconverged"
                reason = "platt_unconverged_identity"
                with self._lock:
                    self._calibration_unconverged_fallbacks += 1
            fit = {"active": True, "reason": reason, "rows": len(rows),
                   "intercept": 0.0 if fallback else intercept,
                   "slope": 1.0 if fallback else slope, "scope": scope,
                   "converged": converged, "iterations": iterations,
                   "fitted_intercept": intercept, "fitted_slope": slope}
            if fallback:
                fit["fallback"] = fallback
            # Isotonic curve over the SAME resolved rows (robust, no convergence).
            fit["isotonic"] = _isotonic_fit([
                (_clamp(float(r["raw_yes_probability"]), 0.01, 0.99),
                 1.0 if r["official_result"] == "YES" else 0.0)
                for r in rows
            ])
            # Out-of-sample self-validation: fit on the OLDER train fold, score the
            # NEWER test fold's Brier for identity / Platt / isotonic. The rows came
            # back resolved_at DESC (newest first) — reverse to chronological.
            fit["oos"] = self._calibration_oos(list(reversed(rows)))
        if self._cache_enabled:
            with self._lock:
                # Only cache if no resolution landed during the (unlocked) solve.
                if self._data_version == version:
                    self._calibration_fit_cache[cache_key] = (version, fit)
        return fit

    @staticmethod
    def _win_loss(selected: Sequence[sqlite3.Row]) -> dict[str, Any]:
        n = len(selected)
        right = sum(1 for row in selected if int(row["correct"] or 0) == 1)
        low, high = _wilson_interval(right, n)
        # Below this many resolved samples, a bucket's rate is statistically
        # untrustworthy and should be read as "not enough data yet".
        threshold = _env_int("Q15_V95_SCOREBOARD_MIN_N", 10, 1, 1000)
        realized = [v for v in (_num(_row_get(row, "realized_cents")) for row in selected) if v is not None]
        pnl_n = len(realized)
        # Does the accuracy CI exclude 0.5? When the whole 95% Wilson interval
        # sits above (or below) a coin flip, the bucket is statistically separated
        # from chance. Read with low_n, this distinguishes "clean but tiny — don't
        # trust it" (e.g. 3-0, low_n True, interval still straddles 0.5) from
        # "genuinely separated from chance" (interval entirely above 0.5).
        ci_excludes_half = bool(
            n and ((low is not None and low > 0.5) or (high is not None and high < 0.5))
        )
        return {
            "right": right, "wrong": n - right, "n": n,
            "accuracy": round(right / n, 4) if n else None,
            "ci_low": low, "ci_high": high,
            "low_n": bool(n and n < threshold),
            "ci_excludes_half": ci_excludes_half,
            "pnl_n": pnl_n,
            "realized_total_cents": round_cents(sum(realized)) if realized else 0.0,
            "realized_avg_cents": round_cents(sum(realized) / pnl_n) if pnl_n else None,
        }

    @staticmethod
    def _classification_metrics(selected: Sequence[sqlite3.Row]) -> dict[str, Any]:
        """Precision/recall for YES and NO, plus false-positive / false-negative
        rates, treating YES as the positive class. Needs predicted_side and
        official_result; rows missing either are skipped."""
        tp = fp = tn = fn = 0
        for row in selected:
            pred = str(_row_get(row, "predicted_side") or "").upper()
            truth = str(_row_get(row, "official_result") or "").upper()
            if pred not in {"YES", "NO"} or truth not in {"YES", "NO"}:
                continue
            if pred == "YES":
                tp += truth == "YES"
                fp += truth == "NO"
            else:
                tn += truth == "NO"
                fn += truth == "YES"

        def _ratio(num: int, den: int) -> float | None:
            return round(num / den, 4) if den else None

        return {
            "tp": tp, "fp": fp, "tn": tn, "fn": fn,
            "precision_yes": _ratio(tp, tp + fp),
            "recall_yes": _ratio(tp, tp + fn),
            "precision_no": _ratio(tn, tn + fn),
            "recall_no": _ratio(tn, tn + fp),
            "false_positive_rate": _ratio(fp, fp + tn),
            "false_negative_rate": _ratio(fn, fn + tp),
        }

    @staticmethod
    def _change_rate(selected: Sequence[sqlite3.Row]) -> dict[str, Any]:
        """How often the prediction drifted from its locked side before close."""
        n = len(selected)
        changed = sum(1 for row in selected if int(_row_get(row, "changed_before_close") or 0) == 1)
        return {"n": n, "changed": changed, "change_rate": round(changed / n, 4) if n else None}

    def _by_grade(self, selected: Sequence[sqlite3.Row]) -> dict[str, Any]:
        """Right/wrong record split by the confidence grade (A/B/C/D) shown."""
        out: dict[str, Any] = {}
        for grade in ("A", "B", "C", "D"):
            bucket = [r for r in selected if str(_row_get(r, "confidence_grade") or "").upper() == grade]
            if bucket:
                out[grade] = self._win_loss(bucket)
        return out

    def _checkpoint_metrics(self, selected: Sequence[sqlite3.Row]) -> dict[str, Any]:
        """Per-interval record: accuracy/P&L (via _win_loss) enriched with the
        classification, stability, and by-grade breakdowns. 10M is the priority
        interval, but every interval gets the same full breakdown so 7M/15M stay
        fully evaluated."""
        return {
            **self._win_loss(selected),
            "classification": self._classification_metrics(selected),
            "stability": self._change_rate(selected),
            "by_grade": self._by_grade(selected),
        }

    @staticmethod
    def _rank_bucket(row: Mapping[str, Any]) -> str:
        try:
            value = int(row["rank"]) if row["rank"] is not None else None
        except (TypeError, ValueError, IndexError, KeyError):
            value = None
        return str(value) if value in (1, 2, 3) else "other"

    # Manipulation reasons recorded at prediction time (see checkpoint_v95).
    _MANIPULATION_REASONS = ("PIN", "ABSORPTION", "DIVERGENCE")

    def _by_manipulation(self, rows: Sequence[sqlite3.Row]) -> dict[str, Any]:
        """Accuracy / realized P&L split by whether price manipulation was suspected.

        Answers "is the model less reliable / less profitable when big players look
        to be pushing the price?" — suspected vs clean, and per individual reason
        (a row counts under each reason it carries) so the data shows which signal
        (pin / absorption / divergence) most clearly precedes a flip.
        """
        def _flag(row: sqlite3.Row) -> int:
            return int(_row_get(row, "manipulation_suspected") or 0)

        suspected = [r for r in rows if _flag(r) == 1]
        clean = [r for r in rows if _flag(r) == 0]
        by_reason: dict[str, Any] = {}
        for reason in self._MANIPULATION_REASONS:
            bucket = [
                r for r in rows
                if reason in {p.strip().upper() for p in str(_row_get(r, "manipulation_reason") or "").split(",") if p.strip()}
            ]
            if bucket:
                by_reason[reason] = self._win_loss(bucket)
        return {
            "suspected": self._win_loss(suspected),
            "clean": self._win_loss(clean),
            "by_reason": by_reason,
        }

    def _scoreboard_rows(self, rows: Sequence[sqlite3.Row]) -> dict[str, Any]:
        """Right/wrong/accuracy by interval (15M/10M/7M), pick rank (#1/#2/#3), and asset."""
        by_checkpoint = {cp: self._checkpoint_metrics([r for r in rows if r["checkpoint"] == cp]) for cp in TRACKED_CHECKPOINTS}
        by_rank = {
            label: self._win_loss([r for r in rows if self._rank_bucket(r) == label])
            for label in ("1", "2", "3", "other")
        }
        # Rank record split by interval — "how the #1/#2/#3 pick fares within each
        # checkpoint", so e.g. the 10M top pick can be judged on its own merits
        # rather than blended across all intervals.
        rank_by_checkpoint = {
            cp: {
                label: self._win_loss(
                    [r for r in rows if r["checkpoint"] == cp and self._rank_bucket(r) == label]
                )
                for label in ("1", "2", "3")
            }
            for cp in TRACKED_CHECKPOINTS
        }
        assets = sorted({str(r["asset"]) for r in rows})
        by_asset = {a: self._win_loss([r for r in rows if str(r["asset"]) == a]) for a in assets}
        # How the top pick (#1) fares per coin — "which coins the #1 pick wins on".
        rank1 = [r for r in rows if self._rank_bucket(r) == "1"]
        top_pick_by_asset = {
            a: self._win_loss([r for r in rank1 if str(r["asset"]) == a])
            for a in sorted({str(r["asset"]) for r in rank1})
        }
        # Pushed vs background: the two separate records. Pushed = predictions an
        # entry was recommended on and the alert was actually delivered; background
        # = every other observation. Background results NEVER inflate pushed accuracy.
        pushed_rows = [r for r in rows if int(_row_get(r, "pushed") or 0) == 1]
        background_rows = [r for r in rows if int(_row_get(r, "pushed") or 0) != 1]
        by_pushed = {
            "pushed": self._win_loss(pushed_rows),
            "background": self._win_loss(background_rows),
        }
        pushed_by_checkpoint = {
            cp: self._win_loss([r for r in pushed_rows if r["checkpoint"] == cp])
            for cp in TRACKED_CHECKPOINTS
        }
        return {
            "overall": self._win_loss(rows), "by_checkpoint": by_checkpoint,
            "by_rank": by_rank, "rank_by_checkpoint": rank_by_checkpoint,
            "by_asset": by_asset, "top_pick_by_asset": top_pick_by_asset,
            "by_manipulation": self._by_manipulation(rows),
            "by_pushed": by_pushed, "pushed_by_checkpoint": pushed_by_checkpoint,
        }

    def resolved_setup_rows(self, checkpoint: str = "10M") -> list[dict[str, Any]]:
        """Read-only export of settled predictions for the setup miner: each row's
        DECISION-TIME feature values + regime + final outcome, ordered by time.

        Only ``official_result``-bearing rows for the given checkpoint and the
        current MODEL_VERSION are returned. The outcome is the target; nothing
        derived from settlement is exposed as a feature. Unparseable feature JSON
        is skipped, never guessed."""
        if not self._available:
            return []
        out: list[dict[str, Any]] = []
        with self._lock, closing(self._connect()) as connection:
            rows = list(connection.execute(
                "SELECT created_at, feature_json, regime, official_result "
                "FROM predictions WHERE model_version=? AND checkpoint=? "
                "AND official_result IS NOT NULL ORDER BY created_at ASC",
                (MODEL_VERSION, checkpoint),
            ))
        for row in rows:
            try:
                features = json.loads(str(row["feature_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(features, dict):
                continue
            out.append({
                "created_at": float(row["created_at"]),
                "features": features,
                "regime": row["regime"],
                "result": str(row["official_result"]).upper(),
            })
        return out

    def resolved_factor_rows(self, checkpoint: str | None = None) -> list[dict[str, Any]]:
        """Read-only export for the shadow FACTOR LAB: every settled official
        prediction with its DECISION-TIME factor values, the final settled result,
        and the settlement-derived analysis TARGETS (``correct`` and
        ``changed_before_close``). Outcome columns are the target of the analysis —
        never re-exposed as a factor — so there is no look-ahead in the factors
        themselves. Unparseable feature JSON is skipped, never guessed.

        Optional ``checkpoint`` filters to one interval; ``None`` returns all.
        """
        if not self._available:
            return []
        query = (
            "SELECT created_at, checkpoint, asset, regime, predicted_side, "
            "official_result, changed_before_close, correct, selected_probability, "
            "feature_json, shadow_factor_json FROM predictions WHERE model_version=? "
            "AND official_result IS NOT NULL"
        )
        params: list[Any] = [MODEL_VERSION]
        if checkpoint is not None:
            query += " AND checkpoint=?"
            params.append(str(checkpoint).upper())
        query += " ORDER BY created_at ASC"
        out: list[dict[str, Any]] = []
        with self._lock, closing(self._connect()) as connection:
            rows = list(connection.execute(query, tuple(params)))
        for row in rows:
            try:
                features = json.loads(str(row["feature_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(features, dict):
                continue
            # Merge the isolated shadow factors (cross-asset, etc.) so the lab grades
            # them alongside the champion features. They are namespaced (``x_*``) by
            # the producer, so they cannot collide with a champion feature name.
            try:
                shadow = json.loads(str(row["shadow_factor_json"] or "{}"))
                if isinstance(shadow, dict):
                    features = {**features, **shadow}
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            out.append({
                "created_at": float(row["created_at"]),
                "checkpoint": str(row["checkpoint"]).upper(),
                "asset": row["asset"],
                "regime": row["regime"],
                "predicted_side": str(row["predicted_side"] or "").upper(),
                "result": str(row["official_result"]).upper(),
                "changed_before_close": int(row["changed_before_close"] or 0),
                "correct": (None if row["correct"] is None else int(row["correct"])),
                "features": features,
            })
        return out

    def resolved_economics_rows(self, checkpoint: str | None = None) -> list[dict[str, Any]]:
        """Read-only export for the shadow entry-economics A/B: per settled row, the
        decision-time probability + executable ask + live cost estimate + outcome +
        realized paper P&L. Only rows that had an executable ask (entry_ask_cents)
        are returned, since both gates are price gates. Outcome columns are the
        target; nothing here feeds a live decision."""
        if not self._available:
            return []
        out: list[dict[str, Any]] = []
        query = (
            "SELECT checkpoint, conservative_probability, entry_ask_cents, entry_cost_cents, "
            "correct, realized_cents FROM predictions WHERE model_version=? "
            "AND official_result IS NOT NULL AND entry_ask_cents IS NOT NULL"
        )
        args: list[Any] = [MODEL_VERSION]
        if checkpoint:
            query += " AND checkpoint=?"
            args.append(self._checkpoint(checkpoint))
        with self._lock, closing(self._connect()) as connection:
            rows = list(connection.execute(query, tuple(args)))
        for row in rows:
            out.append({
                "checkpoint": row["checkpoint"],
                "prob": _num(_row_get(row, "conservative_probability")),
                "ask_cents": _num(_row_get(row, "entry_ask_cents")),
                "cost_cents": _num(_row_get(row, "entry_cost_cents"), 0.0),
                "correct": bool(_row_get(row, "correct")),
                "realized_cents": _num(_row_get(row, "realized_cents")),
            })
        return out

    def resolved_shadow_signal_rows(self, checkpoint: str | None = None) -> list[dict[str, Any]]:
        """Read-only export for the experimental shadow-signal A/B: per settled row,
        the calibrated YES probability the live system used, the settled outcome, and
        the recorded experimental signals. Ordered oldest-first so the A/B can do a
        genuine time-ordered train/test split. Nothing here feeds a live decision."""
        if not self._available:
            return []
        query = (
            "SELECT checkpoint, calibrated_yes_probability, official_result, shadow_signal_json "
            "FROM predictions WHERE model_version=? AND official_result IN ('YES','NO') "
            "AND shadow_signal_json IS NOT NULL"
        )
        args: list[Any] = [MODEL_VERSION]
        if checkpoint:
            query += " AND checkpoint=?"
            args.append(self._checkpoint(checkpoint))
        query += " ORDER BY created_at ASC"
        with self._lock, closing(self._connect()) as connection:
            rows = list(connection.execute(query, tuple(args)))
        out: list[dict[str, Any]] = []
        dropped = 0  # accumulated unlocked; folded into the counter under the lock
        for row in rows:
            try:
                signals = json.loads(str(row["shadow_signal_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                dropped += 1
                continue
            champion_yes = _num(_row_get(row, "calibrated_yes_probability"))
            if champion_yes is None or not isinstance(signals, dict):
                continue
            out.append({
                "checkpoint": row["checkpoint"],
                "champion_yes": champion_yes,
                "outcome": 1 if str(row["official_result"]) == "YES" else 0,
                "signals": signals,
            })
        if dropped:
            with self._lock:
                self._dropped_feature_rows += dropped
        return out

    def shadow_signal_experiment(self, checkpoint: str | None = None) -> dict[str, Any]:
        """Run the background A/B: for each experimental signal, does it improve the
        probability out of sample? Returns a JSON-friendly summary for the report and
        /api. Read-only; never promotes anything (promotion stays manual)."""
        from q15_upgrade import shadow_signals
        config = shadow_signals.SignalConfig.from_env()
        rows = self.resolved_shadow_signal_rows(checkpoint)
        scores = shadow_signals.evaluate(rows, config)
        return {
            "available": True,
            "enabled": config.enabled,
            "model_version": MODEL_VERSION,
            "rows_considered": len(rows),
            "min_rows": config.min_rows,
            "signals": shadow_signals.scores_to_dict(scores),
        }

    def scoreboard(self) -> dict[str, Any]:
        """User-facing record: how often each interval, rank, and asset was right/wrong."""
        if not self._available:
            return {"available": False, "error": self._last_error}
        with self._lock, closing(self._connect()) as connection:
            rows = list(connection.execute(
                "SELECT checkpoint, correct, rank, asset, realized_cents, "
                "predicted_side, official_result, confidence_grade, changed_before_close, "
                "manipulation_suspected, manipulation_reason, pushed "
                "FROM predictions WHERE model_version=? AND official_result IS NOT NULL",
                (MODEL_VERSION,),
            ))
        return {
            "available": True, "model_version": MODEL_VERSION,
            "intervals": TRACKED_CHECKPOINTS, "priority_interval": self.primary_learning_checkpoint,
            **self._scoreboard_rows(rows),
        }

    # ------------------------------------------------------------------ flip risk
    @staticmethod
    def _flip_observations(rows: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
        """Point-in-time flip observations from resolved contracts.

        For each ticker, walks the frozen sides 15M -> 10M -> 7M -> resolution and
        emits one observation per consecutive transition: the EARLIER checkpoint's
        side + recorded flip-risk score, and whether the side flipped at the next
        stage. The score is the one observed AT the earlier checkpoint, so it never
        uses information from after the (potential) flip. Direction is keyed by the
        earlier side ("NO → YES" = a NO prediction that could flip to YES).
        """
        order = ("15M", "10M", "7M")
        by_ticker: dict[str, dict[str, Any]] = {}
        result_of: dict[str, str] = {}
        for r in rows:
            tk = str(r["ticker"])
            cp = str(r["checkpoint"])
            by_ticker.setdefault(tk, {})[cp] = {
                "side": str(_row_get(r, "predicted_side") or "").upper(),
                "score": _num(_row_get(r, "flip_risk_score")),
                "asset": str(r["asset"]),
            }
            res = str(_row_get(r, "official_result") or "").upper()
            if res in {"YES", "NO"}:
                result_of[tk] = res

        out: list[dict[str, Any]] = []

        def _emit(ticker: str, checkpoint: str, earlier: Mapping[str, Any], later_side: str) -> None:
            e_side = earlier["side"]
            if e_side not in {"YES", "NO"} or later_side not in {"YES", "NO"}:
                return
            if earlier["score"] is None:
                return  # no live score recorded at that checkpoint -> cannot learn
            direction = f"{e_side} → {'NO' if e_side == 'YES' else 'YES'}"
            out.append({
                "ticker": ticker, "checkpoint": checkpoint, "asset": earlier["asset"],
                "direction": direction, "score": float(earlier["score"]),
                "flipped": 1 if e_side != later_side else 0,
            })

        for tk, cps in by_ticker.items():
            present = [c for c in order if c in cps]
            for earlier, later in zip(order, order[1:]):
                if earlier in cps and later in cps:
                    _emit(tk, earlier, cps[earlier], cps[later]["side"])
            if present and tk in result_of:
                last = present[-1]
                _emit(tk, last, cps[last], result_of[tk])
        return out

    @staticmethod
    def _flip_scope_stats(observations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Bucketed flip-rate curve + learned threshold for one scope."""
        n = len(observations)
        flipped_scores = [float(o["score"]) for o in observations if o["flipped"]]
        buckets: dict[str, dict[str, Any]] = {}
        for label in flip_risk.BUCKET_ORDER:
            in_bucket = [o for o in observations if flip_risk.bucket_label(o["score"]) == label]
            if in_bucket:
                flips = sum(int(o["flipped"]) for o in in_bucket)
                buckets[label] = {
                    "n": len(in_bucket), "flips": flips,
                    "flip_rate": round(flips / len(in_bucket), 4),
                }
        target = _env_float("Q15_V95_FLIP_TARGET_RATE", 0.40, 0.0, 1.0)
        min_bucket_n = _env_int("Q15_V95_FLIP_BUCKET_MIN_N", 5, 1, 1000)
        threshold = None
        for label in flip_risk.BUCKET_ORDER:  # lowest bucket whose flip-rate clears target
            b = buckets.get(label)
            if b and b["n"] >= min_bucket_n and b["flip_rate"] >= target:
                threshold = float(label.split("-")[0])
                break
        mode_bucket = max(
            ((lbl, b) for lbl, b in buckets.items() if b["flips"]),
            key=lambda kv: kv[1]["flips"], default=(None, None),
        )[0]
        flipped_scores_sorted = sorted(flipped_scores)
        median = (flipped_scores_sorted[len(flipped_scores_sorted) // 2] if flipped_scores_sorted else None)
        return {
            "samples": n, "flips": len(flipped_scores),
            "avg_score_before_flip": round(sum(flipped_scores) / len(flipped_scores), 1) if flipped_scores else None,
            "median_score_before_flip": median,
            "mode_bucket_before_flip": mode_bucket,
            "threshold": threshold, "buckets": buckets,
        }

    def flip_stats(self) -> dict[str, Any]:
        """Learned flip statistics from resolved contracts only.

        Returns, per (checkpoint, direction): overall + per-asset bucketed flip
        rates, learned thresholds, and sample sizes. Uses ONLY the score recorded
        live at each checkpoint — never future candles or the final result — so it
        is an honest "what risk level preceded a flip" estimate. Cached against the
        data version (bumped on every resolution)."""
        if not self._available:
            return {"available": False, "error": self._last_error}
        with self._lock:
            version = self._data_version
            cached = getattr(self, "_flip_stats_cache", None)
            if self._cache_enabled and cached and cached[0] == version:
                return cached[1]
        with self._lock, closing(self._connect()) as connection:
            rows = list(connection.execute(
                "SELECT ticker, checkpoint, asset, predicted_side, official_result, flip_risk_score "
                "FROM predictions WHERE model_version=? AND official_result IS NOT NULL",
                (MODEL_VERSION,),
            ))
        observations = self._flip_observations(rows)
        directions = ("YES → NO", "NO → YES")
        out: dict[str, Any] = {}
        for cp in TRACKED_CHECKPOINTS:
            out[cp] = {}
            for direction in directions:
                scoped = [o for o in observations if o["checkpoint"] == cp and o["direction"] == direction]
                by_asset = {}
                for asset in sorted({o["asset"] for o in scoped}):
                    by_asset[asset] = self._flip_scope_stats([o for o in scoped if o["asset"] == asset])
                out[cp][direction] = {
                    "overall": self._flip_scope_stats(scoped),
                    "by_asset": by_asset,
                }
        result = {"available": True, "model_version": MODEL_VERSION,
                  "total_observations": len(observations), "by_checkpoint": out}
        if self._cache_enabled:
            with self._lock:
                if self._data_version == version:
                    self._flip_stats_cache = (version, result)
        return result

    def record_flip_warning(self, *, asset: str, checkpoint: str, ticker: str, direction: str,
                            risk_score: float, flip_probability: float | None,
                            confidence: float, now: float) -> bool:
        """Log a HIGH FLIP RISK alert for later true/false-positive scoring.

        One row per (ticker, checkpoint, direction); duplicates are ignored so the
        warning-performance precision is not inflated by re-fires."""
        if not self._available or not ticker:
            return False
        checkpoint = self._checkpoint(checkpoint)
        with self._lock, closing(self._connect()) as connection:
            cur = connection.execute(
                """INSERT OR IGNORE INTO flip_warnings(
                       model_version,asset,checkpoint,ticker,direction,
                       risk_score,flip_probability,confidence,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (MODEL_VERSION, str(asset), checkpoint, str(ticker), str(direction),
                 _num(risk_score), _num(flip_probability), _num(confidence), float(now)),
            )
            connection.commit()
            return cur.rowcount == 1

    def reconcile_flip_warnings(self) -> int:
        """Score each fired warning against whether the prediction actually flipped.

        A warning is a true positive if a flip observation matching its ticker /
        checkpoint / direction exists once the contract resolved; advance time is
        measured to the contract close. Resolve-time only — never retrains on open
        contracts. Returns the number newly reconciled."""
        if not self._available:
            return 0
        with self._lock, closing(self._connect()) as connection:
            pending = list(connection.execute(
                "SELECT * FROM flip_warnings WHERE model_version=? AND resolved=0", (MODEL_VERSION,),
            ))
            if not pending:
                return 0
            pred_rows = list(connection.execute(
                "SELECT ticker, checkpoint, asset, predicted_side, official_result, flip_risk_score, "
                "close_time, realized_cents FROM predictions "
                "WHERE model_version=? AND official_result IS NOT NULL", (MODEL_VERSION,),
            ))
            obs = self._flip_observations(pred_rows)
            flips = {(o["ticker"], o["checkpoint"], o["direction"]) for o in obs if o["flipped"]}
            close_by_ticker: dict[str, float] = {}
            realized_by_ticker: dict[str, float] = {}
            resolved_tickers: set[str] = set()
            for r in pred_rows:
                tk = str(r["ticker"])
                resolved_tickers.add(tk)
                ct = _num(_row_get(r, "close_time"))
                if ct is not None:
                    close_by_ticker[tk] = ct
                rc = _num(_row_get(r, "realized_cents"))
                if rc is not None:
                    realized_by_ticker[tk] = rc
            count = 0
            for w in pending:
                tk = str(w["ticker"])
                if tk not in resolved_tickers:
                    continue  # contract not settled yet
                occurred = (tk, str(w["checkpoint"]), str(w["direction"])) in flips
                close_t = close_by_ticker.get(tk)
                advance = (close_t - float(w["created_at"])) if close_t is not None else None
                realized = realized_by_ticker.get(tk)
                connection.execute(
                    "UPDATE flip_warnings SET resolved=1,flip_occurred=?,advance_seconds=?,realized_cents=? WHERE id=?",
                    (1 if occurred else 0, advance, realized, int(w["id"])),
                )
                count += 1
            connection.commit()
            return count

    def flip_warning_performance(self) -> dict[str, Any]:
        """Precision / detection-rate / advance-time / P&L of fired flip warnings.

        Broken down by checkpoint, direction, asset, and risk-score bucket. Missed
        flips = actual flips with no warning logged."""
        if not self._available:
            return {"available": False, "error": self._last_error}
        with self._lock, closing(self._connect()) as connection:
            warnings = list(connection.execute(
                "SELECT * FROM flip_warnings WHERE model_version=? AND resolved=1", (MODEL_VERSION,),
            ))
            pred_rows = list(connection.execute(
                "SELECT ticker, checkpoint, asset, predicted_side, official_result, flip_risk_score "
                "FROM predictions WHERE model_version=? AND official_result IS NOT NULL", (MODEL_VERSION,),
            ))
        all_flips = [o for o in self._flip_observations(pred_rows) if o["flipped"]]
        warned_keys = {(str(w["ticker"]), str(w["checkpoint"]), str(w["direction"])) for w in warnings if int(w["flip_occurred"] or 0)}

        def _agg(ws: Sequence[sqlite3.Row], flips: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
            alerts = len(ws)
            correct = sum(int(w["flip_occurred"] or 0) for w in ws)
            false = alerts - correct
            actual = len(flips)
            detected = sum(1 for f in flips if (f["ticker"], f["checkpoint"], f["direction"]) in warned_keys)
            advances = [float(w["advance_seconds"]) for w in ws if w["advance_seconds"] is not None and int(w["flip_occurred"] or 0)]
            pnl = [float(w["realized_cents"]) for w in ws if w["realized_cents"] is not None]
            return {
                "alerts": alerts, "correct": correct, "false": false,
                "precision": round(correct / alerts, 4) if alerts else None,
                "actual_flips": actual, "detected": detected, "missed": actual - detected,
                "detection_rate": round(detected / actual, 4) if actual else None,
                "avg_advance_seconds": round(sum(advances) / len(advances), 1) if advances else None,
                "realized_total_cents": round(sum(pnl), 2) if pnl else 0.0,
            }

        by_checkpoint = {cp: _agg([w for w in warnings if str(w["checkpoint"]) == cp],
                                  [f for f in all_flips if f["checkpoint"] == cp]) for cp in TRACKED_CHECKPOINTS}
        directions = ("YES → NO", "NO → YES")
        by_direction = {d: _agg([w for w in warnings if str(w["direction"]) == d],
                                [f for f in all_flips if f["direction"] == d]) for d in directions}
        assets = sorted({str(w["asset"]) for w in warnings} | {str(f["asset"]) for f in all_flips})
        by_asset = {a: _agg([w for w in warnings if str(w["asset"]) == a],
                            [f for f in all_flips if str(f["asset"]) == a]) for a in assets}
        by_bucket = {}
        for label in flip_risk.BUCKET_ORDER:
            ws = [w for w in warnings if w["risk_score"] is not None and flip_risk.bucket_label(float(w["risk_score"])) == label]
            if ws:
                by_bucket[label] = _agg(ws, [])
        # The dedicated high-flip-risk Telegram alert is OFF by default (owner
        # removed that UI; re-enable with Q15_V95_FLIP_ALERTS_ENABLED). When it
        # is off, NO warnings are ever recorded, so "0 detected / N missed" is a
        # disabled channel, NOT a detection failure — surface the flag so the
        # report can say so honestly instead of implying 100% missed.
        alerts_enabled = _env_bool("Q15_V95_FLIP_ALERTS_ENABLED", False)
        return {
            "available": True, "model_version": MODEL_VERSION,
            "alerts_enabled": alerts_enabled,
            "overall": _agg(warnings, all_flips),
            "by_checkpoint": by_checkpoint, "by_direction": by_direction,
            "by_asset": by_asset, "by_score_bucket": by_bucket,
        }

    @staticmethod
    def _paired_better_test(rows: Sequence[sqlite3.Row], worse_key: str, better_key: str) -> dict[str, Any]:
        """Paired test of whether `better_key` Brier is genuinely below `worse_key`.

        d_i = worse - better (positive favors the challenger). Returns the mean
        Brier reduction, t statistic, and two-sided p-value, so promotion rests on
        statistical significance rather than a fixed margin."""
        diffs = []
        for row in rows:
            worse = _num(_row_get(row, worse_key))
            better = _num(_row_get(row, better_key))
            if worse is not None and better is not None:
                diffs.append(worse - better)
        n = len(diffs)
        if n < 2:
            return {"n": n, "mean_brier_reduction": None, "t": None, "p_value": None,
                    "favored": False, "reason": "insufficient_pairs"}
        mean = sum(diffs) / n
        variance = sum((d - mean) ** 2 for d in diffs) / (n - 1)
        # Effect-size floor. The real spurious-promotion vector is a sample whose
        # diffs are nearly identical AND tiny: with real float Brier values the
        # variance is numerically tiny (not exactly zero), so the standard error
        # collapses and an economically meaningless mean reduction (e.g. +0.0002
        # Brier) blows the t statistic up and reads as highly significant.
        # Significance is not materiality, so require a minimum mean Brier
        # reduction before the challenger can be favored. The opposite case —
        # small variance with a *large* mean — is genuinely strong, consistent
        # evidence and is intentionally NOT blocked. This only gates the
        # observational manual-review screen (never live weights), so a small
        # default is safe; the default is negligible against realistic
        # improvements (~0.01-0.02 Brier). Set to 0.0 to disable.
        min_effect = _env_float("Q15_V95_PROMOTION_MIN_EFFECT", 0.002, 0.0, 1.0)
        if abs(mean) < min_effect:
            return {"n": n, "mean_brier_reduction": round(mean, 6), "t": None, "p_value": None,
                    "favored": False, "reason": "effect_below_floor"}
        se = math.sqrt(variance / n) if variance > 0 else 0.0
        t = (mean / se) if se > 0 else (math.inf if mean > 0 else -math.inf if mean < 0 else 0.0)
        return {
            "n": n, "mean_brier_reduction": round(mean, 6),
            "t": round(t, 4) if math.isfinite(t) else None,
            # Paired one-sample test on the diffs → df = n - 1 (exact Student-t).
            "p_value": _round_p(_two_sided_p(t, df=n - 1)), "favored": mean > 0, "reason": "ok",
        }

    def metrics(self) -> dict[str, Any]:
        if not self._available:
            return {"available": False, "error": self._last_error}
        with self._lock, closing(self._connect()) as connection:
            rows = list(connection.execute(
                "SELECT * FROM predictions WHERE model_version=? AND official_result IS NOT NULL ORDER BY resolved_at",
                (MODEL_VERSION,),
            ))
        def aggregate(selected: Sequence[sqlite3.Row]) -> dict[str, Any]:
            if not selected:
                return {"resolved": 0}
            # Average each scoring column over only the rows that actually carry a
            # finite value. A single NULL (e.g. a row written under an older schema
            # before brier/logloss were populated) must not raise from float(None)
            # or silently collapse the whole checkpoint group to {"resolved": 0}.
            def _mean(key: str) -> float | None:
                vals = [v for v in (_num(_row_get(row, key)) for row in selected) if v is not None]
                return (sum(vals) / len(vals)) if vals else None
            return {
                "resolved": len(selected),
                "correct": sum(int(row["correct"] or 0) for row in selected),
                "accuracy": sum(int(row["correct"] or 0) for row in selected) / len(selected),
                "champion_brier": _mean("champion_brier"),
                "challenger_brier": _mean("challenger_brier"),
                "baseline_brier": _mean("baseline_brier"),
                "champion_logloss": _mean("champion_logloss"),
                "challenger_logloss": _mean("challenger_logloss"),
                "baseline_logloss": _mean("baseline_logloss"),
            }
        overall = aggregate(rows)
        by_checkpoint = {cp: aggregate([r for r in rows if r["checkpoint"] == cp]) for cp in TRACKED_CHECKPOINTS}
        by_regime: dict[str, Any] = {}
        for regime in sorted({str(r["regime"]) for r in rows}):
            by_regime[regime] = aggregate([r for r in rows if r["regime"] == regime])
        bins: list[dict[str, Any]] = []
        for lower in range(50, 100, 5):
            upper = lower + 5
            chosen = []
            for row in rows:
                side_p = float(row["selected_probability"])
                pct = side_p * 100.0
                if lower <= pct < upper:
                    chosen.append(row)
            if chosen:
                bins.append({
                    "band": f"{lower}-{upper}%", "count": len(chosen),
                    "mean_predicted": sum(float(r["selected_probability"]) for r in chosen) / len(chosen),
                    "actual_win_rate": sum(int(r["correct"] or 0) for r in chosen) / len(chosen),
                })
        # Expected Calibration Error: count-weighted mean |predicted - actual| over
        # the populated bins. The canonical second-order calibration stat alongside
        # Brier/log-loss; observational only (never steers a live decision). Bands
        # only span [50%, 100%) — the selected side's probability is always >= 0.5 —
        # so this is the ECE of the chosen-side confidence.
        _ece_n = sum(b["count"] for b in bins)
        ece = (
            sum(b["count"] * abs(b["mean_predicted"] - b["actual_win_rate"]) for b in bins) / _ece_n
            if _ece_n else None
        )
        alpha = _env_float("Q15_V95_PROMOTION_ALPHA", 0.05, 0.0001, 0.5)
        # Optional Bonferroni correction across the two paired tests (vs champion AND
        # vs baseline). Default OFF preserves the historical screening behaviour
        # (each test at `alpha`, combined Type-I ~2*alpha). When ON, each test must
        # clear alpha/2 so the family-wise rate stays <= alpha. Read-only: this only
        # tightens a manual-review flag; production weights stay frozen and nothing
        # auto-promotes either way.
        bonferroni = _env_bool("Q15_V95_PROMOTION_BONFERRONI", False)
        per_test_alpha = (alpha / 2.0) if bonferroni else alpha
        promotion_by_checkpoint: dict[str, dict[str, Any]] = {}
        for checkpoint in LEARNING_CHECKPOINTS:
            cp_rows = [r for r in rows if r["checkpoint"] == checkpoint]
            resolved = len(cp_rows)
            candidate = False
            vs_champion = vs_baseline = None
            reason = "learning_disabled" if not self.learning_enabled(checkpoint) else "insufficient_resolved_rows"
            if self.learning_enabled(checkpoint) and resolved >= self.minimum_promotion_rows:
                # Significant paired Brier improvement over BOTH champion and baseline.
                # Each paired test must clear `per_test_alpha`: with Bonferroni ON that
                # is alpha/2 (family-wise <= alpha across the two tests); OFF it is the
                # raw alpha (historical behaviour). Either way this is only a screening
                # gate that flags candidates for manual review — it never promotes
                # automatically (production_weights_frozen).
                vs_champion = self._paired_better_test(cp_rows, "champion_brier", "challenger_brier")
                vs_baseline = self._paired_better_test(cp_rows, "baseline_brier", "challenger_brier")
                pc, pb = vs_champion["p_value"], vs_baseline["p_value"]
                significant = (
                    vs_champion["favored"] and pc is not None and pc < per_test_alpha
                    and vs_baseline["favored"] and pb is not None and pb < per_test_alpha
                )
                candidate = bool(significant)
                reason = "eligible_for_manual_review" if candidate else "challenger_not_significantly_better"
            promotion_by_checkpoint[checkpoint] = {
                "candidate": candidate, "reason": reason, "resolved": resolved,
                "learning_enabled": self.learning_enabled(checkpoint),
                "alpha": alpha, "per_test_alpha": per_test_alpha, "bonferroni": bonferroni,
                "vs_champion": vs_champion, "vs_baseline": vs_baseline,
            }
        primary = promotion_by_checkpoint[self.primary_learning_checkpoint]
        return {
            "available": True, "model_version": MODEL_VERSION, "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "overall": overall, "by_checkpoint": by_checkpoint, "by_regime": by_regime,
            "scoreboard": self._scoreboard_rows(rows),
            "calibration_bands": bins, "expected_calibration_error": ece,
            "promotion_candidate": bool(primary["candidate"]),
            "promotion_reason": str(primary["reason"]), "promotion_by_checkpoint": promotion_by_checkpoint,
            "primary_learning_checkpoint": self.primary_learning_checkpoint,
            "automatic_promotion": False, "minimum_promotion_rows": self.minimum_promotion_rows,
        }

    def reserve_notification(self, *, event_key: str, checkpoint: str, state: str,
                             fingerprint: str, now: float | None = None) -> str | None:
        """Atomic persistent transition gate. Failed sends can be retried."""
        if not self._available:
            return None
        now = now or time.time()
        reservation = _env_float("Q15_V95_TELEGRAM_RESERVATION_SECONDS", 30.0, 5.0, 300.0)
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM notification_state WHERE event_key=?", (event_key,)).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO notification_state(event_key,checkpoint,state,fingerprint,reserved_until,updated_at) VALUES(?,?,?,?,?,?)",
                    (event_key, checkpoint, state, fingerprint, now + reservation, now),
                )
                connection.commit()
                return event_key
            if row["sent_at"] is None and float(row["reserved_until"] or 0.0) <= now:
                connection.execute(
                    "UPDATE notification_state SET state=?,fingerprint=?,reserved_until=?,updated_at=? WHERE event_key=?",
                    (state, fingerprint, now + reservation, now, event_key),
                )
                connection.commit()
                return event_key
            previous_state = str(row["state"])
            meaningful = previous_state != state and (
                state in {"ENTRY_RECOMMENDED", "ENTRY_WITHDRAWN", "CHECKPOINT_CLOSED", "RESULT_RESOLVED"}
                or previous_state == "ENTRY_RECOMMENDED"
            )
            if row["sent_at"] is not None and meaningful:
                transition_key = f"{event_key}|{previous_state}->{state}"
                existing = connection.execute("SELECT * FROM notification_state WHERE event_key=?", (transition_key,)).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO notification_state(event_key,checkpoint,state,fingerprint,reserved_until,updated_at) VALUES(?,?,?,?,?,?)",
                        (transition_key, checkpoint, state, fingerprint, now + reservation, now),
                    )
                    connection.commit()
                    return transition_key
                if existing["sent_at"] is None and float(existing["reserved_until"] or 0.0) <= now:
                    connection.execute(
                        "UPDATE notification_state SET fingerprint=?,reserved_until=?,updated_at=? WHERE event_key=?",
                        (fingerprint, now + reservation, now, transition_key),
                    )
                    connection.commit()
                    return transition_key
            connection.commit()
            return None

    def notification_state(self, event_key: str) -> str | None:
        if not self._available:
            return None
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute("SELECT state FROM notification_state WHERE event_key=?", (event_key,)).fetchone()
            return None if row is None else str(row["state"])

    def complete_notification(self, *, event_key: str, success: bool, now: float | None = None) -> None:
        if not self._available:
            return
        now = now or time.time()
        with self._lock, closing(self._connect()) as connection:
            if success:
                connection.execute(
                    "UPDATE notification_state SET sent_at=?,reserved_until=NULL,updated_at=? WHERE event_key=?",
                    (now, now, event_key),
                )
                if "->" in event_key:
                    base_key, transition = event_key.rsplit("|", 1)
                    final_state = transition.split("->", 1)[1]
                    connection.execute(
                        "UPDATE notification_state SET state=?,updated_at=? WHERE event_key=?",
                        (final_state, now, base_key),
                    )
            else:
                connection.execute(
                    "UPDATE notification_state SET reserved_until=NULL,failures=failures+1,updated_at=? WHERE event_key=?",
                    (now, event_key),
                )
            connection.commit()

    def status(self) -> dict[str, Any]:
        if not self._available:
            return {"available": False, "path": str(self.path), "error": self._last_error}
        with self._lock, closing(self._connect()) as connection:
            counts = connection.execute(
                """SELECT COUNT(*) total,
                   SUM(CASE WHEN official_result IS NOT NULL THEN 1 ELSE 0 END) resolved,
                   SUM(CASE WHEN checkpoint='15M' THEN 1 ELSE 0 END) fifteen,
                   SUM(CASE WHEN checkpoint='10M' THEN 1 ELSE 0 END) ten,
                   SUM(CASE WHEN checkpoint='7M' THEN 1 ELSE 0 END) seven,
                   SUM(CASE WHEN learning_applied=1 THEN 1 ELSE 0 END) learned
                   FROM predictions WHERE model_version=?""",
                (MODEL_VERSION,),
            ).fetchone()
            notifications = connection.execute(
                "SELECT COUNT(*) total,SUM(CASE WHEN sent_at IS NOT NULL THEN 1 ELSE 0 END) sent,SUM(failures) failures FROM notification_state"
            ).fetchone()
            # Compact pushed-prediction record per checkpoint, for the live alert's
            # "current pushed accuracy" line (settled pushed predictions only).
            pushed_rows = list(connection.execute(
                """SELECT checkpoint,
                   SUM(CASE WHEN official_result IS NOT NULL THEN 1 ELSE 0 END) settled,
                   SUM(CASE WHEN correct=1 THEN 1 ELSE 0 END) correct_n
                   FROM predictions WHERE model_version=? AND pushed=1 GROUP BY checkpoint""",
                (MODEL_VERSION,),
            ))
            last_update = connection.execute("SELECT * FROM checkpoint_challenger_updates ORDER BY id DESC LIMIT 1").fetchone()
            updates_by_checkpoint = {
                checkpoint: int(connection.execute(
                    "SELECT COUNT(*) FROM checkpoint_challenger_updates WHERE checkpoint=?", (checkpoint,)
                ).fetchone()[0] or 0)
                for checkpoint in ("10M", "15M")
            }
            regime_rows = list(connection.execute(
                "SELECT checkpoint, regime, MAX(CASE WHEN name='intercept' THEN updates ELSE 0 END) AS results "
                "FROM regime_challenger_weights GROUP BY checkpoint, regime ORDER BY results DESC"
            ))
        regime_challengers = [
            {
                "checkpoint": str(r["checkpoint"]), "regime": str(r["regime"]),
                "results": int(r["results"] or 0),
                "active": int(r["results"] or 0) >= self.minimum_regime_updates,
            }
            for r in regime_rows
        ]
        return {
            "available": True, "path": str(self.path), "version": VERSION,
            "model_version": MODEL_VERSION, "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "unique_predictions": int(counts["total"] or 0), "unique_resolved": int(counts["resolved"] or 0),
            "fifteen_minute_predictions": int(counts["fifteen"] or 0),
            "ten_minute_predictions": int(counts["ten"] or 0),
            "seven_minute_predictions": int(counts["seven"] or 0),
            "shadow_updates_applied": int(counts["learned"] or 0),
            "champion_weights": dict(CHAMPION_WEIGHTS),
            "challenger_weights": {checkpoint: self.challenger_weights(checkpoint) for checkpoint in ("10M", "15M")},
            "primary_learning_checkpoint": self.primary_learning_checkpoint,
            "learning_enabled_by_checkpoint": dict(self.learning_enabled_by_checkpoint),
            "learning_rate_by_checkpoint": dict(self.learning_rate_by_checkpoint),
            "minimum_learning_quality_by_checkpoint": dict(self.minimum_learning_quality_by_checkpoint),
            "shadow_updates_by_checkpoint": updates_by_checkpoint,
            "regime_learning_enabled": self.regime_learning_enabled,
            "minimum_regime_updates": self.minimum_regime_updates,
            "regime_challengers": regime_challengers,
            "shadow_learning_enabled": self.shadow_learning_enabled, "production_weights_frozen": True,
            "automatic_promotion": False, "automatic_threshold_changes": False,
            "last_shadow_update": dict(last_update) if last_update else None,
            "dropped_feature_rows": int(self._dropped_feature_rows),
            "dropped_feature_rows_by_checkpoint": {k: int(v) for k, v in self._dropped_feature_rows_by_checkpoint.items()},
            "last_learning_step_magnitude": float(self._last_learning_step_magnitude),
            "learning_frozen_results": int(self._learning_frozen_results),
            "calibration_unconverged_fallbacks": int(self._calibration_unconverged_fallbacks),
            "shadow_errors": int(self._shadow_errors),
            "last_shadow_error": self._last_shadow_error,
            "notifications": {"total": int(notifications["total"] or 0), "sent": int(notifications["sent"] or 0), "failures": int(notifications["failures"] or 0)},
            "pushed_by_checkpoint": {
                str(r["checkpoint"]): {
                    "settled": int(r["settled"] or 0), "right": int(r["correct_n"] or 0),
                    "accuracy": (round(int(r["correct_n"] or 0) / int(r["settled"]), 4) if int(r["settled"] or 0) else None),
                }
                for r in pushed_rows
            },
        }


__all__ = [
    "CHAMPION_WEIGHTS", "FEATURE_SCHEMA_VERSION", "MODEL_VERSION", "READ_ONLY", "VERSION", "V95Ledger"
]
