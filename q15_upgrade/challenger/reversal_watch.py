"""ReversalWatch — paper delivery of the challenger's cheap-YES reversal pocket.

PREREGISTERED RULE (frozen 2026-08-01 from the forward-settled challenger-v5
shadow record, 2026-06-22 -> 2026-08-01; analysis in HANDOFF.md "Reversal watch"
entry): deliver a PAPER alert whenever the challenger decides BUY_YES with

    checkpoint in (10M, 7M)
    asset      in (BTC, ETH, DOGE)
    executable YES ask < 45c

That exact slice settled 131 majors events at 79.4% win / +42.8c per contract
net of the challenger's full cost model (clustered-bootstrap 5th percentile
+36.5c). Everything outside the frozen slice (15M, HYPE/SOL/XRP/BNB, expensive
YES, the NO side) was net-negative or unproven on the same evidence, so the
defaults below ARE the preregistered rule — widening them is a new hypothesis
and needs its own forward record.

This module is strictly read-only: it never places, modifies, or cancels an
order, never touches the production champion, and writes only one idempotent
claim table inside the challenger's own SQLite file so a restart can never
re-fire an alert for a contract+checkpoint that already delivered. The alert
itself re-reports the pocket's live settled record on every fire, so a decaying
edge is visible on the message that depends on it.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Iterable

from ..timez import fmt_eastern_hm

logger = logging.getLogger(__name__)

REVERSAL_WATCH_MARKER = "REVERSAL WATCH"

_LOCK_SCHEMA = """
CREATE TABLE IF NOT EXISTS reversal_alert_lock (
    model_version TEXT NOT NULL,
    contract TEXT NOT NULL,
    checkpoint TEXT NOT NULL,
    created_at REAL NOT NULL,
    ask_cents REAL,
    prob_yes REAL,
    net_edge_cents REAL,
    UNIQUE(model_version, contract, checkpoint)
);
"""


class ReversalWatch:
    """Gate + idempotent claim + message builder for the cheap-YES pocket."""

    def __init__(self, db_path: str, *, model_version: str,
                 max_ask_cents: float = 45.0,
                 assets: Iterable[str] = ("BTC", "ETH", "DOGE"),
                 checkpoints: Iterable[str] = ("10M", "7M")):
        self.db_path = db_path
        self.model_version = str(model_version)
        self.max_ask_cents = float(max_ask_cents)
        self.assets = tuple(a.upper() for a in assets)
        self.checkpoints = tuple(c.upper() for c in checkpoints)
        # Own connection to the challenger's SQLite file (WAL is enabled by the
        # ShadowLedger, so concurrent reads/writes do not block). The watch only
        # ever writes its own claim table; the pocket record line is read-only.
        self._conn = sqlite3.connect(db_path, timeout=15.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute(_LOCK_SCHEMA)

    # ---- the frozen gate ----
    def matches(self, pred, *, asset: str, checkpoint: str) -> bool:
        """True when a challenger prediction sits inside the preregistered pocket.

        Requires an actual tradeable BUY_YES decision (the production decision
        object already enforces the no-trade zone, risk gates and OOD fail-safe),
        on a preregistered asset/checkpoint, below the preregistered ask ceiling.
        """
        dec = getattr(pred, "decision", None)
        if dec is None or getattr(dec, "action", None) != "BUY_YES":
            return False
        if str(asset or "").upper() not in self.assets:
            return False
        if str(checkpoint or "").upper() not in self.checkpoints:
            return False
        ask = getattr(dec, "executable_ask_cents", None)
        if ask is None or float(ask) >= self.max_ask_cents:
            return False
        return True

    # ---- idempotent claim (restart-safe) ----
    def _claim(self, *, contract: str, checkpoint: str, created_at: float,
               ask_cents: float, prob_yes: float, net_edge_cents: float) -> bool:
        """True iff this (model_version, contract, checkpoint) is newly claimed."""
        with self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO reversal_alert_lock("
                "model_version, contract, checkpoint, created_at,"
                " ask_cents, prob_yes, net_edge_cents) VALUES(?,?,?,?,?,?,?)",
                (self.model_version, contract, checkpoint, float(created_at),
                 float(ask_cents), float(prob_yes),
                 float(net_edge_cents) if net_edge_cents is not None else None),
            )
        return cursor.rowcount == 1

    # ---- live pocket record (the "all eyes on it" line) ----
    def pocket_record(self) -> dict:
        """Settled record of the EXACT preregistered slice under this
        model_version: n, win rate, average net P&L per contract. Read-only."""
        placeholders_a = ",".join("?" for _ in self.assets)
        placeholders_c = ",".join("?" for _ in self.checkpoints)
        empty = {"n": 0, "win_rate": None, "avg_pnl_cents": None}
        try:
            row = self._conn.execute(
                "SELECT COUNT(*) n,"
                " AVG(CASE WHEN official_result='YES' THEN 1.0 ELSE 0.0 END) w,"
                " AVG(hypothetical_pnl_cents) pnl"
                " FROM shadow_predictions"
                f" WHERE model_version=? AND side='YES'"
                f" AND checkpoint IN ({placeholders_c})"
                f" AND asset IN ({placeholders_a})"
                " AND executable_ask_cents < ?"
                " AND official_result IS NOT NULL",
                (self.model_version, *self.checkpoints, *self.assets,
                 self.max_ask_cents),
            ).fetchone()
        except sqlite3.Error:
            # The record line is advisory; a missing/unreadable table must never
            # kill the alert itself (fail soft to "no settled picks yet").
            logger.exception("reversal watch pocket record read failed (ignored)")
            return empty
        n = int(row["n"] or 0)
        return {
            "n": n,
            "win_rate": (float(row["w"]) if row["w"] is not None else None),
            "avg_pnl_cents": (float(row["pnl"]) if row["pnl"] is not None else None),
        }

    # ---- message ----
    def build_message(self, pred, *, asset: str, checkpoint: str,
                      close_time: float | None) -> str:
        dec = pred.decision
        ask = float(dec.executable_ask_cents)
        prob = float(getattr(pred, "prob_yes", 0.0))
        market = getattr(pred, "market_yes_prob", None)
        net = getattr(dec, "net_edge_cents", None)
        rec = self.pocket_record()
        lines = [
            f"{asset} {checkpoint} - BUY YES @ {ask:.0f}c",
            f"Model P(YES) {prob * 100:.1f}%"
            + (f" vs market {float(market) * 100:.1f}%" if market is not None else ""),
        ]
        if net is not None:
            lines.append(f"Net edge +{float(net):.1f}c after costs")
        if close_time:
            lines.append(f"Closes {fmt_eastern_hm(close_time)}")
        if rec["n"]:
            wr = f"{rec['win_rate'] * 100:.1f}%" if rec["win_rate"] is not None else "N/A"
            pnl = (f"{rec['avg_pnl_cents']:+.1f}c avg"
                   if rec["avg_pnl_cents"] is not None else "")
            lines.append(
                f"Pocket record ({self.model_version}): {rec['n']} settled"
                f" - {wr} win - {pnl}")
        else:
            lines.append(f"Pocket record ({self.model_version}): no settled picks yet")
        lines.append("Read-only paper signal - no order placed")
        return (f"<b>{REVERSAL_WATCH_MARKER} - PAPER</b>\n"
                f"<pre>{chr(10).join(lines)}</pre>")

    # ---- main entry: gate -> claim -> message (or None) ----
    def consider(self, pred, *, ticker: str, asset: str, checkpoint: str,
                 close_time: float | None, created_at: float | None = None) -> str | None:
        """Return the alert message for a freshly claimed pocket pick, else None.

        Never raises: a watch failure must not disturb the shadow recorder that
        calls it. Idempotent across restarts via the claim table.
        """
        try:
            if not self.matches(pred, asset=asset, checkpoint=checkpoint):
                return None
            dec = pred.decision
            if not self._claim(
                contract=str(ticker), checkpoint=str(checkpoint).upper(),
                created_at=float(created_at or time.time()),
                ask_cents=float(dec.executable_ask_cents),
                prob_yes=float(getattr(pred, "prob_yes", 0.0)),
                net_edge_cents=getattr(dec, "net_edge_cents", None),
            ):
                return None
            return self.build_message(pred, asset=str(asset).upper(),
                                      checkpoint=str(checkpoint).upper(),
                                      close_time=close_time)
        except Exception:
            logger.exception("reversal watch consider failed (ignored)")
            return None
