"""Drift-hypothesis shadow recorder (13M / ask 65-73 / alt / taker, record-only).

Forward-tests ONE pre-registered hypothesis discovered by the constrained
strategy search (see HANDOFF): near-strike YES contracts benefit from crypto's
small upward intraday drift, so at the 13M mark an at-the-money YES pick with
low flip-risk is underpriced by the 65-73 market. This module records every
qualifying pick (one per 15m interval) and grades it on settlement so the edge
can be confirmed or killed on data the search never saw.

It NEVER trades, notifies, or touches any live path. Pure observation, exactly
like interval_research: it writes to its own SQLite ledger and exposes a
scoreboard with the frozen kill/promote bars.

Pre-registered rule (frozen 2026-07-06; env-overridable for research only):
  interval == 13M, asset not in {BTC, ETH}, ask in [65, 73],
  distance_sigma <= 3e-5 (near-strike), flip_probability <= 30, side == YES.
  If multiple candidates qualify in an interval, take the highest model-vs-market
  disagreement (side_prob - ask/100); one pick per interval; else NO PICK.

Pre-registered decision bars (evaluated on FORWARD, post-2026-07-06 rows):
  KILL    at n>=40  if EV/pick <= 0 or win rate < breakeven.
  PROMOTE at n>=150 if EV/pick >= +2c AND Wilson-95 lower bound on win rate
          > breakeven AND no single day > 40% of total P&L AND >= 3 distinct
          assets contribute.
"""
from __future__ import annotations

import math
import os
import sqlite3
import time
from typing import Any, Mapping, Sequence

