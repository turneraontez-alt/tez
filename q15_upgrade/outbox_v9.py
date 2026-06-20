"""Persistent, retryable Telegram outbox for Q15 V9.

The outbox marks a message SENT only after Telegram confirms delivery.  It is
read-only with respect to trading and contains no exchange-order code.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping

VERSION = "q15-v9-telegram-outbox"
LOGGER = logging.getLogger(__name__)
STATUSES = {"PENDING", "SENDING", "SENT", "FAILED_RETRYABLE", "DEAD_LETTER"}


def _now() -> float:
    return time.time()


def _safe_error(value: Any, token: str | None = None) -> str:
    text = str(value or "unknown delivery error")[:1000]
    if token:
        text = text.replace(token, "***")
    return text


class _SQLiteBackend:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=20.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _migrate(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS q15_telegram_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    contract_id TEXT,
                    checkpoint TEXT,
                    alert_type TEXT,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    next_attempt_at REAL NOT NULL,
                    last_attempt_at REAL,
                    sent_at REAL,
                    last_error TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_q15_outbox_due ON q15_telegram_outbox(status, next_attempt_at)"
            )
            connection.commit()

    def recover(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE q15_telegram_outbox SET status='FAILED_RETRYABLE', next_attempt_at=? "
                "WHERE status='SENDING'",
                (_now(),),
            )
            connection.commit()

    def enqueue(self, record: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO q15_telegram_outbox (
                    idempotency_key, contract_id, checkpoint, alert_type, payload,
                    status, attempt_count, created_at, next_attempt_at
                ) VALUES (?, ?, ?, ?, ?, 'PENDING', 0, ?, ?)
                """,
                (
                    record["idempotency_key"], record.get("contract_id"),
                    record.get("checkpoint"), record.get("alert_type"),
                    record["payload"], record["created_at"], record["next_attempt_at"],
                ),
            )
            row = connection.execute(
                "SELECT * FROM q15_telegram_outbox WHERE idempotency_key=?",
                (record["idempotency_key"],),
            ).fetchone()
            connection.commit()
        return dict(row) if row else {}

    def claim(self, row_id: int | None = None) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if row_id is None:
                row = connection.execute(
                    "SELECT * FROM q15_telegram_outbox WHERE status IN ('PENDING','FAILED_RETRYABLE') "
                    "AND next_attempt_at<=? ORDER BY created_at ASC LIMIT 1",
                    (_now(),),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM q15_telegram_outbox WHERE id=? AND status IN ('PENDING','FAILED_RETRYABLE')",
                    (row_id,),
                ).fetchone()
            if not row:
                connection.rollback()
                return None
            changed = connection.execute(
                "UPDATE q15_telegram_outbox SET status='SENDING', last_attempt_at=? "
                "WHERE id=? AND status IN ('PENDING','FAILED_RETRYABLE')",
                (_now(), row["id"]),
            ).rowcount
            if changed != 1:
                connection.rollback()
                return None
            connection.commit()
            result = dict(row)
            result["status"] = "SENDING"
            return result

    def complete(self, row_id: int, success: bool, error: str | None, next_attempt: float | None, dead: bool) -> None:
        with self._lock, self._connect() as connection:
            if success:
                connection.execute(
                    "UPDATE q15_telegram_outbox SET status='SENT', attempt_count=attempt_count+1, "
                    "sent_at=?, last_error=NULL WHERE id=?",
                    (_now(), row_id),
                )
            else:
                status = "DEAD_LETTER" if dead else "FAILED_RETRYABLE"
                connection.execute(
                    "UPDATE q15_telegram_outbox SET status=?, attempt_count=attempt_count+1, "
                    "next_attempt_at=?, last_error=? WHERE id=?",
                    (status, float(next_attempt or _now()), error, row_id),
                )
            connection.commit()

    def rows(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM q15_telegram_outbox WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM q15_telegram_outbox ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(row) for row in rows]

    def health(self) -> dict[str, Any]:
        with self._connect() as connection:
            counts = {
                row["status"]: int(row["n"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS n FROM q15_telegram_outbox GROUP BY status"
                ).fetchall()
            }
            oldest = connection.execute(
                "SELECT MIN(created_at) AS oldest FROM q15_telegram_outbox "
                "WHERE status IN ('PENDING','FAILED_RETRYABLE','SENDING')"
            ).fetchone()
            last_sent = connection.execute(
                "SELECT MAX(sent_at) AS last_sent FROM q15_telegram_outbox WHERE status='SENT'"
            ).fetchone()
        return {
            "counts": counts,
            "oldest_pending_age_seconds": None if not oldest or oldest["oldest"] is None else max(0.0, _now() - oldest["oldest"]),
            "last_successful_send": None if not last_sent else last_sent["last_sent"],
            "backend": "sqlite",
        }


class _PostgresBackend:
    def __init__(self, store: Any):
        self.store = store
        self._migrate()

    def _migrate(self) -> None:
        ok = self.store.execute(
            """
            CREATE TABLE IF NOT EXISTS q15_telegram_outbox (
                id BIGSERIAL PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                contract_id TEXT,
                checkpoint TEXT,
                alert_type TEXT,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                created_at DOUBLE PRECISION NOT NULL,
                next_attempt_at DOUBLE PRECISION NOT NULL,
                last_attempt_at DOUBLE PRECISION,
                sent_at DOUBLE PRECISION,
                last_error TEXT
            )
            """
        )
        if ok is False:
            raise RuntimeError("Postgres outbox migration failed")
        self.store.execute(
            "CREATE INDEX IF NOT EXISTS idx_q15_outbox_due ON q15_telegram_outbox(status, next_attempt_at)"
        )

    def recover(self) -> None:
        self.store.execute(
            "UPDATE q15_telegram_outbox SET status='FAILED_RETRYABLE', next_attempt_at=%s WHERE status='SENDING'",
            (_now(),),
        )

    def enqueue(self, record: Mapping[str, Any]) -> dict[str, Any]:
        self.store.execute(
            """
            INSERT INTO q15_telegram_outbox (
                idempotency_key, contract_id, checkpoint, alert_type, payload,
                status, attempt_count, created_at, next_attempt_at
            ) VALUES (%s,%s,%s,%s,%s,'PENDING',0,%s,%s)
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            (
                record["idempotency_key"], record.get("contract_id"), record.get("checkpoint"),
                record.get("alert_type"), record["payload"], record["created_at"], record["next_attempt_at"],
            ),
        )
        rows = self.store.query(
            "SELECT * FROM q15_telegram_outbox WHERE idempotency_key=%s LIMIT 1",
            (record["idempotency_key"],),
        )
        return dict(rows[0]) if rows else {}

    def claim(self, row_id: int | None = None) -> dict[str, Any] | None:
        if row_id is None:
            rows = self.store.query(
                """
                UPDATE q15_telegram_outbox SET status='SENDING', last_attempt_at=%s
                WHERE id=(
                    SELECT id FROM q15_telegram_outbox
                    WHERE status IN ('PENDING','FAILED_RETRYABLE') AND next_attempt_at<=%s
                    ORDER BY created_at ASC FOR UPDATE SKIP LOCKED LIMIT 1
                ) AND status IN ('PENDING','FAILED_RETRYABLE')
                RETURNING *
                """,
                (_now(), _now()),
            )
        else:
            rows = self.store.query(
                "UPDATE q15_telegram_outbox SET status='SENDING', last_attempt_at=%s "
                "WHERE id=%s AND status IN ('PENDING','FAILED_RETRYABLE') RETURNING *",
                (_now(), row_id),
            )
        return dict(rows[0]) if rows else None

    def complete(self, row_id: int, success: bool, error: str | None, next_attempt: float | None, dead: bool) -> None:
        if success:
            self.store.execute(
                "UPDATE q15_telegram_outbox SET status='SENT', attempt_count=attempt_count+1, sent_at=%s, last_error=NULL WHERE id=%s",
                (_now(), row_id),
            )
        else:
            self.store.execute(
                "UPDATE q15_telegram_outbox SET status=%s, attempt_count=attempt_count+1, next_attempt_at=%s, last_error=%s WHERE id=%s",
                ("DEAD_LETTER" if dead else "FAILED_RETRYABLE", float(next_attempt or _now()), error, row_id),
            )

    def rows(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        if status:
            return self.store.query(
                "SELECT * FROM q15_telegram_outbox WHERE status=%s ORDER BY created_at DESC LIMIT %s",
                (status, limit),
            )
        return self.store.query("SELECT * FROM q15_telegram_outbox ORDER BY created_at DESC LIMIT %s", (limit,))

    def health(self) -> dict[str, Any]:
        rows = self.store.query("SELECT status, COUNT(*) AS n FROM q15_telegram_outbox GROUP BY status")
        counts = {str(row.get("status")): int(row.get("n") or 0) for row in rows}
        oldest = self.store.query(
            "SELECT MIN(created_at) AS oldest FROM q15_telegram_outbox WHERE status IN ('PENDING','FAILED_RETRYABLE','SENDING')"
        )
        sent = self.store.query("SELECT MAX(sent_at) AS last_sent FROM q15_telegram_outbox WHERE status='SENT'")
        oldest_value = oldest[0].get("oldest") if oldest else None
        return {
            "counts": counts,
            "oldest_pending_age_seconds": None if oldest_value is None else max(0.0, _now() - float(oldest_value)),
            "last_successful_send": sent[0].get("last_sent") if sent else None,
            "backend": "postgres",
        }


class ReliableTelegramOutbox:
    """Notifier-compatible persistent outbox with bounded retry."""

    BACKOFF_SECONDS = (30.0, 60.0, 120.0, 300.0, 600.0)

    def __init__(self, store: Any, raw_notifier: Any, sqlite_path: str | None = None):
        self.raw = raw_notifier
        self.store = store
        self.enabled = bool(getattr(raw_notifier, "enabled", False))
        self.max_attempts = max(1, int(os.getenv("Q15_V9_OUTBOX_MAX_ATTEMPTS", "6")))
        self.worker_enabled = os.getenv("Q15_V9_OUTBOX_WORKER", "true").strip().lower() in {"1", "true", "yes", "on"}
        self.network_disabled = os.getenv("Q15_V9_DISABLE_NETWORK", "false").strip().lower() in {"1", "true", "yes", "on"}
        self._stop = threading.Event()
        self._delivery_lock = threading.RLock()
        self.last_error = None
        self.last_sent_at = getattr(raw_notifier, "last_sent_at", None)
        self.sent_count = int(getattr(raw_notifier, "sent_count", 0) or 0)
        try:
            if bool(getattr(store, "enabled", False)) and hasattr(store, "execute") and hasattr(store, "query"):
                self.backend: Any = _PostgresBackend(store)
            else:
                path = sqlite_path or os.getenv("Q15_V9_OUTBOX_SQLITE_PATH", "data/q15_telegram_outbox.sqlite3")
                self.backend = _SQLiteBackend(path)
        except Exception as exc:
            LOGGER.error("Postgres outbox unavailable; using SQLite fallback: %s", exc)
            path = sqlite_path or os.getenv("Q15_V9_OUTBOX_SQLITE_PATH", "data/q15_telegram_outbox.sqlite3")
            self.backend = _SQLiteBackend(path)
        self.backend.recover()
        self._worker: threading.Thread | None = None
        if self.worker_enabled and self.enabled and not self.network_disabled:
            self._worker = threading.Thread(target=self._run, name="q15-telegram-outbox", daemon=True)
            self._worker.start()

    def status(self) -> str:
        raw_status = self.raw.status() if hasattr(self.raw, "status") else ("configured" if self.enabled else "disabled")
        if not self.enabled:
            return str(raw_status)
        health = self.health()
        retryable = int((health.get("counts") or {}).get("FAILED_RETRYABLE", 0))
        dead = int((health.get("counts") or {}).get("DEAD_LETTER", 0))
        if dead:
            return "configured_with_dead_letters"
        if retryable:
            return "configured_retrying"
        return "configured_outbox"

    def _key(self, text: str, supplied: str | None) -> str:
        if supplied:
            return str(supplied)[:500]
        digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
        # Five-minute bucket prevents endless duplicate routine messages while
        # allowing a materially later update with the same body.
        return f"auto:{int(_now() // 300)}:{digest}"

    @staticmethod
    def _metadata(text: str) -> tuple[str | None, str | None, str | None]:
        upper = text.upper()
        checkpoint = "10M" if "10M" in upper else "15M" if "15M" in upper else None
        alert_type = "ENTRY" if "ENTRY" in upper else "WATCH" if "WATCH" in upper else "REPORT" if "REPORT" in upper else "MESSAGE"
        contract_id = None
        return contract_id, checkpoint, alert_type

    def send(self, text: str, idempotency_key: str | None = None, **_: Any) -> bool:
        if not self.enabled:
            return False
        key = self._key(str(text), idempotency_key)
        contract_id, checkpoint, alert_type = self._metadata(str(text))
        row = self.backend.enqueue({
            "idempotency_key": key,
            "contract_id": contract_id,
            "checkpoint": checkpoint,
            "alert_type": alert_type,
            "payload": str(text),
            "created_at": _now(),
            "next_attempt_at": _now(),
        })
        if not row:
            self.last_error = "outbox_enqueue_failed"
            return False
        if row.get("status") == "SENT":
            return True
        if not self.network_disabled:
            self._attempt(int(row["id"]))
        # True means durably accepted by the outbox, not necessarily delivered.
        return True

    def _attempt(self, row_id: int | None = None) -> bool:
        with self._delivery_lock:
            row = self.backend.claim(row_id)
            if not row:
                return False
            attempts_before = int(row.get("attempt_count") or 0)
            try:
                delivered = bool(self.raw.send(row["payload"]))
                error = None if delivered else _safe_error(getattr(self.raw, "last_error", None), getattr(self.raw, "token", None))
            except Exception as exc:
                delivered = False
                error = _safe_error(exc, getattr(self.raw, "token", None))
            if delivered:
                self.backend.complete(int(row["id"]), True, None, None, False)
                self.sent_count = int(getattr(self.raw, "sent_count", self.sent_count + 1) or self.sent_count + 1)
                self.last_sent_at = getattr(self.raw, "last_sent_at", _now())
                self.last_error = None
                return True
            attempt_number = attempts_before + 1
            dead = attempt_number >= self.max_attempts
            backoff = self.BACKOFF_SECONDS[min(attempt_number - 1, len(self.BACKOFF_SECONDS) - 1)]
            self.backend.complete(int(row["id"]), False, error, _now() + backoff, dead)
            self.last_error = error
            return False

    def _run(self) -> None:
        while not self._stop.wait(2.0):
            try:
                processed = self._attempt(None)
                if not processed:
                    self._stop.wait(3.0)
            except Exception as exc:
                self.last_error = _safe_error(exc, getattr(self.raw, "token", None))
                LOGGER.error("Telegram outbox worker error: %s", self.last_error)
                self._stop.wait(5.0)

    def close(self) -> None:
        self._stop.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=2.0)

    def rows(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.backend.rows(limit=limit)

    def dead_letters(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.backend.rows(status="DEAD_LETTER", limit=limit)

    def health(self) -> dict[str, Any]:
        health = self.backend.health()
        health.update({
            "version": VERSION,
            "enabled": self.enabled,
            "worker_enabled": self.worker_enabled,
            "network_disabled": self.network_disabled,
            "max_attempts": self.max_attempts,
            "last_delivery_error": self.last_error,
            "read_only": True,
        })
        return health
