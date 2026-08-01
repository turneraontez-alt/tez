"""Durable paper-only ledger for prospective path forecasts."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Mapping


_SCHEMA = """
CREATE TABLE IF NOT EXISTS path_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    model_version TEXT NOT NULL,
    feature_schema_version TEXT NOT NULL,
    asset TEXT NOT NULL,
    close_time REAL NOT NULL,
    checkpoint_seconds INTEGER NOT NULL,
    decision_time REAL NOT NULL,
    captured_offset_seconds REAL NOT NULL,
    target_px REAL NOT NULL,
    current_px REAL NOT NULL,
    current_yes_mid REAL,
    top_archetype TEXT NOT NULL,
    top_archetype_probability REAL NOT NULL,
    settlement_yes_probability REAL NOT NULL,
    strike_cross_probability REAL NOT NULL,
    turn_delay_seconds_q10 REAL,
    turn_delay_seconds_q50 REAL,
    turn_delay_seconds_q90 REAL,
    prediction_json TEXT NOT NULL,
    feature_json TEXT NOT NULL,
    paper_only INTEGER NOT NULL DEFAULT 1,
    official_result TEXT,
    resolved_at REAL,
    actual_archetype TEXT,
    actual_strike_crossed INTEGER,
    settlement_correct INTEGER,
    UNIQUE(model_version, asset, close_time, checkpoint_seconds)
);
CREATE INDEX IF NOT EXISTS idx_path_forecasts_close
    ON path_forecasts(close_time, asset, checkpoint_seconds);
CREATE INDEX IF NOT EXISTS idx_path_forecasts_pending
    ON path_forecasts(official_result, close_time);
"""


class PathForecastLedger:
    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, timeout=15.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def record(
        self,
        *,
        created_at: float,
        asset: str,
        close_time: float,
        checkpoint_seconds: int,
        decision_time: float,
        captured_offset_seconds: float,
        target_px: float,
        current_px: float,
        current_yes_mid: float | None,
        prediction: Mapping[str, Any],
        feature_vector: list[float | None],
    ) -> tuple[int, bool]:
        values = (
            float(created_at),
            str(prediction["model_version"]),
            str(prediction["feature_schema_version"]),
            str(asset).upper(),
            float(close_time),
            int(checkpoint_seconds),
            float(decision_time),
            float(captured_offset_seconds),
            float(target_px),
            float(current_px),
            None if current_yes_mid is None else float(current_yes_mid),
            str(prediction["top_archetype"]),
            float(prediction["top_archetype_probability"]),
            float(prediction["settlement_yes_probability"]),
            float(prediction["strike_cross_probability"]),
            prediction.get("turn_delay_seconds_q10"),
            prediction.get("turn_delay_seconds_q50"),
            prediction.get("turn_delay_seconds_q90"),
            json.dumps(dict(prediction), sort_keys=True, separators=(",", ":")),
            json.dumps(feature_vector, separators=(",", ":")),
        )
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO path_forecasts("
                "created_at,model_version,feature_schema_version,asset,close_time,"
                "checkpoint_seconds,decision_time,captured_offset_seconds,target_px,current_px,"
                "current_yes_mid,top_archetype,top_archetype_probability,"
                "settlement_yes_probability,strike_cross_probability,turn_delay_seconds_q10,"
                "turn_delay_seconds_q50,turn_delay_seconds_q90,prediction_json,feature_json"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            inserted = cursor.rowcount == 1
            row = self._conn.execute(
                "SELECT id FROM path_forecasts WHERE model_version=? AND asset=? "
                "AND close_time=? AND checkpoint_seconds=?",
                (prediction["model_version"], str(asset).upper(), float(close_time), int(checkpoint_seconds)),
            ).fetchone()
            self._conn.commit()
        if row is None:
            raise sqlite3.IntegrityError("path forecast row was not durable after insert")
        return int(row["id"]), inserted

    def rows(self, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(10_000, int(limit)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM path_forecasts ORDER BY id DESC LIMIT ?", (bounded,)
            ).fetchall()
        return [dict(row) for row in rows]

    def pending(self, *, before_close_time: float, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(1_000, int(limit)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM path_forecasts WHERE official_result IS NULL "
                "AND close_time<=? ORDER BY close_time,id LIMIT ?",
                (float(before_close_time), bounded),
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve(
        self,
        row_id: int,
        *,
        official_result: str,
        resolved_at: float,
        actual_archetype: str,
        actual_strike_crossed: int,
        settlement_correct: int,
    ) -> bool:
        official = str(official_result or "").upper()
        if official not in {"YES", "NO"}:
            raise ValueError("official result must be YES or NO")
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE path_forecasts SET official_result=?,resolved_at=?,actual_archetype=?,"
                "actual_strike_crossed=?,settlement_correct=? WHERE id=? AND official_result IS NULL",
                (
                    official,
                    float(resolved_at),
                    str(actual_archetype),
                    int(bool(actual_strike_crossed)),
                    int(bool(settlement_correct)),
                    int(row_id),
                ),
            )
            self._conn.commit()
        return cursor.rowcount == 1

    def status(self) -> dict[str, Any]:
        with self._lock:
            total = int(self._conn.execute("SELECT COUNT(*) FROM path_forecasts").fetchone()[0])
            resolved = int(self._conn.execute(
                "SELECT COUNT(*) FROM path_forecasts WHERE official_result IN ('YES','NO')"
            ).fetchone()[0])
            latest = self._conn.execute(
                "SELECT created_at,asset,close_time,checkpoint_seconds,top_archetype,"
                "top_archetype_probability,settlement_yes_probability,strike_cross_probability "
                "FROM path_forecasts ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return {
            "db_path": self.db_path,
            "rows": total,
            "resolved_rows": resolved,
            "latest": None if latest is None else dict(latest),
        }
