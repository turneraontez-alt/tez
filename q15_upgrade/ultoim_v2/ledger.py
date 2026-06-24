"""Ultoim V2 — isolated persistence, settlement grading, and scoreboard.

Completely separate from the live V9.5 ledger and from Ultoim Build: its own
SQLite file, its own tables, its own ids / counters / model version / reset
marker / session id. Rows are immutable once written (UNIQUE on
``(model_version, ticker, interval)``); settlement only fills the official
result / correctness / P&L and never rewrites a row.

Two locks enforce the V2 dedup contract:
  * ``ultoim_v2_report_lock`` — one RECORD per (interval, window): the first fire
    in a (interval, window) claims the lock and writes the chosen row.
  * ``ultoim_v2_alert_lock`` — one ALERT per (ticker, window): a contract may be
    recorded at several checkpoints in a window, but is alerted at most once.
"""
from __future__ import annotations

import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .fifteen_min import S15_VERSION

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ultoim_v2_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    model_version TEXT NOT NULL,
    asset TEXT NOT NULL,
    ticker TEXT NOT NULL,
    interval TEXT NOT NULL,
    window_key INTEGER NOT NULL,
    mark_seconds REAL,
    fired INTEGER NOT NULL DEFAULT 0,
    predicted_side TEXT,
    selected_probability REAL,
    calibrated_yes_probability REAL,
    conservative_probability REAL,
    market_implied_yes_probability REAL,
    raw_yes_probability REAL,
    net_edge_cents REAL,
    entry_ask_cents REAL,
    best_entry_cents INTEGER,
    fee_cents REAL,
    total_cost_cents REAL,
    spread_cents REAL,
    depth_contracts REAL,
    quote_age_seconds REAL,
    spot_stale_age_seconds REAL,
    distance_sigma REAL,
    regime_name TEXT,
    regime_directional TEXT,
    data_quality REAL,
    evidence_quality REAL,
    manipulation_suspected INTEGER DEFAULT 0,
    flip_probability REAL,
    order_flow_persistence REAL,
    book_resiliency REAL,
    prediction_stability REAL,
    x_market_flow REAL,
    gate_a_pass INTEGER,
    gate_b_pass INTEGER,
    gate_c_pass INTEGER,
    reason_codes TEXT,
    gate_min_conf REAL,
    gate_ask_lo REAL,
    gate_ask_hi REAL,
    gate_min_edge REAL,
    close_time REAL,
    snapshot_id TEXT,
    session_id TEXT,
    delivery_status TEXT NOT NULL DEFAULT 'PENDING',
    message_id INTEGER,
    delivery_error TEXT,
    alerted INTEGER DEFAULT 0,
    official_result TEXT,
    resolved_at REAL,
    correct INTEGER,
    hypothetical_pnl_cents REAL,
    base_rate_side TEXT,
    record_kind TEXT NOT NULL DEFAULT 'DELIVERED_CANDIDATE',
    research_fired INTEGER DEFAULT 0,
    conf_gap REAL,
    blowup_risk REAL,
    screen_version TEXT,
    s15_pass INTEGER,
    s15_codes TEXT,
    s15_cal_drift REAL,
    s15_version TEXT,
    pin_break_drift REAL,
    threshold_interaction REAL,
    champion_flow REAL,
    UNIQUE(model_version, ticker, interval)
);
CREATE INDEX IF NOT EXISTS idx_ultoim_v2_resolve
    ON ultoim_v2_predictions(model_version, official_result, close_time);

CREATE TABLE IF NOT EXISTS ultoim_v2_report_lock (
    model_version TEXT NOT NULL,
    interval TEXT NOT NULL,
    window_key INTEGER NOT NULL,
    locked_at REAL NOT NULL,
    message_id INTEGER,
    PRIMARY KEY(model_version, interval, window_key)
);

CREATE TABLE IF NOT EXISTS ultoim_v2_alert_lock (
    model_version TEXT NOT NULL,
    ticker TEXT NOT NULL,
    window_key INTEGER NOT NULL,
    locked_at REAL NOT NULL,
    PRIMARY KEY(model_version, ticker, window_key)
);

