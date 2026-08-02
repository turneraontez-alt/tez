"""Durable, dormant ledger and outbox for a future V21 PAPER challenger.

Nothing imports this module from the live collector.  It provides the storage
boundary that a manually activated, historically passing artifact must use.
It has no Telegram client and no order capability.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Any, Mapping, Sequence

from . import rti_microstructure_v21_paper_identity as identity
from .costs import (
    KALSHI_Q15_FEE_SCHEDULE_VERSION,
    rti_simulated_execution,
    rti_simulated_net_pnl_cents,
)


ACCEPTED_PAPER = "ACCEPTED_PAPER"
DECISION_STATUSES = {
    ACCEPTED_PAPER,
    "REJECTED_EDGE_POLICY",
    "NONEXECUTABLE_BOOK",
    "DATA_INELIGIBLE",
    "MODEL_ERROR",
}
TERMINAL_NOTIFICATION_STATES = {"SENT", "EXPIRED", "DEAD_LETTER", "MUTED"}
COHORT_ASSETS = {
    "NON_BTC_TRANSFER": {"BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP"},
    "BTC": {"BTC"},
}
IMMUTABLE_COLUMNS = (
    "decision_key", "artifact_sha256", "cohort", "asset", "ticker",
    "close_time", "decision_timestamp", "parent_id", "intermediate_id",
    "delayed_id", "source_feature_evidence_sha256",
    "feature_evidence_sha256", "feature_vector_sha256",
    "market_side_probability", "candidate_survival_probability",
    "v20_ablation_survival_probability", "side", "decision_status",
    "reason_codes", "selected_margin", "entry_ask_cents", "spread_cents",
    "displayed_depth_contracts", "simulated_contracts",
    "slippage_cents_per_contract", "fee_schedule_version",
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_key TEXT PRIMARY KEY,
    artifact_sha256 TEXT NOT NULL,
    created_at REAL NOT NULL,
    cohort TEXT NOT NULL,
    asset TEXT NOT NULL,
    ticker TEXT NOT NULL,
    close_time REAL NOT NULL,
    decision_timestamp REAL NOT NULL,
    parent_id INTEGER NOT NULL,
    intermediate_id INTEGER NOT NULL,
    delayed_id INTEGER NOT NULL,
    source_feature_evidence_sha256 TEXT NOT NULL,
    feature_evidence_sha256 TEXT NOT NULL,
    feature_vector_sha256 TEXT NOT NULL,
    market_side_probability REAL NOT NULL,
    candidate_survival_probability REAL NOT NULL,
    v20_ablation_survival_probability REAL NOT NULL,
    side TEXT NOT NULL,
    decision_status TEXT NOT NULL,
    reason_codes TEXT NOT NULL,
    selected_margin REAL NOT NULL,
    entry_ask_cents REAL,
    spread_cents REAL,
    displayed_depth_contracts REAL,
    simulated_contracts INTEGER NOT NULL,
    slippage_cents_per_contract REAL NOT NULL,
    fee_schedule_version TEXT NOT NULL,
    official_result TEXT,
    settled_at REAL,
    settlement_evidence_json TEXT,
    settlement_evidence_sha256 TEXT,
    correct INTEGER,
    fee_slippage_adjusted_pnl_cents REAL,
    UNIQUE (artifact_sha256, cohort, parent_id, intermediate_id, delayed_id),
    CHECK (side IN ('YES', 'NO')),
    CHECK (decision_status IN (
        'ACCEPTED_PAPER', 'REJECTED_EDGE_POLICY', 'NONEXECUTABLE_BOOK',
        'DATA_INELIGIBLE', 'MODEL_ERROR'
    )),
    CHECK (official_result IS NULL OR official_result IN ('YES', 'NO')),
    CHECK (correct IS NULL OR correct IN (0, 1))
);
CREATE TABLE IF NOT EXISTS notification_outbox (
    decision_key TEXT PRIMARY KEY REFERENCES decisions(decision_key),
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_until REAL,
    message_id TEXT,
    notified_at REAL,
    last_error TEXT,
    CHECK (status IN ('QUEUED', 'SENT', 'EXPIRED', 'DEAD_LETTER', 'MUTED'))
);
CREATE INDEX IF NOT EXISTS idx_v21_paper_decisions_status_close
    ON decisions(decision_status, close_time);
CREATE INDEX IF NOT EXISTS idx_v21_paper_decisions_settlement
    ON decisions(official_result, close_time);
CREATE INDEX IF NOT EXISTS idx_v21_paper_outbox_delivery
    ON notification_outbox(status, expires_at, lease_until);
"""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"v21_paper_{name}_invalid")
    try:
        output = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"v21_paper_{name}_invalid") from exc
    if not math.isfinite(output):
        raise ValueError(f"v21_paper_{name}_invalid")
    return output


