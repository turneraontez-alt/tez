"""Durable, separate evidence ledger for Q15 MarketLead."""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
from collections import Counter
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
    proxy_distance_side_bps REAL,
    rti_proxy_source_count INTEGER,
    rti_proxy_sources_json TEXT,
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
    paper_limit_cents REAL,
    paper_queue_ahead_contracts REAL,
    paper_limit_touched INTEGER NOT NULL DEFAULT 0,
    paper_limit_touch_at REAL,
    paper_touch_price_cents REAL,
    execution_status TEXT NOT NULL DEFAULT 'WAITING_TOUCH',
    markout_side_5s_cents REAL,
    markout_side_15s_cents REAL,
    markout_side_30s_cents REAL,
    joint_alignment INTEGER NOT NULL DEFAULT 0,
    lead_lag_candidate INTEGER NOT NULL DEFAULT 0,
    evidence_status TEXT NOT NULL,
    missing_reasons_json TEXT NOT NULL,
    limitations_json TEXT NOT NULL DEFAULT '[]',
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

CREATE TABLE IF NOT EXISTS marketlead_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_key TEXT NOT NULL UNIQUE,
    system_version TEXT NOT NULL,
    asset TEXT NOT NULL,
    ticker TEXT NOT NULL,
    window_key INTEGER NOT NULL,
    mark_seconds INTEGER NOT NULL,
    payload TEXT NOT NULL,
    expires_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING_ENQUEUE',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_attempt_at REAL,
    next_attempt_at REAL NOT NULL,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_marketlead_notifications_due
