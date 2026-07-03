"""Tests for the shadow two-sided quoter (strangle_shadow.py).

Deterministic: the module is driven purely by observe() calls with explicit
`now`. Fills are CONSERVATIVE by design — a resting bid fills only when the
opposing ask trades at/below it, never on a mid touch. All P&L is
outcome-independent (LOCKED/HEDGED hold both sides; exactly one settles at 100).
"""
from __future__ import annotations

import os

import pytest

import strangle_shadow as ss


@pytest.fixture()
def quoter(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_STRANGLE_SHADOW", "true")
    monkeypatch.setenv("Q15_STRANGLE_SHADOW_DB", str(tmp_path / "s.sqlite3"))
    monkeypatch.setenv("Q15_STRANGLE_SHADOW_WIDTH", "5")
    q = ss.StrangleShadow()
    assert q.enabled
    return q


CLOSE = 1_800_000_000.0


def obs(q, sr, yb, ya, asset="BTC", close=CLOSE):
    q.observe(asset=asset, close_time=close, seconds_remaining=sr,
              yes_bid=yb, yes_ask=ya, now=close - sr)


def row(q, asset="BTC", close=CLOSE):
    return q._conn.execute(
        "SELECT * FROM strangle_windows WHERE asset=? AND close_time=?",
        (asset, close)).fetchone()


def test_default_off_is_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("Q15_STRANGLE_SHADOW", raising=False)
    q = ss.StrangleShadow(db_path=str(tmp_path / "x.sqlite3"))
    assert not q.enabled
    q.observe(asset="BTC", close_time=CLOSE, seconds_remaining=780,
              yes_bid=48, yes_ask=52, now=1.0)
    assert not os.path.exists(tmp_path / "x.sqlite3") or q._conn is None


def test_quotes_open_at_13m_mark(quoter):
    obs(quoter, 800, 48, 52)          # before the open window -> no quote
    assert row(quoter) is None
    obs(quoter, 779, 48, 52)          # inside [open_floor, open_sr] -> quote
    r = row(quoter)
    assert r["state"] == "QUOTED"
    assert r["mid0"] == 50.0
    assert r["yes_bid"] == 45.0       # mid - width
    assert r["no_bid"] == 45.0        # (100-mid) - width


def test_wide_spread_skipped(quoter):
    obs(quoter, 779, 40, 60)          # spread 20 > max 6
    assert row(quoter)["state"] == "SKIPPED_SPREAD"


def test_both_fill_locks_two_widths_fee_free(quoter):
    obs(quoter, 779, 48, 52)          # quoted: yes_bid 45 / no_bid 45
    obs(quoter, 700, 44, 45)          # yes ask 45 <= our 45 -> YES leg fills (timer arms)
    obs(quoter, 695, 55, 58)          # 5s later, within hedge_delay: NO ask 45 <= 45 -> LOCKED
    r = row(quoter)
    assert r["state"] == "LOCKED"
    assert r["pnl_cents"] == pytest.approx(10.0)   # 100 - 45 - 45, no fee
    assert r["yes_fill_px"] == 45.0 and r["no_fill_px"] == 45.0


def test_single_fill_hedges_after_delay_bounded_loss(quoter):
    obs(quoter, 779, 48, 52)          # yes_bid 45 / no_bid 45
    obs(quoter, 700, 42, 44)          # yes ask 44 <= 45 -> YES fills, timer arms (20s)
    obs(quoter, 690, 42, 44)          # 10s later: still within delay -> no hedge yet
    assert row(quoter)["state"] == "QUOTED"
    obs(quoter, 675, 42, 44)          # 25s after fill: deadline passed -> hedge
    r = row(quoter)
    assert r["state"] == "HEDGED"
    assert r["hedge_side"] == "NO"
    # hedge = (100 - yes_bid_mkt 42) + slip 1.0 = 59; cost = 45 + 59 = 104
    assert r["hedge_px"] == pytest.approx(59.0)
    fee = ss.taker_fee_cents(59.0)
    assert r["pnl_cents"] == pytest.approx(100.0 - 104.0 - fee)
    assert r["pnl_cents"] > -8.0      # bounded, never a -68c directional loss


def test_mid_touch_does_not_fill(quoter):
    obs(quoter, 779, 48, 52)          # yes_bid 45
    obs(quoter, 700, 44, 46)          # mid 45 touches our bid, ask 46 > 45 -> NO fill
    assert row(quoter)["state"] == "QUOTED"


def test_ttl_expires_unfilled_quotes(quoter):
    obs(quoter, 779, 48, 52)
    obs(quoter, 650, 48, 52)          # below cancel_sr 660 with no fills
    r = row(quoter)
    assert r["state"] == "EXPIRED" and r["pnl_cents"] == 0.0


def test_restart_never_requotes_same_window(quoter, tmp_path, monkeypatch):
    obs(quoter, 779, 48, 52)
    assert row(quoter)["state"] == "QUOTED"
    # simulate restart: fresh instance, same DB
    monkeypatch.setenv("Q15_STRANGLE_SHADOW_DB", quoter.db_path)
    q2 = ss.StrangleShadow()
    obs(q2, 770, 30, 34)              # would quote very different levels
    r = row(q2)
    assert r["yes_bid"] == 45.0       # original quote untouched, no duplicate row
    n = q2._conn.execute("SELECT COUNT(*) FROM strangle_windows").fetchone()[0]
    assert n == 1


def test_finalize_sweeps_closed_windows(quoter):
    obs(quoter, 779, 48, 52)
    quoter.finalize_expired(now=CLOSE + 10)
    r = row(quoter)
    assert r["state"] == "EXPIRED"
    assert r["last_yes_mid"] == pytest.approx(50.0)


def test_scoreboard_shape(quoter):
    obs(quoter, 779, 48, 52)
    obs(quoter, 700, 44, 45)
    obs(quoter, 690, 55, 58)          # LOCKED
    sb = quoter.scoreboard()
    assert sb["available"] and sb["graded_windows"] == 1
    assert sb["by_state"]["LOCKED"]["n"] == 1


def test_strangle_shadow_health_reports_latest_created_at(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_STRANGLE_SHADOW", "true")
    monkeypatch.setenv("Q15_STRANGLE_SHADOW_DB", str(tmp_path / "health.sqlite3"))
    q = ss.StrangleShadow()

    missing = ss.strangle_shadow_health(now=CLOSE)
    assert missing["enabled"] is True
    assert missing["rows_written"] == 0
    assert missing["latest_age_seconds"] is None
    assert missing["status"] == "empty"

    obs(q, 779, 48, 52)
    health = ss.strangle_shadow_health(now=CLOSE - 700)
    assert health["enabled"] is True
    assert health["status"] == "ok"
    assert health["rows_written"] == 1
    assert health["latest_created_at"] == pytest.approx(CLOSE - 779)
    assert health["latest_age_seconds"] == pytest.approx(79.0)


def test_multi_round_harvests_each_mark(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_STRANGLE_SHADOW", "true")
    monkeypatch.setenv("Q15_STRANGLE_SHADOW_DB", str(tmp_path / "mr.sqlite3"))
    monkeypatch.setenv("Q15_STRANGLE_SHADOW_WIDTH", "5")
    monkeypatch.setenv("Q15_STRANGLE_SHADOW_OPEN_MARKS", "780,600")
    q = ss.StrangleShadow()
    obs(q, 779, 48, 52)               # round 780 quotes
    obs(q, 700, 44, 45)               # YES fills (timer arms)
    obs(q, 695, 55, 58)               # NO fills within delay -> LOCKED round 780
    obs(q, 599, 48, 52)               # round 600 opens fresh quotes
    obs(q, 470, 48, 52)               # below 600-120 -> round 600 EXPIRED unfilled
    rows = q._conn.execute(
        "SELECT round_mark, state, pnl_cents FROM strangle_windows ORDER BY round_mark DESC"
    ).fetchall()
    assert [(r["round_mark"], r["state"]) for r in rows] == [(780.0, "LOCKED"), (600.0, "EXPIRED")]
    assert rows[0]["pnl_cents"] == pytest.approx(10.0)


def test_legacy_db_degrades_to_single_round(tmp_path, monkeypatch):
    # build a legacy DB (no round_mark, UNIQUE(asset, close_time))
    import sqlite3 as s3
    db = str(tmp_path / "legacy.sqlite3")
    c = s3.connect(db)
    c.execute("""CREATE TABLE strangle_windows (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
        asset TEXT NOT NULL, close_time REAL NOT NULL, quote_sr REAL, mid0 REAL,
        width REAL, yes_bid REAL, no_bid REAL, spread0 REAL, state TEXT NOT NULL,
        yes_fill_ts REAL, yes_fill_px REAL, no_fill_ts REAL, no_fill_px REAL,
        hedge_ts REAL, hedge_side TEXT, hedge_px REAL, hedge_fee REAL,
        pnl_cents REAL, last_yes_mid REAL, finalized_at REAL, utc_hour INTEGER,
        ttl_s REAL, hedge_delay_s REAL, spread_at_first_fill REAL,
        postfill_mids_json TEXT, UNIQUE(asset, close_time))""")
    c.commit(); c.close()
    monkeypatch.setenv("Q15_STRANGLE_SHADOW", "true")
    monkeypatch.setenv("Q15_STRANGLE_SHADOW_DB", db)
    monkeypatch.setenv("Q15_STRANGLE_SHADOW_OPEN_MARKS", "780,600")
    q = ss.StrangleShadow()
    assert q.enabled                                  # migration succeeded
    obs(q, 779, 48, 52)                               # round 780 works
    assert row(q)["state"] == "QUOTED"
    obs(q, 599, 48, 52)                               # round 600 silently ignored (legacy UNIQUE)
    n = q._conn.execute("SELECT COUNT(*) FROM strangle_windows").fetchone()[0]
    assert n == 1
