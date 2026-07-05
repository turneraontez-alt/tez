"""Interval-timing research — durable capture store (read-only research).

One SQLite table, ``interval_captures``, with a row per (model_version, ticker,
interval). A row is EITHER a real capture (``predicted_side`` set, all economics
fields populated) OR a missing-row (``predicted_side`` NULL, ``missing_reason``
set with one of config.REASON_CODES). Prediction-quality fields and trade-quality
fields are stored side by side but never collapsed into one score.

Idempotent and restart-safe: the UNIQUE(model_version, ticker, interval) key plus
INSERT OR IGNORE means re-recording the same (ticker, interval) — across the ~1s
loop or across a process restart — never duplicates or overwrites.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping, Sequence

_SCHEMA = """
CREATE TABLE IF NOT EXISTS interval_captures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_version TEXT NOT NULL,
    ticker TEXT NOT NULL,
    asset TEXT,
    interval TEXT NOT NULL,
    role TEXT,
    mark_seconds INTEGER,
    window_key INTEGER,
    captured_at REAL,
    seconds_remaining REAL,
    close_time REAL,
    -- prediction quality
    prediction_available INTEGER DEFAULT 0,
    predicted_side TEXT,
    raw_yes_probability REAL,
    calibrated_yes_probability REAL,
    conservative_probability REAL,
    flip_probability REAL,
    manipulation_score REAL,
    manipulation_suspected INTEGER DEFAULT 0,
    distance_from_strike REAL,
    prediction_stability REAL,
    data_quality REAL,
    data_quality_status TEXT,
    -- trade quality / executable economics
    yes_bid_cents REAL,
    yes_ask_cents REAL,
    spread_cents REAL,
    depth_contracts REAL,
    entry_ask_cents REAL,
    net_edge_cents REAL,
    fee_cents REAL,
    slippage_cents REAL,
    total_cost_cents REAL,
    trade_decision TEXT,
    entry_recommended INTEGER DEFAULT 0,
    entry_reject_reason TEXT,
    -- exact Kalshi settlement-index context (CF Benchmarks RTI), nullable when unavailable
    index_px REAL,
    basis_cents REAL,
    index_age_s REAL,
    -- missing-capture accounting
    missing_reason TEXT,
    -- settlement
    official_result TEXT,
    resolved_at REAL,
    correct INTEGER,
    realized_pnl_cents REAL,
    UNIQUE(model_version, ticker, interval)
);
CREATE INDEX IF NOT EXISTS idx_ir_resolved ON interval_captures(interval, official_result);
CREATE INDEX IF NOT EXISTS idx_ir_ticker ON interval_captures(ticker);
"""

_CAPTURE_COLUMNS = (
    "model_version", "ticker", "asset", "interval", "role", "mark_seconds", "window_key",
    "captured_at", "seconds_remaining", "close_time",
    "prediction_available", "predicted_side", "raw_yes_probability",
    "calibrated_yes_probability", "conservative_probability", "flip_probability",
    "manipulation_score", "manipulation_suspected", "distance_from_strike",
    "prediction_stability", "data_quality", "data_quality_status",
    "yes_bid_cents", "yes_ask_cents", "spread_cents", "depth_contracts",
    "entry_ask_cents", "net_edge_cents", "fee_cents", "slippage_cents",
    "total_cost_cents", "trade_decision", "entry_recommended", "entry_reject_reason",
    "index_px", "basis_cents", "index_age_s",
    "missing_reason",
)


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        f = float(value)
        return f
    except (TypeError, ValueError):
        return None


class IntervalResearchLedger:
    def __init__(self, db_path: str):
        self.path = Path(db_path)
        self._lock = threading.Lock()
        self._available = False
        self._last_error: str | None = None
        try:
            if self.path.parent and str(self.path.parent) not in ("", "."):
                os.makedirs(self.path.parent, exist_ok=True)
            with closing(self._connect()) as conn:
                conn.executescript(_SCHEMA)
                self._migrate(conn)
                conn.commit()
            self._available = True
        except sqlite3.Error as exc:  # durable but never fatal to the live loop
            self._last_error = f"interval-research ledger init failed: {exc}"

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        existing = {str(row["name"]) for row in conn.execute("PRAGMA table_info(interval_captures)")}
        for name, decl in (
            ("index_px", "REAL"),
            ("basis_cents", "REAL"),
            ("index_age_s", "REAL"),
        ):
            if name not in existing:
                conn.execute(f"ALTER TABLE interval_captures ADD COLUMN {name} {decl}")

    @property
    def available(self) -> bool:
        return self._available

    def has_capture(self, model_version: str, ticker: str, interval: str) -> bool:
        if not self._available:
            return False
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM interval_captures WHERE model_version=? AND ticker=? AND interval=? "
                "AND predicted_side IS NOT NULL LIMIT 1",
                (model_version, str(ticker), interval),
            ).fetchone()
        return row is not None

    def has_row(self, model_version: str, ticker: str, interval: str) -> bool:
        """Any row (capture OR missing) for this (ticker, interval) — used to dedup
        the in-band cycles cheaply before building a capture."""
        if not self._available:
            return False
        with self._lock, closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM interval_captures WHERE model_version=? AND ticker=? AND interval=? LIMIT 1",
                (model_version, str(ticker), interval),
            ).fetchone()
        return row is not None

    def captures_for_window(
        self, model_version: str, interval: str, window_key: int
    ) -> list[dict[str, Any]]:
        """All scored captures for one (interval, window) — the cross-asset slate
        the top-pick ranker compares. Read-only."""
        if not self._available:
            return []
        with self._lock, closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT ticker, asset, close_time, predicted_side, "
                "calibrated_yes_probability, yes_ask_cents, entry_ask_cents, "
                "spread_cents, captured_at, seconds_remaining "
                "FROM interval_captures WHERE model_version=? AND interval=? "
                "AND window_key=? AND predicted_side IS NOT NULL",
                (model_version, interval, int(window_key)),
            ).fetchall()
        return [dict(r) for r in rows]

    def record_capture(self, row: Mapping[str, Any]) -> bool:
        """Insert one captured interval. Returns True only if a NEW row was created
        (INSERT OR IGNORE on the unique key — restart/loop safe)."""
        if not self._available:
            return False
        values = [row.get(col) for col in _CAPTURE_COLUMNS]
        placeholders = ",".join("?" for _ in _CAPTURE_COLUMNS)
        cols = ",".join(_CAPTURE_COLUMNS)
        with self._lock, closing(self._connect()) as conn:
            cur = conn.execute(
                f"INSERT OR IGNORE INTO interval_captures ({cols}) VALUES ({placeholders})",
                values,
            )
            conn.commit()
            return cur.rowcount > 0

    def record_missing(self, *, model_version: str, ticker: str, asset: str | None,
                       interval: str, reason: str, window_key: int | None = None,
                       captured_at: float | None = None) -> bool:
        """Record exactly one missing-reason row for (ticker, interval). No-op if a
        row (capture or missing) already exists for that pair."""
        if not self._available or not ticker:
            return False
        with self._lock, closing(self._connect()) as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO interval_captures "
                "(model_version, ticker, asset, interval, role, window_key, captured_at, "
                "prediction_available, missing_reason) VALUES (?,?,?,?,?,?,?,0,?)",
                (model_version, str(ticker), asset, interval,
                 None, window_key, captured_at, reason),
            )
            conn.commit()
            return cur.rowcount > 0

    def resolve(self, model_version: str, ticker: str, official_result: str,
                resolved_at: float) -> int:
        """Attach the settled outcome to every captured row for this contract.

        ``correct`` = predicted_side matched the result; realized P&L is the
        executable-price payoff of entering the predicted side at ``entry_ask_cents``
        (win => 100 - ask, loss => -ask). Missing rows (no predicted_side) get the
        result stamped but no correctness/P&L. Read-only wrt the live system."""
        official_result = str(official_result or "").upper()
        if not self._available or official_result not in {"YES", "NO"} or not ticker:
            return 0
        with self._lock, closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, predicted_side, entry_ask_cents FROM interval_captures "
                "WHERE model_version=? AND ticker=? AND official_result IS NULL",
                (model_version, str(ticker)),
            ).fetchall()
            n = 0
            for r in rows:
                side = str(r["predicted_side"] or "").upper()
                correct = None
                pnl = None
                if side in {"YES", "NO"}:
                    correct = 1 if side == official_result else 0
                    ask = _num(r["entry_ask_cents"])
                    if ask is not None:
                        pnl = round((100.0 - ask) if correct else -ask, 4)
                conn.execute(
                    "UPDATE interval_captures SET official_result=?, resolved_at=?, correct=?, "
                    "realized_pnl_cents=? WHERE id=?",
                    (official_result, resolved_at, correct, pnl, r["id"]),
                )
                n += 1
            conn.commit()
            return n

    def resolved_rows(self, interval: str | None = None) -> list[dict[str, Any]]:
        """All settled rows (optionally one interval) as plain dicts, for analysis."""
        if not self._available:
            return []
        q = "SELECT * FROM interval_captures WHERE official_result IS NOT NULL"
        args: list[Any] = []
        if interval is not None:
            q += " AND interval=?"
            args.append(interval)
        with self._lock, closing(self._connect()) as conn:
            return [dict(r) for r in conn.execute(q, args).fetchall()]

    def counts(self) -> dict[str, Any]:
        """Row counts by interval and a captured-vs-missing split, for the report."""
        if not self._available:
            return {"available": False}
        with self._lock, closing(self._connect()) as conn:
            by_interval = {
                r["interval"]: r["n"]
                for r in conn.execute(
                    "SELECT interval, COUNT(*) n FROM interval_captures GROUP BY interval"
                ).fetchall()
            }
            captured = conn.execute(
                "SELECT COUNT(*) FROM interval_captures WHERE predicted_side IS NOT NULL"
            ).fetchone()[0]
            missing = conn.execute(
                "SELECT COUNT(*) FROM interval_captures WHERE predicted_side IS NULL"
            ).fetchone()[0]
            by_reason = {
                r["missing_reason"]: r["n"]
                for r in conn.execute(
                    "SELECT missing_reason, COUNT(*) n FROM interval_captures "
                    "WHERE missing_reason IS NOT NULL GROUP BY missing_reason"
                ).fetchall()
            }
        return {"available": True, "by_interval": by_interval,
                "captured": captured, "missing": missing, "by_missing_reason": by_reason}
