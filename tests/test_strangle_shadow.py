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
