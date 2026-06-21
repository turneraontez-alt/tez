"""ShadowLedger — immutable persistence + scoring for the challenger.

Records every shadow prediction (with the control's probability alongside) BEFORE
the outcome is known, then grades it after official settlement. Predictions are
never modified after settlement except to attach the outcome and hypothetical
P&L. Writes only to its own SQLite file — never the production tables.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from typing import Any, Iterable, Mapping

from .mathx import clamp, wilson_interval

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    model_version TEXT NOT NULL,
    asset TEXT,
    contract TEXT,
    checkpoint TEXT,
    spot REAL,
    target REAL,
    seconds_remaining REAL,
    settlement_source TEXT,
    yes_bid REAL, yes_ask REAL, no_bid REAL, no_ask REAL,
    control_prob_yes REAL,
    challenger_raw_prob_yes REAL,
    challenger_prob_yes REAL,
    confidence REAL,
    edge_vs_market REAL,
    net_edge_cents REAL,
    recommendation TEXT,
    side TEXT,
    executable_ask_cents REAL,
    total_cost_cents REAL,
    hypothetical_size_fraction REAL,
    top_factors_json TEXT,
    warnings_json TEXT,
    feature_json TEXT,
    official_result TEXT,
    resolved_at REAL,
    hypothetical_pnl_cents REAL,
    UNIQUE(model_version, contract, checkpoint)
);
CREATE INDEX IF NOT EXISTS idx_shadow_resolved ON shadow_predictions(checkpoint, official_result);
"""


