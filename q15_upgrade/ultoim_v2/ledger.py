"""Ultoim V2 — isolated persistence, settlement grading, and scoreboard.

Completely separate from the live V9.5 ledger and from Ultoim Build: its own
SQLite file, its own tables, its own ids / counters / model version / reset
marker / session id. Rows are immutable once written (UNIQUE on
``(model_version, ticker, interval)``); settlement only fills the official
result / correctness / P&L and never rewrites a row.

Two locks enforce the V2 dedup contract:
  * ``ultoim_v2_report_lock`` — one RECORD per (interval, window): the first fire
    in a (interval, window) claims the lock and writes the chosen row.
  * ``ultoim_v2_alert_lock`` — one ALERT per (ticker, window): a contract may be
    recorded at several checkpoints in a window, but is alerted at most once.
"""
from __future__ import annotations

import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ultoim_v2_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    model_version TEXT NOT NULL,
    asset TEXT NOT NULL,
    ticker TEXT NOT NULL,
    interval TEXT NOT NULL,
    window_key INTEGER NOT NULL,
    mark_seconds REAL,
    fired INTEGER NOT NULL DEFAULT 0,
    predicted_side TEXT,
    selected_probability REAL,
    calibrated_yes_probability REAL,
    conservative_probability REAL,
    market_implied_yes_probability REAL,
    raw_yes_probability REAL,
    net_edge_cents REAL,
    entry_ask_cents REAL,
    best_entry_cents INTEGER,
    fee_cents REAL,
    total_cost_cents REAL,
    spread_cents REAL,
    depth_contracts REAL,
    quote_age_seconds REAL,
    spot_stale_age_seconds REAL,
    distance_sigma REAL,
    regime_name TEXT,
    regime_directional TEXT,
    data_quality REAL,
    evidence_quality REAL,
    manipulation_suspected INTEGER DEFAULT 0,
    flip_probability REAL,
    order_flow_persistence REAL,
    book_resiliency REAL,
    prediction_stability REAL,
    x_market_flow REAL,
    gate_a_pass INTEGER,
    gate_b_pass INTEGER,
    gate_c_pass INTEGER,
    reason_codes TEXT,
    gate_min_conf REAL,
    gate_ask_lo REAL,
    gate_ask_hi REAL,
    gate_min_edge REAL,
    close_time REAL,
    snapshot_id TEXT,
    session_id TEXT,
    delivery_status TEXT NOT NULL DEFAULT 'PENDING',
    message_id INTEGER,
    delivery_error TEXT,
    alerted INTEGER DEFAULT 0,
    official_result TEXT,
    resolved_at REAL,
    correct INTEGER,
    hypothetical_pnl_cents REAL,
    base_rate_side TEXT,
    UNIQUE(model_version, ticker, interval)
);
CREATE INDEX IF NOT EXISTS idx_ultoim_v2_resolve
    ON ultoim_v2_predictions(model_version, official_result, close_time);

CREATE TABLE IF NOT EXISTS ultoim_v2_report_lock (
    model_version TEXT NOT NULL,
    interval TEXT NOT NULL,
    window_key INTEGER NOT NULL,
    locked_at REAL NOT NULL,
    message_id INTEGER,
    PRIMARY KEY(model_version, interval, window_key)
);

CREATE TABLE IF NOT EXISTS ultoim_v2_alert_lock (
    model_version TEXT NOT NULL,
    ticker TEXT NOT NULL,
    window_key INTEGER NOT NULL,
    locked_at REAL NOT NULL,
    PRIMARY KEY(model_version, ticker, window_key)
);

