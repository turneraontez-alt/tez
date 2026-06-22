"""PolymarketLedger — immutable persistence + scoring for the up/down shadow.

Records OUR model's up/down call plus Polymarket's executable book BEFORE the
window closes, then grades both OUR model and the MARKET against Polymarket's own
resolution after settlement. Writes only to its own SQLite file; one row per
(model_version, condition_id, checkpoint), immutable until resolved.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any

WINDOW_SECONDS: float = 900.0
REPORT_CHECKPOINTS: tuple[str, ...] = ("15M", "10M", "7M")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS polymarket_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    model_version TEXT NOT NULL,
    asset TEXT,
    checkpoint TEXT,
    close_time REAL,
    window_id INTEGER,
    condition_id TEXT,
    yes_token_id TEXT,
    spot REAL,
    open_price REAL,
    seconds_remaining REAL,
    model_prob_up REAL,
    market_prob_up REAL,
    poly_yes_bid REAL, poly_yes_ask REAL, poly_no_bid REAL, poly_no_ask REAL,
    poly_spread REAL,
    poly_depth REAL,
    poly_last_trade REAL,
    model_side TEXT,
    market_side TEXT,
    official_result TEXT,
    resolved_at REAL,
    model_correct INTEGER,
    market_correct INTEGER,
    snapshot_id TEXT,
    UNIQUE(model_version, condition_id, checkpoint)
);
CREATE INDEX IF NOT EXISTS idx_poly_resolved ON polymarket_predictions(checkpoint, official_result);
CREATE TABLE IF NOT EXISTS polymarket_meta (
    model_version TEXT PRIMARY KEY,
    reset_at REAL NOT NULL
);
"""


def window_id(close: Any) -> int | None:
    """The 15-minute window index a close timestamp belongs to (nearest boundary,
    matching the challenger ledger so per-cycle close jitter collapses to one
    window). None for a missing/invalid close."""
    if close is None:
        return None
    try:
        return int(round(float(close) / WINDOW_SECONDS))
    except (TypeError, ValueError):
        return None


def _side(prob_up: float | None) -> str | None:
    """UP if P(up) >= 0.5 else DOWN; None if no probability."""
    if prob_up is None:
        return None
    return "UP" if float(prob_up) >= 0.5 else "DOWN"