FEATURES_VERSION = "drift-shadow-v1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS drift_picks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    model_version TEXT,
    asset TEXT NOT NULL,
    ticker TEXT NOT NULL,
    window_key INTEGER NOT NULL,
    close_time REAL,
    side TEXT NOT NULL,
    ask_cents REAL NOT NULL,
    distance_sigma REAL,
    flip_probability REAL,
    calibrated_yes_probability REAL,
    side_prob REAL,
    disagreement REAL,
    slate_n INTEGER,
    features_version TEXT NOT NULL,
    official_result TEXT,
    resolved_at REAL,
    correct INTEGER,
    pnl_cents REAL,
    UNIQUE(model_version, window_key)
);
"""


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _num(v: Any) -> float | None:
    try:
        if v is None or isinstance(v, bool):
            return None
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def taker_fee_cents(ask: float) -> int:
    p = max(0.0, min(100.0, ask)) / 100.0
    if p <= 0.0 or p >= 1.0:
        return 0
    return int(math.ceil(7.0 * p * (1.0 - p)))


def net_pnl_cents(ask: float, correct: bool) -> float:
    gross = (100.0 - ask) if correct else -ask
    return gross - float(taker_fee_cents(ask))


def wilson_lower(correct: int, n: int, z: float = 1.96) -> float | None:
    if n <= 0:
        return None
    p = correct / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4 * n * n)) / denom
    return centre - half


class DriftShadow:
    """Record-only forward test of the near-strike-YES drift hypothesis."""

    def __init__(self, db_path: str | None = None):
        self.enabled = _bool("Q15_DRIFT_SHADOW", True)
        self.db_path = db_path or os.environ.get(
            "Q15_DRIFT_SHADOW_DB", "data/q15_drift_shadow_v1.sqlite3")
        self._conn: sqlite3.Connection | None = None
        if not self.enabled:
            return
        try:
            import pathlib
            pathlib.Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=15.0)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        except sqlite3.Error:
            self._conn = None
            self.enabled = False

    # -- rule parameters (frozen defaults; env override for research only) -----
    @property
    def ask_lo(self) -> float:
        return _envf("Q15_DRIFT_SHADOW_ASK_LO", 65.0)

    @property
    def ask_hi(self) -> float:
        return _envf("Q15_DRIFT_SHADOW_ASK_HI", 73.0)

    @property
    def dist_max(self) -> float:
        return _envf("Q15_DRIFT_SHADOW_DIST_MAX", 3e-5)

    @property
    def flip_max(self) -> float:
        return _envf("Q15_DRIFT_SHADOW_FLIP_MAX", 30.0)

    @property
    def side(self) -> str:
        return (os.environ.get("Q15_DRIFT_SHADOW_SIDE", "YES") or "YES").upper()

    def _qualifies(self, cap: Mapping[str, Any]) -> bool:
        asset = str(cap.get("asset") or "").upper()
        if asset in {"BTC", "ETH"} or not asset:
            return False
        side = str(cap.get("predicted_side") or "").upper()
        if side != self.side:
            return False
        ask = _num(cap.get("yes_ask_cents"))
        if ask is None or not (self.ask_lo <= ask <= self.ask_hi):
            return False
        dist = _num(cap.get("distance_from_strike"))
        if dist is None or dist > self.dist_max:
            return False
        flip = _num(cap.get("flip_probability"))
        if flip is None or flip > self.flip_max:
            return False
        return True

    @staticmethod
    def _disagreement(cap: Mapping[str, Any]) -> float:
        cal = _num(cap.get("calibrated_yes_probability"))
        ask = _num(cap.get("yes_ask_cents"))
        if cal is None or ask is None:
            return -9.0
        side = str(cap.get("predicted_side") or "").upper()
        side_prob = cal if side == "YES" else 1.0 - cal
        return side_prob - ask / 100.0

    def observe_window(self, *, model_version: str, window_key: int,
                       close_time: float | None, slate: Sequence[Mapping[str, Any]],
                       now: float) -> bool:
        """Evaluate one 15m interval's 13M slate. Records the single best
        qualifying pick (idempotent per window). Returns True if a pick was
        recorded. Never raises."""
        if not self.enabled or self._conn is None:
            return False
        try:
            quals = [c for c in slate if self._qualifies(c)]
            if not quals:
                return False
            top = max(quals, key=self._disagreement)
            cal = _num(top.get("calibrated_yes_probability"))
            ask = _num(top.get("yes_ask_cents"))
            side = str(top.get("predicted_side") or "").upper()
            side_prob = (cal if side == "YES" else 1.0 - cal) if cal is not None else None
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO drift_picks (created_at, model_version, asset,"
                " ticker, window_key, close_time, side, ask_cents, distance_sigma,"
                " flip_probability, calibrated_yes_probability, side_prob, disagreement,"
                " slate_n, features_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, model_version, str(top.get("asset")), str(top.get("ticker")),
                 int(window_key), close_time, side, ask,
                 _num(top.get("distance_from_strike")), _num(top.get("flip_probability")),
                 cal, side_prob, self._disagreement(top), len(quals), FEATURES_VERSION))
            self._conn.commit()
            return cur.rowcount > 0
        except sqlite3.Error:
            return False

    def resolve(self, events: Sequence[Mapping[str, Any]] | None, now: float) -> int:
        """Grade recorded picks from the champion's settlement events. Read-only."""
        if not self.enabled or self._conn is None or not events:
            return 0
        graded = 0
        try:
            for ev in events:
                if not isinstance(ev, Mapping):
                    continue
                ticker = ev.get("ticker") or ev.get("contract")
                result = str(ev.get("result") or ev.get("official_result") or "").upper()
                if not ticker or result not in {"YES", "NO"}:
                    continue
                for row in self._conn.execute(
                        "SELECT id, side, ask_cents FROM drift_picks WHERE ticker=?"
                        " AND official_result IS NULL", (str(ticker),)).fetchall():
                    correct = str(row["side"]).upper() == result
                    self._conn.execute(
                        "UPDATE drift_picks SET official_result=?, resolved_at=?, correct=?,"
                        " pnl_cents=? WHERE id=?",
                        (result, now, 1 if correct else 0,
                         net_pnl_cents(float(row["ask_cents"]), correct), row["id"]))
                    graded += 1
            self._conn.commit()
        except sqlite3.Error:
            pass
        return graded

    def scoreboard(self) -> dict[str, Any]:
        """Live standing vs the frozen pre-registered bars."""
        if not self.enabled or self._conn is None:
            return {"available": False, "enabled": self.enabled}
        rows = self._conn.execute(
            "SELECT asset, ask_cents, correct, pnl_cents, created_at FROM drift_picks"
            " WHERE official_result IS NOT NULL").fetchall()
        n = len(rows)
        pending = self._conn.execute(
            "SELECT COUNT(*) FROM drift_picks WHERE official_result IS NULL").fetchone()[0]
        base = {"available": True, "enabled": True, "features_version": FEATURES_VERSION,
                "rule": {"ask": [self.ask_lo, self.ask_hi], "dist_max": self.dist_max,
                         "flip_max": self.flip_max, "side": self.side},
                "n_resolved": n, "n_pending": int(pending)}
        if n == 0:
            base.update({"status": "empty"})
            return base
        correct = sum(1 for r in rows if r["correct"])
        total_pnl = sum(float(r["pnl_cents"]) for r in rows)
        wr = correct / n
        breakeven = sum(r["ask_cents"] + taker_fee_cents(r["ask_cents"]) for r in rows) / n / 100.0
        wlb = wilson_lower(correct, n)
        # concentration guards
        by_day: dict[str, float] = {}
        for r in rows:
            day = time.strftime("%Y-%m-%d", time.gmtime(r["created_at"]))
            by_day[day] = by_day.get(day, 0.0) + float(r["pnl_cents"])
        max_day_frac = (max((abs(v) for v in by_day.values()), default=0.0) /
                        abs(total_pnl)) if total_pnl else None
        assets = {str(r["asset"]) for r in rows}
        ev = total_pnl / n
        # frozen bars
        kill = n >= 40 and (ev <= 0 or wr < breakeven)
        promote = (n >= 150 and ev >= 2.0 and wlb is not None and wlb > breakeven
                   and (max_day_frac is None or max_day_frac <= 0.40) and len(assets) >= 3)
        status = "KILL" if kill else ("PROMOTE" if promote else "ACCRUING")
        base.update({
            "status": status, "win_rate": round(wr, 3), "breakeven_rate": round(breakeven, 3),
            "wilson_lb": wlb and round(wlb, 3), "ev_cents": round(ev, 2),
            "total_pnl_cents": round(total_pnl, 0), "n_assets": len(assets),
            "max_day_pnl_frac": max_day_frac and round(max_day_frac, 2),
            "bars": {"kill_at_n": 40, "promote_at_n": 150,
                     "promote_needs": "ev>=2 & wilson_lb>breakeven & max_day<=40% & assets>=3"},
        })
        return base

    def health(self, now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        if not self.enabled or self._conn is None:
            return {"enabled": self.enabled, "status": "disabled"}
        row = self._conn.execute(
            "SELECT COUNT(*) n, MAX(created_at) latest FROM drift_picks").fetchone()
        latest = _num(row["latest"])
        return {"enabled": True, "rows_written": int(row["n"] or 0),
                "latest_created_at": latest,
                "latest_age_seconds": (now - latest) if latest is not None else None,
                "status": "ok" if row["n"] else "empty"}


_singleton: DriftShadow | None = None


def get_recorder() -> DriftShadow | None:
    """Process-wide singleton; None when disabled so the call site is a cheap no-op."""
    global _singleton
    if _singleton is not None:
        return _singleton if _singleton.enabled else None
    _singleton = DriftShadow()
    return _singleton if _singleton.enabled else None


def reset_recorder() -> None:
    global _singleton
    _singleton = None
