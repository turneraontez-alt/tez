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
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from q15_upgrade import flip_risk

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


def _two_sided_p(t: float) -> float:
    """Two-sided p-value from a t/z statistic via a normal approximation.

    Promotion only runs at n >= 50, where the normal approximation to Student's
    t is accurate enough for a screening gate (final calls remain manual)."""
    if not math.isfinite(t):
        # A non-finite statistic (NaN, or ±inf from a degenerate zero-variance
        # sample) carries no usable evidence of a real difference, so the
        # conservative p-value is 1.0 — never treat it as maximal significance.
        return 1.0
    return _clamp(2.0 * (1.0 - _normal_cdf(abs(t))), 0.0, 1.0)


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
        # up instead of quietly thinning calibration/learning inputs.
        self._dropped_feature_rows = 0
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
                    checkpoint TEXT NOT NULL,
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
            now = time.time()
            for checkpoint in LEARNING_CHECKPOINTS:
                for name, base in CHAMPION_WEIGHTS.items():
                    connection.execute(
                        "INSERT OR IGNORE INTO checkpoint_challenger_weights(checkpoint,name,base_value,value,updated_at) VALUES(?,?,?,?,?)",
                        (checkpoint, name, base, base, now),
                    )
            connection.commit()

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        existing = {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    @staticmethod
    def _checkpoint(value: Any) -> str:
        checkpoint = str(value or "").strip().upper()
        return checkpoint if checkpoint in TRACKED_CHECKPOINTS else "10M"

    def learning_enabled(self, checkpoint: str) -> bool:
        return bool(self.learning_enabled_by_checkpoint[self._checkpoint(checkpoint)])

    @staticmethod
    def _regime_key(regime: Any) -> str:
        return (str(regime or "UNKNOWN").strip().upper() or "UNKNOWN")

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
                          flip_evidence_count: int | None = None) -> tuple[str, bool]:
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
            connection.commit()
            return prediction_id, cursor.rowcount == 1

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
                    realized = round((100.0 - ask - cost) if correct else -(ask + cost), 4)
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
                logger.warning("Unparseable feature_json for prediction_id=%s; skipping learning", prediction_id)
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
            # rows still have limited influence.
            sample_weight = 0.25 + 0.75 * quality_factor
            if checkpoint == self.primary_learning_checkpoint:
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
        if len(rows) < 10 or len(winners) < 3 or len(losers) < 3:
            result: dict[str, Any] = {"active": False, "resolved": len(rows)}
        else:
            names = [name for name in CHAMPION_WEIGHTS if name != "intercept"]
            def centroid(selected: Sequence[sqlite3.Row]) -> dict[str, float]:
                sums = {name: 0.0 for name in names}
                weights = {name: 0.0 for name in names}
                for row in selected:
                    try:
                        values = json.loads(str(row["feature_json"]))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        self._dropped_feature_rows += 1
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
        if self._cache_enabled:
            with self._lock:
                if self._data_version == version:
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
        calibrated = _clamp(_sigmoid(fit["intercept"] + fit["slope"] * _logit(raw)), 0.01, 0.99)
        return {
            "probability": calibrated, "active": True, "reason": "platt_current_version",
            "rows": fit["rows"], "intercept": fit["intercept"], "slope": fit["slope"],
            "scope": fit["scope"],
        }

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
            # Penalized logistic regression y ~ intercept + slope*logit(raw_p).
            intercept, slope = 0.0, 1.0
            converged = False
            iterations = 0
            for _ in range(12):
                iterations += 1
                g0 = -0.20 * intercept
                g1 = -0.20 * (slope - 1.0)
                h00 = 0.20
                h01 = 0.0
                h11 = 0.20
                for row in rows:
                    x = _clamp(_logit(float(row["raw_yes_probability"])), -4.0, 4.0)
                    y = 1.0 if row["official_result"] == "YES" else 0.0
                    p = _sigmoid(intercept + slope * x)
                    residual = y - p
                    variance = max(1e-6, p * (1.0 - p))
                    g0 += residual
                    g1 += residual * x
                    h00 += variance
                    h01 += variance * x
                    h11 += variance * x * x
                determinant = h00 * h11 - h01 * h01
                if determinant <= 1e-9:
                    break
                d0 = (g0 * h11 - g1 * h01) / determinant
                d1 = (g1 * h00 - g0 * h01) / determinant
                intercept = _clamp(intercept + d0, -0.75, 0.75)
                slope = _clamp(slope + d1, 0.50, 1.50)
                if abs(d0) + abs(d1) < 1e-6:
                    converged = True
                    break
            if not converged:
                # Rare given regularization; surface it instead of silently
                # shipping an unconverged calibrator.
                logger.warning(
                    "Platt calibration did not converge for checkpoint=%s scope=%s rows=%d "
                    "after %d iterations (intercept=%.4f slope=%.4f)",
                    checkpoint, scope, len(rows), iterations, intercept, slope,
                )
            fit = {"active": True, "reason": "platt_current_version", "rows": len(rows),
                   "intercept": intercept, "slope": slope, "scope": scope,
                   "converged": converged, "iterations": iterations}
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
        return {
            "right": right, "wrong": n - right, "n": n,
            "accuracy": round(right / n, 4) if n else None,
            "ci_low": low, "ci_high": high,
            "low_n": bool(n and n < threshold),
            "pnl_n": pnl_n,
            "realized_total_cents": round(sum(realized), 2) if realized else 0.0,
            "realized_avg_cents": round(sum(realized) / pnl_n, 2) if pnl_n else None,
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
        return {
            "available": True, "model_version": MODEL_VERSION,
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
            return {"n": n, "mean_brier_reduction": None, "t": None, "p_value": None, "favored": False}
        mean = sum(diffs) / n
        variance = sum((d - mean) ** 2 for d in diffs) / (n - 1)
        se = math.sqrt(variance / n) if variance > 0 else 0.0
        t = (mean / se) if se > 0 else (math.inf if mean > 0 else -math.inf if mean < 0 else 0.0)
        return {
            "n": n, "mean_brier_reduction": round(mean, 6),
            "t": round(t, 4) if math.isfinite(t) else None,
            "p_value": round(_two_sided_p(t), 6), "favored": mean > 0,
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
            return {
                "resolved": len(selected),
                "correct": sum(int(row["correct"] or 0) for row in selected),
                "accuracy": sum(int(row["correct"] or 0) for row in selected) / len(selected),
                "champion_brier": sum(float(row["champion_brier"]) for row in selected) / len(selected),
                "challenger_brier": sum(float(row["challenger_brier"]) for row in selected) / len(selected),
                "baseline_brier": sum(float(row["baseline_brier"]) for row in selected) / len(selected),
                "champion_logloss": sum(float(row["champion_logloss"]) for row in selected) / len(selected),
                "challenger_logloss": sum(float(row["challenger_logloss"]) for row in selected) / len(selected),
                "baseline_logloss": sum(float(row["baseline_logloss"]) for row in selected) / len(selected),
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
        alpha = _env_float("Q15_V95_PROMOTION_ALPHA", 0.05, 0.0001, 0.5)
        promotion_by_checkpoint: dict[str, dict[str, Any]] = {}
        for checkpoint in LEARNING_CHECKPOINTS:
            cp_rows = [r for r in rows if r["checkpoint"] == checkpoint]
            resolved = len(cp_rows)
            candidate = False
            vs_champion = vs_baseline = None
            reason = "learning_disabled" if not self.learning_enabled(checkpoint) else "insufficient_resolved_rows"
            if self.learning_enabled(checkpoint) and resolved >= self.minimum_promotion_rows:
                # Significant paired Brier improvement over BOTH champion and baseline.
                vs_champion = self._paired_better_test(cp_rows, "champion_brier", "challenger_brier")
                vs_baseline = self._paired_better_test(cp_rows, "baseline_brier", "challenger_brier")
                pc, pb = vs_champion["p_value"], vs_baseline["p_value"]
                significant = (
                    vs_champion["favored"] and pc is not None and pc < alpha
                    and vs_baseline["favored"] and pb is not None and pb < alpha
                )
                candidate = bool(significant)
                reason = "eligible_for_manual_review" if candidate else "challenger_not_significantly_better"
            promotion_by_checkpoint[checkpoint] = {
                "candidate": candidate, "reason": reason, "resolved": resolved,
                "learning_enabled": self.learning_enabled(checkpoint),
                "alpha": alpha, "vs_champion": vs_champion, "vs_baseline": vs_baseline,
            }
        primary = promotion_by_checkpoint[self.primary_learning_checkpoint]
        return {
            "available": True, "model_version": MODEL_VERSION, "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "overall": overall, "by_checkpoint": by_checkpoint, "by_regime": by_regime,
            "scoreboard": self._scoreboard_rows(rows),
            "calibration_bands": bins, "promotion_candidate": bool(primary["candidate"]),
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