CREATE TABLE IF NOT EXISTS ultoim_v2_meta (
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


class UltoimV2Ledger:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._ensure_columns()
            self._conn.commit()

    # Additive migration for DBs created before a column existed. CREATE TABLE IF
    # NOT EXISTS never alters an existing table, so new nullable columns are added
    # here (old rows read NULL). Must be called inside ``self._lock``.
    def _ensure_columns(self) -> None:
        existing = {r["name"] for r in
                    self._conn.execute("PRAGMA table_info(ultoim_v2_predictions)")}
        for col, ddl in (("x_market_flow", "x_market_flow REAL"),):
            if col not in existing:
                self._conn.execute(
                    f"ALTER TABLE ultoim_v2_predictions ADD COLUMN {ddl}")

    # -- meta / reset marker + session id ------------------------------------
    def ensure_reset_marker(self, model_version: str, now: float) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM ultoim_v2_meta WHERE key='reset_at'"
            ).fetchone()
            if row is not None:
                # Guarantee a session id even on an already-initialised DB.
                sess = self._conn.execute(
                    "SELECT value FROM ultoim_v2_meta WHERE key='session_id'"
                ).fetchone()
                if sess is None:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO ultoim_v2_meta(key,value) VALUES('session_id',?)",
                        (str(now),),
                    )
                    self._conn.commit()
                return float(row["value"])
            self._conn.execute(
                "INSERT OR REPLACE INTO ultoim_v2_meta(key,value) VALUES('reset_at',?)",
                (str(now),),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO ultoim_v2_meta(key,value) VALUES('model_version',?)",
                (model_version,),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO ultoim_v2_meta(key,value) VALUES('session_id',?)",
                (str(now),),
            )
            self._conn.commit()
            return now

    def reset_at(self) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM ultoim_v2_meta WHERE key='reset_at'"
            ).fetchone()
        return float(row["value"]) if row is not None else None

    def session_id(self) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM ultoim_v2_meta WHERE key='session_id'"
            ).fetchone()
        return str(row["value"]) if row is not None else None

    # -- report lock (one RECORD per interval per window) ---------------------
    def report_locked(self, model_version: str, interval: str, window_key: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM ultoim_v2_report_lock "
                "WHERE model_version=? AND interval=? AND window_key=?",
                (model_version, interval, window_key),
            ).fetchone()
        return row is not None

    def lock_report(self, model_version: str, interval: str, window_key: int, now: float) -> bool:
        """Claim the (interval, window) lock. Returns True iff this call created it
        (idempotent — a duplicate/concurrent fire is rejected)."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO ultoim_v2_report_lock"
                "(model_version,interval,window_key,locked_at) VALUES(?,?,?,?)",
                (model_version, interval, window_key, now),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def set_report_message(self, model_version: str, interval: str, window_key: int,
                           message_id: int | None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE ultoim_v2_report_lock SET message_id=? "
                "WHERE model_version=? AND interval=? AND window_key=?",
                (message_id, model_version, interval, window_key),
            )
            self._conn.commit()

    # -- alert lock (one ALERT per CONTRACT per window, across checkpoints) ----
    def alert_locked(self, model_version: str, ticker: str, window_key: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM ultoim_v2_alert_lock "
                "WHERE model_version=? AND ticker=? AND window_key=?",
                (model_version, ticker, window_key),
            ).fetchone()
        return row is not None

    def claim_alert(self, model_version: str, ticker: str, window_key: int, now: float) -> bool:
        """Claim the (ticker, window) alert lock. Returns True iff this is the FIRST
        claim — i.e. this contract has not yet been alerted in this window."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO ultoim_v2_alert_lock"
                "(model_version,ticker,window_key,locked_at) VALUES(?,?,?,?)",
                (model_version, ticker, window_key, now),
            )
            self._conn.commit()
            return cur.rowcount == 1

    # -- record a decision (immutable) ----------------------------------------
    _COLS = (
        "created_at", "model_version", "asset", "ticker", "interval", "window_key",
        "mark_seconds", "fired", "predicted_side", "selected_probability",
        "calibrated_yes_probability", "conservative_probability",
        "market_implied_yes_probability", "raw_yes_probability", "net_edge_cents",
        "entry_ask_cents", "best_entry_cents", "fee_cents", "total_cost_cents",
        "spread_cents", "depth_contracts", "quote_age_seconds", "spot_stale_age_seconds",
        "distance_sigma", "regime_name", "regime_directional", "data_quality",
        "evidence_quality", "manipulation_suspected", "flip_probability",
        "order_flow_persistence", "book_resiliency", "prediction_stability",
        "x_market_flow",
        "gate_a_pass", "gate_b_pass", "gate_c_pass", "reason_codes",
        "gate_min_conf", "gate_ask_lo", "gate_ask_hi", "gate_min_edge",
        "close_time", "snapshot_id", "session_id", "delivery_status",
    )

    def record_decision(self, row: Mapping[str, Any]) -> int | None:
        placeholders = ",".join("?" for _ in self._COLS)
        values = [row.get(c) for c in self._COLS]
        with self._lock:
            try:
                cur = self._conn.execute(
                    f"INSERT INTO ultoim_v2_predictions({','.join(self._COLS)}) "
                    f"VALUES({placeholders})",
                    values,
                )
                self._conn.commit()
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                # Duplicate (model_version, ticker, interval) — already recorded.
                return None

    def mark_delivery(self, row_id: int, status: str, message_id: int | None,
                      error: str | None = None) -> None:
        alerted = 1 if status == "SENT" else 0
        with self._lock:
            self._conn.execute(
                "UPDATE ultoim_v2_predictions "
                "SET delivery_status=?, message_id=?, delivery_error=?, alerted=? "
                "WHERE id=?",
                (status, message_id, error, alerted, row_id),
            )
            self._conn.commit()

    # -- settlement grading ---------------------------------------------------
    def unresolved_closed(self, model_version: str, now: float) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT ticker FROM ultoim_v2_predictions "
                "WHERE model_version=? AND official_result IS NULL "
                "AND close_time IS NOT NULL AND close_time <= ? LIMIT 500",
                (model_version, now),
            ).fetchall()
        return [str(r["ticker"]) for r in rows]

    def resolve(self, model_version: str, ticker: str, official_result: str,
                now: float | None = None) -> int:
        """Grade every ungraded row for a settled ticker. Idempotent (acts only on
        rows with official_result IS NULL). Returns rows graded."""
        official = str(official_result).upper()
        if official not in ("YES", "NO"):
            return 0
        ts = time.time() if now is None else now
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, predicted_side, entry_ask_cents FROM ultoim_v2_predictions "
                "WHERE model_version=? AND ticker=? AND official_result IS NULL",
                (model_version, ticker),
            ).fetchall()
            graded = 0
            for r in rows:
                side = str(r["predicted_side"] or "").upper()
                correct = 1 if side == official else 0
                ask = r["entry_ask_cents"]
                pnl = None
                if ask is not None:
                    pnl = (100.0 - float(ask)) if correct else (-float(ask))
                self._conn.execute(
                    "UPDATE ultoim_v2_predictions SET official_result=?, resolved_at=?, "
                    "correct=?, hypothetical_pnl_cents=? WHERE id=?",
                    (official, ts, correct, pnl, r["id"]),
                )
                graded += 1
            self._conn.commit()
        return graded

    # -- scoreboard -----------------------------------------------------------
    def scoreboard(self, model_version: str, *, min_n: int = 30) -> dict[str, Any]:
        with self._lock:
            all_rows = self._conn.execute(
                "SELECT * FROM ultoim_v2_predictions WHERE model_version=?",
                (model_version,),
            ).fetchall()
            counts = dict(self._conn.execute(
                "SELECT delivery_status, COUNT(*) FROM ultoim_v2_predictions "
                "WHERE model_version=? GROUP BY delivery_status", (model_version,),
            ).fetchall())
        # Headline 'entries' stats: delivered + fired rows only. all_observations
        # exposes every recorded row (fired + abstains).
        entries = [r for r in all_rows
                   if r["fired"] == 1 and r["official_result"] is not None]
        all_resolved = [r for r in all_rows if r["official_result"] is not None]
        out: dict[str, Any] = {
            "available": True,
            "model_version": model_version,
            "reset_at": self.reset_at(),
            "session_id": self.session_id(),
            "min_n": min_n,
            "resolved": len(entries),
            "total_recorded": len(all_rows),
            "delivery_counts": {str(k): int(v) for k, v in counts.items()},
            "all_observations": {
                "recorded": len(all_rows),
                "resolved": len(all_resolved),
                "fired": sum(1 for r in all_rows if r["fired"] == 1),
            },
        }
        out["overall"] = self._agg(entries, min_n)
        out["overall"].update(self._base_rate(entries))
        out["by_interval"] = {iv: self._agg([r for r in entries if r["interval"] == iv], min_n)
                              for iv in ("15M", "10M", "7M")}
        out["by_regime_directional"] = {
            rd: self._agg([r for r in entries
                           if str(r["regime_directional"] or "") == rd], min_n)
            for rd in ("YES_PRONE", "NO_PRONE", "BALANCED")
        }
        return out

    @staticmethod
    def _agg(rows: Sequence[sqlite3.Row], min_n: int) -> dict[str, Any]:
        n = len(rows)
        right = sum(1 for r in rows if r["correct"] == 1)
        pnl_rows = [float(r["hypothetical_pnl_cents"]) for r in rows
                    if r["hypothetical_pnl_cents"] is not None]
        p, lo, hi = _wilson(right, n)
        return {
            "n": n, "right": right, "wrong": n - right,
            "accuracy": p, "ci_low": lo, "ci_high": hi,
            "low_n": n < min_n,
            "pnl_n": len(pnl_rows),
            "pnl_total_cents": round(sum(pnl_rows), 2) if pnl_rows else 0.0,
            "pnl_avg_cents": round(sum(pnl_rows) / len(pnl_rows), 3) if pnl_rows else None,
        }

    @staticmethod
    def _base_rate(rows: Sequence[sqlite3.Row]) -> dict[str, Any]:
        """Majority-class accuracy (always bet the more common settled side) and the
        model's edge over it. The base_rate_side is the majority settled outcome."""
        n = len(rows)
        if n == 0:
            return {"base_rate": None, "base_rate_side": None, "edge_over_base": None}
        n_no = sum(1 for r in rows if str(r["official_result"] or "").upper() == "NO")
        n_yes = sum(1 for r in rows if str(r["official_result"] or "").upper() == "YES")
        base_rate = max(n_no, n_yes) / n
        base_side = "NO" if n_no >= n_yes else "YES"
        right = sum(1 for r in rows if r["correct"] == 1)
        accuracy = right / n
        return {
            "base_rate": base_rate,
            "base_rate_side": base_side,
            "edge_over_base": accuracy - base_rate,
        }

    def loss_rows(self, model_version: str, limit: int = 20) -> list[dict[str, Any]]:
        """The wrong (correct=0) fired entries with their full feature vector, for
        diagnostics. Most-recently-resolved first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ultoim_v2_predictions "
                "WHERE model_version=? AND fired=1 AND correct=0 "
                "ORDER BY resolved_at DESC LIMIT ?",
                (model_version, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_rows(self, model_version: str, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ultoim_v2_predictions WHERE model_version=? "
                "ORDER BY created_at DESC LIMIT ?",
                (model_version, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
