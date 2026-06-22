"""Entry-performance ledger (entry-econ-v1).

Tracks ENTRY recommendations independently from directional predictions, in its
OWN SQLite file — it never writes to the production tables or the challenger
ledger. One evolving row per (model_version, contract, checkpoint) is upserted as
the entry economics are re-evaluated each cycle (idempotent — no row explosion,
restart-safe). The terms of an ENTER are frozen separately the moment that ENTER
is actually delivered before close: only such "sent" entries count as official
results. WAIT and SKIP are never counted as wins/losses but are kept for the
background studies (did a WAIT later reach its target? did a SKIP avoid a loss?).
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from ..money import round_cents
from .engine import EntryEconomicsResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entry_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    model_version TEXT NOT NULL,
    econ_version TEXT,
    asset TEXT,
    contract TEXT,
    checkpoint TEXT,
    close_time REAL,
    decision TEXT NOT NULL,
    side TEXT,
    raw_prob REAL,
    conservative_prob REAL,
    executable_ask_cents REAL,
    recommended_low_cents REAL,
    recommended_high_cents REAL,
    maximum_entry_cents REAL,
    spread_cents REAL,
    depth_contracts REAL,
    slippage_cents REAL,
    fee_cents REAL,
    total_cost_cents REAL,
    gross_edge_cents REAL,
    net_edge_cents REAL,
    ev_per_contract_cents REAL,
    ev_after_uncertainty_cents REAL,
    break_even_prob REAL,
    risk_reward REAL,
    liquidity_capacity REAL,
    main_blocker TEXT,
    -- frozen ENTER-at-send terms (the official entry; set once) --
    enter_sent INTEGER NOT NULL DEFAULT 0,
    enter_sent_at REAL,
    sent_ask_cents REAL,
    sent_total_cost_cents REAL,
    sent_net_edge_cents REAL,
    sent_conservative_prob REAL,
    -- price excursion after the ENTER was sent --
    max_favorable_cents REAL,
    max_adverse_cents REAL,
    -- settlement --
    official_result TEXT,
    resolved_at REAL,
    direction_correct INTEGER,
    entry_justified INTEGER,
    realized_pnl_cents REAL,
    detail_json TEXT,
    snapshot_id TEXT,
    UNIQUE(model_version, contract, checkpoint)
);
CREATE INDEX IF NOT EXISTS idx_entry_resolved ON entry_records(checkpoint, official_result);
CREATE INDEX IF NOT EXISTS idx_entry_decision ON entry_records(model_version, decision);
"""


def _r(x: Any) -> float | None:
    return round_cents(x)


