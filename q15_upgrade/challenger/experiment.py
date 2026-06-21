"""Experiment governance (addendum Section H).

A frozen pre-registration record + an append-only experiment ledger and holdout
access log. Every model / feature set / hyperparameter / threshold / calibration /
cost variation tried — including rejected and failed ones — is recorded, so the
final report cannot cherry-pick the best run, and final-holdout touches are
auditable.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class PreRegistration:
    primary_metric: str = "out_of_sample_log_loss"
    secondary_metrics: list[str] = field(default_factory=lambda: [
        "brier_score", "expected_calibration_error", "net_expected_value_cents",
        "max_drawdown", "paired_log_loss_diff_vs_control",
    ])
    primary_challenger: str = "logistic"          # conservative first; GBM only at scale
    main_feature_set: str = "derived + champion-signals + prediction-market microstructure"
    cost_assumptions: str = "fee 7%·notional, slippage 0.5·spread, impact 0.25c, +latency/adverse margins"
    calibration_selection_rule: str = "lowest validation log-loss among {identity,platt,isotonic}"
    threshold_selection_rule: str = "validation-only; sensitivity-tested; no test-set tuning"
    final_holdout_period: str = "NOT_PROVIDED — freeze most-recent 4-6 months once data exists"
    promotion_criteria: list[str] = field(default_factory=lambda: [
        "OOS log-loss < control (paired, block-bootstrap CI excludes 0)",
        "Brier <= control and better calibration (ECE)",
        "positive net EV after realistic costs; acceptable under stress",
        "drawdown <= control; stable across >=3 walk-forward periods",
        "no leakage; passes offline-vs-live parity; latency within budget",
        "adequate shadow sample (effective, not overlapping) across regimes",
    ])
    failure_criteria: list[str] = field(default_factory=lambda: [
        "merely copies market price w/o improving log-loss/Brier/calibration/EV",
        "improvement concentrated in one brief regime",
        "fails under moderately higher costs",
        "calibration deteriorates; unstable month-to-month",
    ])
    frozen_at: float | None = None

    def freeze(self) -> "PreRegistration":
        if self.frozen_at is None:
            self.frozen_at = time.time()
        return self

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at REAL NOT NULL,
    kind TEXT,                 -- model|feature_set|hyperparam|threshold|calibration|cost|backtest
    description TEXT,
    config_json TEXT,
    metrics_json TEXT,
    status TEXT,               -- proposed|run|rejected|failed|kept
    code_commit TEXT,
    config_hash TEXT
);
CREATE TABLE IF NOT EXISTS holdout_access (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    accessed_at REAL NOT NULL,
    reason TEXT,
    influenced_design INTEGER  -- 1 if this access influenced a design decision (taints holdout)
);
CREATE TABLE IF NOT EXISTS preregistration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    frozen_at REAL NOT NULL,
    record_json TEXT NOT NULL
);
"""


class ExperimentLedger:
    """Append-only experiment + holdout-access ledger."""

    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def register(self, prereg: PreRegistration) -> None:
        prereg.freeze()
        self._conn.execute(
            "INSERT INTO preregistration (frozen_at, record_json) VALUES (?,?)",
            (prereg.frozen_at, prereg.to_json()),
        )
        self._conn.commit()

    def log_experiment(self, *, kind: str, description: str, status: str,
                       config: dict | None = None, metrics: dict | None = None,
                       code_commit: str = "", config_hash: str = "") -> int:
        cur = self._conn.execute(
            "INSERT INTO experiments (logged_at, kind, description, config_json, "
            "metrics_json, status, code_commit, config_hash) VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), kind, description, json.dumps(config or {}),
             json.dumps(metrics or {}), status, code_commit, config_hash),
        )
        self._conn.commit()
        return cur.lastrowid

    def log_holdout_access(self, reason: str, influenced_design: bool) -> None:
        self._conn.execute(
            "INSERT INTO holdout_access (accessed_at, reason, influenced_design) VALUES (?,?,?)",
            (time.time(), reason, 1 if influenced_design else 0),
        )
        self._conn.commit()

    def holdout_tainted(self) -> bool:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM holdout_access WHERE influenced_design=1"
        ).fetchone()
        return (row["n"] or 0) > 0

    def all_experiments(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM experiments ORDER BY id")]