CREATE TABLE IF NOT EXISTS ultoim_v2_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS ultoim_v2_exit_warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    model_version TEXT NOT NULL,
    asset TEXT,
    ticker TEXT NOT NULL,
    window_key INTEGER NOT NULL,
    entry_side TEXT,
    entry_ask_cents REAL,
    entry_interval TEXT,
    entry_created_at REAL,
    warn_seconds_remaining REAL,
    flipped_to_side TEXT,
    flip_calibrated_yes REAL,
    flip_selected_probability REAL,
    flip_market_implied_yes REAL,
    confirm_cycles INTEGER,
    confirm_span_seconds REAL,
    exit_value_cents REAL,
    evidence_json TEXT,
    close_time REAL,
    session_id TEXT,
    delivery_status TEXT NOT NULL DEFAULT 'PENDING',
    message_id INTEGER,
    delivery_error TEXT,
    official_result TEXT,
    resolved_at REAL,
    warning_correct INTEGER,
    false_alarm INTEGER,
    recovered_cents REAL,
    forfeited_cents REAL,
    UNIQUE(model_version, ticker, window_key)
);
CREATE INDEX IF NOT EXISTS idx_ultoim_v2_exit_resolve
    ON ultoim_v2_exit_warnings(model_version, official_result, close_time);
