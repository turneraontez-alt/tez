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
    close_time REAL,
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
    ood_score REAL,
    ood_reasons_json TEXT,
    lineage_json TEXT,
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
        close_time: float | None = None,
        model_version: str = "challenger-v1",
        lineage: dict | None = None,
    ) -> int | None:
        dec = prediction.decision
        total_cost = dec.costs.total_cents if (dec and dec.costs) else None
        fv = prediction.feature_vector
        row = (
            created_at if created_at is not None else time.time(),
            model_version, asset, contract, checkpoint,
            getattr(fv, "spot", None), getattr(fv, "target", None),
            getattr(fv, "seconds_remaining", None),
            close_time,
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
            getattr(prediction, "ood_score", None),
            json.dumps(getattr(prediction, "ood_reasons", [])),
            json.dumps(lineage or {}),
            None, None, None,
        )
        try:
            cur = self._conn.execute(
                """INSERT INTO shadow_predictions
                (created_at, model_version, asset, contract, checkpoint, spot, target,
                 seconds_remaining, close_time, settlement_source, yes_bid, yes_ask, no_bid, no_ask,
                 control_prob_yes, challenger_raw_prob_yes, challenger_prob_yes, confidence,
                 edge_vs_market, net_edge_cents, recommendation, side, executable_ask_cents,
                 total_cost_cents, hypothetical_size_fraction, top_factors_json, warnings_json,
                 feature_json, ood_score, ood_reasons_json, lineage_json,
                 official_result, resolved_at, hypothetical_pnl_cents)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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


    # ---- ranked Top-1/2/3 comparison ----
    def _resolved_cases(self, model_version: str):
        """Group resolved rows into cases. A CASE = one 15-min market (close_time)
        at one checkpoint; its members are the per-asset predictions in it."""
        rows = list(self._conn.execute(
            "SELECT checkpoint, close_time, created_at, asset, challenger_prob_yes, "
            "control_prob_yes, official_result FROM shadow_predictions "
            "WHERE model_version=? AND official_result IS NOT NULL",
            (model_version,),
        ))
        cases: dict[tuple, list] = {}
        for r in rows:
            close = r["close_time"] if r["close_time"] is not None else r["created_at"]
            key = (str(r["checkpoint"]), round(float(close)))
            cases.setdefault(key, []).append(r)
        return cases

    @staticmethod
    def _rank(rows, prob_key):
        """Rank a case's predictions by confidence (decisiveness = |p-0.5|), desc.

        Decisiveness is used for BOTH models so the ranking is comparable. Each
        returned entry: (asset, side, correct)."""
        cand = [r for r in rows if r[prob_key] is not None]
        cand.sort(key=lambda r: abs(float(r[prob_key]) - 0.5), reverse=True)
        out = []
        for r in cand:
            side = "YES" if float(r[prob_key]) >= 0.5 else "NO"
            correct = side == str(r["official_result"]).upper()
            out.append((r["asset"], side, correct))
        return out

    def ranked_comparison(self, model_version: str = "challenger-v1", top_k: int = 3) -> dict[str, Any]:
        """Top-1/2/3 correctness for both models, scored per rank (no double count).

        Within each case, predictions are ranked by confidence. Rank k is correct
        if that pick's side matched the official result. Each case contributes at
        most one result per rank. Overall = sum over ranks 1..top_k.
        """
        cases = self._resolved_cases(model_version)
        models = {"challenger": "challenger_prob_yes", "native": "control_prob_yes"}
        stats = {m: {k: {"correct": 0, "wrong": 0} for k in range(1, top_k + 1)} for m in models}
        for case_rows in cases.values():
            for m, pk in models.items():
                ranked = self._rank(case_rows, pk)
                for k in range(min(top_k, len(ranked))):
                    _, _, correct = ranked[k]
                    stats[m][k + 1]["correct" if correct else "wrong"] += 1

        def _finish(d):
            out = {}
            tot_c = tot_w = 0
            for k in range(1, top_k + 1):
                c, w = d[k]["correct"], d[k]["wrong"]
                tot_c += c
                tot_w += w
                n = c + w
                out[f"rank{k}"] = {"correct": c, "wrong": w,
                                   "accuracy": round(c / n, 4) if n else None}
            n = tot_c + tot_w
            out["overall"] = {"correct": tot_c, "wrong": tot_w,
                              "accuracy": round(tot_c / n, 4) if n else None}
            return out

        return {"n_cases": len(cases), "top_k": top_k,
                "challenger": _finish(stats["challenger"]),
                "native": _finish(stats["native"])}

    def latest_window_cases(self, model_version: str = "challenger-v1", top_k: int = 3) -> dict[str, Any]:
        """Per-checkpoint top-k picks (both models) for the most recent settled
        close window — for the human-readable example block in the report."""
        cases = self._resolved_cases(model_version)
        if not cases:
            return {"close": None, "checkpoints": {}}
        latest_close = max(close for (_cp, close) in cases)
        out = {"close": latest_close, "checkpoints": {}}
        for (cp, close), rows in cases.items():
            if close != latest_close:
                continue
            out["checkpoints"][cp] = {
                "challenger": self._rank(rows, "challenger_prob_yes")[:top_k],
                "native": self._rank(rows, "control_prob_yes")[:top_k],
            }
        return out

    def latest_window_end_results(self, model_version: str = "challenger-v1",
                                  checkpoints: tuple[str, ...] = ("15M", "10M")) -> dict[str, Any]:
        """For the most recent settled 15-min window, each model's END-RESULT call
        per asset at the given checkpoints (default 15M & 10M).

        A 15-min market settles once; each checkpoint is an independent decision
        time predicting that SAME end result. For each asset we return the actual
        result and, per checkpoint, each model's predicted side + whether it was
        right — so you can see if both models called the final outcome correctly
        as the window counted down. Windows are bucketed by the 15-min boundary so
        all assets settling together are grouped, even if their close timestamps
        differ by a few seconds.
        """
        rows = list(self._conn.execute(
            "SELECT checkpoint, close_time, created_at, asset, challenger_prob_yes, "
            "control_prob_yes, official_result FROM shadow_predictions "
            "WHERE model_version=? AND official_result IS NOT NULL",
            (model_version,),
        ))
        if not rows:
            return {"close": None, "checkpoints": list(checkpoints), "assets": []}

        def _window(r):
            close = r["close_time"] if r["close_time"] is not None else r["created_at"]
            return int(float(close) // 900)

        latest = max(_window(r) for r in rows)
        want = set(checkpoints)
        by_asset: dict[str, dict] = {}
        close_ts = None
        for r in rows:
            if _window(r) != latest or str(r["checkpoint"]) not in want:
                continue
            close_ts = r["close_time"] if r["close_time"] is not None else r["created_at"]
            official = str(r["official_result"]).upper()
            a = by_asset.setdefault(str(r["asset"]), {
                "asset": str(r["asset"]), "official": official, "checkpoints": {}})

            def _side_hit(prob, _official=official):
                if prob is None:
                    return None
                side = "YES" if float(prob) >= 0.5 else "NO"
                return (side, side == _official)

            a["checkpoints"][str(r["checkpoint"])] = {
                "challenger": _side_hit(r["challenger_prob_yes"]),
                "native": _side_hit(r["control_prob_yes"]),
            }
        return {"close": close_ts, "checkpoints": list(checkpoints),
                "assets": sorted(by_asset.values(), key=lambda x: x["asset"])}

    def comparison(self, model_version: str = "challenger-v1") -> dict[str, Any]:
        """Paired challenger-vs-control accuracy, overall and by checkpoint.

        Control = the production champion's probability stored alongside each
        shadow prediction, so this is strictly paired on identical contracts.
        """
        rows = list(self._conn.execute(
            "SELECT checkpoint, challenger_prob_yes, control_prob_yes, official_result "
            "FROM shadow_predictions WHERE model_version=? AND official_result IS NOT NULL",
            (model_version,),
        ))

        def _acc(subset):
            n = len(subset)
            if n == 0:
                return {"n": 0, "challenger_accuracy": None, "current_accuracy": None}
            cy = sum(1 for r in subset
                     if (float(r["challenger_prob_yes"]) >= 0.5) == (str(r["official_result"]).upper() == "YES"))
            ctl = [r for r in subset if r["control_prob_yes"] is not None]
            cu = sum(1 for r in ctl
                     if (float(r["control_prob_yes"]) >= 0.5) == (str(r["official_result"]).upper() == "YES"))
            return {
                "n": n,
                "challenger_accuracy": round(cy / n, 4),
                "current_accuracy": round(cu / len(ctl), 4) if ctl else None,
            }

        out = {"overall": _acc(rows), "by_checkpoint": {}}
        for cp in sorted({str(r["checkpoint"]) for r in rows}):
            out["by_checkpoint"][cp] = _acc([r for r in rows if str(r["checkpoint"]) == cp])
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
