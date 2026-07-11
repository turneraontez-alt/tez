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
STATUSES = {
    "PENDING", "SENDING", "SENT", "FAILED_RETRYABLE", "DEAD_LETTER", "EXPIRED",
}


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
                    expires_at REAL,
                    last_attempt_at REAL,
                    sent_at REAL,
                    last_error TEXT
                )
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(q15_telegram_outbox)"
                ).fetchall()
            }
            if "expires_at" not in columns:
                connection.execute(
                    "ALTER TABLE q15_telegram_outbox ADD COLUMN expires_at REAL"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_q15_outbox_due ON q15_telegram_outbox(status, next_attempt_at)"
            )
            connection.commit()

    def recover(self) -> None:
        with self._connect() as connection:
            now = _now()
            connection.execute(
                "UPDATE q15_telegram_outbox SET status='EXPIRED', "
                "last_error='outbox:EXPIRED' WHERE expires_at IS NOT NULL "
                "AND expires_at<=? AND status IN ('PENDING','FAILED_RETRYABLE','SENDING')",
                (now,),
            )
            connection.execute(
                "UPDATE q15_telegram_outbox SET status='FAILED_RETRYABLE', next_attempt_at=? "
                "WHERE status='SENDING'",
                (now,),
            )
            connection.commit()

    def enqueue(self, record: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO q15_telegram_outbox (
                    idempotency_key, contract_id, checkpoint, alert_type, payload,
                    status, attempt_count, created_at, next_attempt_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    record["idempotency_key"], record.get("contract_id"),
                    record.get("checkpoint"), record.get("alert_type"),
                    record["payload"], record.get("status", "PENDING"),
                    record["created_at"], record["next_attempt_at"],
                    record.get("expires_at"),
                ),
            )
            if record.get("expires_at") is not None:
                connection.execute(
                    "UPDATE q15_telegram_outbox SET expires_at=COALESCE(expires_at, ?) "
                    "WHERE idempotency_key=?",
                    (record.get("expires_at"), record["idempotency_key"]),
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

    def expire(self, row_id: int) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE q15_telegram_outbox SET status='EXPIRED', "
                "last_error='outbox:EXPIRED' WHERE id=? AND status<>'SENT'",
                (int(row_id),),
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

    def status_by_key(self, idempotency_key: str) -> str | None:
        """Terminal/transient delivery status of one queued message, by key, or
        None if the key is unknown. Lets a caller credit a message as delivered
        from the outbox's TRUE outcome (SENT) — including a background-worker
        retry — instead of the synchronous first attempt."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM q15_telegram_outbox WHERE idempotency_key=?",
                (str(idempotency_key),),
            ).fetchone()
        return None if row is None else str(row["status"])

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
                expires_at DOUBLE PRECISION,
                last_attempt_at DOUBLE PRECISION,
                sent_at DOUBLE PRECISION,
                last_error TEXT
            )
            """
        )
        if ok is False:
            raise RuntimeError("Postgres outbox migration failed")
        self.store.execute(
            "ALTER TABLE q15_telegram_outbox ADD COLUMN IF NOT EXISTS "
            "expires_at DOUBLE PRECISION"
        )
        self.store.execute(
            "CREATE INDEX IF NOT EXISTS idx_q15_outbox_due ON q15_telegram_outbox(status, next_attempt_at)"
        )

    def recover(self) -> None:
        now = _now()
        self.store.execute(
            "UPDATE q15_telegram_outbox SET status='EXPIRED', "
            "last_error='outbox:EXPIRED' WHERE expires_at IS NOT NULL "
            "AND expires_at<=%s AND status IN ('PENDING','FAILED_RETRYABLE','SENDING')",
            (now,),
        )
        self.store.execute(
            "UPDATE q15_telegram_outbox SET status='FAILED_RETRYABLE', next_attempt_at=%s WHERE status='SENDING'",
            (now,),
        )

    def enqueue(self, record: Mapping[str, Any]) -> dict[str, Any]:
        self.store.execute(
            """
            INSERT INTO q15_telegram_outbox (
                idempotency_key, contract_id, checkpoint, alert_type, payload,
                status, attempt_count, created_at, next_attempt_at, expires_at
            ) VALUES (%s,%s,%s,%s,%s,%s,0,%s,%s,%s)
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            (
                record["idempotency_key"], record.get("contract_id"), record.get("checkpoint"),
                record.get("alert_type"), record["payload"],
                record.get("status", "PENDING"), record["created_at"],
                record["next_attempt_at"], record.get("expires_at"),
            ),
        )
        if record.get("expires_at") is not None:
            self.store.execute(
                "UPDATE q15_telegram_outbox SET expires_at=COALESCE(expires_at, %s) "
                "WHERE idempotency_key=%s",
                (record.get("expires_at"), record["idempotency_key"]),
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

    def expire(self, row_id: int) -> None:
        self.store.execute(
            "UPDATE q15_telegram_outbox SET status='EXPIRED', "
            "last_error='outbox:EXPIRED' WHERE id=%s AND status<>'SENT'",
            (int(row_id),),
        )

    def rows(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        if status:
            return self.store.query(
                "SELECT * FROM q15_telegram_outbox WHERE status=%s ORDER BY created_at DESC LIMIT %s",
                (status, limit),
            )
        return self.store.query("SELECT * FROM q15_telegram_outbox ORDER BY created_at DESC LIMIT %s", (limit,))

    def status_by_key(self, idempotency_key: str) -> str | None:
        """Delivery status of one queued message by key, or None if unknown."""
        rows = self.store.query(
            "SELECT status FROM q15_telegram_outbox WHERE idempotency_key=%s",
            (str(idempotency_key),),
        )
        if not rows:
            return None
        row = rows[0]
        return None if row.get("status") is None else str(row["status"])

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

    def _enqueue(
        self,
        text: str,
        idempotency_key: str | None,
        *,
        expires_at: float | None = None,
    ) -> dict[str, Any]:
        key = self._key(str(text), idempotency_key)
        contract_id, checkpoint, alert_type = self._metadata(str(text))
        expiry = None if expires_at is None else float(expires_at)
        now = _now()
        return self.backend.enqueue({
            "idempotency_key": key,
            "contract_id": contract_id,
            "checkpoint": checkpoint,
            "alert_type": alert_type,
            "payload": str(text),
            "status": "EXPIRED" if expiry is not None and expiry <= now else "PENDING",
            "created_at": now,
            "next_attempt_at": now,
            "expires_at": expiry,
        })

    def send(
        self,
        text: str,
        idempotency_key: str | None = None,
        *,
        expires_at: float | None = None,
        not_after: float | None = None,
        **_: Any,
    ) -> bool:
        if not self.enabled:
            return False
        expiry = expires_at if expires_at is not None else not_after
        row = self._enqueue(str(text), idempotency_key, expires_at=expiry)
        if not row:
            self.last_error = "outbox_enqueue_failed"
            return False
        if row.get("status") == "SENT":
            return True
        if row.get("status") == "EXPIRED":
            self.last_error = "outbox:EXPIRED"
            return False
        if not self.network_disabled:
            self._attempt(int(row["id"]))
        # True means durably accepted by the outbox, not necessarily delivered.
        return True

    def enqueue_with_result(
        self,
        text: str,
        idempotency_key: str | None = None,
        *,
        expires_at: float | None = None,
        not_after: float | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        """Persist a message without performing network I/O in the caller.

        This is the producer-side API for latency-sensitive refresh loops.  The
        background worker owns delivery.  A pre-existing ``SENT`` key is exposed
        as delivered so callers can reconcile their own durable ledger without
        creating another Telegram message.
        """
        base = {
            "ok": False,
            "delivered": False,
            "muted": False,
            "message_id": None,
            "error": None,
            "outbox_status": None,
        }
        if not self.enabled:
            return {**base, "muted": True, "error": "telegram_unconfigured"}
        expiry = expires_at if expires_at is not None else not_after
        row = self._enqueue(str(text), idempotency_key, expires_at=expiry)
        if not row:
            self.last_error = "outbox_enqueue_failed"
            return {**base, "error": self.last_error}
        status = str(row.get("status") or "") or None
        delivered = status == "SENT"
        return {
            **base,
            "ok": True,
            "delivered": delivered,
            "outbox_status": status,
        }

    def send_with_result(
        self,
        text: str,
        idempotency_key: str | None = None,
        *,
        expires_at: float | None = None,
        not_after: float | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        """Outbox-backed equivalent of ``TelegramNotifier.send_with_result``.

        Returns ``{ok, delivered, muted, message_id}`` describing the SYNCHRONOUS
        first attempt, while still enqueuing durably so the background worker
        retries on failure. The message_id is taken from the wrapped notifier's
        own result *inside* the locked attempt — never read back from shared
        notifier state afterwards — so a concurrent worker delivery cannot race it.

        Without this, callers that branch on ``send_with_result`` (the official
        interval report) fall back to reading ``last_message_id`` off the outbox,
        which it does not have, so a real delivery is mis-scored as a failure and
        the official record / Shadow-vs-Yours native side never fill.
        """
        base = {"ok": False, "delivered": False, "muted": False, "message_id": None}
        if not self.enabled:
            return dict(base)
        expiry = expires_at if expires_at is not None else not_after
        row = self._enqueue(str(text), idempotency_key, expires_at=expiry)
        if not row:
            self.last_error = "outbox_enqueue_failed"
            return dict(base)
        if row.get("status") == "SENT":
            # Idempotent dedup: an identical message already delivered. Handled, but
            # there is no fresh message_id to attach for this call.
            return {"ok": True, "delivered": True, "muted": False, "message_id": None}
        if row.get("status") == "EXPIRED":
            self.last_error = "outbox:EXPIRED"
            return {
                "ok": False,
                "delivered": False,
                "muted": False,
                "message_id": None,
                "error": self.last_error,
                "outbox_status": "EXPIRED",
            }
        if self.network_disabled:
            # Durably queued; the worker is disabled, so nothing was attempted now.
            return {"ok": True, "delivered": False, "muted": False, "message_id": None}
        res = self._attempt_result(int(row["id"]))
        return {"ok": bool(res.get("ok")), "delivered": bool(res.get("delivered")),
                "muted": bool(res.get("muted")), "message_id": res.get("message_id")}

    def _raw_send(self, payload: str) -> dict[str, Any]:
        """Deliver one payload through the wrapped notifier, normalized to
        ``{ok, delivered, muted, message_id, error}``.

        Prefers the rich ``send_with_result`` interface (which carries the Telegram
        message_id) and falls back to ``send`` for legacy notifiers.  Some legacy
        adapters return the same structured mapping as the rich interface rather
        than a bool; those mappings must be interpreted by their fields, not by
        container truthiness (a non-empty failure dict is still a failure)."""
        token = getattr(self.raw, "token", None)
        if hasattr(self.raw, "send_with_result"):
            try:
                result = self.raw.send_with_result(payload)
            except Exception as exc:
                return {"ok": False, "delivered": False, "muted": False,
                        "message_id": None, "error": _safe_error(exc, token)}
            return self._normalize_send_result(result, token)
        try:
            result = self.raw.send(payload)
        except Exception as exc:
            return {"ok": False, "delivered": False, "muted": False,
                    "message_id": None, "error": _safe_error(exc, token)}
        return self._normalize_send_result(result, token)

    def _normalize_send_result(self, result: Any, token: str | None) -> dict[str, Any]:
        """Normalize rich mappings and legacy booleans without inventing delivery.

        ``ok`` is the outbox's *handled* bit: a real delivery or an intentional
        mute completes the row, while accepted-but-not-delivered results remain
        retryable.  For old bool-only notifiers, ``True`` retains its historical
        meaning of a delivered send without a proof id.
        """
        if isinstance(result, Mapping):
            has_delivery_contract = "delivered" in result or "muted" in result
            delivered = bool(result.get("delivered"))
            muted = bool(result.get("muted"))
            if has_delivery_contract:
                handled = delivered or muted
            else:
                # Mapping-only legacy adapters sometimes expose just {ok: ...}.
                # Preserve their bool semantics, including delivery without an id.
                handled = bool(result.get("ok"))
                delivered = handled
            message_id = result.get("message_id")
            if message_id is None and delivered:
                message_id = getattr(self.raw, "last_message_id", None)
            raw_error = result.get("error") or getattr(self.raw, "last_error", None)
            return {
                "ok": handled,
                "delivered": delivered,
                "muted": muted,
                "message_id": message_id,
                "error": None if handled else _safe_error(raw_error, token),
            }

        delivered = bool(result)
        return {
            "ok": delivered,
            "delivered": delivered,
            "muted": False,
            "message_id": getattr(self.raw, "last_message_id", None) if delivered else None,
            "error": (
                None
                if delivered
                else _safe_error(getattr(self.raw, "last_error", None), token)
            ),
        }

    def _attempt(self, row_id: int | None = None) -> bool:
        # True when a claimed row was handled (delivered or an intentional mute);
        # False when nothing was due or the attempt failed (so the worker backs off).
        return bool(self._attempt_result(row_id).get("ok"))

    def _attempt_result(self, row_id: int | None = None) -> dict[str, Any]:
        with self._delivery_lock:
            row = self.backend.claim(row_id)
            if not row:
                return {"ok": False, "delivered": False, "muted": False, "message_id": None}
            expires_at = row.get("expires_at")
            if expires_at is not None and _now() >= float(expires_at):
                self.backend.expire(int(row["id"]))
                self.last_error = "outbox:EXPIRED"
                return {
                    "ok": True,
                    "delivered": False,
                    "muted": False,
                    "message_id": None,
                    "error": self.last_error,
                    "expired": True,
                    "outbox_status": "EXPIRED",
                }
            attempts_before = int(row.get("attempt_count") or 0)
            res = self._raw_send(row["payload"])
            if res["ok"]:
                # Handled (delivered OR an intentional mute) -> durably done, no retry.
                self.backend.complete(int(row["id"]), True, None, None, False)
                self.sent_count = int(getattr(self.raw, "sent_count", self.sent_count + 1) or self.sent_count + 1)
                self.last_sent_at = getattr(self.raw, "last_sent_at", _now())
                self.last_error = None
                return res
            attempt_number = attempts_before + 1
            dead = attempt_number >= self.max_attempts
            backoff = self.BACKOFF_SECONDS[min(attempt_number - 1, len(self.BACKOFF_SECONDS) - 1)]
            self.backend.complete(int(row["id"]), False, res.get("error"), _now() + backoff, dead)
            self.last_error = res.get("error")
            return res

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

    def status_by_key(self, idempotency_key: str) -> str | None:
        """True delivery status of a previously-enqueued message, by key. Used to
        credit the Shadow-vs-Yours native record from the outbox's actual outcome
        (a sync OR background-worker delivery), not just the synchronous attempt.
        Returns None when disabled or the key is unknown; never raises."""
        if not self.enabled:
            return None
        try:
            return self.backend.status_by_key(str(idempotency_key))
        except Exception as exc:
            self.last_error = _safe_error(exc, getattr(self.raw, "token", None))
            return None

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
