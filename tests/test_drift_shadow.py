"""Tests for the drift-hypothesis shadow recorder (q15_upgrade/drift_shadow.py).

Deterministic: driven by explicit slates + settlement events. Record-only. v2
records a 60-80 superset envelope (all qualifying picks, ranked); three nested
books (primary 65-73 top-1 / volume 60-73 all / diagnostic 74-80) grade from
the one stream, each against its frozen bar — pinned here so config drift is
caught.
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
    assert rec._qualifies(_cap(yes_ask_cents=60.0))           # envelope floor (v2)
    assert rec._qualifies(_cap(yes_ask_cents=80.0))           # envelope ceiling (v2)
    assert not rec._qualifies(_cap(yes_ask_cents=59.0))       # below envelope
    assert not rec._qualifies(_cap(yes_ask_cents=81.0))       # above envelope
    assert not rec._qualifies(_cap(distance_from_strike=1e-3))  # not near-strike
    assert not rec._qualifies(_cap(flip_probability=40.0))    # flip too high
    assert not rec._qualifies(_cap(distance_from_strike=None))  # missing -> skip


def test_records_all_quals_ranked(rec):
    slate = [
        _cap(ticker="A", calibrated_yes_probability=0.68, yes_ask_cents=66.0),  # disagree .02
        _cap(ticker="B", calibrated_yes_probability=0.78, yes_ask_cents=67.0),  # disagree .11 <- rank 1
        _cap(ticker="C", predicted_side="NO"),                                   # disqualified
    ]
    assert rec.observe_window(model_version="m", window_key=10, close_time=9000.0,
                              slate=slate, now=1000.0) is True
    rows = {r["ticker"]: r for r in rec._conn.execute(
        "SELECT ticker, pick_rank FROM drift_picks").fetchall()}
    assert set(rows) == {"A", "B"}          # v2: every qualifying candidate recorded
    assert rows["B"]["pick_rank"] == 1      # best disagreement ranked first
    assert rows["A"]["pick_rank"] == 2
    # idempotent per (window, ticker)
    assert rec.observe_window(model_version="m", window_key=10, close_time=9000.0,
                              slate=slate, now=1010.0) is False
    assert rec._conn.execute("SELECT COUNT(*) FROM drift_picks").fetchone()[0] == 2


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


def test_scoreboard_nested_books(rec):
    sb = rec.scoreboard()
    assert sb["book_primary_65_73_top1"]["status"] == "empty"
    # 40 resolved 67c losers -> primary KILL bar trips; volume book (kill_n=60) still accruing
    for i in range(40):
        rec.observe_window(model_version="m", window_key=100 + i, close_time=9000.0,
                           slate=[_cap(ticker=f"L{i}", yes_ask_cents=67.0)], now=1000.0 + i)
    rec.resolve([{"ticker": f"L{i}", "result": "NO"} for i in range(40)], now=2000.0)
    sb = rec.scoreboard()
    assert sb["book_primary_65_73_top1"]["n_resolved"] == 40
    assert sb["book_primary_65_73_top1"]["status"] == "KILL"
    assert sb["book_volume_60_73_all"]["n_resolved"] == 40
    assert sb["book_volume_60_73_all"]["status"] == "ACCRUING"   # kill gate is n>=60
    assert sb["book_diag_74_80"]["status"] == "empty"
    # a 62c pick lands in the volume book but NOT the primary band
    rec.observe_window(model_version="m", window_key=999, close_time=9000.0,
                       slate=[_cap(ticker="CHEAP", yes_ask_cents=62.0)], now=3000.0)
    rec.resolve([{"ticker": "CHEAP", "result": "YES"}], now=4000.0)
    sb = rec.scoreboard()
    assert sb["book_primary_65_73_top1"]["n_resolved"] == 40
    assert sb["book_volume_60_73_all"]["n_resolved"] == 41


def test_primary_top1_recomputed_within_band(rec):
    # envelope rank-1 is a 62c pick (outside primary band); primary book must
    # use the best-disagreement row WITHIN 65-73, not the envelope rank
    slate = [
        _cap(ticker="CHEAP", calibrated_yes_probability=0.90, yes_ask_cents=62.0),  # disagree .28 (rank1)
        _cap(ticker="BAND",  calibrated_yes_probability=0.80, yes_ask_cents=67.0),  # disagree .13
    ]
    rec.observe_window(model_version="m", window_key=500, close_time=9000.0,
                       slate=slate, now=1000.0)
    rec.resolve([{"ticker": "CHEAP", "result": "YES"}, {"ticker": "BAND", "result": "YES"}], now=2000.0)
    sb = rec.scoreboard()
    assert sb["book_primary_65_73_top1"]["n_resolved"] == 1   # the 67c row
    assert sb["book_volume_60_73_all"]["n_resolved"] == 2


def test_v1_schema_migrates_aside(tmp_path, monkeypatch):
    import sqlite3 as s3
    db = str(tmp_path / "legacy.sqlite3")
    c = s3.connect(db)
    c.execute("""CREATE TABLE drift_picks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
        model_version TEXT, asset TEXT NOT NULL, ticker TEXT NOT NULL,
        window_key INTEGER NOT NULL, close_time REAL, side TEXT NOT NULL,
        ask_cents REAL NOT NULL, distance_sigma REAL, flip_probability REAL,
        calibrated_yes_probability REAL, side_prob REAL, disagreement REAL,
        slate_n INTEGER, features_version TEXT NOT NULL, official_result TEXT,
        resolved_at REAL, correct INTEGER, pnl_cents REAL,
        UNIQUE(model_version, window_key))""")
    c.execute("INSERT INTO drift_picks (created_at, asset, ticker, window_key, side,"
              " ask_cents, features_version) VALUES (1,'DOGE','T1',1,'YES',67,'drift-shadow-v1')")
    c.commit(); c.close()
    monkeypatch.setenv("Q15_DRIFT_SHADOW", "true")
    monkeypatch.setenv("Q15_DRIFT_SHADOW_DB", db)
    ds.reset_recorder()
    r = ds.DriftShadow()
    assert r.enabled                       # migration succeeded
    # v1 rows preserved aside; new table has pick_rank
    assert r._conn.execute("SELECT COUNT(*) FROM drift_picks_v1").fetchone()[0] == 1
    assert r.observe_window(model_version="m", window_key=1, close_time=9000.0,
                            slate=[_cap(ticker="T2")], now=10.0) is True
    assert r._conn.execute("SELECT pick_rank FROM drift_picks").fetchone()[0] == 1


def test_scoreboard_accruing_below_kill_n(rec):
    for i in range(20):
        rec.observe_window(model_version="m", window_key=200 + i, close_time=9000.0,
                           slate=[_cap(ticker=f"X{i}", yes_ask_cents=67.0)], now=1000.0 + i)
    rec.resolve([{"ticker": f"X{i}", "result": "NO"} for i in range(20)], now=2000.0)
    sb = rec.scoreboard()
    assert sb["book_primary_65_73_top1"]["n_resolved"] == 20
    assert sb["book_primary_65_73_top1"]["status"] == "ACCRUING"  # below the n=40 kill gate


def test_health_reports_rows(rec):
    missing = rec.health(now=1000.0)
    assert missing["status"] == "empty" and missing["rows_written"] == 0
    rec.observe_window(model_version="m", window_key=300, close_time=9000.0,
                       slate=[_cap()], now=900.0)
    h = rec.health(now=1000.0)
    assert h["status"] == "ok" and h["rows_written"] == 1
    assert h["latest_age_seconds"] == pytest.approx(100.0)
