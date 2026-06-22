"""Ultoim Build — isolated persistence, settlement grading, and scoreboard.

Completely separate from the live V9.5 ledger: its own SQLite file, its own
tables, its own prediction ids / counters / model version / reset marker. Picks
are immutable once written (INSERT OR IGNORE on a unique key); settlement only
fills the official result / correctness / P&L and never rewrites a pick. The
visible record counts only DELIVERED picks; delivery-failed picks are kept for
background learning but excluded from the official totals.
"""
from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ultoim_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    model_version TEXT NOT NULL,
    asset TEXT NOT NULL,
    ticker TEXT NOT NULL,
    interval TEXT NOT NULL,
    window_key INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    predicted_side TEXT NOT NULL,
    outcome_pct REAL,                 -- displayed % for the chosen side
    selected_yes_probability REAL,
    conservative_probability REAL,
    net_edge_cents REAL,
    entry_ask_cents REAL,
    quality_score REAL,
    confidence_grade TEXT,            -- Ultoim multi-factor A/B/C
    data_quality REAL,
    evidence_quality REAL,
    model_agreement REAL,
    manipulation_suspected INTEGER DEFAULT 0,
    flip_probability REAL,
    flip_decision TEXT,               -- YES = predicted to flip, NO = hold
    original_predicted_side TEXT,
    expected_flip_side TEXT,
    order_flow_persistence REAL,
    book_resiliency REAL,
    prediction_stability REAL,
    close_time REAL,
    snapshot_id TEXT,
    delivery_status TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING|SENT|MUTED|DELIVERY_FAILED
    message_id INTEGER,
    delivery_error TEXT,
    official_result TEXT,             -- YES|NO once settled
    resolved_at REAL,
    correct INTEGER,                  -- 1/0 once settled
    hypothetical_pnl_cents REAL,
    flip_occurred INTEGER,            -- final result != original side
    flip_call_correct INTEGER,        -- flip_decision matched flip_occurred
    UNIQUE(model_version, ticker, interval)
);
CREATE INDEX IF NOT EXISTS idx_ultoim_resolve
    ON ultoim_predictions(model_version, official_result, close_time);

CREATE TABLE IF NOT EXISTS ultoim_report_lock (
    model_version TEXT NOT NULL,
    interval TEXT NOT NULL,
    window_key INTEGER NOT NULL,
    locked_at REAL NOT NULL,
    message_id INTEGER,
    PRIMARY KEY(model_version, interval, window_key)
);

