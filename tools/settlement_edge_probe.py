"""Capture Kalshi quotes in the FINAL MINUTE, beside the settlement index.

WHY
---
Kalshi's 15-minute crypto binaries settle on a 60-sample average of the CF
Benchmarks index. That means the settlement value is progressively BANKED as
the final minute elapses: at T-45s, 15 of 60 samples remain, so the outcome is
already mechanically determined whenever the partial average sits further from
the strike than those 15 samples could move it.

Measured over 1,245 windows of existing index data:

    T-45s   48.2% of contracts already locked   -> 100.00% resolved as computed
    T-30s   61.9% locked                        -> 100.00%
    T-10s   85.7% locked                        -> 100.00%

That is arithmetic on published data, not forecasting. The open question - the
ONLY one this repo has no data for - is what the order book charges at that
moment. If locked contracts quote at ~99c there is no trade. If they quote
materially below, the gap is an edge that needs no prediction at all.

No Kalshi quote has ever been recorded inside the final 60s alongside index
coverage, so the question is currently unanswerable. This collector fixes that.

SAFETY
------
Read-only. Public market data only, no auth, no orders, no Telegram. Writes to
its own SQLite table and nothing else. Default-OFF; set Q15_SETTLE_PROBE=true.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Any, Mapping, Sequence

logger = logging.getLogger("q15.settle_probe")

# Seconds-before-close at which to snapshot the book. Chosen so the banked
# fraction spans 0/60 .. 45/60 of the settlement average.
DEFAULT_MARKS = (60, 45, 30, 15, 5)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS settlement_edge_probe (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at REAL NOT NULL,
    asset TEXT NOT NULL,
    ticker TEXT NOT NULL,
    close_time REAL NOT NULL,
    mark_seconds INTEGER NOT NULL,
    strike REAL,
    yes_bid REAL,
    yes_ask REAL,
    no_bid REAL,
    no_ask REAL,
    index_px REAL,
    final_minute_avg_px REAL,
    final_minute_window_size INTEGER,
    banked_fraction REAL,
    frozen_settlement REAL,
    locked INTEGER,
    implied_side TEXT,
    official_result TEXT,
    resolved_at REAL,
    UNIQUE(ticker, mark_seconds)
);
CREATE INDEX IF NOT EXISTS idx_probe_close ON settlement_edge_probe(close_time);
"""


def enabled() -> bool:
    return (os.environ.get("Q15_SETTLE_PROBE", "") or "").strip().lower() in (
        "1", "true", "yes", "on")


def db_path() -> str:
    return os.environ.get("Q15_SETTLE_PROBE_DB") or "data/q15_settlement_edge_probe.sqlite3"


def marks() -> tuple[int, ...]:
    raw = (os.environ.get("Q15_SETTLE_PROBE_MARKS") or "").strip()
    if not raw:
        return DEFAULT_MARKS
    out = []
    for part in raw.split(","):
        try:
            v = int(float(part.strip()))
        except (TypeError, ValueError):
            continue
        if 1 <= v <= 120:
            out.append(v)
    return tuple(sorted(set(out), reverse=True)) or DEFAULT_MARKS


def open_db(path: str | None = None) -> sqlite3.Connection:
    path = path or db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=False)
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    con.executescript(_SCHEMA)
    con.commit()
    return con


def banked_fraction(window_size: Any) -> float | None:
    """Share of the 60-sample settlement average already observed."""
    try:
        k = int(window_size)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, k / 60.0))


def frozen_settlement(partial_avg: Any, window_size: Any, index_px: Any) -> float | None:
    """Settlement value if the index simply held its current level.

    settle = f * partial_avg + (1-f) * index_px, where f is the banked share.
    With no final-minute average yet, the best estimate is the index itself.
    """
    try:
        idx = float(index_px)
    except (TypeError, ValueError):
        return None
    f = banked_fraction(window_size)
    if f is None or f <= 0.0:
        return idx
    try:
        avg = float(partial_avg)
    except (TypeError, ValueError):
        return idx
    return f * avg + (1.0 - f) * idx


# Never claim a lock until at least this share of the settlement average is
# banked. The move bound below is calibrated to the SHORT remainder of a final
# minute; applied to a full minute (or more) it is far too tight and would
# declare certainty on an ordinary pre-close price gap. A false 'locked' is the
# expensive error — it is the one that would put money on a coin flip.
MIN_BANKED_TO_LOCK = 0.25


