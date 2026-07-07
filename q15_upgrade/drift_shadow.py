"""Drift-hypothesis shadow recorder (13M near-strike YES, record-only).

Forward-tests the pre-registered near-strike-YES hypothesis (see HANDOFF): at
the 13M mark, at-the-money alt YES contracts with low flip-risk are underpriced.
It NEVER trades, notifies, or touches any live path. Pure observation: it writes
its own SQLite ledger and exposes a scoreboard with frozen kill/promote bars.

v2 (2026-07-07): the recorder captures a SUPERSET envelope (ask 60-80, every
qualifying candidate per interval, with pick_rank) so that THREE nested books
grade simultaneously from one stream, each against its own frozen bar:

  Book P (primary; pre-registered 2026-07-06, bars UNCHANGED):
    ask 65-73, top-1 per interval by disagreement (pick_rank==1 within 65-73).
    KILL n>=40 if EV<=0 or WR<breakeven; PROMOTE n>=150 if EV>=+2c AND
    Wilson-95 LB > breakeven AND no day >40% of P&L AND >=3 assets.
  Book V (volume expansion; pre-registered 2026-07-07 after a 38-hypothesis
    parallel search + adversarial audit — ask-floor widening to 60 confirmed,
    multi-pick confirmed; the 74-80 ceiling was a train-mirage and is EXCLUDED):
    ask 60-73, ALL qualifying picks. KILL n>=60 if EV<=0 or WR<breakeven;
    PROMOTE n>=150 if EV>=+2c AND Wilson-95 LB > breakeven.
  Book X (diagnostic only, no bar, never tradeable from this data alone):
    ask 74-80 rows — recorded to settle the train-mirage question forward.

Core rule (all books): asset not in {BTC, ETH}, side == YES,
distance_sigma <= 3e-5 (near-strike), flip_probability <= 30 — every one of
these conditions is load-bearing by ablation; dist/fp relaxations were tested
and their marginal cohorts LOSE (see HANDOFF 2026-07-07).

Sizing (2026-07-07, the one lever of 28 tested that raised profit without
touching accuracy): each row records size_weight — 1.5x when disagreement is
above the frozen train-fit high tercile (-0.0917), 0.5x below the low tercile
(-0.1157), else 1.0x. On the tape: +19% total P&L at identical trades; OOS
check (train-fit thresholds on the test half) +9.47 vs +9.26 c/unit. The
scoreboard reports each book's weighted P&L alongside flat.
"""
from __future__ import annotations

import math
import os
import sqlite3
import time
from typing import Any, Mapping, Sequence