CREATE TABLE IF NOT EXISTS ultoim_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _window_key(close_time: float | None, now: float) -> int:
    """Bucket a contract into its 15-minute settlement window so all assets that
    settle together share one report lock."""
    basis = float(close_time) if close_time is not None else float(now)
    return int(basis // 900)


def _wilson(right: int, n: int, z: float = 1.96) -> tuple[float | None, float | None, float | None]:
    if n <= 0:
        return None, None, None
    p = right / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return p, centre - half, centre + half


class UltoimLedger:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- meta / reset marker --------------------------------------------------
    def ensure_reset_marker(self, model_version: str, now: float) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM ultoim_meta WHERE key='reset_at'"
            ).fetchone()
            if row is not None:
                return float(row["value"])
            self._conn.execute(
                "INSERT OR REPLACE INTO ultoim_meta(key,value) VALUES('reset_at',?)",
                (str(now),),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO ultoim_meta(key,value) VALUES('model_version',?)",
                (model_version,),
            )
            self._conn.commit()
            return now

    def reset_at(self) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM ultoim_meta WHERE key='reset_at'"
            ).fetchone()
        return float(row["value"]) if row is not None else None

    # -- dedup lock (one report per interval per window) ----------------------
    def report_locked(self, model_version: str, interval: str, window_key: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM ultoim_report_lock "
                "WHERE model_version=? AND interval=? AND window_key=?",
                (model_version, interval, window_key),
            ).fetchone()
        return row is not None

    def lock_report(self, model_version: str, interval: str, window_key: int, now: float) -> bool:
        """Claim the (interval, window) lock. Returns True iff this call created
        it (idempotent — a duplicate/concurrent fire is rejected)."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO ultoim_report_lock"
                "(model_version,interval,window_key,locked_at) VALUES(?,?,?,?)",
                (model_version, interval, window_key, now),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def set_report_message(self, model_version: str, interval: str, window_key: int,
                           message_id: int | None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE ultoim_report_lock SET message_id=? "
                "WHERE model_version=? AND interval=? AND window_key=?",
                (message_id, model_version, interval, window_key),
            )
            self._conn.commit()

    # -- record a pick (immutable) -------------------------------------------
    def record_pick(self, pick: Mapping[str, Any]) -> int | None:
        cols = (
            "created_at", "model_version", "asset", "ticker", "interval", "window_key",
            "rank", "predicted_side", "outcome_pct", "selected_yes_probability",
            "conservative_probability", "net_edge_cents", "entry_ask_cents",
            "quality_score", "confidence_grade", "data_quality", "evidence_quality",
            "model_agreement", "manipulation_suspected", "flip_probability",
            "flip_decision", "original_predicted_side", "expected_flip_side",
            "order_flow_persistence", "book_resiliency", "prediction_stability",
            "close_time", "snapshot_id", "delivery_status",
        )
        placeholders = ",".join("?" for _ in cols)
        values = [pick.get(c) for c in cols]
        with self._lock:
            try:
                cur = self._conn.execute(
                    f"INSERT INTO ultoim_predictions({','.join(cols)}) VALUES({placeholders})",
                    values,
                )
                self._conn.commit()
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                # Duplicate (model_version, ticker, interval) — already recorded.
                return None

    def mark_delivery(self, pick_id: int, status: str, message_id: int | None,
                      error: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE ultoim_predictions SET delivery_status=?, message_id=?, delivery_error=? "
                "WHERE id=?",
                (status, message_id, error, pick_id),
            )
            self._conn.commit()

    # -- settlement grading ---------------------------------------------------
    def unresolved_closed(self, model_version: str, now: float) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT ticker FROM ultoim_predictions "
                "WHERE model_version=? AND official_result IS NULL "
                "AND close_time IS NOT NULL AND close_time <= ? LIMIT 500",
                (model_version, now),
            ).fetchall()
        return [str(r["ticker"]) for r in rows]

    def resolve(self, model_version: str, ticker: str, official_result: str,
                now: float | None = None) -> int:
        """Grade every ungraded pick for a settled ticker. Idempotent (only acts
        on rows with official_result IS NULL). Returns rows graded."""
        official = str(official_result).upper()
        if official not in ("YES", "NO"):
            return 0
        ts = time.time() if now is None else now
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, predicted_side, original_predicted_side, flip_decision, "
                "entry_ask_cents FROM ultoim_predictions "
                "WHERE model_version=? AND ticker=? AND official_result IS NULL",
                (model_version, ticker),
            ).fetchall()
            graded = 0
            for r in rows:
                side = str(r["predicted_side"]).upper()
                correct = 1 if side == official else 0
                ask = r["entry_ask_cents"]
                pnl = None
                if ask is not None:
                    pnl = (100.0 - float(ask)) if correct else (-float(ask))
                original = str(r["original_predicted_side"] or side).upper()
                flip_occurred = 1 if original != official else 0
                flip_call = str(r["flip_decision"] or "NO").upper()
                flip_call_correct = 1 if ((flip_call == "YES") == bool(flip_occurred)) else 0
                self._conn.execute(
                    "UPDATE ultoim_predictions SET official_result=?, resolved_at=?, "
                    "correct=?, hypothetical_pnl_cents=?, flip_occurred=?, flip_call_correct=? "
                    "WHERE id=?",
                    (official, ts, correct, pnl, flip_occurred, flip_call_correct, r["id"]),
                )
                graded += 1
            self._conn.commit()
        return graded

    # -- scoreboard (visible = delivered only) --------------------------------
    def scoreboard(self, model_version: str, *, delivered_only: bool = True) -> dict[str, Any]:
        where = "model_version=? AND official_result IS NOT NULL"
        params: list[Any] = [model_version]
        if delivered_only:
            where += " AND delivery_status='SENT'"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM ultoim_predictions WHERE {where}", params
            ).fetchall()
            counts = dict(self._conn.execute(
                "SELECT delivery_status, COUNT(*) FROM ultoim_predictions "
                "WHERE model_version=? GROUP BY delivery_status", (model_version,),
            ).fetchall())
            total_recorded = self._conn.execute(
                "SELECT COUNT(*) FROM ultoim_predictions WHERE model_version=?",
                (model_version,),
            ).fetchone()[0]
        out: dict[str, Any] = {
            "available": True,
            "model_version": model_version,
            "reset_at": self.reset_at(),
            "resolved": len(rows),
            "total_recorded": int(total_recorded),
            "delivery_counts": {str(k): int(v) for k, v in counts.items()},
        }
        out["overall"] = self._agg(rows)
        out["by_interval"] = {iv: self._agg([r for r in rows if r["interval"] == iv])
                              for iv in ("12M", "10M", "7M")}
        out["by_rank"] = {str(k): self._agg([r for r in rows if r["rank"] == k])
                          for k in (1, 2, 3)}
        out["by_grade"] = {g: self._agg([r for r in rows if r["confidence_grade"] == g])
                           for g in ("A", "B", "C")}
        out["by_side"] = {sd: self._agg([r for r in rows if str(r["predicted_side"]).upper() == sd])
                          for sd in ("YES", "NO")}
        out["by_asset"] = {}
        for asset in sorted({str(r["asset"]) for r in rows}):
            out["by_asset"][asset] = self._agg([r for r in rows if r["asset"] == asset])
        out["flip"] = self._flip_agg(rows)
        return out

    @staticmethod
    def _agg(rows: Sequence[sqlite3.Row]) -> dict[str, Any]:
        n = len(rows)
        right = sum(1 for r in rows if r["correct"] == 1)
        pnl_rows = [float(r["hypothetical_pnl_cents"]) for r in rows
                    if r["hypothetical_pnl_cents"] is not None]
        p, lo, hi = _wilson(right, n)
        return {
            "n": n, "right": right, "wrong": n - right,
            "accuracy": p, "ci_low": lo, "ci_high": hi,
            "low_n": n < 30,
            "pnl_n": len(pnl_rows),
            "pnl_total_cents": round(sum(pnl_rows), 2) if pnl_rows else 0.0,
            "pnl_avg_cents": round(sum(pnl_rows) / len(pnl_rows), 3) if pnl_rows else None,
        }

    @staticmethod
    def _flip_agg(rows: Sequence[sqlite3.Row]) -> dict[str, Any]:
        graded = [r for r in rows if r["flip_call_correct"] is not None]
        n = len(graded)
        right = sum(1 for r in graded if r["flip_call_correct"] == 1)
        occurred = sum(1 for r in graded if r["flip_occurred"] == 1)
        called_yes = [r for r in graded if str(r["flip_decision"]).upper() == "YES"]
        called_no = [r for r in graded if str(r["flip_decision"]).upper() == "NO"]
        p, lo, hi = _wilson(right, n)
        return {
            "n": n, "call_accuracy": p, "ci_low": lo, "ci_high": hi, "low_n": n < 30,
            "actual_flips": occurred,
            "flip_yes_calls": {"n": len(called_yes),
                               "correct": sum(1 for r in called_yes if r["flip_call_correct"] == 1)},
            "flip_no_calls": {"n": len(called_no),
                              "correct": sum(1 for r in called_no if r["flip_call_correct"] == 1)},
            "note": "flip predictor in active research — not validated until it beats "
                    "the champion flip_risk_score out-of-sample",
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