class PolymarketLedger:
    def __init__(self, db_path: str):
        self.db_path = db_path
        # check_same_thread=False: the runner does all reads/writes on its single
        # background worker thread (not the thread that built the runner). A lock
        # serializes writes defensively even though only that one worker uses it.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- reset marker ----
    def reset_marker(self, model_version: str, configured_reset_at: float = 0.0) -> float:
        row = self._conn.execute(
            "SELECT reset_at FROM polymarket_meta WHERE model_version=?", (model_version,)
        ).fetchone()
        if row is not None:
            return float(row["reset_at"])
        reset_at = float(configured_reset_at) if configured_reset_at else time.time()
        self._conn.execute(
            "INSERT OR IGNORE INTO polymarket_meta (model_version, reset_at) VALUES (?,?)",
            (model_version, reset_at),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT reset_at FROM polymarket_meta WHERE model_version=?", (model_version,)
        ).fetchone()
        return float(row["reset_at"]) if row else reset_at

    def archived_versions(self, current_model_version: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT model_version FROM polymarket_predictions WHERE model_version<>? "
            "ORDER BY model_version", (current_model_version,),
        ).fetchall()
        return [str(r["model_version"]) for r in rows]

    # ---- write path (immutable pre-settlement) ----
    def record(
        self, *, model_version: str, asset: str | None, checkpoint: str | None,
        close_time: float | None, condition_id: str | None, yes_token_id: str | None,
        spot: float | None, open_price: float | None, seconds_remaining: float | None,
        model_prob_up: float | None, market_prob_up: float | None,
        poly_yes_bid: float | None = None, poly_yes_ask: float | None = None,
        poly_no_bid: float | None = None, poly_no_ask: float | None = None,
        poly_spread: float | None = None, poly_depth: float | None = None,
        poly_last_trade: float | None = None, created_at: float | None = None,
        snapshot_id: str | None = None,
    ) -> int | None:
        """Insert one immutable pre-settlement row. Idempotent on
        (model_version, condition_id, checkpoint): a duplicate keeps the first."""
        if not condition_id:
            return None  # no matched Polymarket contract -> nothing to store
        row = (
            created_at if created_at is not None else time.time(),
            model_version, asset, checkpoint, close_time, window_id(close_time),
            condition_id, yes_token_id, spot, open_price, seconds_remaining,
            model_prob_up, market_prob_up,
            poly_yes_bid, poly_yes_ask, poly_no_bid, poly_no_ask,
            poly_spread, poly_depth, poly_last_trade,
            _side(model_prob_up), _side(market_prob_up), snapshot_id,
        )
        try:
            with self._lock:
                cur = self._conn.execute(
                    """INSERT INTO polymarket_predictions
                       (created_at, model_version, asset, checkpoint, close_time, window_id,
                        condition_id, yes_token_id, spot, open_price, seconds_remaining,
                        model_prob_up, market_prob_up,
                        poly_yes_bid, poly_yes_ask, poly_no_bid, poly_no_ask,
                        poly_spread, poly_depth, poly_last_trade,
                        model_side, market_side, snapshot_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    row,
                )
                self._conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    # ---- settlement ----
    def resolve(self, condition_id: str, official_result: str,
                settled_at: float | None = None, model_version: str = "polymarket-v1") -> int:
        """Grade EVERY unresolved checkpoint row for a settled Polymarket contract.

        ``official_result`` is "UP" or "DOWN" (Polymarket's own resolution). Both
        OUR model and the market are graded by whether their stored side matched.
        Returns the number of rows newly resolved (0 if none / bad result)."""
        official = str(official_result).upper()
        if official not in {"UP", "DOWN"}:
            return 0
        ts = settled_at if settled_at is not None else time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, model_side, market_side FROM polymarket_predictions "
                "WHERE model_version=? AND condition_id=? AND official_result IS NULL",
                (model_version, str(condition_id)),
            ).fetchall()
            n = 0
            for r in rows:
                model_correct = None if r["model_side"] is None else int(r["model_side"] == official)
                market_correct = None if r["market_side"] is None else int(r["market_side"] == official)
                self._conn.execute(
                    "UPDATE polymarket_predictions SET official_result=?, resolved_at=?, "
                    "model_correct=?, market_correct=? WHERE id=?",
                    (official, ts, model_correct, market_correct, r["id"]),
                )
                n += 1
            if n:
                self._conn.commit()
        return n

    def unresolved_closed(self, model_version: str, now: float | None = None) -> list[dict]:
        """Distinct (condition_id, asset, close_time) for rows whose window has
        closed but which are still ungraded — the reconcile pass queries these."""
        cutoff = now if now is not None else time.time()
        rows = self._conn.execute(
            "SELECT DISTINCT condition_id, asset, close_time FROM polymarket_predictions "
            "WHERE model_version=? AND official_result IS NULL AND close_time IS NOT NULL "
            "AND close_time <= ?",
            (model_version, float(cutoff)),
        ).fetchall()
        return [{"condition_id": r["condition_id"], "asset": r["asset"],
                 "close_time": r["close_time"]} for r in rows]

    # ---- read / scoring ----
    def _resolved_rows(self, model_version: str) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM polymarket_predictions WHERE model_version=? "
            "AND official_result IS NOT NULL", (model_version,)))

    def accuracy_by_checkpoint(self, model_version: str = "polymarket-v1") -> dict[str, Any]:
        """Per-checkpoint model-vs-market correctness (resolved rows only) plus an
        overall total. Each checkpoint -> {model:{c,w,acc}, market:{c,w,acc}}."""
        def _blank():
            return {"model": {"correct": 0, "wrong": 0},
                    "market": {"correct": 0, "wrong": 0}}

        by_cp = {cp: _blank() for cp in REPORT_CHECKPOINTS}
        n = 0
        for r in self._resolved_rows(model_version):
            cp = str(r["checkpoint"])
            if cp not in by_cp:
                continue
            n += 1
            if r["model_correct"] is not None:
                by_cp[cp]["model"]["correct" if r["model_correct"] else "wrong"] += 1
            if r["market_correct"] is not None:
                by_cp[cp]["market"]["correct" if r["market_correct"] else "wrong"] += 1

        def _acc(d):
            tot = d["correct"] + d["wrong"]
            return {"correct": d["correct"], "wrong": d["wrong"],
                    "accuracy": round(d["correct"] / tot, 4) if tot else None}

        out = {"n": n, "by_checkpoint": {}}
        for cp in REPORT_CHECKPOINTS:
            out["by_checkpoint"][cp] = {"model": _acc(by_cp[cp]["model"]),
                                        "market": _acc(by_cp[cp]["market"])}
        return out

    def latest_window(self, model_version: str = "polymarket-v1") -> dict[str, Any]:
        """The most recent window that has ANY rows (resolved or not), per
        checkpoint: OUR side + prob, the market-implied side + prob, the executable
        ask (cents) for our predicted side, and the result if settled. Drives the
        compact panel."""
        rows = list(self._conn.execute(
            "SELECT * FROM polymarket_predictions WHERE model_version=? "
            "AND window_id IS NOT NULL", (model_version,)))
        if not rows:
            return {"window_id": None, "close_time": None, "checkpoints": {}}
        latest = max(int(r["window_id"]) for r in rows)
        out: dict[str, Any] = {"window_id": latest,
                               "close_time": float(latest) * WINDOW_SECONDS,
                               "checkpoints": {}}
        for r in rows:
            if int(r["window_id"]) != latest:
                continue
            cp = str(r["checkpoint"])
            # Executable ask (cents) for OUR predicted side: UP -> yes ask, DOWN -> no ask.
            ask = r["poly_yes_ask"] if r["model_side"] == "UP" else r["poly_no_ask"]
            out["checkpoints"][cp] = {
                "asset": r["asset"],
                "model_side": r["model_side"],
                "model_prob_up": r["model_prob_up"],
                "market_side": r["market_side"],
                "market_prob_up": r["market_prob_up"],
                "our_side_ask_cents": ask,
                "official_result": r["official_result"],
            }
        return out
