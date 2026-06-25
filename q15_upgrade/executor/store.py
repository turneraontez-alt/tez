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
            return FAILED, 0, None
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


_COLS = (
    "created_at", "action", "ticker", "asset", "interval", "window_key",
    "client_order_id", "order_id", "requested_count", "filled_count",
    "limit_price_cents", "stake_cents", "mode", "http_ok", "http_status",
    "fill_status", "snapshot_age_ms", "balance_latency_ms", "order_latency_ms",
    "response_json",
)


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
        self._conn.commit()

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
