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

from .config import ChallengerConfig
from .mathx import clamp, wilson_interval

# The live challenger model_version (env Q15_CHALLENGER_MODEL_VERSION, default
# "challenger-v5"). Reporting and marker methods default to THIS so an ad-hoc,
# export, or review caller that omits the version scores the rows that actually
# exist. The previous hardcoded "challenger-v1" default matched zero rows once
# the live version advanced past v1, silently reporting resolved=0 — and the
# curated learning snapshot inherited it (tools/learning_export.py calls
# scoreboard() with no version). Resolved at import; the long-running runner
# always threads cfg.model_version explicitly, so only bare callers rely on this.
_DEFAULT_MODEL_VERSION = ChallengerConfig().model_version

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
    native_predicted_side TEXT,
    native_prob_yes REAL,
    native_rank INTEGER,
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
    native_sent INTEGER NOT NULL DEFAULT 0,
    native_delivery_status TEXT,
    native_delivery_error TEXT,
    native_delivery_at REAL,
    snapshot_id TEXT,
    UNIQUE(model_version, contract, checkpoint)
);
CREATE INDEX IF NOT EXISTS idx_shadow_resolved ON shadow_predictions(checkpoint, official_result);
CREATE TABLE IF NOT EXISTS shadow_meta (
    model_version TEXT PRIMARY KEY,
    reset_at REAL NOT NULL
);
"""

# Intervals graded in the Shadow-vs-Your-System comparison, most-distant first.
REPORT_CHECKPOINTS: tuple[str, ...] = ("15M", "10M", "7M")

# Length of a Kalshi crypto window. Real contract closes land on exact 15-minute
# wall-clock boundaries, which are exact multiples of this in epoch seconds.
WINDOW_SECONDS: float = 900.0


def _window_id(close: Any) -> int | None:
    """The 15-minute window index a close timestamp belongs to.

    The production canonical stores ``close_time`` as ``now + seconds_remaining``
    (an estimate re-measured every cycle and per asset), so two predictions for the
    SAME contract — different assets in one window, or the same asset at 15M/10M/7M
    — can carry close timestamps that differ by a few seconds. Bucketing by the
    NEAREST boundary (``round(close/900)``) collapses that jitter back to the one
    true window, so every asset in a window groups together (enabling ranks #2/#3)
    and an asset's three checkpoints never split across adjacent windows. Returns
    None for a missing/invalid close.
    """
    if close is None:
        return None
    try:
        return int(round(float(close) / WINDOW_SECONDS))
    except (TypeError, ValueError):
        return None


class ShadowLedger:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a DB file was first created. New columns
        must be nullable / have a default so existing rows stay valid."""
        cols = {row["name"] for row in
                self._conn.execute("PRAGMA table_info(shadow_predictions)")}
        if "native_sent" not in cols:
            self._conn.execute(
                "ALTER TABLE shadow_predictions ADD COLUMN native_sent INTEGER NOT NULL DEFAULT 0")
        # Delivery-status audit columns (added after native_sent). They record WHY a
        # Your System pick is/ isn't in the visible record: 'SENT' (delivered before
        # close → counts) vs 'DELIVERY_FAILED' (generated but the send failed → stays
        # background, with the exact error preserved). Nullable so legacy rows stay
        # valid and simply carry no delivery audit.
        for name, ddl in (
            ("native_delivery_status", "ADD COLUMN native_delivery_status TEXT"),
            ("native_delivery_error", "ADD COLUMN native_delivery_error TEXT"),
            ("native_delivery_at", "ADD COLUMN native_delivery_at REAL"),
            # Shared frozen-snapshot id: both systems (challenger + control) are
            # scored from the SAME snapshot per interval, and that snapshot id is
            # stamped on the paired row so the simultaneity is auditable.
            ("snapshot_id", "ADD COLUMN snapshot_id TEXT"),
            # Idempotency key of the OFFICIAL report this pick was sent in. Lets the
            # delivery status be reconciled from the outbox's TRUE outcome (a sync or
            # background-worker delivery) instead of the synchronous first attempt,
            # so a pick is credited SENT only when its report actually reached
            # Telegram, and DELIVERY_FAILED only when the outbox exhausts retries.
            ("native_delivery_key", "ADD COLUMN native_delivery_key TEXT"),
            # Production ("Your System") DECISION captured at observe time so the
            # comparison grades/ranks the native side by what production ACTUALLY
            # predicted — its sent ``predicted_side`` (from the CALIBRATED prob),
            # its calibrated probability, and its cross-asset ``rank`` — NOT the raw
            # champion probability stored in ``control_prob_yes``. Raw and calibrated
            # land on opposite sides of 0.5 in ~34% of rows, so deriving the side
            # from the raw prob mis-graded ~38% of native picks (a real loss showed
            # as ✓). Nullable; legacy rows fall back to ``control_prob_yes >= 0.5``.
            ("native_predicted_side", "ADD COLUMN native_predicted_side TEXT"),
            ("native_prob_yes", "ADD COLUMN native_prob_yes REAL"),
            ("native_rank", "ADD COLUMN native_rank INTEGER"),
        ):
            if name not in cols:
                self._conn.execute(f"ALTER TABLE shadow_predictions {ddl}")

    def close(self) -> None:
        self._conn.close()

    # ---- "Your System" sent-before-close marker (official grading rule) ----
    def mark_native_sent(self, contract: str, checkpoint: str,
                         model_version: str = _DEFAULT_MODEL_VERSION,
                         delivered_at: float | None = None) -> bool:
        """Flag the native (Your System) prediction for (contract, checkpoint) as
        delivered before close, so it counts in the VISIBLE record. Also stamps the
        delivery-status audit ('SENT' + timestamp, clearing any prior failure).
        Idempotent: a re-mark or a no-matching-row call is a harmless no-op (the row
        stays background)."""
        cur = self._conn.execute(
            "UPDATE shadow_predictions SET native_sent=1, native_delivery_status='SENT', "
            "native_delivery_error=NULL, native_delivery_at=? "
            "WHERE model_version=? AND contract=? AND checkpoint=? AND native_sent=0",
            (delivered_at if delivered_at is not None else time.time(),
             model_version, str(contract), str(checkpoint)),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def mark_native_delivery_failed(self, contract: str, checkpoint: str, error: str,
                                    model_version: str = _DEFAULT_MODEL_VERSION,
                                    failed_at: float | None = None) -> bool:
        """Record that Your System GENERATED a prediction for (contract, checkpoint)
        but the official send FAILED. The row stays background (native_sent=0, never
        in the visible win/loss totals), but the exact error and time are preserved
        so the prediction is auditable and not silently lost. Never overwrites a row
        already marked SENT (a real delivery wins). Idempotent on the error text."""
        cur = self._conn.execute(
            "UPDATE shadow_predictions SET native_delivery_status='DELIVERY_FAILED', "
            "native_delivery_error=?, native_delivery_at=? "
            "WHERE model_version=? AND contract=? AND checkpoint=? AND native_sent=0",
            (str(error)[:500], failed_at if failed_at is not None else time.time(),
             model_version, str(contract), str(checkpoint)),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def mark_native_pending(self, contract: str, checkpoint: str, delivery_key: str,
                            model_version: str = _DEFAULT_MODEL_VERSION) -> bool:
        """Tag Your System's pick for (contract, checkpoint) with the official
        report's idempotency key, WITHOUT deciding sent/failed yet. The send was
        durably queued in the outbox; the true outcome (a sync or worker delivery,
        or a dead-letter) is resolved later by ``reconcile_native_delivery``. This
        replaces the old "mark failed the instant the synchronous attempt didn't
        deliver", which mis-scored every worker-delivered report as a failure.
        Never overwrites a row already SENT. Idempotent."""
        cur = self._conn.execute(
            "UPDATE shadow_predictions SET native_delivery_key=?, "
            "native_delivery_status=COALESCE(native_delivery_status,'PENDING') "
            "WHERE model_version=? AND contract=? AND checkpoint=? AND native_sent=0",
            (str(delivery_key), model_version, str(contract), str(checkpoint)),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def reconcile_native_delivery(self, status_lookup: Any,
                                  model_version: str = _DEFAULT_MODEL_VERSION) -> dict[str, int]:
        """Resolve every still-pending native pick from the outbox's TRUE status.

        ``status_lookup(key)`` returns the outbox row status for an official
        report's idempotency key ('SENT', 'DEAD_LETTER', a transient state, or
        None). For each distinct key still awaiting a verdict:
          * 'SENT'        -> promote the report's picks to SENT (they count in the
                             visible record), crediting a delivery that happened on
                             the synchronous attempt OR a background-worker retry;
          * 'DEAD_LETTER' -> mark the picks DELIVERY_FAILED (retries exhausted);
          * anything else -> leave pending (the worker may still deliver).
        Returns counts of rows newly promoted / failed. Read-only wrt production."""
        keys = [
            str(row["native_delivery_key"])
            for row in self._conn.execute(
                "SELECT DISTINCT native_delivery_key FROM shadow_predictions "
                "WHERE model_version=? AND native_sent=0 AND native_delivery_key IS NOT NULL "
                "AND COALESCE(native_delivery_status,'PENDING') IN ('PENDING','DELIVERY_FAILED')",
                (model_version,),
            ).fetchall()
        ]
        promoted = failed = 0
        now = time.time()
        for key in keys:
            try:
                status = status_lookup(key)
            except Exception:
                status = None
            if status == "SENT":
                cur = self._conn.execute(
                    "UPDATE shadow_predictions SET native_sent=1, native_delivery_status='SENT', "
                    "native_delivery_error=NULL, native_delivery_at=? "
                    "WHERE model_version=? AND native_delivery_key=? AND native_sent=0",
                    (now, model_version, key),
                )
                promoted += cur.rowcount
            elif status == "DEAD_LETTER":
                cur = self._conn.execute(
                    "UPDATE shadow_predictions SET native_delivery_status='DELIVERY_FAILED', "
                    "native_delivery_error='outbox_dead_letter', native_delivery_at=? "
                    "WHERE model_version=? AND native_delivery_key=? AND native_sent=0 "
                    "AND COALESCE(native_delivery_status,'PENDING')<>'DELIVERY_FAILED'",
                    (now, model_version, key),
                )
                failed += cur.rowcount
        if promoted or failed:
            self._conn.commit()
        return {"promoted": promoted, "failed": failed}

    def delivery_audit(self, model_version: str = _DEFAULT_MODEL_VERSION) -> dict[str, int]:
        """Counts of native delivery outcomes for observability: how many generated
        Your System picks were SENT, failed (DELIVERY_FAILED), or are still pending
        (no status yet — generated this window, not yet sent/closed)."""
        rows = self._conn.execute(
            "SELECT COALESCE(native_delivery_status,'PENDING') AS s, COUNT(*) AS n "
            "FROM shadow_predictions WHERE model_version=? GROUP BY s",
            (model_version,),
        ).fetchall()
        out = {"SENT": 0, "DELIVERY_FAILED": 0, "PENDING": 0}
        for r in rows:
            out[str(r["s"])] = int(r["n"])
        return out

    def archived_versions(self, current_model_version: str) -> list[str]:
        """Model versions present in the file OTHER than the current one — the
        PRE-SYNCHRONIZED-RESET archive. These rows are retained for debugging /
        background research and are NEVER scored into the current visible record
        (every scoring query filters on the current model_version)."""
        rows = self._conn.execute(
            "SELECT DISTINCT model_version FROM shadow_predictions WHERE model_version<>? "
            "ORDER BY model_version",
            (current_model_version,),
        ).fetchall()
        return [str(r["model_version"]) for r in rows]

    # ---- reset marker (so the report can show "Comparison reset: <UTC>") ----
    def reset_marker(self, model_version: str, configured_reset_at: float = 0.0) -> float:
        """Return the reset timestamp for ``model_version``, stamping it on first use.

        The visible comparison for a model_version begins at this instant. Because
        all scoring filters on ``model_version``, predictions recorded under any
        OTHER version (e.g. the pre-reset ``challenger-v1`` rows) stay archived in
        the same file and are never mixed into the new record. The stamp is written
        once and never moved, so the displayed reset time is stable.
        """
        row = self._conn.execute(
            "SELECT reset_at FROM shadow_meta WHERE model_version=?", (model_version,)
        ).fetchone()
        if row is not None:
            return float(row["reset_at"])
        reset_at = float(configured_reset_at) if configured_reset_at else time.time()
        self._conn.execute(
            "INSERT OR IGNORE INTO shadow_meta (model_version, reset_at) VALUES (?,?)",
            (model_version, reset_at),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT reset_at FROM shadow_meta WHERE model_version=?", (model_version,)
        ).fetchone()
        return float(row["reset_at"]) if row else reset_at

    # ---- write path (immutable pre-settlement) ----
    def record(
        self,
        prediction,
        *,
        asset: str | None,
        contract: str | None,
        checkpoint: str | None,
        control_prob_yes: float | None,
        native_predicted_side: str | None = None,
        native_prob_yes: float | None = None,
        native_rank: int | None = None,
        settlement_source: str | None = None,
        created_at: float | None = None,
        close_time: float | None = None,
        model_version: str = _DEFAULT_MODEL_VERSION,
        lineage: dict | None = None,
        snapshot_id: str | None = None,
    ) -> int | None:
        dec = prediction.decision
        total_cost = dec.costs.total_cents if (dec and dec.costs) else None
        fv = prediction.feature_vector
        # Normalise the captured production decision (defensive: only YES/NO sides,
        # numeric prob/rank are stored; anything else is dropped to NULL so a bad
        # value never silently mis-grades).
        nps = str(native_predicted_side).upper() if native_predicted_side is not None else None
        if nps not in ("YES", "NO"):
            nps = None
        try:
            npy = float(native_prob_yes) if native_prob_yes is not None else None
        except (TypeError, ValueError):
            npy = None
        try:
            nrk = int(native_rank) if native_rank is not None else None
        except (TypeError, ValueError):
            nrk = None
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
            snapshot_id,
            nps, npy, nrk,
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
                 official_result, resolved_at, hypothetical_pnl_cents, snapshot_id,
                 native_predicted_side, native_prob_yes, native_rank)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                row,
            )
            self._conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None  # already recorded this (contract, checkpoint) — keep first

    # ---- settlement ----
    def resolve(self, contract: str, checkpoint: str, official_result: str,
                settled_at: float | None = None, model_version: str = _DEFAULT_MODEL_VERSION) -> bool:
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
    def training_samples(self, checkpoint: str | None = None, model_version: str = _DEFAULT_MODEL_VERSION):
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

    def scoreboard(self, model_version: str = _DEFAULT_MODEL_VERSION) -> dict[str, Any]:
        rows = list(self._conn.execute(
            "SELECT challenger_prob_yes, control_prob_yes, native_prob_yes, "
            "official_result, recommendation, "
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
        # Control = the champion's FINAL (calibrated) probability — the value the
        # system actually produces — so its accuracy/Brier match production and the
        # challenger-vs-champion comparison is calibrated-vs-calibrated (fair).
        # Legacy rows lacking the captured calibrated prob fall back to the raw
        # ``control_prob_yes``.
        def _control_prob(r):
            v = r["native_prob_yes"] if r["native_prob_yes"] is not None else r["control_prob_yes"]
            return clamp(float(v), 1e-6, 1 - 1e-6) if v is not None else None
        ct = [_control_prob(r) for r in rows]

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
            "control_prob_yes, native_predicted_side, native_rank, "
            "COALESCE(native_prob_yes, control_prob_yes) AS native_rank_prob, "
            "official_result, native_sent FROM shadow_predictions "
            "WHERE model_version=? AND official_result IS NOT NULL",
            (model_version,),
        ))
        cases: dict[tuple, list] = {}
        for r in rows:
            close = r["close_time"] if r["close_time"] is not None else r["created_at"]
            # Bucket by the 15-minute window (nearest boundary), NOT the exact close
            # second — otherwise per-asset/per-cycle close jitter shatters one window
            # into singleton cases, leaving only rank #1 and dropping intervals.
            wid = _window_id(close)
            if wid is None:
                continue
            key = (str(r["checkpoint"]), wid)
            cases.setdefault(key, []).append(r)
        return cases

    @staticmethod
    def _explicit_side(stored_side, fallback_prob) -> str | None:
        """The authoritative YES/NO side: the model's STORED decided side when it
        is a valid YES/NO, else derived from ``fallback_prob >= 0.5`` (legacy rows
        that predate decision capture). None only when neither is usable.

        This is the fix for the mis-grade: the native champion's sent side comes
        from its CALIBRATED probability, so re-thresholding the raw probability it
        was stored with flipped the call (and the ✓/✗) on ~38% of rows."""
        if stored_side is not None:
            s = str(stored_side).upper()
            if s in ("YES", "NO"):
                return s
        if fallback_prob is not None:
            return "YES" if float(fallback_prob) >= 0.5 else "NO"
        return None

    @staticmethod
    def _rank(rows, prob_key, sent_only: bool = False,
              side_key: str | None = None, rank_key: str | None = None):
        """Rank a case's predictions by confidence and grade each (asset, side, correct).

        Confidence ORDER: by ``rank_key`` (the model's own cross-asset rank, lower =
        more confident) when present, else by decisiveness ``|prob_key - 0.5|`` desc.
        SIDE / correctness: ``side_key`` (the model's AUTHORITATIVE decided side) when
        present and valid, else ``prob_key >= 0.5`` (legacy fallback). For the native
        "Your System" side this means the ✓/✗ matches the side production ACTUALLY
        sent (from its calibrated prob), and #1/#2/#3 follow production's real rank —
        not the raw champion probability the row was stored with. With ``sent_only``
        only delivered-before-close (``native_sent``) rows are eligible."""
        cand = [r for r in rows if r[prob_key] is not None
                and (not sent_only or r["native_sent"])]

        def _order(r):
            rv = r[rank_key] if rank_key is not None else None
            if rv is not None:
                # Ranked rows first, by production rank ascending (#1 most confident).
                return (0, float(rv), 0.0)
            # Unranked: most decisive first (negate so higher decisiveness sorts earlier).
            return (1, 0.0, -abs(float(r[prob_key]) - 0.5))
        cand.sort(key=_order)
        out = []
        for r in cand:
            stored = r[side_key] if side_key is not None else None
            side = ShadowLedger._explicit_side(stored, r[prob_key])
            correct = side == str(r["official_result"]).upper()
            out.append((r["asset"], side, correct))
        return out

    # Per-model ranking config. The native "Your System" side is graded/ranked from
    # production's CAPTURED decision (side, calibrated prob, rank) and is sent-gated
    # (visible only if delivered before close); the challenger shadow is graded on
    # its own calibrated ``challenger_prob_yes`` (the value that drives its decision)
    # and counts every created row. Keys: prob (confidence-ranking column, also the
    # side fallback), sent (sent-gated?), side_key (authoritative side column or None),
    # rank_key (explicit rank column or None).
    @staticmethod
    def _ranking_models(native_sent_only: bool) -> dict[str, dict]:
        return {
            "challenger": {"prob": "challenger_prob_yes", "sent": False,
                           "side_key": None, "rank_key": None},
            "native": {"prob": "native_rank_prob", "sent": bool(native_sent_only),
                       "side_key": "native_predicted_side", "rank_key": "native_rank"},
        }

    def ranked_comparison(self, model_version: str = _DEFAULT_MODEL_VERSION, top_k: int = 3,
                          native_sent_only: bool = True) -> dict[str, Any]:
        """Top-1/2/3 correctness for both models, scored per rank (no double count).

        Within each case, predictions are ranked by confidence. Rank k is correct
        if that pick's side matched the official result. Each case contributes at
        most one result per rank. Overall = sum over ranks 1..top_k. The native
        side counts only delivered-before-close predictions when sent-gated.
        """
        cases = self._resolved_cases(model_version)
        models = self._ranking_models(native_sent_only)
        stats = {m: {k: {"correct": 0, "wrong": 0} for k in range(1, top_k + 1)} for m in models}
        for case_rows in cases.values():
            for m, spec in models.items():
                ranked = self._rank(case_rows, spec["prob"], sent_only=spec["sent"],
                                    side_key=spec["side_key"], rank_key=spec["rank_key"])
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

    def ranked_by_checkpoint(self, model_version: str = _DEFAULT_MODEL_VERSION, top_k: int = 3,
                             checkpoints: tuple[str, ...] = REPORT_CHECKPOINTS,
                             native_sent_only: bool = True) -> dict[str, Any]:
        """All-time per-interval, per-rank correctness for BOTH systems.

        For each interval (15M/10M/7M) and each rank 1..top_k, returns
        {correct, wrong, accuracy} for the Shadow (challenger) and Your System
        (native), plus a per-interval total (summed over the ranks). Each resolved
        case contributes at most one result per (interval, rank) — the UNIQUE
        (model_version, contract, checkpoint) row key plus per-rank scoring means a
        prediction is graded exactly once. The native side counts only
        delivered-before-close predictions when sent-gated. Intervals with no
        settled case yet read 0W-0L / accuracy None so the report can show
        "0W-0L | N/A".
        """
        cases = self._resolved_cases(model_version)
        models = self._ranking_models(native_sent_only)

        def _blank():
            return {m: {k: {"correct": 0, "wrong": 0} for k in range(1, top_k + 1)}
                    for m in models}

        by_cp = {cp: _blank() for cp in checkpoints}
        for (cp, _close), case_rows in cases.items():
            bucket = by_cp.get(cp)
            if bucket is None:
                continue
            for m, spec in models.items():
                ranked = self._rank(case_rows, spec["prob"], sent_only=spec["sent"],
                                    side_key=spec["side_key"], rank_key=spec["rank_key"])
                for k in range(min(top_k, len(ranked))):
                    _, _, correct = ranked[k]
                    bucket[m][k + 1]["correct" if correct else "wrong"] += 1

        def _rank_dict(d):
            c, w = d["correct"], d["wrong"]
            n = c + w
            return {"correct": c, "wrong": w, "accuracy": round(c / n, 4) if n else None}

        out: dict[str, Any] = {"top_k": top_k, "checkpoints": list(checkpoints),
                               "by_checkpoint": {}}
        for cp in checkpoints:
            cp_out = {"challenger": {}, "native": {}}
            for m in models:
                tot_c = tot_w = 0
                for k in range(1, top_k + 1):
                    rd = _rank_dict(by_cp[cp][m][k])
                    cp_out[m][f"rank{k}"] = rd
                    tot_c += rd["correct"]
                    tot_w += rd["wrong"]
                n = tot_c + tot_w
                cp_out[m]["total"] = {"correct": tot_c, "wrong": tot_w,
                                      "accuracy": round(tot_c / n, 4) if n else None}
            out["by_checkpoint"][cp] = cp_out
        return out

    def latest_window_cases(self, model_version: str = _DEFAULT_MODEL_VERSION, top_k: int = 3,
                            native_sent_only: bool = True) -> dict[str, Any]:
        """Per-checkpoint top-k picks (both models) for the most recent settled
        close window — for the human-readable example block in the report. The
        native side lists only delivered-before-close picks when sent-gated."""
        cases = self._resolved_cases(model_version)
        if not cases:
            return {"close": None, "checkpoints": {}}
        # Case keys are (checkpoint, window_id); the latest window is the max id.
        # The true contract close = window_id * 900 (the 15-min boundary), a valid
        # epoch the report renders as HH:MM.
        latest_wid = max(wid for (_cp, wid) in cases)
        out = {"close": float(latest_wid) * WINDOW_SECONDS, "checkpoints": {}}
        for (cp, wid), rows in cases.items():
            if wid != latest_wid:
                continue
            out["checkpoints"][cp] = {
                "challenger": self._rank(rows, "challenger_prob_yes")[:top_k],
                "native": self._rank(rows, "native_rank_prob",
                                     sent_only=bool(native_sent_only),
                                     side_key="native_predicted_side",
                                     rank_key="native_rank")[:top_k],
            }
        return out

    def best_filled_window_cases(self, model_version: str = _DEFAULT_MODEL_VERSION, top_k: int = 3,
                                 native_sent_only: bool = True) -> dict[str, Any]:
        """Per-checkpoint ranked top-k picks (both models) from the settled window
        that best FILLS the ranks — so the END-RESULT grid 'tries its best' on every
        interval/rank instead of leaving slots blank.

        Each checkpoint is resolved INDEPENDENTLY (it is its own decision time), so a
        sparse just-closed 15M window no longer drops 10M/7M whose latest settled
        window is a little older. For each checkpoint we pick the MOST RECENT settled
        window that has at least ``top_k`` graded picks; if none in its history reaches
        ``top_k``, we fall back to the window with the most picks (ties → most recent),
        so we still show as many real, graded ranks as exist. A checkpoint with no
        settled window at all is simply absent (the renderer shows — for it). Returns
        ``{"checkpoints": {cp: {"close", "challenger", "native"}}}``; ``close`` is that
        checkpoint's own 15-min boundary so the row can be labelled with its window."""
        cases = self._resolved_cases(model_version)  # {(cp, wid): rows}
        by_cp: dict[str, list[tuple[int, list]]] = {}
        for (cp, wid), rows in cases.items():
            by_cp.setdefault(str(cp), []).append((int(wid), rows))
        out: dict[str, Any] = {"checkpoints": {}}
        for cp, windows in by_cp.items():
            windows.sort(key=lambda x: x[0], reverse=True)  # newest window first
            chosen = next((w for w in windows if len(w[1]) >= top_k), None)
            if chosen is None:
                # No window reaches top_k picks: take the fullest, newest on a tie.
                chosen = max(windows, key=lambda x: (len(x[1]), x[0]))
            wid, rows = chosen
            out["checkpoints"][cp] = {
                "close": float(wid) * WINDOW_SECONDS,
                "challenger": self._rank(rows, "challenger_prob_yes")[:top_k],
                "native": self._rank(rows, "native_rank_prob",
                                     sent_only=bool(native_sent_only),
                                     side_key="native_predicted_side",
                                     rank_key="native_rank")[:top_k],
            }
        return out

    def latest_window_end_results(self, model_version: str = _DEFAULT_MODEL_VERSION,
                                  checkpoints: tuple[str, ...] = REPORT_CHECKPOINTS,
                                  native_sent_only: bool = True) -> dict[str, Any]:
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
            "control_prob_yes, native_predicted_side, native_prob_yes, "
            "official_result, native_sent FROM shadow_predictions "
            "WHERE model_version=? AND official_result IS NOT NULL",
            (model_version,),
        ))
        if not rows:
            return {"close": None, "checkpoints": list(checkpoints), "assets": []}

        def _window(r):
            close = r["close_time"] if r["close_time"] is not None else r["created_at"]
            return _window_id(close)

        windows = [w for w in (_window(r) for r in rows) if w is not None]
        if not windows:
            return {"close": None, "checkpoints": list(checkpoints), "assets": []}
        latest = max(windows)
        want = set(checkpoints)
        by_asset: dict[str, dict] = {}
        # Display close = the window's true 15-min boundary, stable across assets.
        close_ts = float(latest) * WINDOW_SECONDS
        for r in rows:
            if _window(r) != latest or str(r["checkpoint"]) not in want:
                continue
            official = str(r["official_result"]).upper()
            a = by_asset.setdefault(str(r["asset"]), {
                "asset": str(r["asset"]), "official": official, "checkpoints": {}})

            def _challenger_hit(prob, _official=official):
                if prob is None:
                    return None
                side = "YES" if float(prob) >= 0.5 else "NO"
                return (side, side == _official)

            def _native_hit(_official=official, _sent=r["native_sent"],
                            _stored=r["native_predicted_side"],
                            _fallback=(r["native_prob_yes"] if r["native_prob_yes"] is not None
                                       else r["control_prob_yes"])):
                # Grade by the side production ACTUALLY sent (calibrated decision),
                # not the raw prob the row was stored with. A gated (native) call
                # shows only if delivered before close; otherwise it reads "—".
                if native_sent_only and not _sent:
                    return None
                side = ShadowLedger._explicit_side(_stored, _fallback)
                if side is None:
                    return None
                return (side, side == _official)

            a["checkpoints"][str(r["checkpoint"])] = {
                "challenger": _challenger_hit(r["challenger_prob_yes"]),
                "native": _native_hit(),
            }
        return {"close": close_ts, "checkpoints": list(checkpoints),
                "assets": sorted(by_asset.values(), key=lambda x: x["asset"])}

    def comparison(self, model_version: str = _DEFAULT_MODEL_VERSION,
                   native_sent_only: bool = True) -> dict[str, Any]:
        """Paired challenger-vs-control accuracy, overall and by checkpoint.

        Control = the production champion's probability stored alongside each
        shadow prediction, so this is strictly paired on identical contracts. The
        challenger accuracy is over all rows; the current (native) accuracy counts
        only delivered-before-close predictions when sent-gated.
        """
        rows = list(self._conn.execute(
            "SELECT checkpoint, challenger_prob_yes, control_prob_yes, "
            "native_predicted_side, native_prob_yes, official_result, "
            "native_sent FROM shadow_predictions WHERE model_version=? AND official_result IS NOT NULL",
            (model_version,),
        ))

        def _acc(subset):
            n = len(subset)
            if n == 0:
                return {"n": 0, "challenger_accuracy": None, "current_accuracy": None}
            cy = sum(1 for r in subset
                     if (float(r["challenger_prob_yes"]) >= 0.5) == (str(r["official_result"]).upper() == "YES"))
            # Native ("Your System") accuracy grades the side production ACTUALLY
            # sent (its calibrated decision), not the raw champion prob.
            ctl = [r for r in subset
                   if (r["native_predicted_side"] is not None or r["control_prob_yes"] is not None)
                   and (not native_sent_only or r["native_sent"])]
            cu = 0
            for r in ctl:
                side = ShadowLedger._explicit_side(
                    r["native_predicted_side"],
                    r["native_prob_yes"] if r["native_prob_yes"] is not None else r["control_prob_yes"])
                if side == str(r["official_result"]).upper():
                    cu += 1
            return {
                "n": n,
                "challenger_accuracy": round(cy / n, 4),
                "current_accuracy": round(cu / len(ctl), 4) if ctl else None,
            }

        out = {"overall": _acc(rows), "by_checkpoint": {}}
        for cp in sorted({str(r["checkpoint"]) for r in rows}):
            out["by_checkpoint"][cp] = _acc([r for r in rows if str(r["checkpoint"]) == cp])
        return out

    # ---- one-time repair for rows recorded before native-decision capture ----
    def backfill_native_decisions(self, lookup, model_version: str = _DEFAULT_MODEL_VERSION) -> dict[str, int]:
        """Stamp production's captured decision (predicted_side, calibrated prob,
        rank) onto existing rows that predate decision capture, so the historical
        record is re-graded by the side production ACTUALLY sent rather than the raw
        champion prob it was stored with.

        ``lookup(contract, checkpoint)`` returns ``(predicted_side, calibrated_prob_yes,
        rank)`` for the matching production prediction, or ``None`` when there is no
        match. Only rows still missing ``native_predicted_side`` are touched; a
        captured decision is never overwritten. Read-only wrt production. Returns
        ``{"scanned", "updated", "unmatched"}``."""
        rows = self._conn.execute(
            "SELECT id, contract, checkpoint FROM shadow_predictions "
            "WHERE model_version=? AND native_predicted_side IS NULL",
            (model_version,),
        ).fetchall()
        scanned = updated = unmatched = 0
        for r in rows:
            scanned += 1
            try:
                info = lookup(r["contract"], r["checkpoint"])
            except (KeyError, TypeError, ValueError):
                info = None
            if not info:
                unmatched += 1
                continue
            side = info[0] if len(info) > 0 else None
            s = str(side).upper() if side is not None else None
            if s not in ("YES", "NO"):
                unmatched += 1
                continue
            try:
                p = float(info[1]) if len(info) > 1 and info[1] is not None else None
            except (TypeError, ValueError):
                p = None
            try:
                rk = int(info[2]) if len(info) > 2 and info[2] is not None else None
            except (TypeError, ValueError):
                rk = None
            self._conn.execute(
                "UPDATE shadow_predictions SET native_predicted_side=?, "
                "native_prob_yes=COALESCE(?, native_prob_yes), "
                "native_rank=COALESCE(?, native_rank) WHERE id=?",
                (s, p, rk, r["id"]),
            )
            updated += 1
        if updated:
            self._conn.commit()
        return {"scanned": scanned, "updated": updated, "unmatched": unmatched}


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
