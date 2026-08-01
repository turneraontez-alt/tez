"""Shadow two-sided quoter ("strangle maker") — PAPER ONLY, default-OFF.

Research background (2026-07-02 path-study, n=44 real windows): resting maker
bids on BOTH sides of the near-strike contract at the ~13M mark, with an
IMMEDIATE taker hedge the moment only one side fills, graded +4.8..+8.0c per
window across the width grid with a bounded worst case (~-6c) — because ~70%
of windows chop enough to fill both bids (locked 2*width profit: total cost
100-2w, settlement pays exactly one leg 100), while trending windows fill only
the adverse side, which the instant hedge caps at roughly -(spread+slippage).
The fill model there was optimistic (mid-touch); THIS module exists to measure
reality: it places VIRTUAL post-only bids, detects fills conservatively (the
market's opposing ask trading at/below our bid), hedges virtually at the
observed ask, and grades every window. It never places, modifies, or cancels a
real order and never sends Telegram.

Promotion bar before anyone trades this (pre-registered): >=500 windows over
>=15 days, per-window-close clustered t>=2, both time halves positive, real
both-fill rate >=40%, and the single-fill hedge cost distribution bounded as
simulated. All grading is outcome-independent (LOCKED and HEDGED positions hold
both sides; exactly one leg settles at 100), so no settlement oracle is needed
for P&L — `last_yes_mid` is recorded for side-level analytics only.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS strangle_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    asset TEXT NOT NULL,
    close_time REAL NOT NULL,
    quote_sr REAL,               -- seconds_remaining when quotes were placed
    mid0 REAL,                   -- yes mid used as fair at quote time
    width REAL,
    yes_bid REAL,                -- our virtual YES bid (yes-price space)
    no_bid REAL,                 -- our virtual NO bid (no-price space)
    spread0 REAL,
    state TEXT NOT NULL,         -- QUOTED / LOCKED / HEDGED / EXPIRED / SKIPPED_*
    yes_fill_ts REAL, yes_fill_px REAL,
    no_fill_ts REAL,  no_fill_px REAL,
    hedge_ts REAL, hedge_side TEXT, hedge_px REAL, hedge_fee REAL,
    pnl_cents REAL,              -- fixed at LOCKED/HEDGED/EXPIRED; outcome-independent
    last_yes_mid REAL,           -- convergence proxy for side analytics
    finalized_at REAL,
    utc_hour INTEGER,            -- session tag at quote time
    ttl_s REAL, hedge_delay_s REAL,
    spread_at_first_fill REAL,   -- market spread when the first leg filled
    postfill_mids_json TEXT,     -- [(dt_s, yes_mid)...] for 120s after first fill
    round_mark REAL NOT NULL DEFAULT 780,  -- seconds-remaining open mark of this round
    UNIQUE(asset, close_time, round_mark)
);
"""


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _num(v: Any) -> float | None:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _db_path() -> str:
    return os.environ.get("Q15_STRANGLE_SHADOW_DB", "data/q15_strangle_shadow_v1.sqlite3")


def taker_fee_cents(price_cents: float) -> float:
    p = max(0.0, min(1.0, price_cents / 100.0))
    return float(math.ceil(7.0 * p * (1.0 - p)))