ON marketlead_notifications(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS marketlead_audit_rules (
    rule_version TEXT PRIMARY KEY,
    config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    registered_at REAL NOT NULL,
    first_observed_at REAL,
    audit_status TEXT NOT NULL DEFAULT 'SHADOW',
    immutable INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS marketlead_audit_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_version TEXT NOT NULL,
    system_version TEXT NOT NULL,
    asset TEXT NOT NULL,
    ticker TEXT NOT NULL,
    window_key INTEGER NOT NULL,
    mark_seconds INTEGER NOT NULL,
    observed_at REAL NOT NULL,
    qualified INTEGER NOT NULL,
    reason_codes_json TEXT NOT NULL,
    feature_schema_version TEXT,
    config_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(rule_version, system_version, ticker, mark_seconds),
    FOREIGN KEY(rule_version) REFERENCES marketlead_audit_rules(rule_version)
);
CREATE INDEX IF NOT EXISTS idx_marketlead_audit_window
ON marketlead_audit_decisions(rule_version, qualified, window_key);

CREATE TRIGGER IF NOT EXISTS marketlead_audit_rules_frozen
BEFORE UPDATE OF rule_version,config_json,config_hash,registered_at,immutable
ON marketlead_audit_rules
BEGIN
    SELECT RAISE(ABORT, 'marketlead audit rule configuration is immutable');
END;
CREATE TRIGGER IF NOT EXISTS marketlead_audit_rules_no_delete
BEFORE DELETE ON marketlead_audit_rules
BEGIN
    SELECT RAISE(ABORT, 'marketlead audit rules cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS marketlead_audit_decisions_no_update
BEFORE UPDATE ON marketlead_audit_decisions
BEGIN
    SELECT RAISE(ABORT, 'marketlead audit decisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS marketlead_audit_decisions_no_delete
BEFORE DELETE ON marketlead_audit_decisions
BEGIN
    SELECT RAISE(ABORT, 'marketlead audit decisions cannot be deleted');
END;
"""

_COLUMNS = (
    "system_version", "asset", "ticker", "window_key", "mark_seconds",
    "observed_at", "close_time", "seconds_remaining", "predicted_side",
    "entry_ask_cents", "spread_cents", "spot_price", "strike_price",
    "official_index_price", "rti_proxy_price", "proxy_gap_bps",
    "proxy_lead_side_bps", "proxy_distance_side_bps", "rti_proxy_source_count",
    "rti_proxy_sources_json",
    "venue_source_count", "venue_dispersion_bps",
    "venue_impulse", "venue_impulse_side", "venue_aligned_fraction",
    "venue_leader", "venue_leader_persistence", "kalshi_available",
    "kalshi_book_age_seconds", "kalshi_event_age_seconds",
    "kalshi_yes_bid_cents", "kalshi_yes_ask_cents",
    "kalshi_microprice_yes_cents", "kalshi_microprice_edge_yes_cents",
    "kalshi_pressure_yes_5s", "kalshi_pressure_yes_15s",
    "kalshi_pressure_yes_30s", "kalshi_trade_imbalance_yes_15s",
    "kalshi_pressure_side", "paper_limit_cents", "paper_queue_ahead_contracts",
    "paper_limit_touched", "paper_limit_touch_at", "paper_touch_price_cents",
    "execution_status",
    "joint_alignment", "lead_lag_candidate", "evidence_status",
    "missing_reasons_json", "limitations_json", "features_json", "build_sha", "config_hash",
    "feature_schema_version",
)


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _config_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _wilson(correct: int, total: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    p = correct / total
    denominator = 1.0 + z * z / total
    center = (p + z * z / (2.0 * total)) / denominator
    margin = z * math.sqrt(
        (p * (1.0 - p) + z * z / (4.0 * total)) / total
    ) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


class MarketLeadLedger:
    def __init__(self, db_path: str, *, read_only: bool = False):
        self.path = Path(db_path)
        self._read_only = bool(read_only)
        self._lock = threading.Lock()
        self._available = False
        self._last_error: str | None = None
        try:
            if self._read_only:
                with self._connect() as connection:
                    connection.execute(
                        "SELECT 1 FROM marketlead_audit_rules LIMIT 1"
                    ).fetchone()
                self._available = True
                return
            if self.path.parent:
                os.makedirs(self.path.parent, exist_ok=True)
            with self._connect() as connection:
                connection.executescript(_SCHEMA)
                self._migrate(connection)
                connection.commit()
            self._available = True
        except sqlite3.Error as exc:
            self._last_error = f"marketlead ledger init failed: {exc}"

    def _connect(self) -> sqlite3.Connection:
        if self._read_only:
            uri = f"file:{self.path.resolve().as_posix()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=3.0)
        else:
            connection = sqlite3.connect(str(self.path), timeout=3.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=3000")
        if self._read_only:
            connection.execute("PRAGMA query_only=ON")
        else:
            connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        existing = {
            str(row[1]) for row in connection.execute(
                "PRAGMA table_info(marketlead_observations)"
            )
        }
        additions = {
            "rti_proxy_source_count": "INTEGER",
            "rti_proxy_sources_json": "TEXT",
            "proxy_distance_side_bps": "REAL",
            "limitations_json": "TEXT NOT NULL DEFAULT '[]'",
            "paper_limit_cents": "REAL",
            "paper_queue_ahead_contracts": "REAL",
            "paper_limit_touched": "INTEGER NOT NULL DEFAULT 0",
            "paper_limit_touch_at": "REAL",
            "paper_touch_price_cents": "REAL",
            "execution_status": "TEXT NOT NULL DEFAULT 'WAITING_TOUCH'",
            "markout_side_5s_cents": "REAL",
            "markout_side_15s_cents": "REAL",
            "markout_side_30s_cents": "REAL",
            "lead_lag_candidate": "INTEGER NOT NULL DEFAULT 0",
        }
        added_lead_lag = "lead_lag_candidate" not in existing
        for column, declaration in additions.items():
            if column not in existing:
                connection.execute(
                    f"ALTER TABLE marketlead_observations ADD COLUMN {column} {declaration}"
                )
        if added_lead_lag:
            # Backfill the version-1 ledger using the version-2 default rule so
            # chronological reports can compare the old and new cohorts.
            connection.execute(
                "UPDATE marketlead_observations SET lead_lag_candidate=1 "
                "WHERE evidence_status='READY' "
                "AND proxy_distance_side_bps>0 AND venue_impulse_side>0 "
                "AND kalshi_pressure_side<=-0.10"
            )

    @property
    def available(self) -> bool:
        return self._available

    def register_audit_rule(
        self,
        rule_version: str,
        config: Mapping[str, Any],
        *,
        registered_at: float | None = None,
    ) -> dict[str, Any]:
        """Register one immutable prospective rule configuration.

        Reusing a version with different thresholds is rejected. A caller must
        choose a new version, which necessarily starts a fresh forward audit.
        """
        version = str(rule_version or "").strip()
        config_json = _canonical_json(config)
        digest = _config_hash(config)
        now = time.time() if registered_at is None else float(registered_at)
        if not self._available or not version:
            return {
                "valid": False,
                "rule_version": version,
                "config_hash": digest,
                "error": "audit_rule_unavailable_or_missing",
            }
        try:
            with self._lock, self._connect() as connection:
                existing = connection.execute(
                    "SELECT * FROM marketlead_audit_rules WHERE rule_version=?",
                    (version,),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO marketlead_audit_rules ("
                        "rule_version,config_json,config_hash,registered_at,audit_status,immutable"
                        ") VALUES (?,?,?,?,?,1)",
                        (version, config_json, digest, now, "SHADOW"),
                    )
                    connection.commit()
                    return {
                        "valid": True,
                        "created": True,
                        "rule_version": version,
                        "config_hash": digest,
                        "registered_at": now,
                        "first_observed_at": None,
                        "audit_status": "SHADOW",
                    }
                row = dict(existing)
                valid = str(row.get("config_hash") or "") == digest
                return {
                    "valid": valid,
                    "created": False,
                    "rule_version": version,
                    "config_hash": digest,
                    "registered_config_hash": row.get("config_hash"),
                    "registered_at": row.get("registered_at"),
                    "first_observed_at": row.get("first_observed_at"),
                    "audit_status": row.get("audit_status"),
                    "error": None if valid else "immutable_rule_config_mismatch",
                }
        except sqlite3.Error as exc:
            self._last_error = f"marketlead audit rule registration failed: {exc}"
            return {
                "valid": False,
                "rule_version": version,
                "config_hash": digest,
                "error": str(exc),
            }

    def record(
        self,
        row: Mapping[str, Any],
        *,
        notification: Mapping[str, Any] | None = None,
        audit: Mapping[str, Any] | None = None,
    ) -> bool:
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
                if cursor.rowcount == 1 and audit is not None:
                    rule_version = str(audit.get("rule_version") or "")
                    config_hash = str(audit.get("config_hash") or "")
                    registered = connection.execute(
                        "SELECT config_hash FROM marketlead_audit_rules "
                        "WHERE rule_version=?",
                        (rule_version,),
                    ).fetchone()
                    if (
                        registered is not None
                        and str(registered["config_hash"] or "") == config_hash
                    ):
                        observed_at = float(row.get("observed_at") or time.time())
                        reasons = audit.get("reason_codes")
                        reason_codes_json = (
                            str(audit.get("reason_codes_json"))
                            if audit.get("reason_codes_json") is not None
                            else json.dumps(
                                list(reasons or []), separators=(",", ":")
                            )
                        )
                        decision_at = float(audit.get("created_at") or time.time())
                        connection.execute(
                            "INSERT OR IGNORE INTO marketlead_audit_decisions ("
                            "rule_version,system_version,asset,ticker,window_key,"
                            "mark_seconds,observed_at,qualified,reason_codes_json,"
                            "feature_schema_version,config_hash,created_at"
                            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                rule_version,
                                row.get("system_version"),
                                row.get("asset"),
                                row.get("ticker"),
                                row.get("window_key"),
                                row.get("mark_seconds"),
                                observed_at,
                                1 if audit.get("qualified") else 0,
                                reason_codes_json,
                                row.get("feature_schema_version"),
                                config_hash,
                                decision_at,
                            ),
                        )
                        connection.execute(
                            "UPDATE marketlead_audit_rules SET "
                            "first_observed_at=COALESCE(first_observed_at,?) "
                            "WHERE rule_version=?",
                            (decision_at, rule_version),
                        )
                    else:
                        self._last_error = (
                            "marketlead audit decision rejected: immutable rule mismatch"
                        )
                if cursor.rowcount == 1 and notification is not None:
                    created_at = float(
                        notification.get("created_at") or row.get("observed_at") or time.time()
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO marketlead_notifications ("
                        "notification_key,system_version,asset,ticker,window_key,"
                        "mark_seconds,payload,expires_at,status,created_at,updated_at,"
                        "next_attempt_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            notification.get("notification_key"),
                            row.get("system_version"),
                            row.get("asset"),
                            row.get("ticker"),
                            row.get("window_key"),
                            row.get("mark_seconds"),
                            notification.get("payload"),
                            notification.get("expires_at"),
                            "PENDING_ENQUEUE",
                            created_at,
                            created_at,
                            created_at,
                        ),
                    )
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

    def record_touch(
        self,
        system_version: str,
        ticker: str,
        *,
        touched_at: float,
        price_cents: float,
    ) -> bool:
        if not self._available:
            return False
        try:
            with self._lock, self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE marketlead_observations SET paper_limit_touched=1,"
                    "paper_limit_touch_at=?,paper_touch_price_cents=?,execution_status='TOUCHED' "
                    "WHERE system_version=? AND ticker=? AND paper_limit_touched=0",
                    (touched_at, price_cents, system_version, ticker),
                )
                connection.commit()
                return cursor.rowcount > 0
        except sqlite3.Error as exc:
            self._last_error = f"marketlead touch failed: {exc}"
            return False

    def set_execution_status(
        self, system_version: str, ticker: str, status: str
    ) -> bool:
        normalized = str(status or "").upper()
        if normalized not in {"WAITING_TOUCH", "TOUCHED", "COMPLETE", "EXPIRED"}:
            return False
        try:
            with self._lock, self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE marketlead_observations SET execution_status=? "
                    "WHERE system_version=? AND ticker=?",
                    (normalized, system_version, ticker),
                )
                connection.commit()
                return cursor.rowcount > 0
        except sqlite3.Error as exc:
            self._last_error = f"marketlead execution status failed: {exc}"
            return False

    def record_markout(
        self,
        system_version: str,
        ticker: str,
        horizon_seconds: int,
        markout_cents: float,
    ) -> bool:
        if horizon_seconds not in {5, 15, 30} or not self._available:
            return False
        column = f"markout_side_{horizon_seconds}s_cents"
        try:
            with self._lock, self._connect() as connection:
                cursor = connection.execute(
                    f"UPDATE marketlead_observations SET {column}=? "
                    f"WHERE system_version=? AND ticker=? AND {column} IS NULL",
                    (markout_cents, system_version, ticker),
                )
                connection.commit()
                return cursor.rowcount > 0
        except sqlite3.Error as exc:
            self._last_error = f"marketlead markout failed: {exc}"
            return False

    def pending_execution_rows(self) -> list[dict[str, Any]]:
        if not self._available:
            return []
        with self._lock, self._connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT ticker,predicted_side,observed_at,paper_limit_cents,"
                "paper_limit_touched,paper_limit_touch_at,paper_touch_price_cents,"
                "markout_side_5s_cents,markout_side_15s_cents,markout_side_30s_cents "
                "FROM marketlead_observations WHERE execution_status IN ('WAITING_TOUCH','TOUCHED')"
            )]

    def rows(self) -> list[dict[str, Any]]:
        if not self._available:
            return []
        with self._lock, self._connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM marketlead_observations ORDER BY observed_at,id"
            )]

    def due_notifications(
        self, now: float | None = None, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        if not self._available:
            return []
        due_at = time.time() if now is None else float(now)
        bounded = max(1, min(int(limit), 250))
        with self._lock, self._connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM marketlead_notifications "
                "WHERE status IN ('PENDING_ENQUEUE','DELIVERY_FAILED') "
                "AND next_attempt_at<=? ORDER BY created_at,id LIMIT ?",
                (due_at, bounded),
            )]

    def notifications_to_reconcile(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if not self._available:
            return []
        bounded = max(1, min(int(limit), 500))
        with self._lock, self._connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM marketlead_notifications "
                "WHERE status='QUEUED_RETRY' ORDER BY updated_at,id LIMIT ?",
                (bounded,),
            )]

    def notification_rows(self) -> list[dict[str, Any]]:
        if not self._available:
            return []
        with self._lock, self._connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM marketlead_notifications ORDER BY created_at,id"
            )]

    def audit_decision_rows(
        self, rule_version: str | None = None
    ) -> list[dict[str, Any]]:
        if not self._available:
            return []
        with self._lock, self._connect() as connection:
            if rule_version is None:
                rows = connection.execute(
                    "SELECT * FROM marketlead_audit_decisions ORDER BY created_at,id"
                )
            else:
                rows = connection.execute(
                    "SELECT * FROM marketlead_audit_decisions "
                    "WHERE rule_version=? ORDER BY created_at,id",
                    (str(rule_version),),
                )
            return [dict(row) for row in rows]

    def notification_performance(
        self, rule_version: str, *, lookback: int = 20
    ) -> dict[str, Any]:
        """Resolved outcomes for one versioned V3 rule, newest first."""
        if not self._available:
            return {
                "available": False,
                "resolved": 0,
                "wins": 0,
                "losses": 0,
                "accuracy": None,
                "gross_pnl_cents": 0.0,
            }
        bounded = max(1, min(int(lookback), 500))
        marker = f"%:{str(rule_version)}:%"
        try:
            with self._lock, self._connect() as connection:
                rows = connection.execute(
                    "SELECT o.correct,o.realized_pnl_cents "
                    "FROM marketlead_notifications n "
                    "JOIN marketlead_observations o ON "
                    "o.system_version=n.system_version AND o.asset=n.asset "
                    "AND o.ticker=n.ticker AND o.window_key=n.window_key "
                    "AND o.mark_seconds=n.mark_seconds "
                    "WHERE n.status='SENT' AND n.notification_key LIKE ? "
                    "AND o.correct IS NOT NULL ORDER BY n.created_at DESC LIMIT ?",
                    (marker, bounded),
                ).fetchall()
            resolved = len(rows)
            wins = sum(int(row["correct"] or 0) for row in rows)
            return {
                "available": True,
                "resolved": resolved,
                "wins": wins,
                "losses": resolved - wins,
                "accuracy": None if not resolved else wins / resolved,
                "gross_pnl_cents": sum(
                    float(row["realized_pnl_cents"] or 0.0) for row in rows
                ),
            }
        except sqlite3.Error as exc:
            self._last_error = f"marketlead notification performance failed: {exc}"
            return {
                "available": False,
                "resolved": 0,
                "wins": 0,
                "losses": 0,
                "accuracy": None,
                "gross_pnl_cents": 0.0,
            }

    def prospective_audit_report(
        self,
        rule_version: str,
        *,
        block_windows: int = 20,
        min_blocks: int = 3,
        accuracy_min: float = 0.86,
        wilson_lb_min: float = 0.75,
    ) -> dict[str, Any]:
        """Score only atomic, post-registration decisions for one frozen rule.

        Promotion uses independent 15-minute windows. A window is a win only
        when every qualifying asset in that window is correct, preventing
        correlated cross-asset rows from inflating the effective sample size.
        Blocks have a fixed number of sequential qualifying windows and never
        move when new data arrives.
        """
        version = str(rule_version or "")
        block_size = max(1, int(block_windows))
        required_blocks = max(1, int(min_blocks))
        accuracy_bar = min(1.0, max(0.0, float(accuracy_min)))
        wilson_bar = min(1.0, max(0.0, float(wilson_lb_min)))
        empty = {
            "available": False,
            "rule_version": version,
            "prospective_only": True,
            "backfill_allowed": False,
            "target_status": "INVALID",
        }
        if not self._available or not version:
            return {**empty, "error": "audit_unavailable_or_missing_rule"}
        try:
            with self._lock, self._connect() as connection:
                rule_row = connection.execute(
                    "SELECT * FROM marketlead_audit_rules WHERE rule_version=?",
                    (version,),
                ).fetchone()
                if rule_row is None:
                    return {**empty, "error": "audit_rule_not_registered"}
                decision_rows = [dict(row) for row in connection.execute(
                    "SELECT d.*,o.build_sha AS observation_build_sha,"
                    "o.config_hash AS observation_config_hash,"
                    "o.feature_schema_version AS observation_feature_schema_version,"
                    "o.official_result,o.resolved_at,o.correct,o.realized_pnl_cents "
                    "FROM marketlead_audit_decisions d "
                    "JOIN marketlead_observations o ON "
                    "o.system_version=d.system_version AND o.asset=d.asset "
                    "AND o.ticker=d.ticker AND o.window_key=d.window_key "
                    "AND o.mark_seconds=d.mark_seconds "
                    "WHERE d.rule_version=? ORDER BY d.window_key,d.asset,d.id",
                    (version,),
                )]
        except sqlite3.Error as exc:
            return {**empty, "error": str(exc)}

        rule = dict(rule_row)
        try:
            registered_config = json.loads(str(rule.get("config_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            registered_config = {}
        scoring = (
            registered_config.get("scoring")
            if isinstance(registered_config, Mapping)
            else None
        )
        if isinstance(scoring, Mapping):
            block_size = max(1, int(scoring.get("block_windows", block_size)))
            required_blocks = max(
                1, int(scoring.get("minimum_complete_blocks", required_blocks))
            )
            accuracy_bar = min(1.0, max(0.0, float(
                scoring.get("accuracy_each_complete_block", accuracy_bar)
            )))
            wilson_bar = min(1.0, max(0.0, float(
                scoring.get("window_wilson_95_low_min", wilson_bar)
            )))
        expected_feature_hash = str(
            registered_config.get("feature_config_hash") or ""
        )
        expected_feature_schema = str(
            registered_config.get("feature_schema_version") or ""
        )
        lineage_mismatch_rows = [
            row for row in decision_rows
            if (
                expected_feature_hash
                and str(row.get("observation_config_hash") or "")
                != expected_feature_hash
            )
            or (
                expected_feature_schema
                and str(row.get("observation_feature_schema_version") or "")
                != expected_feature_schema
            )
        ]
        reject_reasons: Counter[str] = Counter()
        for row in decision_rows:
            if int(row.get("qualified") or 0) == 1:
                continue
            try:
                reasons = json.loads(str(row.get("reason_codes_json") or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                reasons = ["AUDIT_REASON_PARSE_ERROR"]
            if isinstance(reasons, list):
                reject_reasons.update(str(reason) for reason in reasons)

        qualified = [
            row for row in decision_rows if int(row.get("qualified") or 0) == 1
        ]
        invalid_timing_rows = [
            row for row in qualified
            if row.get("correct") is not None
            and (
                row.get("resolved_at") is None
                or float(row["resolved_at"]) < float(row["created_at"])
            )
        ]
        invalid_timing_ids = {int(row["id"]) for row in invalid_timing_rows}
        resolved_rows = [
            row for row in qualified
            if row.get("correct") is not None
            and int(row["id"]) not in invalid_timing_ids
        ]
        row_wins = sum(int(row.get("correct") or 0) for row in resolved_rows)
        row_low, row_high = _wilson(row_wins, len(resolved_rows))

        by_window: dict[int, list[dict[str, Any]]] = {}
        for row in qualified:
            by_window.setdefault(int(row["window_key"]), []).append(row)
        window_results: list[dict[str, Any]] = []
        for window_key in sorted(by_window):
            rows = by_window[window_key]
            resolved = all(
                row.get("correct") is not None
                and int(row["id"]) not in invalid_timing_ids
                for row in rows
            )
            win = bool(resolved and all(int(row.get("correct") or 0) == 1 for row in rows))
            window_results.append({
                "window_key": window_key,
                "qualified_rows": len(rows),
                "resolved": resolved,
                "win": win if resolved else None,
                "gross_pnl_cents": (
                    sum(float(row.get("realized_pnl_cents") or 0.0) for row in rows)
                    if resolved else None
                ),
            })
        resolved_windows = [row for row in window_results if row["resolved"]]
        window_wins = sum(1 for row in resolved_windows if row["win"])
        window_low, window_high = _wilson(window_wins, len(resolved_windows))

        blocks: list[dict[str, Any]] = []
        for start in range(0, len(window_results), block_size):
            selected = window_results[start:start + block_size]
            resolved = [row for row in selected if row["resolved"]]
            wins = sum(1 for row in resolved if row["win"])
            complete = len(selected) == block_size and len(resolved) == block_size
            blocks.append({
                "block": len(blocks) + 1,
                "start_window_key": selected[0]["window_key"],
                "end_window_key": selected[-1]["window_key"],
                "windows": len(selected),
                "resolved_windows": len(resolved),
                "wins": wins,
                "losses": len(resolved) - wins,
                "accuracy": None if not resolved else wins / len(resolved),
                "gross_pnl_cents": sum(
                    float(row.get("gross_pnl_cents") or 0.0) for row in resolved
                ),
                "complete": complete,
            })
        complete_blocks = [block for block in blocks if block["complete"]]
        failed_blocks = [
            block for block in complete_blocks
            if float(block.get("accuracy") or 0.0) < accuracy_bar
            or float(block.get("gross_pnl_cents") or 0.0) <= 0.0
        ]
        enough_blocks = len(complete_blocks) >= required_blocks
        wilson_pass = window_low is not None and window_low >= wilson_bar
        if invalid_timing_rows or lineage_mismatch_rows:
            target_status = "INVALID"
        elif failed_blocks:
            target_status = "FAIL"
        elif not enough_blocks:
            target_status = "COLLECTING"
        elif wilson_pass:
            target_status = "PASS"
        else:
            target_status = "FAIL"

        return {
            "available": True,
            "rule_version": version,
            "config_hash": rule.get("config_hash"),
            "registered_config": registered_config,
            "registered_at": rule.get("registered_at"),
            "first_observed_at": rule.get("first_observed_at"),
            "immutable": bool(rule.get("immutable")),
            "prospective_only": True,
            "backfill_allowed": False,
            "decision_origin": "atomic_observation_insert",
            "lineage_integrity": {
                "valid": not lineage_mismatch_rows,
                "mismatched_rows": len(lineage_mismatch_rows),
                "implementation_hash": registered_config.get(
                    "implementation_hash"
                ),
                "observed_build_shas": sorted({
                    str(row.get("observation_build_sha") or "unknown")
                    for row in decision_rows
                }),
                "expected_feature_config_hash": expected_feature_hash or None,
                "expected_feature_schema_version": expected_feature_schema or None,
            },
            "outcome_timing_integrity": {
                "valid": not invalid_timing_rows,
                "invalid_rows": len(invalid_timing_rows),
                "require_resolved_at_after_decision_at": True,
            },
            "decisions": len(decision_rows),
            "qualified": len(qualified),
            "rejected": len(decision_rows) - len(qualified),
            "reject_reason_counts": dict(sorted(reject_reasons.items())),
            "row_metrics": {
                "resolved": len(resolved_rows),
                "wins": row_wins,
                "losses": len(resolved_rows) - row_wins,
                "accuracy": None if not resolved_rows else row_wins / len(resolved_rows),
                "wilson_95_low": row_low,
                "wilson_95_high": row_high,
                "gross_pnl_cents": sum(
                    float(row.get("realized_pnl_cents") or 0.0)
                    for row in resolved_rows
                ),
            },
            "window_metrics": {
                "qualifying_windows": len(window_results),
                "resolved": len(resolved_windows),
                "wins": window_wins,
                "losses": len(resolved_windows) - window_wins,
                "accuracy": (
                    None if not resolved_windows else window_wins / len(resolved_windows)
                ),
                "wilson_95_low": window_low,
                "wilson_95_high": window_high,
                "gross_pnl_cents": sum(
                    float(row.get("gross_pnl_cents") or 0.0)
                    for row in resolved_windows
                ),
            },
            "blocks": blocks,
            "requirements": {
                "block_windows": block_size,
                "minimum_complete_blocks": required_blocks,
                "minimum_resolved_windows": block_size * required_blocks,
                "accuracy_each_complete_block": accuracy_bar,
                "positive_gross_pnl_each_complete_block": True,
                "window_wilson_95_low_min": wilson_bar,
            },
            "complete_blocks": len(complete_blocks),
            "failed_blocks": [block["block"] for block in failed_blocks],
            "target_status": target_status,
        }

    def mark_notification_attempt(
        self,
        notification_key: str,
        *,
        status: str,
        attempted_at: float,
        last_error: str | None,
        next_attempt_at: float | None = None,
    ) -> bool:
        if not self._available:
            return False
        try:
            with self._lock, self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE marketlead_notifications SET status=?,"
                    "attempt_count=attempt_count+1,updated_at=?,last_attempt_at=?,"
                    "next_attempt_at=?,last_error=? WHERE notification_key=?",
                    (
                        str(status),
                        attempted_at,
                        attempted_at,
                        attempted_at if next_attempt_at is None else next_attempt_at,
                        last_error,
                        str(notification_key),
                    ),
                )
                connection.commit()
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            self._last_error = f"marketlead notification update failed: {exc}"
            return False

    def reconcile_notification(
        self,
        notification_key: str,
        *,
        status: str,
        updated_at: float,
        last_error: str | None,
    ) -> bool:
        if not self._available:
            return False
        try:
            with self._lock, self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE marketlead_notifications SET status=?,updated_at=?,"
                    "last_error=? WHERE notification_key=?",
                    (str(status), updated_at, last_error, str(notification_key)),
                )
                connection.commit()
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            self._last_error = f"marketlead notification reconcile failed: {exc}"
            return False

    def status(self) -> dict[str, Any]:
        if not self._available:
            return {"available": False, "last_error": self._last_error}
        try:
            with self._lock, self._connect() as connection:
                row = connection.execute(
                    "SELECT COUNT(*) n,SUM(official_result IS NOT NULL) resolved,"
                    "SUM(evidence_status='READY') ready,SUM(joint_alignment=1) aligned,"
                    "SUM(lead_lag_candidate=1) lead_lag "
                    "FROM marketlead_observations"
                ).fetchone()
                notification_rows = connection.execute(
                    "SELECT status,COUNT(*) n FROM marketlead_notifications GROUP BY status"
                ).fetchall()
            notification_counts = {
                str(item["status"]): int(item["n"] or 0)
                for item in notification_rows
            }
            return {
                "available": True,
                "observations": int(row["n"] or 0),
                "resolved": int(row["resolved"] or 0),
                "ready": int(row["ready"] or 0),
                "joint_alignment": int(row["aligned"] or 0),
                "lead_lag_candidates": int(row["lead_lag"] or 0),
                "v3_notifications": {
                    "total": sum(notification_counts.values()),
                    "counts": notification_counts,
                },
                "last_error": self._last_error,
            }
        except sqlite3.Error as exc:
            return {"available": False, "last_error": str(exc)}


__all__ = ["MarketLeadLedger"]
