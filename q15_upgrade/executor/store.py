"""Ultoim V2 EXECUTOR — order/fill recorder (durable; answers "how many orders missed").

The executor places a GTC limit at the snapshot ask and OPTIMISTICALLY books the position in
memory; nothing was persisted, so "did it actually fill?" was unanswerable from our side. This
records EVERY placement with the RAW broker response plus a best-effort fill classification,
against the real Kalshi schema the repo already relies on (see scripts/exec_preflight.py):
``data[.order].{status, fill_count, order_id}``. The raw response is ALWAYS stored, so a wrong
classification can be re-derived later. Recording is best-effort: a store failure NEVER disrupts
the order path (every public method swallows its own exceptions).

IMPORTANT — immediate vs final: ``classify_fill`` reads the POST-time response, so ``RESTED``
means "not filled AT PLACEMENT" — a resting limit can still fill later in the 15-min window.
Only a later reconcile / the account's settled order history tells final "missed". The live
account view (``exec_preflight.py --fills``) is the source of truth for FINAL misses; this store
is the immediate-fill + signal-correlated (interval/asset/window) record.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Any, Mapping

logger = logging.getLogger("q15.executor.store")

# Immediate (POST-time) fill classification labels.
FILLED, PARTIAL, RESTED, CANCELED, FAILED, DRY_RUN, UNKNOWN = (
    "FILLED", "PARTIAL", "RESTED", "CANCELED", "FAILED", "DRY_RUN", "UNKNOWN")
# The POST failed in a way that does NOT prove the order was rejected (read
# timeout, mid-flight connection drop). The order may be live at Kalshi. Kept
# distinct from FAILED so reconciliation can find exactly these rows.
UNCERTAIN = "UNCERTAIN"
# Labels that mean "no (full) fill" — what "missed" counts.
MISSED_LABELS = (RESTED, CANCELED)


def _inner_order(data: Any) -> Mapping[str, Any]:
    """Kalshi V2 may return {"order": {...}} or a flat object — normalize to the order dict."""
    if isinstance(data, Mapping):
        inner = data.get("order")
        if isinstance(inner, Mapping):
            return inner
        return data
    return {}


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def classify_fill(res: Mapping[str, Any], requested_count: int) -> tuple[str, int | None, str | None]:
    """Best-effort IMMEDIATE fill classification from a ``place_order`` result.
    Returns ``(label, filled_count|None, order_id|None)``. NEVER raises. Uses the real Kalshi
    schema (data[.order].{status, fill_count, order_id}); any unexpected shape -> UNKNOWN."""
    try:
        if not res.get("ok"):
            # An ambiguous transport failure is not a rejection — the order may
            # be live. Report 0 filled (we genuinely do not know) but label it so
            # it can be reconciled against the account rather than written off.
            return (UNCERTAIN if res.get("uncertain") else FAILED), 0, None
        if res.get("dry_run"):
            return DRY_RUN, None, None
        order = _inner_order(res.get("data"))
        oid = order.get("order_id") if order.get("order_id") is not None else order.get("id")
        oid = str(oid) if oid is not None else None
        filled = _num(order.get("fill_count"))
        status = str(order.get("status") or "").lower()
        req = int(requested_count) if requested_count else 0
        if filled is not None:
            fc = int(filled)
            if req and fc >= req:
                return FILLED, fc, oid
            if fc > 0:
                return PARTIAL, fc, oid
            if status in ("canceled", "cancelled", "expired"):
                return CANCELED, 0, oid
            return RESTED, 0, oid
        # No fill_count field — fall back to the textual status.
        if status in ("executed", "filled", "matched"):
            return FILLED, None, oid
        if status in ("canceled", "cancelled", "expired"):
            return CANCELED, None, oid
        if status in ("resting", "pending", "open", "live"):
            return RESTED, None, oid
        return UNKNOWN, None, oid
    except Exception:  # noqa: BLE001 - classification must never break the order path
        return UNKNOWN, None, None


def _count(value: Any) -> int | None:
    num = _num(value)
    return int(num) if num is not None else None


def _first_num(order: Mapping[str, Any], *names: str) -> float | None:
    for name in names:
        value = _num(order.get(name))
        if value is not None:
            return value
    return None


def classify_final_order(order: Mapping[str, Any], requested_count: int) -> dict[str, Any]:
    """Best-effort FINAL order classification from account order history.

    This is intentionally schema-tolerant: Kalshi account history has used both
    count/fill-count naming variants. Unknown shapes stay UNKNOWN instead of
    being promoted to a fill.
    """
    try:
        data = _inner_order(order)
        status = str(data.get("status") or "").lower()
        filled = _count(
            data.get("fill_count")
            if data.get("fill_count") is not None
            else data.get("filled_count")
        )
        remaining = _count(
            data.get("remaining_count")
            if data.get("remaining_count") is not None
            else data.get("remaining_contracts")
        )
        req = int(requested_count) if requested_count else 0
        if filled is None and remaining is not None and req:
            filled = max(0, req - remaining)

        if filled is not None:
            if req and filled >= req:
                fill_status = FILLED
            elif filled > 0:
                fill_status = PARTIAL
            elif status in {"canceled", "cancelled", "expired"}:
                fill_status = CANCELED
            elif status in {"resting", "pending", "open", "live"}:
                fill_status = RESTED
            else:
                fill_status = UNKNOWN
        elif status in {"executed", "filled", "matched"}:
            fill_status = FILLED
        elif status in {"canceled", "cancelled", "expired"}:
            fill_status = CANCELED
        elif status in {"resting", "pending", "open", "live"}:
            fill_status = RESTED
        else:
            fill_status = UNKNOWN

        return {
            "status": status or None,
            "fill_status": fill_status,
            "filled_count": filled,
            "remaining_count": remaining,
            "average_fill_price": _first_num(
                data,
                "average_fill_price",
                "avg_fill_price",
                "average_price",
                "avg_price",
            ),
            "average_fee_paid": _first_num(
                data,
                "average_fee_paid",
                "avg_fee_paid",
                "fee_paid",
                "fees",
            ),
        }
    except Exception:  # noqa: BLE001 - final reconciliation must be fail-safe
        return {
            "status": None,
            "fill_status": UNKNOWN,
            "filled_count": None,
            "remaining_count": None,
            "average_fill_price": None,
            "average_fee_paid": None,
        }


_COLS = (
    "created_at", "action", "ticker", "asset", "interval", "window_key",
    "client_order_id", "order_id", "requested_count", "filled_count",
    "limit_price_cents", "stake_cents", "mode", "http_ok", "http_status",
    "fill_status", "snapshot_age_ms", "balance_latency_ms", "order_latency_ms",
    "response_json",
)

_FINAL_COLS = {
    "final_status": "TEXT",
    "final_fill_status": "TEXT",
    "final_filled_count": "INTEGER",
    "final_remaining_count": "INTEGER",
    "final_average_fill_price": "REAL",
    "final_average_fee_paid": "REAL",
    "final_reconciled_at": "REAL",
    "final_response_json": "TEXT",
}


class ExecutorStore:
    """Durable sqlite log of every order the executor placed + its (immediate) fill outcome."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS executor_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL, action TEXT, ticker TEXT, asset TEXT, interval TEXT,
                window_key INTEGER, client_order_id TEXT, order_id TEXT,
                requested_count INTEGER, filled_count INTEGER, limit_price_cents INTEGER,
                stake_cents INTEGER, mode TEXT, http_ok INTEGER, http_status INTEGER,
                fill_status TEXT, snapshot_age_ms REAL, balance_latency_ms REAL,
                order_latency_ms REAL, response_json TEXT)""")
        # Small durable key/value side table. Holds the daily-stop reference
        # (day-start balance + its UTC date) so the circuit breaker survives a
        # process restart instead of re-arming at the drawn-down balance.
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS executor_meta (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at REAL)""")
        self._ensure_columns()
        self._conn.commit()

    # -- durable key/value -------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        """Return a stored meta value, or None (never raises)."""
        try:
            row = self._conn.execute(
                "SELECT value FROM executor_meta WHERE key=?", (str(key),)).fetchone()
        except Exception:  # noqa: BLE001 - best-effort; caller falls back to defaults
            logger.exception("executor meta read failed (ignored)")
            return None
        return None if row is None or row[0] is None else str(row[0])

    def set_meta(self, key: str, value: str) -> None:
        """Upsert a meta value (never raises)."""
        try:
            import time as _time
            self._conn.execute(
                "INSERT INTO executor_meta (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (str(key), str(value), _time.time()))
            self._conn.commit()
        except Exception:  # noqa: BLE001 - best-effort; never disrupt the executor
            logger.exception("executor meta write failed (ignored)")

    def recent_entry_orders(self, since_seconds: float = 7200.0,
                            mode: str | None = None) -> list[dict[str, Any]]:
        """Most recent SUCCESSFUL entry placement per (window_key, ticker).

        Feeds position rehydrate after a restart: without it ``state.positions``
        comes back empty and every defensive exit refuses with NO_POSITION while
        a real, funded position rides to settlement. Carries ``order_id`` so a
        still-resting entry can be CANCELLED rather than only sold against.

        ``mode`` ("LIVE" / "dry-run") restricts the rows to placements made in the
        SAME posture the caller is running in now, so a book that was flipped from
        dry-run to live does not inherit simulated positions (or the reverse).

        Rows are returned oldest-first so a caller building a dict naturally
        keeps the newest. Best-effort: any failure returns []."""
        try:
            import time as _time
            cutoff = _time.time() - float(since_seconds)
            sql = ("SELECT window_key, ticker, order_id, requested_count, filled_count, "
                   "fill_status, limit_price_cents, mode, created_at "
                   "FROM executor_orders "
                   "WHERE action='entry' AND http_ok=1 AND created_at >= ? "
                   "AND window_key IS NOT NULL AND ticker IS NOT NULL")
            args: tuple = (cutoff,)
            if mode is not None:
                sql += " AND mode=?"
                args = (cutoff, str(mode))
            rows = self._conn.execute(sql + " ORDER BY created_at ASC", args).fetchall()
        except Exception:  # noqa: BLE001 - best-effort; never block executor init
            logger.exception("recent_entry_orders failed (position rehydrate skipped)")
            return []
        out: list[dict[str, Any]] = []
        for wk, tk, oid, req, filled, status, px, mode, created in rows:
            try:
                window_key = int(wk)
            except (TypeError, ValueError):
                continue
            out.append({
                "window_key": window_key,
                "ticker": str(tk),
                "order_id": None if oid is None else str(oid),
                "requested_count": _count(req) or 0,
                "filled_count": _count(filled),
                "fill_status": None if status is None else str(status),
                "limit_price_cents": _count(px),
                "mode": None if mode is None else str(mode),
                "created_at": created,
            })
        return out

    def _ensure_columns(self) -> None:
        """Migrate old local order stores without disturbing existing immediate-fill rows."""
        existing = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(executor_orders)").fetchall()
        }
        for name, column_type in _FINAL_COLS.items():
            if name not in existing:
                try:
                    self._conn.execute(
                        f"ALTER TABLE executor_orders ADD COLUMN {name} {column_type}"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise

    def record(self, **fields: Any) -> None:
        """Insert one order row (parameterized). Best-effort — swallows its own errors."""
        try:
            vals = [fields.get(c) for c in _COLS]
            self._conn.execute(
                f"INSERT INTO executor_orders ({','.join(_COLS)}) "
                f"VALUES ({','.join('?' * len(_COLS))})", vals)
            self._conn.commit()
        except Exception:  # noqa: BLE001 - recording is best-effort; never disrupt the order
            logger.exception("executor order record failed (ignored)")

    def recent_window_entries(self, since_seconds: float = 7200.0) -> dict[int, set[str]]:
        """Per settlement ``window_key``, the set of tickers with a SUCCESSFUL entry placement
        (``http_ok=1`` — the same placements that incremented the in-memory ``window_count``) in
        the last ``since_seconds``. The executor uses this to REHYDRATE the per-window cap after a
        process restart: ``window_count`` otherwise lives only in memory, so a restart mid-window
        would reset it to 0 and admit MORE than ``max_picks_per_window`` entries in one window.
        Best-effort: any failure returns ``{}`` and the executor keeps its fresh in-memory state."""
        out: dict[int, set[str]] = {}
        try:
            import time as _time
            cutoff = _time.time() - float(since_seconds)
            rows = self._conn.execute(
                "SELECT window_key, ticker FROM executor_orders "
                "WHERE action='entry' AND http_ok=1 AND created_at >= ? "
                "AND window_key IS NOT NULL AND ticker IS NOT NULL",
                (cutoff,)).fetchall()
        except Exception:  # noqa: BLE001 - best-effort; never block executor init
            logger.exception("recent_window_entries failed (cap rehydrate skipped)")
            return {}
        for wk, tk in rows:
            try:
                out.setdefault(int(wk), set()).add(str(tk))
            except (TypeError, ValueError):
                continue
        return out

    def fill_summary(self, *, action: str | None = None) -> dict[str, Any]:
        """Counts by fill_status (+ missed / filled / fill_rate). Empty dict-shape on error."""
        try:
            where, args = ("WHERE action=?", (action,)) if action else ("", ())
            rows = self._conn.execute(
                f"SELECT fill_status, COUNT(*), COALESCE(SUM(stake_cents),0) "
                f"FROM executor_orders {where} GROUP BY fill_status", args).fetchall()
            by = {r[0]: {"n": r[1], "stake_cents": r[2]} for r in rows}
            total = sum(v["n"] for v in by.values())
            missed = sum(by.get(s, {}).get("n", 0) for s in MISSED_LABELS)
            filled = by.get(FILLED, {}).get("n", 0)
            partial = by.get(PARTIAL, {}).get("n", 0)
            live = total - by.get(DRY_RUN, {}).get("n", 0) - by.get(FAILED, {}).get("n", 0)
            return {"total": total, "by_status": by, "missed": missed, "filled": filled,
                    "partial": partial, "live_orders": live,
                    "fill_rate": (filled / live if live else None)}
        except Exception:  # noqa: BLE001
            logger.exception("fill_summary failed")
            return {"total": 0, "by_status": {}, "missed": 0, "filled": 0, "partial": 0,
                    "live_orders": 0, "fill_rate": None}

    def reconcile_orders(
        self,
        account_orders: list[Mapping[str, Any]],
        *,
        now: float | None = None,
    ) -> dict[str, int]:
        """Attach final broker order state to local order rows.

        Matching is by broker ``order_id`` first, then by Kalshi's deterministic UUID
        form of our raw ``client_order_id``. The original immediate-fill columns are
        left untouched so placement-time and final outcomes can be compared.
        """
        try:
            from .trading_client import _coid_uuid

            ts = __import__("time").time() if now is None else float(now)
            rows = self._conn.execute(
                "SELECT id, order_id, client_order_id, requested_count FROM executor_orders"
            ).fetchall()
            by_key: dict[str, tuple[Any, ...]] = {}
            for row in rows:
                local_id, order_id, client_order_id, _requested = row
                if order_id:
                    by_key[f"order:{order_id}"] = row
                if client_order_id:
                    raw = str(client_order_id)
                    by_key[f"client:{raw}"] = row
                    by_key[f"client:{_coid_uuid(raw)}"] = row

            matched_local_ids: set[int] = set()
            updated = 0
            for account_order in account_orders:
                order = _inner_order(account_order)
                order_id = order.get("order_id") if order.get("order_id") is not None else order.get("id")
                client_order_id = order.get("client_order_id")
                match = None
                if order_id is not None:
                    match = by_key.get(f"order:{order_id}")
                if match is None and client_order_id is not None:
                    match = by_key.get(f"client:{client_order_id}")
                if match is None:
                    continue
                local_id, _local_order_id, _local_client_order_id, requested_count = match
                final = classify_final_order(order, int(requested_count or 0))
                self._conn.execute(
                    "UPDATE executor_orders SET final_status=?, final_fill_status=?, "
                    "final_filled_count=?, final_remaining_count=?, final_average_fill_price=?, "
                    "final_average_fee_paid=?, final_reconciled_at=?, final_response_json=? "
                    "WHERE id=?",
                    (
                        final["status"],
                        final["fill_status"],
                        final["filled_count"],
                        final["remaining_count"],
                        final["average_fill_price"],
                        final["average_fee_paid"],
                        ts,
                        json.dumps(order, default=str)[:4000],
                        local_id,
                    ),
                )
                matched_local_ids.add(int(local_id))
                updated += 1
            self._conn.commit()
            return {
                "input": len(account_orders),
                "matched": len(matched_local_ids),
                "updated": updated,
                "unmatched": len(account_orders) - updated,
            }
        except Exception:  # noqa: BLE001 - reconciliation must not corrupt executor operation
            logger.exception("reconcile_orders failed")
            return {"input": len(account_orders or []), "matched": 0, "updated": 0,
                    "unmatched": len(account_orders or [])}

    def final_fill_summary(self, *, action: str | None = None) -> dict[str, Any]:
        """Counts by reconciled final_fill_status; unreconciled rows are excluded."""
        try:
            clauses = ["final_fill_status IS NOT NULL"]
            args: list[Any] = []
            if action:
                clauses.append("action=?")
                args.append(action)
            where = "WHERE " + " AND ".join(clauses)
            rows = self._conn.execute(
                f"SELECT final_fill_status, COUNT(*), COALESCE(SUM(stake_cents),0) "
                f"FROM executor_orders {where} GROUP BY final_fill_status",
                tuple(args),
            ).fetchall()
            by = {r[0]: {"n": r[1], "stake_cents": r[2]} for r in rows}
            total = sum(v["n"] for v in by.values())
            missed = sum(by.get(s, {}).get("n", 0) for s in MISSED_LABELS)
            filled = by.get(FILLED, {}).get("n", 0)
            partial = by.get(PARTIAL, {}).get("n", 0)
            live = total - by.get(DRY_RUN, {}).get("n", 0) - by.get(FAILED, {}).get("n", 0)
            return {"total": total, "by_status": by, "missed": missed, "filled": filled,
                    "partial": partial, "live_orders": live,
                    "fill_rate": (filled / live if live else None)}
        except Exception:  # noqa: BLE001
            logger.exception("final_fill_summary failed")
            return {"total": 0, "by_status": {}, "missed": 0, "filled": 0, "partial": 0,
                    "live_orders": 0, "fill_rate": None}

    def record_order_result(self, *, action: str, pick: Any, decision: Any,
                            res: Mapping[str, Any], age_ms: float | None,
                            bal_ms: float | None, order_ms: float | None,
                            client_order_id: str) -> tuple[str, int | None]:
        """Classify ``res`` and record it. Returns (fill_status, filled_count). Never raises."""
        fill_status, filled, order_id = classify_fill(res, getattr(decision, "count", 0) or 0)
        try:
            self.record(
                created_at=__import__("time").time(), action=action,
                ticker=getattr(pick, "ticker", None), asset=getattr(pick, "asset", None),
                interval=getattr(pick, "interval", None), window_key=getattr(pick, "window_key", None),
                client_order_id=client_order_id, order_id=order_id,
                requested_count=getattr(decision, "count", None),
                filled_count=filled, limit_price_cents=getattr(decision, "limit_price_cents", None),
                stake_cents=getattr(decision, "stake_cents", None),
                mode=("dry-run" if res.get("dry_run") else "LIVE"),
                http_ok=1 if res.get("ok") else 0, http_status=res.get("status"),
                fill_status=fill_status, snapshot_age_ms=age_ms,
                balance_latency_ms=bal_ms, order_latency_ms=order_ms,
                response_json=json.dumps(res.get("data") if res.get("data") is not None else res,
                                         default=str)[:2000])
        except Exception:  # noqa: BLE001
            logger.exception("record_order_result failed (ignored)")
        return fill_status, filled
