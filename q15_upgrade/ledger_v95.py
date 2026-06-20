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
import math
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

VERSION = "q15-v9.5.1-ledger-v2-10m-primary"
MODEL_VERSION = "q15-v9.5.1-champion-ensemble-10m-primary-v1"
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
        return 0.0
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
        legacy_enabled = _env_bool("Q15_V95_SHADOW_LEARNING", True)
        # 7M is tracked and graded for accuracy alongside 15M/10M, but its weight
        # learning is observational by default (like 15M). Enabling it requires the
        # challenger weight tables to admit '7M' — see _initialize.
        self.learning_enabled_by_checkpoint = {
            "10M": _env_bool("Q15_V95_10M_SHADOW_LEARNING", legacy_enabled),
            "15M": _env_bool("Q15_V95_15M_SHADOW_LEARNING", False),
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
                          costs: Mapping[str, Any] | None = None) -> tuple[str, bool]:
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
                       feature_json,contribution_json,quote_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    prediction_id, MODEL_VERSION, FEATURE_SCHEMA_VERSION, ticker, asset, checkpoint,
                    created_at, close_time, predicted_side, raw_yes_probability,
                    calibrated_yes_probability, challenger_yes_probability, baseline_yes_probability,
                    selected_probability, conservative_probability, data_quality, evidence_quality,
                    trade_quality, trade_decision, regime, rank_value, entry_ask, entry_cost,
                    _json(dict(features)), _json(dict(contributions)), _json(dict(quote)),
                ),
            )
            connection.commit()
            return prediction_id, cursor.rowcount == 1

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
                sample_weight = min(1.0, sample_weight * 1.10)
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
            for _ in range(12):
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
                    break
            fit = {"active": True, "reason": "platt_current_version", "rows": len(rows),
                   "intercept": intercept, "slope": slope, "scope": scope}
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
    def _rank_bucket(row: Mapping[str, Any]) -> str:
        try:
            value = int(row["rank"]) if row["rank"] is not None else None
        except (TypeError, ValueError, IndexError, KeyError):
            value = None
        return str(value) if value in (1, 2, 3) else "other"

    def _scoreboard_rows(self, rows: Sequence[sqlite3.Row]) -> dict[str, Any]:
        """Right/wrong/accuracy by interval (15M/10M/7M), pick rank (#1/#2/#3), and asset."""
        by_checkpoint = {cp: self._win_loss([r for r in rows if r["checkpoint"] == cp]) for cp in TRACKED_CHECKPOINTS}
        by_rank = {
            label: self._win_loss([r for r in rows if self._rank_bucket(r) == label])
            for label in ("1", "2", "3", "other")
        }
        assets = sorted({str(r["asset"]) for r in rows})
        by_asset = {a: self._win_loss([r for r in rows if str(r["asset"]) == a]) for a in assets}
        # How the top pick (#1) fares per coin — "which coins the #1 pick wins on".
        rank1 = [r for r in rows if self._rank_bucket(r) == "1"]
        top_pick_by_asset = {
            a: self._win_loss([r for r in rank1 if str(r["asset"]) == a])
            for a in sorted({str(r["asset"]) for r in rank1})
        }
        return {
            "overall": self._win_loss(rows), "by_checkpoint": by_checkpoint,
            "by_rank": by_rank, "by_asset": by_asset, "top_pick_by_asset": top_pick_by_asset,
        }

    def scoreboard(self) -> dict[str, Any]:
        """User-facing record: how often each interval, rank, and asset was right/wrong."""
        if not self._available:
            return {"available": False, "error": self._last_error}
        with self._lock, closing(self._connect()) as connection:
            rows = list(connection.execute(
                "SELECT checkpoint, correct, rank, asset, realized_cents FROM predictions "
                "WHERE model_version=? AND official_result IS NOT NULL",
                (MODEL_VERSION,),
            ))
        return {
            "available": True, "model_version": MODEL_VERSION,
            "intervals": TRACKED_CHECKPOINTS, **self._scoreboard_rows(rows),
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
            "notifications": {"total": int(notifications["total"] or 0), "sent": int(notifications["sent"] or 0), "failures": int(notifications["failures"] or 0)},
        }


__all__ = [
    "CHAMPION_WEIGHTS", "FEATURE_SCHEMA_VERSION", "MODEL_VERSION", "READ_ONLY", "VERSION", "V95Ledger"
]