class ShadowLedger:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- write path (immutable pre-settlement) ----
    def record(
        self,
        prediction,
        *,
        asset: str | None,
        contract: str | None,
        checkpoint: str | None,
        control_prob_yes: float | None,
        settlement_source: str | None = None,
        created_at: float | None = None,
        model_version: str = "challenger-v1",
    ) -> int | None:
        dec = prediction.decision
        total_cost = dec.costs.total_cents if (dec and dec.costs) else None
        fv = prediction.feature_vector
        row = (
            created_at if created_at is not None else time.time(),
            model_version, asset, contract, checkpoint,
            getattr(fv, "spot", None), getattr(fv, "target", None),
            getattr(fv, "seconds_remaining", None),
            settlement_source,
            getattr(fv, "yes_bid_cents", None), getattr(fv, "yes_ask_cents", None),
            getattr(fv, "no_bid_cents", None), getattr(fv, "no_ask_cents", None),
            control_prob_yes,
            prediction.raw_prob_yes,
            prediction.prob_yes,
            prediction.confidence,
            prediction.edge_vs_market,
            prediction.net_edge_cents,
            prediction.recommendation,
            dec.side if dec else None,
            dec.executable_ask_cents if dec else None,
            total_cost,
            dec.hypothetical_size_fraction if dec else 0.0,
            json.dumps(prediction.top_factors),
            json.dumps(prediction.warnings),
            json.dumps(prediction.feature_details),
            None, None, None,
        )
        try:
            cur = self._conn.execute(
                """INSERT INTO shadow_predictions
                (created_at, model_version, asset, contract, checkpoint, spot, target,
                 seconds_remaining, settlement_source, yes_bid, yes_ask, no_bid, no_ask,
                 control_prob_yes, challenger_raw_prob_yes, challenger_prob_yes, confidence,
                 edge_vs_market, net_edge_cents, recommendation, side, executable_ask_cents,
                 total_cost_cents, hypothetical_size_fraction, top_factors_json, warnings_json,
                 feature_json, official_result, resolved_at, hypothetical_pnl_cents)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                row,
            )
            self._conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None  # already recorded this (contract, checkpoint) — keep first

    # ---- settlement ----
    def resolve(self, contract: str, checkpoint: str, official_result: str,
                settled_at: float | None = None, model_version: str = "challenger-v1") -> bool:
        official_result = str(official_result).upper()
        if official_result not in {"YES", "NO"}:
            return False
        cur = self._conn.execute(
            "SELECT id, side, executable_ask_cents, total_cost_cents, recommendation "
            "FROM shadow_predictions WHERE model_version=? AND contract=? AND checkpoint=? "
            "AND official_result IS NULL",
            (model_version, contract, checkpoint),
        )
        r = cur.fetchone()
        if r is None:
            return False
        pnl = None
        if r["recommendation"] in ("BUY_YES", "BUY_NO") and r["executable_ask_cents"] is not None:
            won = (r["side"] == official_result)
            payoff = 100.0 if won else 0.0
            cost_basis = r["executable_ask_cents"] + (r["total_cost_cents"] or 0.0)
            pnl = payoff - cost_basis
        self._conn.execute(
            "UPDATE shadow_predictions SET official_result=?, resolved_at=?, hypothetical_pnl_cents=? "
            "WHERE id=?",
            (official_result, settled_at if settled_at is not None else time.time(), pnl, r["id"]),
        )
        self._conn.commit()
        return True

    # ---- read / scoring ----
    def training_samples(self, checkpoint: str | None = None, model_version: str = "challenger-v1"):
        q = ("SELECT created_at, feature_json, official_result FROM shadow_predictions "
             "WHERE model_version=? AND official_result IS NOT NULL AND feature_json IS NOT NULL")
        args: list[Any] = [model_version]
        if checkpoint:
            q += " AND checkpoint=?"
            args.append(checkpoint)
        q += " ORDER BY created_at ASC"
        ts: list[float] = []
        feats: list[dict] = []
        y: list[int] = []
        for row in self._conn.execute(q, args):
            try:
                fj = json.loads(row["feature_json"])
            except (TypeError, ValueError):
                continue
            ts.append(float(row["created_at"]))
            feats.append(fj)
            y.append(1 if str(row["official_result"]).upper() == "YES" else 0)
        return ts, feats, y

    def scoreboard(self, model_version: str = "challenger-v1") -> dict[str, Any]:
        rows = list(self._conn.execute(
            "SELECT challenger_prob_yes, control_prob_yes, official_result, recommendation, "
            "hypothetical_pnl_cents FROM shadow_predictions "
            "WHERE model_version=? AND official_result IS NOT NULL",
            (model_version,),
        ))
        n = len(rows)
        out: dict[str, Any] = {"resolved": n}
        if n == 0:
            out["note"] = "no resolved shadow predictions yet — accumulate data before judging"
            return out
        y = [1 if str(r["official_result"]).upper() == "YES" else 0 for r in rows]
        ch = [clamp(float(r["challenger_prob_yes"]), 1e-6, 1 - 1e-6) for r in rows]
        ct = [clamp(float(r["control_prob_yes"]), 1e-6, 1 - 1e-6) if r["control_prob_yes"] is not None else None
              for r in rows]

        out["challenger"] = _prob_metrics(ch, y)
        if all(c is not None for c in ct):
            out["control"] = _prob_metrics([c for c in ct], y)
        # trade stats (challenger)
        traded = [r for r in rows if r["recommendation"] in ("BUY_YES", "BUY_NO")]
        wins = sum(1 for r in traded if (r["hypothetical_pnl_cents"] or 0) > 0)
        pnl = sum((r["hypothetical_pnl_cents"] or 0.0) for r in traded)
        wlo, whi = wilson_interval(wins, len(traded)) if traded else (0.0, 0.0)
        out["trading"] = {
            "n_trades": len(traded),
            "trade_rate": round(len(traded) / n, 4),
            "win_rate": round(wins / len(traded), 4) if traded else None,
            "win_rate_wilson95": [round(wlo, 4), round(whi, 4)] if traded else None,
            "total_hypothetical_pnl_cents": round(pnl, 2),
            "avg_pnl_cents": round(pnl / len(traded), 3) if traded else None,
        }
        return out


def _prob_metrics(p: list[float], y: list[int], bins: int = 10) -> dict[str, Any]:
    n = len(p)
    brier = sum((pi - yi) ** 2 for pi, yi in zip(p, y)) / n
    logloss = -sum(yi * math.log(pi) + (1 - yi) * math.log(1 - pi) for pi, yi in zip(p, y)) / n
    acc = sum(1 for pi, yi in zip(p, y) if (pi >= 0.5) == (yi == 1)) / n
    # Expected calibration error.
    buckets = [[0, 0.0, 0.0] for _ in range(bins)]  # count, sum_p, sum_y
    for pi, yi in zip(p, y):
        b = min(bins - 1, int(pi * bins))
        buckets[b][0] += 1
        buckets[b][1] += pi
        buckets[b][2] += yi
    ece = 0.0
    for cnt, sp, sy in buckets:
        if cnt:
            ece += (cnt / n) * abs(sy / cnt - sp / cnt)
    return {"n": n, "brier": round(brier, 5), "log_loss": round(logloss, 5),
            "accuracy": round(acc, 4), "ece": round(ece, 5)}