FEATURES_VERSION = "drift-shadow-v2"

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
    pick_rank INTEGER,
    size_weight REAL,
    features_version TEXT NOT NULL,
    official_result TEXT,
    resolved_at REAL,
    correct INTEGER,
    pnl_cents REAL,
    UNIQUE(model_version, window_key, ticker)
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
            self._migrate_v1_locked()
            self._conn.executescript(_SCHEMA)
            self._migrate_columns_locked()
            self._conn.commit()
        except sqlite3.Error:
            self._conn = None
            self.enabled = False

    def _migrate_v1_locked(self) -> None:
        """v1 -> v2: the v1 table had UNIQUE(model_version, window_key) (top-1 only)
        and no pick_rank. Rename it aside so v2's superset schema applies; v1 rows
        stay queryable in drift_picks_v1 (at most ~1 day of data existed)."""
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='drift_picks'"
        ).fetchone()
        if row and "UNIQUE(model_version, window_key)" in str(row["sql"]) \
                and "ticker" not in str(row["sql"]).split("UNIQUE", 1)[1]:
            self._conn.execute("ALTER TABLE drift_picks RENAME TO drift_picks_v1")

    def _migrate_columns_locked(self) -> None:
        """v2 tables created before the sizing lever lack size_weight; add it in
        place (pre-existing rows keep NULL, read as weight 1.0 at scoreboard time)."""
        cols = {str(r["name"]) for r in self._conn.execute("PRAGMA table_info(drift_picks)")}
        if "size_weight" not in cols:
            self._conn.execute("ALTER TABLE drift_picks ADD COLUMN size_weight REAL")

    # -- rule parameters (frozen defaults; env override for research only) -----
    # v2 records the 60-80 SUPERSET envelope; books P (65-73 top-1) and
    # V (60-73 all) are sliced from the recorded rows at scoreboard time.
    @property
    def ask_lo(self) -> float:
        return _envf("Q15_DRIFT_SHADOW_ASK_LO", 60.0)

    @property
    def ask_hi(self) -> float:
        return _envf("Q15_DRIFT_SHADOW_ASK_HI", 80.0)

    @property
    def dist_max(self) -> float:
        return _envf("Q15_DRIFT_SHADOW_DIST_MAX", 3e-5)

    @property
    def flip_max(self) -> float:
        return _envf("Q15_DRIFT_SHADOW_FLIP_MAX", 30.0)

    @property
    def side(self) -> str:
        return (os.environ.get("Q15_DRIFT_SHADOW_SIDE", "YES") or "YES").upper()

    # sizing terciles: FROZEN from the train half (2026-07-07); env for research only
    @property
    def w_lo_threshold(self) -> float:
        return _envf("Q15_DRIFT_SHADOW_W_LO_T", -0.1157)

    @property
    def w_hi_threshold(self) -> float:
        return _envf("Q15_DRIFT_SHADOW_W_HI_T", -0.0917)

    def size_weight(self, disagreement: float | None) -> float:
        if disagreement is None:
            return 1.0
        if disagreement >= self.w_hi_threshold:
            return 1.5
        if disagreement < self.w_lo_threshold:
            return 0.5
        return 1.0

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
        """Evaluate one 15m interval's 13M slate. Records EVERY qualifying
        candidate in the recording envelope, ranked by disagreement (pick_rank
        1 = best). Idempotent per (window, ticker). Returns True if any new row
        was recorded. Never raises."""
        if not self.enabled or self._conn is None:
            return False
        try:
            quals = [c for c in slate if self._qualifies(c)]
            if not quals:
                return False
            quals.sort(key=self._disagreement, reverse=True)
            wrote = False
            for rank, cand in enumerate(quals, start=1):
                cal = _num(cand.get("calibrated_yes_probability"))
                ask = _num(cand.get("yes_ask_cents"))
                side = str(cand.get("predicted_side") or "").upper()
                dis = self._disagreement(cand)
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO drift_picks (created_at, model_version, asset,"
                    " ticker, window_key, close_time, side, ask_cents, distance_sigma,"
                    " flip_probability, calibrated_yes_probability, side_prob, disagreement,"
                    " slate_n, pick_rank, size_weight, features_version)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now, model_version, str(cand.get("asset")), str(cand.get("ticker")),
                     int(window_key), close_time, side, ask,
                     _num(cand.get("distance_from_strike")), _num(cand.get("flip_probability")),
                     cal, (cal if side == "YES" else 1.0 - cal) if cal is not None else None,
                     dis, len(quals), rank, self.size_weight(dis), FEATURES_VERSION))
                wrote = wrote or cur.rowcount > 0
            self._conn.commit()
            return wrote
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

    def _book_rows(self, lo: float, hi: float, top1: bool) -> list[sqlite3.Row]:
        rows = self._conn.execute(
            "SELECT window_key, asset, ask_cents, disagreement, correct, pnl_cents,"
            " size_weight, created_at FROM drift_picks WHERE official_result IS NOT NULL"
            " AND ask_cents >= ? AND ask_cents <= ?", (lo, hi)).fetchall()
        if not top1:
            return rows
        best: dict[int, sqlite3.Row] = {}
        for r in rows:
            wk = int(r["window_key"])
            cur = best.get(wk)
            if cur is None or (r["disagreement"] or -9) > (cur["disagreement"] or -9):
                best[wk] = r
        return list(best.values())

    def _grade_book(self, rows: list[sqlite3.Row], *, kill_n: int, promote_n: int,
                    concentration_guards: bool) -> dict[str, Any]:
        n = len(rows)
        if n == 0:
            return {"n_resolved": 0, "status": "empty"}
        correct = sum(1 for r in rows if r["correct"])
        total_pnl = sum(float(r["pnl_cents"]) for r in rows)
        wr = correct / n
        # disagreement-tercile sizing, reported ALONGSIDE flat — kill/promote
        # bars grade the FLAT numbers only (sizing is observational, not a gate)
        weights = [float(r["size_weight"]) if r["size_weight"] is not None else 1.0
                   for r in rows]
        weighted_pnl = sum(w * float(r["pnl_cents"]) for w, r in zip(weights, rows))
        total_weight = sum(weights)
        breakeven = sum(r["ask_cents"] + taker_fee_cents(r["ask_cents"]) for r in rows) / n / 100.0
        wlb = wilson_lower(correct, n)
        by_day: dict[str, float] = {}
        for r in rows:
            day = time.strftime("%Y-%m-%d", time.gmtime(r["created_at"]))
            by_day[day] = by_day.get(day, 0.0) + float(r["pnl_cents"])
        max_day_frac = (max((abs(v) for v in by_day.values()), default=0.0) /
                        abs(total_pnl)) if total_pnl else None
        assets = {str(r["asset"]) for r in rows}
        ev = total_pnl / n
        kill = n >= kill_n and (ev <= 0 or wr < breakeven)
        promote = (n >= promote_n and ev >= 2.0 and wlb is not None and wlb > breakeven)
        if concentration_guards:
            promote = promote and (max_day_frac is None or max_day_frac <= 0.40) and len(assets) >= 3
        status = "KILL" if kill else ("PROMOTE" if promote else "ACCRUING")
        return {"n_resolved": n, "status": status, "win_rate": round(wr, 3),
                "breakeven_rate": round(breakeven, 3), "wilson_lb": wlb and round(wlb, 3),
                "ev_cents": round(ev, 2), "total_pnl_cents": round(total_pnl, 0),
                "weighted_pnl_cents": round(weighted_pnl, 1),
                "weighted_ev_per_unit_cents": (round(weighted_pnl / total_weight, 2)
                                               if total_weight else None),
                "n_assets": len(assets),
                "max_day_pnl_frac": max_day_frac and round(max_day_frac, 2)}

    def scoreboard(self) -> dict[str, Any]:
        """Three nested books graded from the one recorded stream, each against
        its own frozen bar (see module docstring)."""
        if not self.enabled or self._conn is None:
            return {"available": False, "enabled": self.enabled}
        pending = self._conn.execute(
            "SELECT COUNT(*) FROM drift_picks WHERE official_result IS NULL").fetchone()[0]
        return {
            "available": True, "enabled": True, "features_version": FEATURES_VERSION,
            "core_rule": {"envelope_ask": [self.ask_lo, self.ask_hi],
                          "dist_max": self.dist_max, "flip_max": self.flip_max,
                          "side": self.side},
            "sizing": {"w_lo_threshold": self.w_lo_threshold,
                       "w_hi_threshold": self.w_hi_threshold,
                       "weights": [0.5, 1.0, 1.5]},
            "n_pending": int(pending),
            "book_primary_65_73_top1": self._grade_book(
                self._book_rows(65.0, 73.0, top1=True),
                kill_n=40, promote_n=150, concentration_guards=True),
            "book_volume_60_73_all": self._grade_book(
                self._book_rows(60.0, 73.0, top1=False),
                kill_n=60, promote_n=150, concentration_guards=False),
            "book_diag_74_80": self._grade_book(
                self._book_rows(74.0, 80.0, top1=False),
                kill_n=10**9, promote_n=10**9, concentration_guards=False),
            "bars": {
                "primary": "KILL n>=40 if EV<=0|WR<be; PROMOTE n>=150 if EV>=2 & WLB>be & day<=40% & assets>=3",
                "volume": "KILL n>=60 if EV<=0|WR<be; PROMOTE n>=150 if EV>=2 & WLB>be",
                "diag_74_80": "no bar; diagnostic only (train-mirage check)",
            },
        }

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