def is_locked(frozen: Any, strike: Any, window_size: Any, index_px: Any,
              move_bound: float = 0.0015) -> bool:
    """Can the unbanked remainder still flip the side?

    The remaining (1-f) of the average is multiplied by however far the index
    can travel; bound that by ``move_bound`` of the index level. Deliberately
    conservative — and never certain before the final minute is meaningfully
    underway, regardless of how wide the gap looks.
    """
    try:
        frozen_v, strike_v, idx = float(frozen), float(strike), float(index_px)
    except (TypeError, ValueError):
        return False
    f = banked_fraction(window_size)
    if f is None or f < MIN_BANKED_TO_LOCK:
        return False
    max_swing = (1.0 - f) * idx * float(move_bound)
    return abs(frozen_v - strike_v) > max_swing


def build_row(*, now: float, asset: str, ticker: str, close_time: float,
              mark: int, strike: Any, book: Mapping[str, Any],
              index: Mapping[str, Any]) -> dict[str, Any]:
    """Assemble one probe row. Pure — no I/O, so it is fully testable."""
    idx_px = index.get("index_px")
    win = index.get("final_minute_window_size")
    frozen = frozen_settlement(index.get("final_minute_avg_px"), win, idx_px)
    locked = is_locked(frozen, strike, win, idx_px)
    side = None
    if frozen is not None and strike is not None:
        try:
            side = "YES" if float(frozen) > float(strike) else "NO"
        except (TypeError, ValueError):
            side = None
    return {
        "captured_at": now, "asset": asset, "ticker": ticker,
        "close_time": close_time, "mark_seconds": int(mark),
        "strike": strike,
        "yes_bid": book.get("yes_bid"), "yes_ask": book.get("yes_ask"),
        "no_bid": book.get("no_bid"), "no_ask": book.get("no_ask"),
        "index_px": idx_px,
        "final_minute_avg_px": index.get("final_minute_avg_px"),
        "final_minute_window_size": win,
        "banked_fraction": banked_fraction(win),
        "frozen_settlement": frozen,
        "locked": 1 if locked else 0,
        "implied_side": side,
    }


_COLS = ("captured_at", "asset", "ticker", "close_time", "mark_seconds", "strike",
         "yes_bid", "yes_ask", "no_bid", "no_ask", "index_px",
         "final_minute_avg_px", "final_minute_window_size", "banked_fraction",
         "frozen_settlement", "locked", "implied_side")


def record(con: sqlite3.Connection, row: Mapping[str, Any]) -> bool:
    """Insert one capture. UNIQUE(ticker, mark) makes it idempotent."""
    try:
        con.execute(
            "INSERT INTO settlement_edge_probe(%s) VALUES(%s)"
            % (",".join(_COLS), ",".join("?" * len(_COLS))),
            [row.get(c) for c in _COLS])
        con.commit()
        return True
    except sqlite3.IntegrityError:
        return False          # already captured this ticker at this mark
    except sqlite3.DatabaseError:
        logger.exception("settlement probe insert failed (ignored)")
        return False


def resolve(con: sqlite3.Connection, ticker: str, official: str,
            now: float | None = None) -> int:
    ts = time.time() if now is None else now
    try:
        cur = con.execute(
            "UPDATE settlement_edge_probe SET official_result=?, resolved_at=? "
            "WHERE ticker=? AND official_result IS NULL", (official, ts, ticker))
        con.commit()
        return int(cur.rowcount or 0)
    except sqlite3.DatabaseError:
        logger.exception("settlement probe resolve failed (ignored)")
        return 0


def summary(con: sqlite3.Connection) -> dict[str, Any]:
    """THE answer, once enough windows accumulate: on contracts whose settlement
    is already locked, how accurate is the computed side, and what does the book
    charge for it? A large gap between accuracy and the implied price is the edge."""
    out: dict[str, Any] = {"by_mark": {}}
    try:
        rows = con.execute(
            "SELECT mark_seconds, locked, implied_side, official_result, "
            "yes_ask, no_ask FROM settlement_edge_probe "
            "WHERE official_result IS NOT NULL").fetchall()
    except sqlite3.DatabaseError:
        return out
    for mark, locked, side, res, ya, na in rows:
        d = out["by_mark"].setdefault(int(mark), {
            "n": 0, "locked": 0, "locked_right": 0, "ask_sum": 0.0, "ask_n": 0})
        d["n"] += 1
        if not locked:
            continue
        d["locked"] += 1
        if side and res and str(side).upper() == str(res).upper():
            d["locked_right"] += 1
        ask = ya if str(side).upper() == "YES" else na
        if ask is not None:
            d["ask_sum"] += float(ask)
            d["ask_n"] += 1
    for d in out["by_mark"].values():
        d["locked_accuracy"] = (d["locked_right"] / d["locked"]) if d["locked"] else None
        d["mean_ask_cents"] = (d["ask_sum"] / d["ask_n"]) if d["ask_n"] else None
        if d["locked_accuracy"] is not None and d["mean_ask_cents"] is not None:
            # cents of edge per contract before fees
            d["gross_edge_cents"] = 100.0 * d["locked_accuracy"] - d["mean_ask_cents"]
    return out