"""


def _window_key(close_time: float | None, now: float) -> int:
    """Bucket a contract into its 15-minute settlement window so all assets that
    settle together share one report lock."""
    basis = float(close_time) if close_time is not None else float(now)
    return int(basis // 900)


def _wilson(right: int, n: int, z: float = 1.96) -> tuple[float | None, float | None, float | None]:
    if n <= 0:
        return None, None, None
    p = right / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return p, centre - half, centre + half


def _research_agg(rows: "Sequence[sqlite3.Row]") -> dict[str, Any]:
    """Shared accuracy + P&L aggregate for the record-only research scoreboards
    (s15, distance). Uses ``hypothetical_pnl_cents`` (win=100-ask, loss=-ask); pure,
    read-only, never reads the gate or affects a decision."""
    n = len(rows)
    right = sum(1 for r in rows if r["correct"] == 1)
    pnls = [float(r["hypothetical_pnl_cents"]) for r in rows
            if r["hypothetical_pnl_cents"] is not None]
    p, lo, hi = _wilson(right, n)
    return {
        "n": n, "right": right, "wrong": n - right,
        "accuracy": p, "ci_low": lo, "ci_high": hi,
        "pnl_total_cents": round(sum(pnls), 2) if pnls else 0.0,
        "pnl_avg_cents": round(sum(pnls) / len(pnls), 3) if pnls else None,
    }


class UltoimV2Ledger:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Idempotently add columns introduced after the first release so an
        already-initialised DB (``CREATE TABLE IF NOT EXISTS`` skips schema changes)
        gains them without a destructive rebuild. Each ALTER uses a constant
        DEFAULT, which SQLite backfills onto existing rows."""
        cols = {
            r["name"]
            for r in self._conn.execute(
                "PRAGMA table_info(ultoim_v2_predictions)"
            ).fetchall()
        }
        additions = (
            ("record_kind", "TEXT NOT NULL DEFAULT 'DELIVERED_CANDIDATE'"),
            ("research_fired", "INTEGER DEFAULT 0"),
            # Record-only blowup SHADOW score (see screen.py). Nullable, never gates.
            ("conf_gap", "REAL"),
            ("blowup_risk", "REAL"),
            ("screen_version", "TEXT"),
            # Record-only cross-asset market flow (measure-first; see runner). Nullable, never gates.
            ("x_market_flow", "REAL"),
            # Record-only 15M selective-entry research SCREEN (see fifteen_min.py). All
            # nullable; populated for 15M-NO rows only; NEVER read by the gate.
            ("s15_pass", "INTEGER"),
            ("s15_codes", "TEXT"),
            ("s15_cal_drift", "REAL"),
            ("s15_version", "TEXT"),
            # Record-only pin-break shadow features (see runner). Nullable, never gate.
            # pin_break_drift = structural z_score (drift through the strike);
            # threshold_interaction = champion's scalar threshold feature (failed-break family).
            ("pin_break_drift", "REAL"),
            ("threshold_interaction", "REAL"),
            # Record-only champion per-asset directional flow factor (feature_values
            # ["flow"]); the flow-against-NO abstain candidate. Nullable, never gates.
            ("champion_flow", "REAL"),
        )
        for name, decl in additions:
            if name not in cols:
                self._conn.execute(
                    f"ALTER TABLE ultoim_v2_predictions ADD COLUMN {name} {decl}"
                )

    # -- meta / reset marker + session id ------------------------------------
    def ensure_reset_marker(self, model_version: str, now: float) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM ultoim_v2_meta WHERE key='reset_at'"
            ).fetchone()
            if row is not None:
                # Guarantee a session id even on an already-initialised DB.
                sess = self._conn.execute(
                    "SELECT value FROM ultoim_v2_meta WHERE key='session_id'"
                ).fetchone()
                if sess is None:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO ultoim_v2_meta(key,value) VALUES('session_id',?)",
                        (str(now),),
                    )
                    self._conn.commit()
                return float(row["value"])
            self._conn.execute(
                "INSERT OR REPLACE INTO ultoim_v2_meta(key,value) VALUES('reset_at',?)",
                (str(now),),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO ultoim_v2_meta(key,value) VALUES('model_version',?)",
                (model_version,),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO ultoim_v2_meta(key,value) VALUES('session_id',?)",
                (str(now),),
            )
            self._conn.commit()
            return now

    def reset_at(self) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM ultoim_v2_meta WHERE key='reset_at'"
            ).fetchone()
        return float(row["value"]) if row is not None else None

    def session_id(self) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM ultoim_v2_meta WHERE key='session_id'"
            ).fetchone()
        return str(row["value"]) if row is not None else None

    # -- report lock (one RECORD per interval per window) ---------------------
    def report_locked(self, model_version: str, interval: str, window_key: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM ultoim_v2_report_lock "
                "WHERE model_version=? AND interval=? AND window_key=?",
                (model_version, interval, window_key),
            ).fetchone()
        return row is not None

    def lock_report(self, model_version: str, interval: str, window_key: int, now: float) -> bool:
        """Claim the (interval, window) lock. Returns True iff this call created it
        (idempotent — a duplicate/concurrent fire is rejected)."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO ultoim_v2_report_lock"
                "(model_version,interval,window_key,locked_at) VALUES(?,?,?,?)",
                (model_version, interval, window_key, now),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def set_report_message(self, model_version: str, interval: str, window_key: int,
                           message_id: int | None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE ultoim_v2_report_lock SET message_id=? "
                "WHERE model_version=? AND interval=? AND window_key=?",
                (message_id, model_version, interval, window_key),
            )
            self._conn.commit()

    # -- alert lock (one ALERT per CONTRACT per window, across checkpoints) ----
    def alert_locked(self, model_version: str, ticker: str, window_key: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM ultoim_v2_alert_lock "
                "WHERE model_version=? AND ticker=? AND window_key=?",
                (model_version, ticker, window_key),
            ).fetchone()
        return row is not None

    def claim_alert(self, model_version: str, ticker: str, window_key: int, now: float) -> bool:
        """Claim the (ticker, window) alert lock. Returns True iff this is the FIRST
        claim — i.e. this contract has not yet been alerted in this window."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO ultoim_v2_alert_lock"
                "(model_version,ticker,window_key,locked_at) VALUES(?,?,?,?)",
                (model_version, ticker, window_key, now),
            )
            self._conn.commit()
            return cur.rowcount == 1

    # -- record a decision (immutable) ----------------------------------------
    _COLS = (
        "created_at", "model_version", "asset", "ticker", "interval", "window_key",
        "mark_seconds", "fired", "predicted_side", "selected_probability",
        "calibrated_yes_probability", "conservative_probability",
        "market_implied_yes_probability", "raw_yes_probability", "net_edge_cents",
        "entry_ask_cents", "best_entry_cents", "fee_cents", "total_cost_cents",
        "spread_cents", "depth_contracts", "quote_age_seconds", "spot_stale_age_seconds",
        "distance_sigma", "regime_name", "regime_directional", "data_quality",
        "evidence_quality", "manipulation_suspected", "flip_probability",
        "order_flow_persistence", "book_resiliency", "prediction_stability",
        "x_market_flow",
        "gate_a_pass", "gate_b_pass", "gate_c_pass", "reason_codes",
        "gate_min_conf", "gate_ask_lo", "gate_ask_hi", "gate_min_edge",
        "close_time", "snapshot_id", "session_id", "delivery_status",
        "record_kind", "research_fired", "conf_gap", "blowup_risk", "screen_version",
        "s15_pass", "s15_codes", "s15_cal_drift", "s15_version",
        "pin_break_drift", "threshold_interaction", "champion_flow",
    )

    # Sensible defaults for columns added after the first release, so any caller
    # that builds a row without them (e.g. older tools/tests) still inserts cleanly
    # rather than tripping the NOT NULL constraint.
    _COL_DEFAULTS = {"record_kind": "DELIVERED_CANDIDATE", "research_fired": 0}

    def record_decision(self, row: Mapping[str, Any]) -> int | None:
        placeholders = ",".join("?" for _ in self._COLS)
        values = [row.get(c) if row.get(c) is not None else self._COL_DEFAULTS.get(c)
                  for c in self._COLS]
        with self._lock:
            try:
                cur = self._conn.execute(
                    f"INSERT INTO ultoim_v2_predictions({','.join(self._COLS)}) "
                    f"VALUES({placeholders})",
                    values,
                )
                self._conn.commit()
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                # Duplicate (model_version, ticker, interval) — already recorded.
                return None

    def mark_delivery(self, row_id: int, status: str, message_id: int | None,
                      error: str | None = None) -> None:
        alerted = 1 if status == "SENT" else 0
        with self._lock:
            self._conn.execute(
                "UPDATE ultoim_v2_predictions "
                "SET delivery_status=?, message_id=?, delivery_error=?, alerted=? "
                "WHERE id=?",
                (status, message_id, error, alerted, row_id),
            )
            self._conn.commit()

    # -- settlement grading ---------------------------------------------------
    def unresolved_closed(self, model_version: str, now: float) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT ticker FROM ultoim_v2_predictions "
                "WHERE model_version=? AND official_result IS NULL "
                "AND close_time IS NOT NULL AND close_time <= ? LIMIT 500",
                (model_version, now),
            ).fetchall()
        return [str(r["ticker"]) for r in rows]

    def resolve(self, model_version: str, ticker: str, official_result: str,
                now: float | None = None) -> int:
        """Grade every ungraded row for a settled ticker. Idempotent (acts only on
        rows with official_result IS NULL). Returns rows graded."""
        official = str(official_result).upper()
        if official not in ("YES", "NO"):
            return 0
        ts = time.time() if now is None else now
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, predicted_side, entry_ask_cents FROM ultoim_v2_predictions "
                "WHERE model_version=? AND ticker=? AND official_result IS NULL",
                (model_version, ticker),
            ).fetchall()
            graded = 0
            for r in rows:
                side = str(r["predicted_side"] or "").upper()
                correct = 1 if side == official else 0
                ask = r["entry_ask_cents"]
                pnl = None
                if ask is not None:
                    pnl = (100.0 - float(ask)) if correct else (-float(ask))
                self._conn.execute(
                    "UPDATE ultoim_v2_predictions SET official_result=?, resolved_at=?, "
                    "correct=?, hypothetical_pnl_cents=? WHERE id=?",
                    (official, ts, correct, pnl, r["id"]),
                )
                graded += 1
            self._conn.commit()
        return graded

    # -- scoreboard -----------------------------------------------------------
    def scoreboard(self, model_version: str, *, min_n: int = 30) -> dict[str, Any]:
        with self._lock:
            all_rows = self._conn.execute(
                "SELECT * FROM ultoim_v2_predictions WHERE model_version=?",
                (model_version,),
            ).fetchall()
            counts = dict(self._conn.execute(
                "SELECT delivery_status, COUNT(*) FROM ultoim_v2_predictions "
                "WHERE model_version=? GROUP BY delivery_status", (model_version,),
            ).fetchall())
        # Headline 'entries' stats: delivered + fired rows only. all_observations
        # exposes every recorded row (fired + abstains).
        entries = [r for r in all_rows
                   if r["fired"] == 1 and r["official_result"] is not None]
        all_resolved = [r for r in all_rows if r["official_result"] is not None]
        out: dict[str, Any] = {
            "available": True,
            "model_version": model_version,
            "reset_at": self.reset_at(),
            "session_id": self.session_id(),
            "min_n": min_n,
            "resolved": len(entries),
            "total_recorded": len(all_rows),
            "delivery_counts": {str(k): int(v) for k, v in counts.items()},
            "all_observations": {
                "recorded": len(all_rows),
                "resolved": len(all_resolved),
                "fired": sum(1 for r in all_rows if r["fired"] == 1),
            },
        }
        # The research population: every row that PASSED the gate (conf+ask+edge),
        # regardless of side. For NO rows this equals ``entries`` (side passes too);
        # YES rows are research-only (never delivered) and appear here so YES-prone
        # windows are finally measurable.
        #
        # ``gate_b_pass AND gate_c_pass`` is the PRIMARY, authoritative source: those
        # two columns are in the ORIGINAL table schema (they predate ``research_fired``)
        # so they are present on EVERY row, and by construction ``research_fired==1`` is
        # exactly ``gate_b_pass==1 AND gate_c_pass==1``. Deriving from them recovers YES
        # research rows whose ``research_fired`` was absent and got backfilled to 0 by the
        # schema migration (the delivered ``fired`` flag is structurally 0 for every YES
        # row, so falling back to it would silently undercount the YES side). The older
        # fallbacks remain for rows / test-fixtures that lack the gate columns.
        def _research_fired(r: sqlite3.Row) -> bool:
            gb, gc = r["gate_b_pass"], r["gate_c_pass"]
            if gb is not None and gc is not None:
                return int(gb) == 1 and int(gc) == 1
            rf = r["research_fired"]
            if rf is not None:
                return int(rf) == 1
            return int(r["fired"] or 0) == 1

        research = [r for r in all_rows
                    if _research_fired(r) and r["official_result"] is not None]
        out["overall"] = self._agg(entries, min_n)
        out["overall"].update(self._base_rate(entries))
        out["by_interval"] = {iv: self._agg([r for r in entries if r["interval"] == iv], min_n)
                              for iv in ("15M", "10M", "7M")}
        out["by_regime_directional"] = {
            rd: self._agg([r for r in entries
                           if str(r["regime_directional"] or "") == rd], min_n)
            for rd in ("YES_PRONE", "NO_PRONE", "BALANCED")
        }
        # Per-side split over the research population (NO = delivered entries,
        # YES = research-only). Each carries its own base-rate/edge so the YES side
        # can be judged against the same standard the NO side is held to.
        out["by_side"] = {}
        for sd in ("NO", "YES"):
            side_rows = [r for r in research
                         if str(r["predicted_side"] or "").upper() == sd]
            agg = self._agg(side_rows, min_n)
            agg.update(self._base_rate(side_rows))
            out["by_side"][sd] = agg
        out["research_resolved"] = len(research)
        return out

    @staticmethod
    def _agg(rows: Sequence[sqlite3.Row], min_n: int) -> dict[str, Any]:
        n = len(rows)
        right = sum(1 for r in rows if r["correct"] == 1)
        pnl_rows = [float(r["hypothetical_pnl_cents"]) for r in rows
                    if r["hypothetical_pnl_cents"] is not None]
        p, lo, hi = _wilson(right, n)
        return {
            "n": n, "right": right, "wrong": n - right,
            "accuracy": p, "ci_low": lo, "ci_high": hi,
            "low_n": n < min_n,
            "pnl_n": len(pnl_rows),
            "pnl_total_cents": round(sum(pnl_rows), 2) if pnl_rows else 0.0,
            "pnl_avg_cents": round(sum(pnl_rows) / len(pnl_rows), 3) if pnl_rows else None,
        }

    @staticmethod
    def _base_rate(rows: Sequence[sqlite3.Row]) -> dict[str, Any]:
        """Majority-class accuracy (always bet the more common settled side) and the
        model's edge over it. The base_rate_side is the majority settled outcome."""
        n = len(rows)
        if n == 0:
            return {"base_rate": None, "base_rate_side": None, "edge_over_base": None}
        n_no = sum(1 for r in rows if str(r["official_result"] or "").upper() == "NO")
        n_yes = sum(1 for r in rows if str(r["official_result"] or "").upper() == "YES")
        base_rate = max(n_no, n_yes) / n
        base_side = "NO" if n_no >= n_yes else "YES"
        right = sum(1 for r in rows if r["correct"] == 1)
        accuracy = right / n
        return {
            "base_rate": base_rate,
            "base_rate_side": base_side,
            "edge_over_base": accuracy - base_rate,
        }

    def loss_rows(self, model_version: str, limit: int = 20) -> list[dict[str, Any]]:
        """The wrong (correct=0) fired entries with their full feature vector, for
        diagnostics. Most-recently-resolved first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ultoim_v2_predictions "
                "WHERE model_version=? AND fired=1 AND correct=0 "
                "ORDER BY resolved_at DESC LIMIT ?",
                (model_version, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def s15_research_scoreboard(self, model_version: str) -> dict[str, Any]:
        """Read-only: how the 15M selective-entry research SCREEN (record-only; see
        ``fifteen_min.py``) WOULD have performed, vs the full settled 15M-NO book.
        NEVER affects delivery — it purely measures the recorded ``s15_pass`` verdict
        so the rule can prove (or disprove) itself on prospective, cross-session data.
        ``book`` is the unfiltered 15M-NO baseline; ``gated`` is the s15_pass=1 subset
        the screen would have taken; the two ``gated_with_*`` cuts add a tilt."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT correct, hypothetical_pnl_cents, s15_pass, s15_codes "
                "FROM ultoim_v2_predictions "
                "WHERE model_version=? AND interval='15M' AND predicted_side='NO' "
                "AND official_result IS NOT NULL",
                (model_version,),
            ).fetchall()

        gated = [r for r in rows if r["s15_pass"] == 1]

        def _with(code: str) -> list[sqlite3.Row]:
            return [r for r in gated if code in (r["s15_codes"] or "").split(",")]

        return {
            "available": True,
            "version": S15_VERSION,
            "book": _research_agg(rows),
            "gated": _research_agg(gated),
            "gated_with_cal_drift": _research_agg(_with("CAL_DRIFT")),
            "gated_with_fresh": _research_agg(_with("FRESH")),
        }

    def distance_research_scoreboard(self, model_version: str,
                                     pin_sigma: float = 0.15) -> dict[str, Any]:
        """Read-only: distance-to-strike (``distance_sigma``) is the one record-only
        shadow feature shown to TRANSPORT across ledgers (near-strike NO loses, far NO
        wins). This measures the candidate abstention prospectively over the settled NO
        rows: NEAR-strike (``|distance_sigma| < pin_sigma`` — the would-abstain pin) vs
        FAR (the would-keep). NEVER gates delivery; pure measurement so the rule can
        prove (or disprove) itself on accruing data.

        ``by_interval`` (keyed "15M"/"10M"/"7M") repeats the same near/far split per
        checkpoint because the top-level aggregate is interval-blind and MISLEADING: on
        real data the aggregate near-pin looks benign only because profitable 10M/7M
        near-strike entries mask the toxic 15M near-strike bucket — which is the bucket
        the 15M-scoped distance gate keys on, and whose N gates the record-first go/no-go.
        Surfacing it per-interval makes that 15M signal visible. Still record-only."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT correct, hypothetical_pnl_cents, distance_sigma, interval "
                "FROM ultoim_v2_predictions "
                "WHERE model_version=? AND predicted_side='NO' "
                "AND official_result IS NOT NULL AND distance_sigma IS NOT NULL",
                (model_version,),
            ).fetchall()

        def _split(subset: "Sequence[sqlite3.Row]") -> dict[str, Any]:
            near = [r for r in subset if abs(float(r["distance_sigma"])) < pin_sigma]
            far = [r for r in subset if abs(float(r["distance_sigma"])) >= pin_sigma]
            return {
                "book": _research_agg(subset),
                "near_pin": _research_agg(near),   # would-abstain (near strike / pin)
                "far": _research_agg(far),         # would-keep (far from strike)
            }

        return {
            "available": True,
            "pin_sigma": pin_sigma,
            **_split(rows),
            "by_interval": {
                iv: _split([r for r in rows if r["interval"] == iv])
                for iv in ("15M", "10M", "7M")
            },
        }

    def flow_research_scoreboard(self, model_version: str,
                                 flow_threshold: float = 0.6) -> dict[str, Any]:
        """Read-only: the flow-against-NO abstain candidate — the single fix that lifted
        P&L ~50% out-of-sample on the v95 NO side (and survived a 3-way time-split +
        leave-one-asset-out). Measures it prospectively over the settled NO rows two ways:
          * by the champion per-asset flow factor (``champion_flow``): would-ABSTAIN
            (flow >= threshold, a NO bet against strong buy-side flow) vs would-KEEP.
            Empty until the live engine records champion_flow.
          * by the v2-native proxy ``regime_directional == 'YES_PRONE'`` (market leans
            YES against our NO) — already recorded, so this cut has data today.
        NEVER gates delivery; pure measurement so the fix can prove itself before gating."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT correct, hypothetical_pnl_cents, champion_flow, regime_directional "
                "FROM ultoim_v2_predictions "
                "WHERE model_version=? AND predicted_side='NO' "
                "AND official_result IS NOT NULL",
                (model_version,),
            ).fetchall()
        # champion_flow cut (None flow => kept; can't abstain without the signal).
        flow_abstain = [r for r in rows if r["champion_flow"] is not None
                        and float(r["champion_flow"]) >= flow_threshold]
        flow_keep = [r for r in rows if not (r["champion_flow"] is not None
                                             and float(r["champion_flow"]) >= flow_threshold)]
        # v2-native regime proxy cut (has data now).
        regime_abstain = [r for r in rows if str(r["regime_directional"] or "") == "YES_PRONE"]
        regime_keep = [r for r in rows if str(r["regime_directional"] or "") != "YES_PRONE"]
        return {
            "available": True,
            "flow_threshold": flow_threshold,
            "book": _research_agg(rows),
            "flow_abstain": _research_agg(flow_abstain),   # would-cut (flow against NO)
            "flow_keep": _research_agg(flow_keep),         # would-keep
            "regime_abstain": _research_agg(regime_abstain),  # v2-native proxy: would-cut
            "regime_keep": _research_agg(regime_keep),
        }

    def resolved_rows(self, model_version: str) -> list[dict[str, Any]]:
        """Every settled row (any side, any record_kind) as plain dicts — the input
        to the read-only validation module. Ordered oldest-first so a time-based
        train/test split is deterministic."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ultoim_v2_predictions "
                "WHERE model_version=? AND official_result IS NOT NULL "
                "ORDER BY created_at ASC, id ASC",
                (model_version,),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_rows(self, model_version: str, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ultoim_v2_predictions WHERE model_version=? "
                "ORDER BY created_at DESC LIMIT ?",
                (model_version, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- defensive-exit / flip warnings ---------------------------------------
    def find_fired_entry(self, model_version: str, ticker: str,
                         window_key: int) -> dict[str, Any] | None:
        """The paper entry that was SUGGESTED earlier in this window for this
        contract (fired=1), if any — the precondition for an exit warning. Earliest
        fired checkpoint wins (that's when the owner would have entered)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM ultoim_v2_predictions "
                "WHERE model_version=? AND ticker=? AND window_key=? AND fired=1 "
                "ORDER BY mark_seconds DESC, created_at ASC LIMIT 1",
                (model_version, ticker, window_key),
            ).fetchone()
        return dict(row) if row is not None else None

    def exit_warning_recorded(self, model_version: str, ticker: str,
                              window_key: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM ultoim_v2_exit_warnings "
                "WHERE model_version=? AND ticker=? AND window_key=?",
                (model_version, ticker, window_key),
            ).fetchone()
        return row is not None

    _EXIT_COLS = (
        "created_at", "model_version", "asset", "ticker", "window_key",
        "entry_side", "entry_ask_cents", "entry_interval", "entry_created_at",
        "warn_seconds_remaining", "flipped_to_side", "flip_calibrated_yes",
        "flip_selected_probability", "flip_market_implied_yes", "confirm_cycles",
        "confirm_span_seconds", "exit_value_cents", "evidence_json", "close_time",
        "session_id", "delivery_status",
    )

    def record_exit_warning(self, row: Mapping[str, Any]) -> int | None:
        """Insert one exit warning. One per (model_version, ticker, window) — a
        duplicate (restart / concurrent) returns None rather than re-warning."""
        placeholders = ",".join("?" for _ in self._EXIT_COLS)
        values = [row.get(c) for c in self._EXIT_COLS]
        with self._lock:
            try:
                cur = self._conn.execute(
                    f"INSERT INTO ultoim_v2_exit_warnings({','.join(self._EXIT_COLS)}) "
                    f"VALUES({placeholders})",
                    values,
                )
                self._conn.commit()
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                return None

    def mark_exit_delivery(self, warning_id: int, status: str,
                           message_id: int | None, error: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE ultoim_v2_exit_warnings "
                "SET delivery_status=?, message_id=?, delivery_error=? WHERE id=?",
                (status, message_id, error, warning_id),
            )
            self._conn.commit()

    def unresolved_exit_warnings(self, model_version: str, now: float) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT ticker FROM ultoim_v2_exit_warnings "
                "WHERE model_version=? AND official_result IS NULL "
                "AND close_time IS NOT NULL AND close_time <= ? LIMIT 500",
                (model_version, now),
            ).fetchall()
        return [str(r["ticker"]) for r in rows]

    def resolve_exit_warning(self, model_version: str, ticker: str,
                             official_result: str, now: float | None = None) -> int:
        """Grade every ungraded exit warning for a settled ticker. The warning was
        CORRECT iff the suggested ENTRY side lost (official_result != entry_side) —
        i.e. exiting saved you. A FALSE ALARM iff the entry side actually won (the
        flip scared you out of a winner). Idempotent. Returns rows graded."""
        official = str(official_result).upper()
        if official not in ("YES", "NO"):
            return 0
        ts = time.time() if now is None else now
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, entry_side, entry_ask_cents, exit_value_cents "
                "FROM ultoim_v2_exit_warnings "
                "WHERE model_version=? AND ticker=? AND official_result IS NULL",
                (model_version, ticker),
            ).fetchall()
            graded = 0
            for r in rows:
                entry_side = str(r["entry_side"] or "").upper()
                entry_lost = entry_side != official
                correct = 1 if entry_lost else 0
                false_alarm = 0 if entry_lost else 1
                exit_val = r["exit_value_cents"]
                ask = r["entry_ask_cents"]
                # Recovered: if the entry would have lost (settles 0), exiting at the
                # exit value recovers that many cents. Forfeited: if the entry would
                # have won (settles 100), exiting forfeits the (100-ask) profit minus
                # whatever the exit value returned over the ask is moot — the cost is
                # the win you walked away from.
                recovered = float(exit_val) if (entry_lost and exit_val is not None) else 0.0
                forfeited = 0.0
                if (not entry_lost) and ask is not None:
                    forfeited = max(0.0, 100.0 - float(ask))
                self._conn.execute(
                    "UPDATE ultoim_v2_exit_warnings SET official_result=?, resolved_at=?, "
                    "warning_correct=?, false_alarm=?, recovered_cents=?, forfeited_cents=? "
                    "WHERE id=?",
                    (official, ts, correct, false_alarm, round(recovered, 2),
                     round(forfeited, 2), r["id"]),
                )
                graded += 1
            self._conn.commit()
        return graded

    def exit_warning_scoreboard(self, model_version: str, *, min_n: int = 10) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM ultoim_v2_exit_warnings WHERE model_version=?",
                (model_version,),
            ).fetchall()
        total = len(rows)
        resolved = [r for r in rows if r["official_result"] is not None]
        n = len(resolved)
        correct = sum(1 for r in resolved if r["warning_correct"] == 1)
        false_alarms = sum(1 for r in resolved if r["false_alarm"] == 1)
        recovered = sum(float(r["recovered_cents"] or 0.0) for r in resolved)
        forfeited = sum(float(r["forfeited_cents"] or 0.0) for r in resolved)
        p, lo, hi = _wilson(correct, n)
        return {
            "total": total,
            "resolved": n,
            "pending": total - n,
            "correct": correct,
            "false_alarms": false_alarms,
            "precision": (correct / n) if n else None,
            "ci_low": lo,
            "ci_high": hi,
            "low_n": n < min_n,
            "recovered_cents": round(recovered, 2),
            "forfeited_cents": round(forfeited, 2),
            "net_cents": round(recovered - forfeited, 2),
            "avg_recovered_per_correct": round(recovered / correct, 2) if correct else None,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