class EntryLedger:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Additive migrations: new nullable columns only, so an older DB file
        opened by a newer build stays valid (raw data preserved)."""
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(entry_records)")}
        additions = {
            "snapshot_id": "ADD COLUMN snapshot_id TEXT",
            "max_favorable_cents": "ADD COLUMN max_favorable_cents REAL",
            "max_adverse_cents": "ADD COLUMN max_adverse_cents REAL",
        }
        for name, ddl in additions.items():
            if name not in cols:
                self._conn.execute(f"ALTER TABLE entry_records {ddl}")

    def close(self) -> None:
        self._conn.close()

    # ---- write path (idempotent upsert of the latest evaluation) ----
    def record_evaluation(self, result: EntryEconomicsResult, *,
                          contract: str | None = None, close_time: float | None = None,
                          created_at: float | None = None, model_version: str | None = None,
                          snapshot_id: str | None = None) -> int | None:
        """Upsert the latest entry evaluation for (model_version, contract, checkpoint).

        Re-evaluating the same contract each cycle updates the SAME row (decision,
        economics, blocker, timestamp) rather than inserting a duplicate. The
        frozen ENTER-at-send terms and any settlement are preserved across updates.
        """
        econ = result.economics
        cons = result.conservative
        contract = contract or result.asset
        mv = model_version or result.version
        now = created_at if created_at is not None else time.time()
        costs = (econ.costs if econ else {}) or {}
        extra = result.extra or {}

        row = {
            "created_at": now, "updated_at": now, "model_version": mv,
            "econ_version": result.version, "asset": result.asset, "contract": contract,
            "checkpoint": result.checkpoint, "close_time": close_time,
            "decision": result.decision, "side": result.side,
            "raw_prob": None if cons is None else cons.raw_side_prob,
            "conservative_prob": None if cons is None else cons.conservative_side_prob,
            "executable_ask_cents": None if econ is None else econ.executable_ask_cents,
            "recommended_low_cents": None if econ is None else econ.recommended_entry_low_cents,
            "recommended_high_cents": None if econ is None else econ.recommended_entry_high_cents,
            "maximum_entry_cents": None if econ is None else econ.maximum_entry_price_cents,
            "spread_cents": _r(extra.get("spread_cents")),
            "depth_contracts": _r(extra.get("depth_contracts")),
            "slippage_cents": _r(costs.get("slippage_cents")),
            "fee_cents": _r(costs.get("fee_cents")),
            "total_cost_cents": _r(costs.get("total_cents")),
            "gross_edge_cents": None if econ is None else econ.gross_edge_cents,
            "net_edge_cents": None if econ is None else econ.net_edge_cents,
            "ev_per_contract_cents": None if econ is None else econ.ev_per_contract_cents,
            "ev_after_uncertainty_cents": None if econ is None else econ.ev_after_uncertainty_cents,
            "break_even_prob": None if econ is None else econ.break_even_prob,
            "risk_reward": None if econ is None else econ.risk_reward_ratio,
            "liquidity_capacity": None if econ is None else econ.liquidity_capacity_contracts,
            "main_blocker": result.main_blocker,
            "detail_json": json.dumps(result.as_dict(), default=str),
            "snapshot_id": snapshot_id,
        }
        existing = self._conn.execute(
            "SELECT id, enter_sent FROM entry_records WHERE model_version=? AND contract=? AND checkpoint=?",
            (mv, contract, result.checkpoint),
        ).fetchone()
        if existing is None:
            cols = ", ".join(row.keys())
            ph = ", ".join("?" for _ in row)
            cur = self._conn.execute(
                f"INSERT INTO entry_records ({cols}) VALUES ({ph})", tuple(row.values()))
            self._conn.commit()
            return cur.lastrowid
        # Update the evolving fields only; never disturb frozen ENTER/settlement.
        updatable = {k: v for k, v in row.items()
                     if k not in {"created_at", "model_version", "contract", "checkpoint"}}
        sets = ", ".join(f"{k}=?" for k in updatable)
        self._conn.execute(
            f"UPDATE entry_records SET {sets} WHERE id=?",
            (*updatable.values(), existing["id"]))
        self._conn.commit()
        return int(existing["id"])

    def mark_enter_sent(self, contract: str, checkpoint: str, *,
                        model_version: str, sent_at: float | None = None,
                        ask_cents: float | None = None, total_cost_cents: float | None = None,
                        net_edge_cents: float | None = None,
                        conservative_prob: float | None = None) -> bool:
        """Freeze the ENTER-at-send terms ONCE (idempotent). Only a row whose ENTER
        was actually delivered before close counts as an official entry result.
        When the economics are not passed, the row's current evaluated terms are
        snapshotted. Never overwrites an already-sent row."""
        existing = self._conn.execute(
            "SELECT id, executable_ask_cents, total_cost_cents, net_edge_cents, conservative_prob "
            "FROM entry_records WHERE model_version=? AND contract=? AND checkpoint=? AND enter_sent=0 "
            "AND decision='ENTER'",
            (model_version, str(contract), str(checkpoint)),
        ).fetchone()
        if existing is None:
            return False  # no current-ENTER row to credit (or already sent)
        cur = self._conn.execute(
            "UPDATE entry_records SET enter_sent=1, enter_sent_at=?, sent_ask_cents=?, "
            "sent_total_cost_cents=?, sent_net_edge_cents=?, sent_conservative_prob=? WHERE id=?",
            (sent_at if sent_at is not None else time.time(),
             _r(ask_cents) if ask_cents is not None else existing["executable_ask_cents"],
             _r(total_cost_cents) if total_cost_cents is not None else existing["total_cost_cents"],
             _r(net_edge_cents) if net_edge_cents is not None else existing["net_edge_cents"],
             conservative_prob if conservative_prob is not None else existing["conservative_prob"],
             existing["id"]),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def update_excursion(self, contract: str, checkpoint: str, *, model_version: str,
                         current_value_cents: float) -> bool:
        """Track max favorable / adverse movement after an ENTER was sent.

        ``current_value_cents`` is the side's current mark (what the held contract
        is worth now). Favorable = how far above the sent all-in cost it has gone;
        adverse = how far below. Only updates sent, unresolved rows."""
        row = self._conn.execute(
            "SELECT id, sent_ask_cents, sent_total_cost_cents, max_favorable_cents, max_adverse_cents "
            "FROM entry_records WHERE model_version=? AND contract=? AND checkpoint=? "
            "AND enter_sent=1 AND official_result IS NULL",
            (model_version, str(contract), str(checkpoint)),
        ).fetchone()
        if row is None or row["sent_ask_cents"] is None:
            return False
        basis = float(row["sent_ask_cents"]) + float(row["sent_total_cost_cents"] or 0.0)
        delta = float(current_value_cents) - basis
        fav = max(float(row["max_favorable_cents"] or 0.0), max(0.0, delta))
        adv = max(float(row["max_adverse_cents"] or 0.0), max(0.0, -delta))
        self._conn.execute(
            "UPDATE entry_records SET max_favorable_cents=?, max_adverse_cents=? WHERE id=?",
            (_r(fav), _r(adv), row["id"]))
        self._conn.commit()
        return True

    # ---- settlement ----
    def resolve(self, contract: str, checkpoint: str, official_result: str, *,
                model_version: str, resolved_at: float | None = None) -> bool:
        """Grade the row at settlement. Sets direction_correct for every row, and
        realized P&L / entry_justified for rows whose ENTER was actually sent."""
        official = str(official_result).upper()
        if official not in {"YES", "NO"}:
            return False
        row = self._conn.execute(
            "SELECT id, side, enter_sent, sent_ask_cents, sent_total_cost_cents, net_edge_cents "
            "FROM entry_records WHERE model_version=? AND contract=? AND checkpoint=? "
            "AND official_result IS NULL",
            (model_version, str(contract), str(checkpoint)),
        ).fetchone()
        if row is None:
            return False
        direction_correct = 1 if str(row["side"] or "").upper() == official else 0
        realized = None
        entry_justified = None
        if row["enter_sent"] and row["sent_ask_cents"] is not None:
            won = direction_correct == 1
            payoff = 100.0 if won else 0.0
            cost_basis = float(row["sent_ask_cents"]) + float(row["sent_total_cost_cents"] or 0.0)
            realized = _r(payoff - cost_basis)
            entry_justified = 1 if (realized is not None and realized > 0) else 0
        self._conn.execute(
            "UPDATE entry_records SET official_result=?, resolved_at=?, direction_correct=?, "
            "realized_pnl_cents=?, entry_justified=? WHERE id=?",
            (official, resolved_at if resolved_at is not None else time.time(),
             direction_correct, realized, entry_justified, row["id"]),
        )
        self._conn.commit()
        return True

    # ---- read / scoreboard ----
    def entry_scoreboard(self, model_version: str) -> dict[str, Any]:
        """Official entry performance: ENTER recommendations actually sent before
        close, graded on settlement. WAIT/SKIP are excluded from wins/losses."""
        rows = list(self._conn.execute(
            "SELECT direction_correct, entry_justified, realized_pnl_cents FROM entry_records "
            "WHERE model_version=? AND enter_sent=1 AND official_result IS NOT NULL",
            (model_version,),
        ))
        n = len(rows)
        wins = sum(1 for r in rows if (r["realized_pnl_cents"] or 0.0) > 0)
        pnl = sum((r["realized_pnl_cents"] or 0.0) for r in rows)
        dir_ok = sum(1 for r in rows if r["direction_correct"] == 1)
        return {
            "official_entries": n,
            "wins": wins,
            "losses": n - wins,
            "win_rate": round(wins / n, 4) if n else None,
            "direction_accuracy": round(dir_ok / n, 4) if n else None,
            "total_pnl_cents": round(pnl, 2),
            "avg_pnl_cents": round(pnl / n, 3) if n else None,
        }

    def decision_counts(self, model_version: str) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT decision, COUNT(*) AS n FROM entry_records WHERE model_version=? GROUP BY decision",
            (model_version,),
        ).fetchall()
        out = {"ENTER": 0, "WAIT": 0, "SKIP": 0}
        for r in rows:
            out[str(r["decision"])] = int(r["n"])
        return out

    def background_studies(self, model_version: str) -> dict[str, Any]:
        """Read-only studies of WAIT/SKIP (never counted as trades):
          * skip_avoided_loss  — SKIPs whose side ultimately LOST (good skip)
          * skip_missed_win    — SKIPs whose side ultimately WON (a missed entry)
          * wait_correct_side  — WAITs whose side ultimately matched the result
          * unsent_enter       — rows that reached ENTER but were never delivered
        """
        resolved = list(self._conn.execute(
            "SELECT decision, side, official_result, enter_sent FROM entry_records "
            "WHERE model_version=? AND official_result IS NOT NULL",
            (model_version,),
        ))

        def _won(r):
            return str(r["side"] or "").upper() == str(r["official_result"]).upper()

        skips = [r for r in resolved if r["decision"] == "SKIP"]
        waits = [r for r in resolved if r["decision"] == "WAIT"]
        enters = [r for r in resolved if r["decision"] == "ENTER"]
        return {
            "skip_total_resolved": len(skips),
            "skip_avoided_loss": sum(1 for r in skips if not _won(r)),
            "skip_missed_win": sum(1 for r in skips if _won(r)),
            "wait_total_resolved": len(waits),
            "wait_correct_side": sum(1 for r in waits if _won(r)),
            "enter_total_resolved": len(enters),
            "enter_unsent": sum(1 for r in enters if not r["enter_sent"]),
        }
