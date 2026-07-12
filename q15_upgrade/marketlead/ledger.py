"""Durable, separate evidence ledger for Q15 MarketLead."""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Mapping


_SCHEMA = """
CREATE TABLE IF NOT EXISTS marketlead_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    system_version TEXT NOT NULL,
    asset TEXT NOT NULL,
    ticker TEXT NOT NULL,
    window_key INTEGER NOT NULL,
    mark_seconds INTEGER NOT NULL,
    observed_at REAL NOT NULL,
    close_time REAL,
    seconds_remaining REAL,
    predicted_side TEXT,
    entry_ask_cents REAL,
    spread_cents REAL,
    spot_price REAL,
    strike_price REAL,
    official_index_price REAL,
    rti_proxy_price REAL,
    proxy_gap_bps REAL,
    proxy_lead_side_bps REAL,
    venue_source_count INTEGER,
    venue_dispersion_bps REAL,
    venue_impulse REAL,
    venue_impulse_side REAL,
    venue_aligned_fraction REAL,
    venue_leader TEXT,
    venue_leader_persistence REAL,
    kalshi_available INTEGER,
    kalshi_book_age_seconds REAL,
    kalshi_event_age_seconds REAL,
    kalshi_yes_bid_cents REAL,
    kalshi_yes_ask_cents REAL,
    kalshi_microprice_yes_cents REAL,
    kalshi_microprice_edge_yes_cents REAL,
    kalshi_pressure_yes_5s REAL,
    kalshi_pressure_yes_15s REAL,
    kalshi_pressure_yes_30s REAL,
    kalshi_trade_imbalance_yes_15s REAL,
    kalshi_pressure_side REAL,
    joint_alignment INTEGER NOT NULL DEFAULT 0,
    evidence_status TEXT NOT NULL,
    missing_reasons_json TEXT NOT NULL,
    features_json TEXT NOT NULL,
    build_sha TEXT,
    config_hash TEXT,
    feature_schema_version TEXT,
    official_result TEXT,
    resolved_at REAL,
    correct INTEGER,
    realized_pnl_cents REAL,
    UNIQUE(system_version, ticker, mark_seconds)
);
CREATE INDEX IF NOT EXISTS idx_marketlead_window
ON marketlead_observations(system_version, window_key);
CREATE INDEX IF NOT EXISTS idx_marketlead_resolved
ON marketlead_observations(system_version, official_result);
"""

_COLUMNS = (
    "system_version", "asset", "ticker", "window_key", "mark_seconds",
    "observed_at", "close_time", "seconds_remaining", "predicted_side",
    "entry_ask_cents", "spread_cents", "spot_price", "strike_price",
    "official_index_price", "rti_proxy_price", "proxy_gap_bps",
    "proxy_lead_side_bps", "venue_source_count", "venue_dispersion_bps",
    "venue_impulse", "venue_impulse_side", "venue_aligned_fraction",
    "venue_leader", "venue_leader_persistence", "kalshi_available",
    "kalshi_book_age_seconds", "kalshi_event_age_seconds",
    "kalshi_yes_bid_cents", "kalshi_yes_ask_cents",
    "kalshi_microprice_yes_cents", "kalshi_microprice_edge_yes_cents",
    "kalshi_pressure_yes_5s", "kalshi_pressure_yes_15s",
    "kalshi_pressure_yes_30s", "kalshi_trade_imbalance_yes_15s",
    "kalshi_pressure_side", "joint_alignment", "evidence_status",
    "missing_reasons_json", "features_json", "build_sha", "config_hash",
    "feature_schema_version",
)


class MarketLeadLedger:
    def __init__(self, db_path: str):
        self.path = Path(db_path)
        self._lock = threading.Lock()
        self._available = False
        self._last_error: str | None = None
        try:
            if self.path.parent:
                os.makedirs(self.path.parent, exist_ok=True)
            with self._connect() as connection:
                connection.executescript(_SCHEMA)
                connection.commit()
            self._available = True
        except sqlite3.Error as exc:
            self._last_error = f"marketlead ledger init failed: {exc}"

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=3.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=3000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @property
    def available(self) -> bool:
        return self._available

    def record(self, row: Mapping[str, Any]) -> bool:
        if not self._available:
            return False
        placeholders = ",".join("?" for _ in _COLUMNS)
        sql = (
            f"INSERT OR IGNORE INTO marketlead_observations ({','.join(_COLUMNS)}) "
            f"VALUES ({placeholders})"
        )
        try:
            with self._lock, self._connect() as connection:
                cursor = connection.execute(sql, tuple(row.get(column) for column in _COLUMNS))
                connection.commit()
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            self._last_error = f"marketlead record failed: {exc}"
            return False

    def resolve(
        self, system_version: str, ticker: str, official_result: str, resolved_at: float
    ) -> int:
        result = str(official_result or "").upper()
        if not self._available or result not in {"YES", "NO"}:
            return 0
        try:
            with self._lock, self._connect() as connection:
                rows = connection.execute(
                    "SELECT id,predicted_side,entry_ask_cents FROM marketlead_observations "
                    "WHERE system_version=? AND ticker=? AND official_result IS NULL",
                    (system_version, ticker),
                ).fetchall()
                for row in rows:
                    side = str(row["predicted_side"] or "").upper()
                    ask = row["entry_ask_cents"]
                    correct = None if side not in {"YES", "NO"} else int(side == result)
                    pnl = None
                    if correct is not None and ask is not None:
                        pnl = 100.0 - float(ask) if correct else -float(ask)
                    connection.execute(
                        "UPDATE marketlead_observations SET official_result=?,resolved_at=?,"
                        "correct=?,realized_pnl_cents=? WHERE id=?",
                        (result, resolved_at, correct, pnl, row["id"]),
                    )
                connection.commit()
                return len(rows)
        except sqlite3.Error as exc:
            self._last_error = f"marketlead resolve failed: {exc}"
            return 0

    def rows(self) -> list[dict[str, Any]]:
        if not self._available:
            return []
        with self._lock, self._connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM marketlead_observations ORDER BY observed_at,id"
            )]

    def status(self) -> dict[str, Any]:
        if not self._available:
            return {"available": False, "last_error": self._last_error}
        try:
            with self._lock, self._connect() as connection:
                row = connection.execute(
                    "SELECT COUNT(*) n,SUM(official_result IS NOT NULL) resolved,"
                    "SUM(evidence_status='READY') ready,SUM(joint_alignment=1) aligned "
                    "FROM marketlead_observations"
                ).fetchone()
            return {
                "available": True,
                "observations": int(row["n"] or 0),
                "resolved": int(row["resolved"] or 0),
                "ready": int(row["ready"] or 0),
                "joint_alignment": int(row["aligned"] or 0),
                "last_error": self._last_error,
            }
        except sqlite3.Error as exc:
            return {"available": False, "last_error": str(exc)}


__all__ = ["MarketLeadLedger"]
