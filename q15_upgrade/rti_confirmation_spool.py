"""Small durable handoff between exact RTI capture and the large strategy ledger."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping


_SCHEMA = """
CREATE TABLE IF NOT EXISTS rti_confirmation_spool (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    ticker TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    interval TEXT NOT NULL,
    close_time REAL NOT NULL,
    target_at REAL NOT NULL,
    release_at REAL NOT NULL,
    source_json TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    last_error TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rti_confirmation_spool_ready
    ON rti_confirmation_spool(next_attempt_at, release_at, id);
CREATE INDEX IF NOT EXISTS idx_rti_confirmation_spool_market
    ON rti_confirmation_spool(ticker, close_time, interval);
"""


def _canonical_source(source: Mapping[str, Any]) -> tuple[str, str]:
    encoded = json.dumps(
        dict(source), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    )
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class RTIConfirmationSpool:
    """Hash-verified, idempotent, outcome-free delayed-source queue."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last_error: str | None = None
        self._completed = 0
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, timeout=0.25,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=250")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def enqueue(
        self,
        *,
        dedupe_key: str,
        ticker: str,
        policy_id: str,
        interval: str,
        close_time: float,
        target_at: float,
        release_at: float,
        source: Mapping[str, Any],
        now: float,
    ) -> bool:
        encoded, digest = _canonical_source(source)
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO rti_confirmation_spool "
                "(dedupe_key,ticker,policy_id,interval,close_time,target_at,"
                "release_at,source_json,source_sha256,next_attempt_at,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(dedupe_key), str(ticker), str(policy_id), str(interval),
                    float(close_time), float(target_at), float(release_at),
                    encoded, digest, float(release_at), float(now),
                ),
            )
            if cur.rowcount == 0:
                row = self._conn.execute(
                    "SELECT source_sha256 FROM rti_confirmation_spool "
                    "WHERE dedupe_key=?", (str(dedupe_key),),
                ).fetchone()
                if row is None or str(row["source_sha256"]) != digest:
                    self._last_error = "confirmation_spool_identity_mismatch"
                    raise ValueError(self._last_error)
            self._conn.commit()
            return cur.rowcount > 0

    def next_ready(self, *, now: float) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM rti_confirmation_spool "
                "WHERE release_at<=? AND next_attempt_at<=? "
                "ORDER BY release_at,id LIMIT 1",
                (float(now), float(now)),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        encoded = str(item.get("source_json") or "")
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        if digest != str(item.get("source_sha256") or ""):
            self._last_error = "confirmation_spool_source_hash_mismatch"
            raise ValueError(self._last_error)
        value = json.loads(encoded)
        if not isinstance(value, Mapping):
            self._last_error = "confirmation_spool_source_not_object"
            raise ValueError(self._last_error)
        item["source"] = dict(value)
        return item

    def mark_completed(self, row_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM rti_confirmation_spool WHERE id=?", (int(row_id),)
            )
            self._conn.commit()
            self._completed += 1
            self._last_error = None

    def mark_failure(self, row_id: int, error: str, *, now: float) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT attempts FROM rti_confirmation_spool WHERE id=?",
                (int(row_id),),
            ).fetchone()
            attempts = 1 + int(row["attempts"] if row is not None else 0)
            delay = min(5.0, 0.25 * (2 ** min(attempts - 1, 5)))
            self._conn.execute(
                "UPDATE rti_confirmation_spool SET attempts=?,next_attempt_at=?,"
                "last_error=? WHERE id=?",
                (attempts, float(now) + delay, str(error)[:500], int(row_id)),
            )
            self._conn.commit()
            self._last_error = str(error)[:500]

    def pending_intervals(self, *, ticker: str, close_time: float) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT interval FROM rti_confirmation_spool "
                "WHERE ticker=? AND close_time=?",
                (str(ticker), float(close_time)),
            ).fetchall()
        return {str(row["interval"]).upper() for row in rows}

    def status(self) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS pending, MIN(release_at) AS next_release_at, "
                "MAX(attempts) AS max_attempts FROM rti_confirmation_spool"
            ).fetchone()
            journal = str(self._conn.execute("PRAGMA journal_mode").fetchone()[0])
            busy_timeout = int(
                self._conn.execute("PRAGMA busy_timeout").fetchone()[0]
            )
            return {
                "path": self.path,
                "pending": int(row["pending"] or 0),
                "next_release_at": row["next_release_at"],
                "maximum_attempts": int(row["max_attempts"] or 0),
                "completed_this_process": self._completed,
                "journal_mode": journal,
                "busy_timeout_ms": busy_timeout,
                "last_error": self._last_error,
                "outcome_fields_present": False,
            }

    def close(self) -> None:
        with self._lock:
            self._conn.close()