def _hash_text(value: Any, name: str) -> str:
    output = str(value or "").lower()
    if len(output) != 64 or any(char not in "0123456789abcdef" for char in output):
        raise ValueError(f"v21_paper_{name}_invalid")
    return output


def decision_key(
    artifact_sha256: str, cohort: str, parent_id: int,
    intermediate_id: int, delayed_id: int,
) -> str:
    return _sha256({
        "artifact_sha256": _hash_text(artifact_sha256, "artifact_sha256"),
        "cohort": str(cohort),
        "parent_id": int(parent_id),
        "intermediate_id": int(intermediate_id),
        "delayed_id": int(delayed_id),
    })


def notification_idempotency_key(key: str) -> str:
    return "V21_PAPER_" + hashlib.sha256(str(key).encode("ascii")).hexdigest()


def _reason_json(value: Any) -> str:
    if isinstance(value, str):
        values = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, Sequence):
        values = [str(part).strip() for part in value if str(part).strip()]
    else:
        raise ValueError("v21_paper_reason_codes_invalid")
    return _canonical_json(sorted(set(values)))


def _row_dict(cursor: sqlite3.Cursor, row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {description[0]: row[index] for index, description in enumerate(cursor.description)}


def build_notification_message(row: Mapping[str, Any]) -> str:
    if row.get("decision_status") != ACCEPTED_PAPER:
        raise ValueError("v21_paper_notification_requires_accepted_pick")
    execution = rti_simulated_execution(
        row.get("entry_ask_cents"), int(row.get("simulated_contracts") or 0),
        row.get("slippage_cents_per_contract"),
    )
    if execution is None:
        raise ValueError("v21_paper_notification_execution_invalid")
    probability = float(row["candidate_survival_probability"])
    expected_value = (
        probability * 100.0
        - float(execution["simulated_fill_cents"])
        - float(execution["fee_cents_per_contract"])
    ) * int(execution["contracts"])
    return "\n".join((
        "V21 PAPER",
        f"rule_version: {identity.ARTIFACT_VERSION}",
        f"cohort: {row['cohort']}",
        f"asset: {row['asset']}",
        f"ticker: {row['ticker']}",
        f"side: {row['side']}",
        f"candidate_probability: {probability:.6f}",
        f"v20_ablation_probability: {float(row['v20_ablation_survival_probability']):.6f}",
        f"entry_ask_cents: {float(row['entry_ask_cents']):.3f}",
        f"expected_value_after_costs_cents: {expected_value:.3f}",
        f"selected_margin: {float(row['selected_margin']):.6f}",
        f"decision_timestamp: {float(row['decision_timestamp']):.3f}",
        "paper_only_no_real_order: true",
    ))


class V21PaperLedger:
    def __init__(
        self, path: Path, *, cohort: str, artifact_sha256: str,
        artifact_created_at_unix: float, prospective_after_close_time: float,
    ) -> None:
        if cohort not in COHORT_ASSETS:
            raise ValueError("v21_paper_cohort_invalid")
        self.path = Path(path)
        self.cohort = cohort
        self.artifact_sha256 = _hash_text(artifact_sha256, "artifact_sha256")
        self.artifact_created_at_unix = _finite(
            artifact_created_at_unix, "artifact_created_at",
        )
        self.prospective_after_close_time = _finite(
            prospective_after_close_time, "prospective_boundary",
        )
        if self.prospective_after_close_time - 720.0 <= self.artifact_created_at_unix:
            raise ValueError("v21_paper_prospective_boundary_invalid")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _metadata(self) -> dict[str, Any]:
        return {
            "ledger_version": identity.LEDGER_VERSION,
            "paper_protocol_id": identity.PROTOCOL_ID,
            "paper_protocol_sha256": identity.PROTOCOL_SHA256,
            "artifact_version": identity.ARTIFACT_VERSION,
            "artifact_sha256": self.artifact_sha256,
            "cohort": self.cohort,
            "assets": sorted(COHORT_ASSETS[self.cohort]),
            "artifact_created_at_unix": self.artifact_created_at_unix,
            "prospective_after_close_time": self.prospective_after_close_time,
            "paper_only": True,
            "automatic_promotion": False,
            "real_trading_allowed": False,
        }

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            trigger_checks = " OR ".join(
                f"OLD.{column} IS NOT NEW.{column}" for column in IMMUTABLE_COLUMNS
            )
            connection.execute(f"""
                CREATE TRIGGER IF NOT EXISTS decisions_immutable_fields
                BEFORE UPDATE ON decisions
                WHEN {trigger_checks}
                BEGIN
                    SELECT RAISE(ABORT, 'v21_paper_immutable_decision_mutation');
                END
            """)
            expected = self._metadata()
            existing = {
                str(row["key"]): json.loads(str(row["value_json"]))
                for row in connection.execute("SELECT key, value_json FROM metadata")
            }
            if existing and existing != expected:
                raise ValueError("v21_paper_ledger_metadata_conflict")
            if not existing:
                connection.executemany(
                    "INSERT INTO metadata(key, value_json) VALUES (?, ?)",
                    [(key, _canonical_json(value)) for key, value in expected.items()],
                )

    def _increment(self, connection: sqlite3.Connection, name: str) -> None:
        connection.execute(
            """INSERT INTO counters(name, value) VALUES (?, 1)
               ON CONFLICT(name) DO UPDATE SET value=value+1""",
            (name,),
        )

    def _normalize_decision(self, value: Mapping[str, Any]) -> dict[str, Any]:
        cohort = str(value.get("cohort") or "")
        asset = str(value.get("asset") or "").upper()
        status = str(value.get("decision_status") or "")
        side = str(value.get("side") or "").upper()
        if cohort != self.cohort or asset not in COHORT_ASSETS[self.cohort]:
            raise ValueError("v21_paper_decision_cohort_asset_invalid")
        if status not in DECISION_STATUSES or side not in {"YES", "NO"}:
            raise ValueError("v21_paper_decision_status_or_side_invalid")
        ticker = str(value.get("ticker") or "")
        if not ticker or any(char.isspace() for char in ticker):
            raise ValueError("v21_paper_ticker_invalid")
        parent_id = int(value["parent_id"])
        intermediate_id = int(value["intermediate_id"])
        delayed_id = int(value["delayed_id"])
        if min(parent_id, intermediate_id, delayed_id) <= 0 or len({
            parent_id, intermediate_id, delayed_id,
        }) != 3:
            raise ValueError("v21_paper_source_lineage_invalid")
        key = decision_key(
            self.artifact_sha256, cohort, parent_id, intermediate_id, delayed_id,
        )
        if value.get("decision_key") not in (None, key):
            raise ValueError("v21_paper_decision_key_invalid")
        close_time = _finite(value.get("close_time"), "close_time")
        decision_timestamp = _finite(
            value.get("decision_timestamp"), "decision_timestamp",
        )
        if (
            close_time < self.prospective_after_close_time
            or close_time % 900.0 != 0.0
            or decision_timestamp <= self.artifact_created_at_unix
            or abs(decision_timestamp - (close_time - 720.0)) > 2.0
        ):
            raise ValueError("v21_paper_decision_not_prospective")
        probabilities = {
            name: _finite(value.get(name), name)
            for name in (
                "market_side_probability", "candidate_survival_probability",
                "v20_ablation_survival_probability",
            )
        }
        if any(not 0.0 <= probability <= 1.0 for probability in probabilities.values()):
            raise ValueError("v21_paper_probability_invalid")
        selected_margin = _finite(value.get("selected_margin"), "selected_margin")
        if selected_margin < 0.0:
            raise ValueError("v21_paper_selected_margin_invalid")
        entry = value.get("entry_ask_cents")
        spread = value.get("spread_cents")
        depth = value.get("displayed_depth_contracts")
        entry = None if entry is None else _finite(entry, "entry_ask_cents")
        spread = None if spread is None else _finite(spread, "spread_cents")
        depth = None if depth is None else _finite(depth, "displayed_depth_contracts")
        contracts = int(value.get("simulated_contracts") or 0)
        slippage = _finite(
            value.get("slippage_cents_per_contract"), "slippage",
        )
        fee_version = str(value.get("fee_schedule_version") or "")
        if contracts != 10 or slippage != 2.0 or fee_version != KALSHI_Q15_FEE_SCHEDULE_VERSION:
            raise ValueError("v21_paper_execution_contract_invalid")
        execution = (
            rti_simulated_execution(entry, contracts, slippage)
            if entry is not None else None
        )
        if status == ACCEPTED_PAPER:
            if (
                entry is None or not 0.0 <= entry <= 98.0
                or spread is None or spread < 0.0
                or depth is None or depth < 10.0
                or execution is None
            ):
                raise ValueError("v21_paper_accepted_fill_invalid")
            edge = probabilities["candidate_survival_probability"] - float(
                execution["fee_slippage_breakeven_rate"]
            )
            if edge + 1e-12 < selected_margin:
                raise ValueError("v21_paper_accepted_edge_invalid")
        created_at = _finite(value.get("created_at", time.time()), "created_at")
        if (
            created_at < decision_timestamp
            or created_at - decision_timestamp > 30.0
            or created_at >= close_time
        ):
            raise ValueError("v21_paper_historical_insert_forbidden")
        return {
            "decision_key": key,
            "artifact_sha256": self.artifact_sha256,
            "created_at": created_at,
            "cohort": cohort,
            "asset": asset,
            "ticker": ticker,
            "close_time": close_time,
            "decision_timestamp": decision_timestamp,
            "parent_id": parent_id,
            "intermediate_id": intermediate_id,
            "delayed_id": delayed_id,
            "source_feature_evidence_sha256": _hash_text(
                value.get("source_feature_evidence_sha256"), "source_feature_hash",
            ),
            "feature_evidence_sha256": _hash_text(
                value.get("feature_evidence_sha256"), "feature_hash",
            ),
            "feature_vector_sha256": _hash_text(
                value.get("feature_vector_sha256"), "feature_vector_hash",
            ),
            **probabilities,
            "side": side,
            "decision_status": status,
            "reason_codes": _reason_json(value.get("reason_codes", [])),
            "selected_margin": selected_margin,
            "entry_ask_cents": entry,
            "spread_cents": spread,
            "displayed_depth_contracts": depth,
            "simulated_contracts": contracts,
            "slippage_cents_per_contract": slippage,
            "fee_schedule_version": fee_version,
        }

    def insert_decision(
        self, value: Mapping[str, Any], *, notify: bool = False,
        message: str | None = None,
    ) -> dict[str, Any]:
        row = self._normalize_decision(value)
        columns = tuple(row)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "SELECT * FROM decisions WHERE decision_key=?", (row["decision_key"],),
            )
            existing = _row_dict(cursor, cursor.fetchone())
            if existing is not None:
                self._increment(connection, "duplicate_decision_attempts")
                if any(existing[column] != row[column] for column in columns):
                    self._increment(connection, "decision_identity_conflicts")
                    connection.commit()
                    raise ValueError("v21_paper_duplicate_decision_conflict")
                return existing
            placeholders = ",".join("?" for _ in columns)
            connection.execute(
                f"INSERT INTO decisions({','.join(columns)}) VALUES ({placeholders})",
                tuple(row[column] for column in columns),
            )
            if row["decision_status"] == ACCEPTED_PAPER:
                text = message if message is not None else build_notification_message(row)
                if "V21 PAPER" not in text or "paper_only_no_real_order" not in text:
                    raise ValueError("v21_paper_notification_message_invalid")
                connection.execute(
                    """INSERT INTO notification_outbox(
                           decision_key, idempotency_key, status, message,
                           created_at, expires_at
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        row["decision_key"],
                        notification_idempotency_key(row["decision_key"]),
                        "QUEUED" if notify else "MUTED",
                        text,
                        row["created_at"],
                        row["close_time"],
                    ),
                )
            cursor = connection.execute(
                "SELECT * FROM decisions WHERE decision_key=?", (row["decision_key"],),
            )
            return _row_dict(cursor, cursor.fetchone()) or {}

    def claim_notifications(
        self, *, owner: str, now: float | None = None,
        lease_seconds: float = 30.0, limit: int = 20,
    ) -> list[dict[str, Any]]:
        timestamp = _finite(time.time() if now is None else now, "claim_time")
        if not owner or lease_seconds <= 0 or limit <= 0:
            raise ValueError("v21_paper_notification_claim_invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE notification_outbox
                   SET status='EXPIRED', lease_owner=NULL, lease_until=NULL
                   WHERE status='QUEUED' AND expires_at<=?""",
                (timestamp,),
            )
            rows = connection.execute(
                """SELECT decision_key FROM notification_outbox
                   WHERE status='QUEUED' AND created_at<=? AND expires_at>? AND
                         (lease_until IS NULL OR lease_until<=?)
                   ORDER BY created_at, decision_key LIMIT ?""",
                (timestamp, timestamp, timestamp, int(limit)),
            ).fetchall()
            keys = [str(row[0]) for row in rows]
            for key in keys:
                connection.execute(
                    """UPDATE notification_outbox
                       SET lease_owner=?, lease_until=?, attempts=attempts+1
                       WHERE decision_key=? AND status='QUEUED'""",
                    (owner, timestamp + float(lease_seconds), key),
                )
            if not keys:
                return []
            placeholders = ",".join("?" for _ in keys)
            return [dict(row) for row in connection.execute(
                f"SELECT * FROM notification_outbox WHERE decision_key IN ({placeholders})",
                keys,
            ).fetchall()]

    def complete_notification(
        self, decision_key_value: str, *, owner: str, message_id: str,
        notified_at: float | None = None,
    ) -> dict[str, Any]:
        timestamp = _finite(
            time.time() if notified_at is None else notified_at, "notified_at",
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM notification_outbox WHERE decision_key=?",
                (decision_key_value,),
            ).fetchone()
            if row is None:
                raise ValueError("v21_paper_notification_missing")
            current = dict(row)
            if current["status"] == "SENT":
                if current["message_id"] == str(message_id):
                    return current
                raise ValueError("v21_paper_notification_terminal_conflict")
            if current["status"] != "QUEUED" or current["lease_owner"] != owner:
                raise ValueError("v21_paper_notification_lease_invalid")
            connection.execute(
                """UPDATE notification_outbox
                   SET status='SENT', message_id=?, notified_at=?, last_error=NULL,
                       lease_owner=NULL, lease_until=NULL
                   WHERE decision_key=?""",
                (str(message_id), timestamp, decision_key_value),
            )
            return dict(connection.execute(
                "SELECT * FROM notification_outbox WHERE decision_key=?",
                (decision_key_value,),
            ).fetchone())

    def fail_notification(
        self, decision_key_value: str, *, owner: str, error: str,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM notification_outbox WHERE decision_key=?",
                (decision_key_value,),
            ).fetchone()
            if row is None or row["status"] != "QUEUED" or row["lease_owner"] != owner:
                raise ValueError("v21_paper_notification_lease_invalid")
            status = "DEAD_LETTER" if int(row["attempts"]) >= int(max_attempts) else "QUEUED"
            connection.execute(
                """UPDATE notification_outbox
                   SET status=?, last_error=?, lease_owner=NULL, lease_until=NULL
                   WHERE decision_key=?""",
                (status, str(error)[:1000], decision_key_value),
            )
            return dict(connection.execute(
                "SELECT * FROM notification_outbox WHERE decision_key=?",
                (decision_key_value,),
            ).fetchone())

    def grade_decision(
        self, decision_key_value: str, *, result: str, market_status: str,
        returned_ticker: str, returned_close_time: float,
        fetched_at: float | None = None, source_id: str = "KALSHI_PUBLIC_MARKET_API",
    ) -> dict[str, Any]:
        official = str(result).upper()
        timestamp = _finite(time.time() if fetched_at is None else fetched_at, "settled_at")
        if official not in {"YES", "NO"} or market_status != "finalized" or source_id != "KALSHI_PUBLIC_MARKET_API":
            raise ValueError("v21_paper_settlement_evidence_invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM decisions WHERE decision_key=?", (decision_key_value,),
            ).fetchone()
            if row is None:
                raise ValueError("v21_paper_settlement_decision_missing")
            decision = dict(row)
            close_time = _finite(returned_close_time, "settlement_close_time")
            if (
                returned_ticker != decision["ticker"]
                or abs(close_time - float(decision["close_time"])) > 1.0
                or timestamp <= float(decision["close_time"])
            ):
                self._increment(connection, "settlement_identity_conflicts")
                connection.commit()
                raise ValueError("v21_paper_settlement_contract_mismatch")
            evidence = {
                "source_id": source_id,
                "market_status": market_status,
                "ticker": returned_ticker,
                "close_time": close_time,
                "result": official,
                "fetched_at": timestamp,
            }
            evidence_json = _canonical_json(evidence)
            evidence_sha = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()
            correct = official == decision["side"]
            pnl = None
            if decision["decision_status"] == ACCEPTED_PAPER:
                per_contract = rti_simulated_net_pnl_cents(
                    decision["entry_ask_cents"], correct,
                    int(decision["simulated_contracts"]),
                    decision["slippage_cents_per_contract"],
                )
                if per_contract is None:
                    raise ValueError("v21_paper_settlement_pnl_invalid")
                pnl = per_contract * int(decision["simulated_contracts"])
            if decision["official_result"] is not None:
                if (
                    decision["official_result"] == official
                    and decision["correct"] == int(correct)
                    and decision["fee_slippage_adjusted_pnl_cents"] == pnl
                ):
                    return decision
                self._increment(connection, "settlement_identity_conflicts")
                connection.commit()
                raise ValueError("v21_paper_settlement_overwrite_forbidden")
            connection.execute(
                """UPDATE decisions SET
                       official_result=?, settled_at=?, settlement_evidence_json=?,
                       settlement_evidence_sha256=?, correct=?,
                       fee_slippage_adjusted_pnl_cents=?
                   WHERE decision_key=? AND official_result IS NULL""",
                (
                    official, timestamp, evidence_json, evidence_sha,
                    int(correct), pnl, decision_key_value,
                ),
            )
            return dict(connection.execute(
                "SELECT * FROM decisions WHERE decision_key=?", (decision_key_value,),
            ).fetchone())

    def health(self, *, now: float | None = None) -> dict[str, Any]:
        timestamp = _finite(time.time() if now is None else now, "health_time")
        with self._connect() as connection:
            decisions = [dict(row) for row in connection.execute("SELECT * FROM decisions")]
            outbox = [dict(row) for row in connection.execute("SELECT * FROM notification_outbox")]
            counters = {
                str(row["name"]): int(row["value"])
                for row in connection.execute("SELECT name, value FROM counters")
            }
        status_counts = Counter(row["decision_status"] for row in decisions)
        notification_counts = Counter(row["status"] for row in outbox)
        pending = [
            row for row in decisions
            if row["official_result"] is None and row["close_time"] < timestamp
        ]
        resolved_accepted = sum(
            row["decision_status"] == ACCEPTED_PAPER and row["official_result"] is not None
            for row in decisions
        )
        next_bar = next((bar for bar in (30, 60, 150) if resolved_accepted < bar), None)
        return {
            "status": "DORMANT" if not decisions else "PAPER_LEDGER_ACTIVE",
            "ledger_version": identity.LEDGER_VERSION,
            "artifact_sha256": self.artifact_sha256,
            "cohort": self.cohort,
            "prospective_after_close_time": self.prospective_after_close_time,
            "decision_status_counts": dict(sorted(status_counts.items())),
            "notification_state_counts": dict(sorted(notification_counts.items())),
            "settlement_pending_count": len(pending),
            "settlement_oldest_age_seconds": (
                max(timestamp - float(row["close_time"]) for row in pending)
                if pending else None
            ),
            "settlement_conflict_count": counters.get("settlement_identity_conflicts", 0),
            "duplicate_decision_attempt_count": counters.get("duplicate_decision_attempts", 0),
            "decision_identity_conflict_count": counters.get("decision_identity_conflicts", 0),
            "resolved_accepted_pick_count": resolved_accepted,
            "next_manual_review_bar": next_bar,
            "paper_only": True,
            "automatic_promotion": False,
            "real_trading_allowed": False,
        }