class StrangleShadow:
    """Virtual both-sides quoter with hedge-on-single-fill. Paper only."""

    def __init__(self, db_path: str | None = None) -> None:
        self.enabled = _env_bool("Q15_STRANGLE_SHADOW", False)
        self.width = _env_float("Q15_STRANGLE_SHADOW_WIDTH", 5.0)
        self.open_sr = _env_float("Q15_STRANGLE_SHADOW_OPEN_SR", 780.0)   # quote at ~13M
        self.open_sr_floor = _env_float("Q15_STRANGLE_SHADOW_OPEN_FLOOR", 690.0)
        self.cancel_sr = _env_float("Q15_STRANGLE_SHADOW_CANCEL_SR", 660.0)  # 120s TTL
        # MULTI-ROUND harvest: the pin premium exists at 13M (impl/real ~1.62) AND
        # decays but persists at 10M/7M (~1.25) — re-quoting after each round's
        # terminal state multiplies the harvest surface per window. Marks are
        # seconds-remaining opens; each mark runs the same open/TTL/hedge cycle
        # with TTL = 120s and floor = mark-90. Default = single legacy round.
        raw_marks = os.environ.get("Q15_STRANGLE_SHADOW_OPEN_MARKS", "")
        try:
            self.open_marks = tuple(sorted((float(x) for x in raw_marks.split(",") if x.strip()),
                                           reverse=True)) or (self.open_sr,)
        except ValueError:
            self.open_marks = (self.open_sr,)
        self.max_spread = _env_float("Q15_STRANGLE_SHADOW_MAX_SPREAD", 6.0)
        self.hedge_slip = _env_float("Q15_STRANGLE_SHADOW_HEDGE_SLIP", 1.0)
        # After the FIRST fill, wait this long for the second maker fill (-> LOCKED)
        # before hedging at market (-> HEDGED). The original path-sim classified
        # both-fill with end-of-window knowledge (look-ahead); this delay is the
        # honest, implementable version that the shadow exists to measure.
        self.hedge_delay = _env_float("Q15_STRANGLE_SHADOW_HEDGE_DELAY_S", 20.0)
        # Round-2 finding: liquidity is ASSET-stratified, not session-stratified —
        # thin-alts (spread>=3c 60-65% of the time) are the primary maker venue.
        # Empty = quote all assets.
        raw_assets = os.environ.get("Q15_STRANGLE_SHADOW_ASSETS", "")
        self.assets = frozenset(a.strip().upper() for a in raw_assets.split(",") if a.strip())
        # Post-fill mid path recording (seconds) — the instrumentation every
        # promotion test (fill-fade overlay, session TTL, vol skew) depends on.
        self.postfill_track_s = _env_float("Q15_STRANGLE_SHADOW_POSTFILL_TRACK_S", 120.0)
        self.db_path = db_path or _db_path()
        self._lock = threading.Lock()
        self._live: dict[tuple[str, float], dict[str, Any]] = {}
        self._last_observe_at: float | None = None
        self._conn: sqlite3.Connection | None = None
        if self.enabled:
            self._connect()

    def _connect(self) -> None:
        try:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=2.0)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(strangle_windows)")}
            if "round_mark" not in cols:
                # legacy table (pre multi-round): add the column; its UNIQUE(asset,
                # close_time) constraint remains, so extra rounds are silently
                # ignored until the DB file is rotated. Degrade gracefully.
                self._conn.execute("ALTER TABLE strangle_windows ADD COLUMN round_mark REAL NOT NULL DEFAULT 780")
                if len(self.open_marks) > 1:
                    logger.warning("strangle shadow: legacy DB schema — multi-round limited to first round until DB rotated")
            self._conn.commit()
        except Exception:
            logger.warning("strangle shadow DB unavailable; disabling", exc_info=True)
            self.enabled = False

    # ------------------------------------------------------------- observe
    def observe(self, *, asset: str, close_time: float | None,
                seconds_remaining: float | None,
                yes_bid: float | None, yes_ask: float | None,
                now: float | None = None) -> None:
        """One quote observation per asset per refresh cycle (~1s)."""
        if not self.enabled or close_time is None or seconds_remaining is None:
            return
        ts = time.time() if now is None else float(now)
        self._last_observe_at = ts
        sr = float(seconds_remaining)
        yb, ya = _num(yes_bid), _num(yes_ask)
        a_up = str(asset).upper()
        if self.assets and a_up not in self.assets:
            return
        with self._lock:
            for mark in self.open_marks:
                key = (a_up, float(close_time), mark)
                st = self._live.get(key)
                if st is None:
                    if (mark - 90.0) <= sr <= mark:
                        self._try_open(key, sr, yb, ya, ts, mark)
                    continue
                if st["state"] == "QUOTED":
                    self._tick_quoted(key, st, sr, yb, ya, ts, mark)

    def _try_open(self, key, sr, yb, ya, ts, mark) -> None:
        asset, close_time = key[0], key[1]
        if yb is None or ya is None or not (0.0 < yb <= ya < 100.0):
            return
        spread = ya - yb
        mid = (yb + ya) / 2.0
        state = "QUOTED"
        if spread > self.max_spread:
            state = "SKIPPED_SPREAD"
        our_yes_bid = mid - self.width
        our_no_bid = (100.0 - mid) - self.width
        if state == "QUOTED" and (our_yes_bid <= 1.0 or our_no_bid <= 1.0):
            state = "SKIPPED_EXTREME"   # too near 0/100 for a two-sided book
        try:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO strangle_windows "
                "(created_at, asset, close_time, quote_sr, mid0, width, yes_bid, no_bid,"
                " spread0, state, utc_hour, ttl_s, hedge_delay_s, round_mark)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, asset, close_time, sr, mid, self.width,
                 our_yes_bid, our_no_bid, spread, state,
                 int(time.gmtime(ts).tm_hour),
                 120.0, self.hedge_delay, mark))
            self._conn.commit()
            if cur.rowcount == 0:       # restart mid-window: never re-quote
                self._live[key] = {"state": "DONE"}
                return
        except Exception:
            logger.debug("strangle open write failed", exc_info=True)
            return
        self._live[key] = {
            "state": state if state == "QUOTED" else "DONE",
            "mid0": mid, "yes_bid": our_yes_bid, "no_bid": our_no_bid,
            "yes_fill": None, "no_fill": None, "last_mid": mid,
        }

    def _tick_quoted(self, key, st, sr, yb, ya, ts, mark) -> None:
        if yb is None or ya is None or not (0.0 < yb <= ya < 100.0):
            return
        st["last_mid"] = (yb + ya) / 2.0
        # Conservative virtual-fill rule: our resting BID fills only when the
        # opposing ASK trades at/below it (a seller crossed to us). The mid
        # merely touching our level does NOT fill — that optimism is what this
        # module exists to remove.
        if (st["yes_fill"] or st["no_fill"]) and st.get("first_fill_ts"):
            # instrumentation: sample the mid for postfill_track_s after first fill
            if ts - st["first_fill_ts"] <= self.postfill_track_s:
                st.setdefault("postfill", []).append(
                    (round(ts - st["first_fill_ts"], 1), round(st["last_mid"], 2)))
        if st["yes_fill"] is None and ya <= st["yes_bid"]:
            st["yes_fill"] = (ts, st["yes_bid"])
            if st.get("first_fill_ts") is None:
                st["first_fill_ts"] = ts
                self._write(key, spread_at_first_fill=ya - yb)
            self._write(key, yes_fill_ts=ts, yes_fill_px=st["yes_bid"])
        no_ask = 100.0 - yb              # buying NO lifts the NO ask = 100 - yes_bid
        if st["no_fill"] is None and no_ask <= st["no_bid"]:
            st["no_fill"] = (ts, st["no_bid"])
            if st.get("first_fill_ts") is None:
                st["first_fill_ts"] = ts
                self._write(key, spread_at_first_fill=ya - yb)
            self._write(key, no_fill_ts=ts, no_fill_px=st["no_bid"])

        if st["yes_fill"] and st["no_fill"]:
            pnl = 100.0 - (st["yes_bid"] + st["no_bid"])      # locked, fee-free
            st["state"] = "DONE"
            self._write(key, state="LOCKED", pnl_cents=pnl, finalized_at=ts,
                        postfill_mids_json=json.dumps(st.get("postfill", [])))
        elif (st["yes_fill"] or st["no_fill"]) and st.get("hedge_deadline") is None:
            # First fill: arm the hedge timer; give the second maker fill a
            # hedge_delay window to arrive before we cross the spread.
            st["hedge_deadline"] = ts + self.hedge_delay
        elif (st["yes_fill"] or st["no_fill"]) and ts >= st["hedge_deadline"]:
            # IMMEDIATE hedge: buy the opposite side at its observed ask + slip.
            if st["yes_fill"]:
                hedge_side, hedge_px = "NO", (100.0 - yb) + self.hedge_slip
                cost = st["yes_bid"] + hedge_px
            else:
                hedge_side, hedge_px = "YES", ya + self.hedge_slip
                cost = st["no_bid"] + hedge_px
            fee = taker_fee_cents(hedge_px)
            pnl = 100.0 - cost - fee                           # both legs held
            st["state"] = "DONE"
            self._write(key, state="HEDGED", hedge_ts=ts, hedge_side=hedge_side,
                        hedge_px=hedge_px, hedge_fee=fee, pnl_cents=pnl,
                        finalized_at=ts,
                        postfill_mids_json=json.dumps(st.get("postfill", [])))
        elif sr < (mark - 120.0):                              # per-round 120s TTL
            st["state"] = "DONE"
            self._write(key, state="EXPIRED", pnl_cents=0.0, finalized_at=ts,
                        last_yes_mid=st["last_mid"])

    def _write(self, key, **cols) -> None:
        asset, close_time, mark = key
        try:
            sets = ", ".join(f"{c}=?" for c in cols)
            self._conn.execute(
                f"UPDATE strangle_windows SET {sets} WHERE asset=? AND close_time=? AND round_mark=?",
                (*cols.values(), asset, close_time, mark))
            self._conn.commit()
        except Exception:
            logger.debug("strangle write failed", exc_info=True)

    # ------------------------------------------------------------ finalize
    def finalize_expired(self, now: float | None = None) -> None:
        """Sweep windows past settlement out of memory; stamp last mid."""
        if not self.enabled:
            return
        ts = time.time() if now is None else float(now)
        with self._lock:
            for key in [k for k in self._live if k[1] <= ts]:
                st = self._live.pop(key)
                if st.get("state") == "QUOTED":                # closed while quoted
                    self._write(key, state="EXPIRED", pnl_cents=0.0,
                                finalized_at=ts, last_yes_mid=st.get("last_mid"))

    # ----------------------------------------------------------- reporting
    def scoreboard(self) -> dict[str, Any]:
        if not self.enabled or self._conn is None:
            return {"available": False}
        try:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) n, ROUND(SUM(pnl_cents),1) pnl,"
                " ROUND(AVG(pnl_cents),2) avg_pnl FROM strangle_windows GROUP BY state"
            ).fetchall()
            tot = self._conn.execute(
                "SELECT COUNT(*) n, ROUND(SUM(pnl_cents),1) pnl FROM strangle_windows"
                " WHERE state IN ('LOCKED','HEDGED','EXPIRED')").fetchone()
            return {"available": True,
                    "by_state": {r["state"]: {"n": r["n"], "pnl": r["pnl"],
                                              "avg": r["avg_pnl"]} for r in rows},
                    "graded_windows": tot["n"], "total_pnl_cents": tot["pnl"]}
        except Exception:
            return {"available": False}


