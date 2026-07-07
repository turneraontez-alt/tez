"""Tests for the drift-hypothesis shadow recorder (q15_upgrade/drift_shadow.py).

Deterministic: driven by explicit slates + settlement events. Record-only; the
pre-registered rule (near-strike YES, ask 65-73, dist<=3e-5, flip<=30) and the
frozen kill/promote bars are pinned here so a config drift is caught.
"""
from __future__ import annotations

import os

import pytest

import q15_upgrade.drift_shadow as ds


@pytest.fixture()
def rec(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_DRIFT_SHADOW", "true")
    monkeypatch.setenv("Q15_DRIFT_SHADOW_DB", str(tmp_path / "drift.sqlite3"))
    ds.reset_recorder()
    r = ds.DriftShadow()
    assert r.enabled
    return r


def _cap(**over):
    base = dict(asset="DOGE", ticker="KXDOGE-1", predicted_side="YES",
                yes_ask_cents=67.0, distance_from_strike=1e-5, flip_probability=20.0,
                calibrated_yes_probability=0.70)
    base.update(over)
    return base


def test_fee_and_pnl():
    # 67c: fee = ceil(7*.67*.33)=ceil(1.55)=2 ; win = 100-67-2 = 31 ; loss = -67-2 = -69
    assert ds.taker_fee_cents(67.0) == 2
    assert ds.net_pnl_cents(67.0, True) == pytest.approx(31.0)
    assert ds.net_pnl_cents(67.0, False) == pytest.approx(-69.0)


def test_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_DRIFT_SHADOW", "false")
    ds.reset_recorder()
    r = ds.DriftShadow(db_path=str(tmp_path / "off.sqlite3"))
    assert not r.enabled
    assert r.observe_window(model_version="m", window_key=1, close_time=1.0,
                            slate=[_cap()], now=1.0) is False
    assert ds.get_recorder() is None


def test_rule_gate_matrix(rec):
    good = _cap()
    assert rec._qualifies(good)
    assert not rec._qualifies(_cap(asset="BTC"))              # major excluded
    assert not rec._qualifies(_cap(predicted_side="NO"))      # NO side excluded
    assert not rec._qualifies(_cap(yes_ask_cents=60.0))       # below band
    assert not rec._qualifies(_cap(yes_ask_cents=80.0))       # above band
    assert not rec._qualifies(_cap(distance_from_strike=1e-3))  # not near-strike
    assert not rec._qualifies(_cap(flip_probability=40.0))    # flip too high
    assert not rec._qualifies(_cap(distance_from_strike=None))  # missing -> skip


def test_records_one_best_pick_per_window(rec):
    slate = [
        _cap(ticker="A", calibrated_yes_probability=0.68, yes_ask_cents=66.0),  # disagree .68-.66=.02
        _cap(ticker="B", calibrated_yes_probability=0.78, yes_ask_cents=67.0),  # disagree .78-.67=.11 <- best
        _cap(ticker="C", predicted_side="NO"),                                   # disqualified
    ]
    assert rec.observe_window(model_version="m", window_key=10, close_time=9000.0,
                              slate=slate, now=1000.0) is True
    rows = rec._conn.execute("SELECT ticker, side, ask_cents FROM drift_picks").fetchall()
    assert len(rows) == 1
    assert rows[0]["ticker"] == "B"
    # idempotent: same window never double-records
    assert rec.observe_window(model_version="m", window_key=10, close_time=9000.0,
                              slate=slate, now=1010.0) is False
    assert rec._conn.execute("SELECT COUNT(*) FROM drift_picks").fetchone()[0] == 1


def test_no_pick_when_none_qualify(rec):
    slate = [_cap(predicted_side="NO"), _cap(yes_ask_cents=90.0)]
    assert rec.observe_window(model_version="m", window_key=11, close_time=9000.0,
                              slate=slate, now=1000.0) is False
    assert rec._conn.execute("SELECT COUNT(*) FROM drift_picks").fetchone()[0] == 0


def test_resolve_grades_pnl(rec):
    rec.observe_window(model_version="m", window_key=12, close_time=9000.0,
                       slate=[_cap(ticker="KXWIN", yes_ask_cents=67.0)], now=1000.0)
    rec.observe_window(model_version="m", window_key=13, close_time=9000.0,
                       slate=[_cap(ticker="KXLOSE", yes_ask_cents=67.0)], now=1000.0)
    rec.resolve([{"ticker": "KXWIN", "result": "YES"},
                 {"ticker": "KXLOSE", "result": "NO"}], now=2000.0)
    rows = {r["ticker"]: r for r in rec._conn.execute(
        "SELECT ticker, correct, pnl_cents FROM drift_picks").fetchall()}
    assert rows["KXWIN"]["correct"] == 1 and rows["KXWIN"]["pnl_cents"] == pytest.approx(31.0)
    assert rows["KXLOSE"]["correct"] == 0 and rows["KXLOSE"]["pnl_cents"] == pytest.approx(-69.0)


def test_scoreboard_bars(rec):
    # empty
    assert rec.scoreboard()["status"] == "empty"
    # 40 resolved, all losers -> KILL bar trips
    for i in range(40):
        rec.observe_window(model_version="m", window_key=100 + i, close_time=9000.0,
                           slate=[_cap(ticker=f"L{i}", yes_ask_cents=67.0)], now=1000.0 + i)
    rec.resolve([{"ticker": f"L{i}", "result": "NO"} for i in range(40)], now=2000.0)
    sb = rec.scoreboard()
    assert sb["n_resolved"] == 40
    assert sb["status"] == "KILL"
    assert sb["ev_cents"] < 0


def test_scoreboard_accruing_below_kill_n(rec):
    for i in range(20):
        rec.observe_window(model_version="m", window_key=200 + i, close_time=9000.0,
                           slate=[_cap(ticker=f"X{i}", yes_ask_cents=67.0)], now=1000.0 + i)
    rec.resolve([{"ticker": f"X{i}", "result": "NO"} for i in range(20)], now=2000.0)
    sb = rec.scoreboard()
    assert sb["n_resolved"] == 20
    assert sb["status"] == "ACCRUING"  # below the n=40 kill gate


def test_health_reports_rows(rec):
    missing = rec.health(now=1000.0)
    assert missing["status"] == "empty" and missing["rows_written"] == 0
    rec.observe_window(model_version="m", window_key=300, close_time=9000.0,
                       slate=[_cap()], now=900.0)
    h = rec.health(now=1000.0)
    assert h["status"] == "ok" and h["rows_written"] == 1
    assert h["latest_age_seconds"] == pytest.approx(100.0)