_singleton: StrangleShadow | None = None
_singleton_lock = threading.Lock()


def get_strangle_shadow() -> StrangleShadow:
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = StrangleShadow()
        return _singleton


def reset_strangle_shadow() -> None:
    global _singleton
    with _singleton_lock:
        _singleton = None


def strangle_shadow_health(now: float | None = None) -> dict[str, Any]:
    """Report DB freshness using MAX(created_at) from strangle_windows."""
    current = time.time() if now is None else float(now)
    enabled = _env_bool("Q15_STRANGLE_SHADOW", False)
    db_path = _db_path()
    info: dict[str, Any] = {
        "enabled": enabled,
        "db_path": db_path,
        "latest_created_at": None,
        "latest_age_seconds": None,
        "watchdog_age_seconds": None,
        "rows_written": 0,
        "status": "disabled" if not enabled else "missing",
        "missing_reason": None if not enabled else "strangle_db_missing",
    }
    path = Path(db_path)
    with _singleton_lock:
        runtime = _singleton
    if runtime is not None and runtime.enabled and runtime._last_observe_at is not None:
        info["watchdog_age_seconds"] = round(
            max(0.0, current - runtime._last_observe_at), 3
        )
    if not path.is_file():
        return info
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n, MAX(created_at) AS max_created_at FROM strangle_windows"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        info["status"] = "error"
        info["missing_reason"] = f"strangle_health_query_error:{type(exc).__name__}"
        return info
    count = int(row[0] or 0) if row else 0
    latest = _num(row[1]) if row else None
    info["rows_written"] = count
    info["latest_created_at"] = latest
    if latest is None:
        info["status"] = "empty" if enabled else "disabled"
        info["missing_reason"] = "strangle_windows_empty" if enabled else None
        return info
    info["latest_age_seconds"] = round(max(0.0, current - latest), 3)
    info["status"] = "ok" if enabled else "disabled"
    info["missing_reason"] = None
    return info
