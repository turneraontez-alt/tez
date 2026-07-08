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


def test_size_weight_tercile_thresholds(rec):
    # frozen train-fit terciles (2026-07-07): lo=-0.1157, hi=-0.0917
    assert rec.w_lo_threshold == pytest.approx(-0.1157)
    assert rec.w_hi_threshold == pytest.approx(-0.0917)
    assert rec.size_weight(None) == 1.0        # missing disagreement -> flat
    assert rec.size_weight(0.03) == 1.5        # strong model edge -> upsize
    assert rec.size_weight(-0.0917) == 1.5     # boundary is inclusive at hi
    assert rec.size_weight(-0.10) == 1.0       # mid tercile
    assert rec.size_weight(-0.1157) == 1.0     # boundary stays mid at lo
    assert rec.size_weight(-0.20) == 0.5       # weak edge -> downsize


def test_size_weight_recorded_on_insert(rec):
    # default _cap: cal .70 @ 67c -> disagreement +.03 >= hi tercile -> 1.5x
    rec.observe_window(model_version="m", window_key=600, close_time=9000.0,
                       slate=[_cap()], now=1000.0)
    w = rec._conn.execute("SELECT size_weight FROM drift_picks").fetchone()[0]
    assert w == pytest.approx(1.5)


def test_scoreboard_weighted_pnl_alongside_flat(rec):
    # 1.5x winner (disagree +.03) + 0.5x loser (disagree -.17); 67c: win +31 / loss -69
    rec.observe_window(model_version="m", window_key=700, close_time=9000.0,
                       slate=[_cap(ticker="WHI", calibrated_yes_probability=0.70)], now=1000.0)
    rec.observe_window(model_version="m", window_key=701, close_time=9000.0,
                       slate=[_cap(ticker="WLO", calibrated_yes_probability=0.50)], now=1000.0)
    rec.resolve([{"ticker": "WHI", "result": "YES"},
                 {"ticker": "WLO", "result": "NO"}], now=2000.0)
    book = rec.scoreboard()["book_volume_60_73_all"]
    assert book["total_pnl_cents"] == pytest.approx(-38.0)        # flat: 31 - 69
    assert book["weighted_pnl_cents"] == pytest.approx(12.0)      # 1.5*31 - 0.5*69
    assert book["ev_cents"] == pytest.approx(-19.0)
    assert book["weighted_ev_per_unit_cents"] == pytest.approx(6.0)  # 12 / (1.5+0.5)
    # sizing never gates the bars: status still graded on flat numbers
    assert book["status"] == "ACCRUING"


def test_v2_pre_sizing_schema_gains_size_weight(tmp_path, monkeypatch):
    # a live v2 DB written before the sizing lever has no size_weight column;
    # init must ALTER it in and read old NULL rows as weight 1.0
    import sqlite3 as s3
    db = str(tmp_path / "v2old.sqlite3")
    c = s3.connect(db)
    c.execute("""CREATE TABLE drift_picks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
        model_version TEXT, asset TEXT NOT NULL, ticker TEXT NOT NULL,
        window_key INTEGER NOT NULL, close_time REAL, side TEXT NOT NULL,
        ask_cents REAL NOT NULL, distance_sigma REAL, flip_probability REAL,
        calibrated_yes_probability REAL, side_prob REAL, disagreement REAL,
        slate_n INTEGER, pick_rank INTEGER, features_version TEXT NOT NULL,
        official_result TEXT, resolved_at REAL, correct INTEGER, pnl_cents REAL,
        UNIQUE(model_version, window_key, ticker))""")
    c.execute("INSERT INTO drift_picks (created_at, asset, ticker, window_key, side,"
              " ask_cents, pick_rank, features_version, official_result, correct, pnl_cents)"
              " VALUES (1,'DOGE','OLD',1,'YES',67,1,'drift-shadow-v2','YES',1,31.0)")
    c.commit(); c.close()
    monkeypatch.setenv("Q15_DRIFT_SHADOW", "true")
    monkeypatch.setenv("Q15_DRIFT_SHADOW_DB", db)
    ds.reset_recorder()
    r = ds.DriftShadow()
    assert r.enabled
    cols = {row["name"] for row in r._conn.execute("PRAGMA table_info(drift_picks)")}
    assert "size_weight" in cols
    # the v1 rename guard must NOT fire on a v2 table
    assert r._conn.execute("SELECT COUNT(*) FROM drift_picks").fetchone()[0] == 1
    book = r.scoreboard()["book_volume_60_73_all"]
    assert book["n_resolved"] == 1
    assert book["weighted_pnl_cents"] == pytest.approx(31.0)   # NULL weight -> 1.0


def test_scoreboard_accruing_below_kill_n(rec):
    for i in range(20):
        rec.observe_window(model_version="m", window_key=200 + i, close_time=9000.0,
                           slate=[_cap(ticker=f"X{i}", yes_ask_cents=67.0)], now=1000.0 + i)
    rec.resolve([{"ticker": f"X{i}", "result": "NO"} for i in range(20)], now=2000.0)
    sb = rec.scoreboard()
    assert sb["book_primary_65_73_top1"]["n_resolved"] == 20
    assert sb["book_primary_65_73_top1"]["status"] == "ACCRUING"  # below the n=40 kill gate


def test_v3_tilt_weight_functions(rec):
    # spread: 3-4c lazy-market cohort upweighted; >=5c downweighted; <=2c flat
    assert ds.DriftShadow.spread_weight(None) == 1.0
    assert ds.DriftShadow.spread_weight(2.0) == 1.0
    assert ds.DriftShadow.spread_weight(3.0) == 1.5
    assert ds.DriftShadow.spread_weight(4.0) == 1.5
    assert ds.DriftShadow.spread_weight(5.0) == 0.5
    # session: US evening rich / EU poor / overnight near-flat
    assert ds.DriftShadow.session_weight(None) == 1.0
    assert ds.DriftShadow.session_weight(20) == 1.33
    assert ds.DriftShadow.session_weight(8) == 0.75
    assert ds.DriftShadow.session_weight(15) == 0.75
    assert ds.DriftShadow.session_weight(3) == 0.84
    # stack is the clipped product
    assert ds.DriftShadow.stack_weight(3.0, 20) == 1.5      # 1.5*1.33 clipped
    assert ds.DriftShadow.stack_weight(5.0, 8) == 0.5       # 0.5*0.75 clipped
    assert ds.DriftShadow.stack_weight(None, None) == 1.0


def test_v3_tilts_recorded_on_pick(rec):
    # captured_at 2026-07-07 20:00 UTC -> hour 20 -> session 1.33; spread 3 -> 1.5
    ts = 1783454400.0  # 2026-07-07T20:00:00Z
    rec.observe_window(model_version="m", window_key=800, close_time=9000.0,
                       slate=[_cap(spread_cents=3.0, depth_contracts=120.0, captured_at=ts)],
                       now=ts + 10)
    row = rec._conn.execute(
        "SELECT spread_cents, depth_contracts, spread_weight, session_weight, stack_weight"
        " FROM drift_picks").fetchone()
    assert row["spread_cents"] == 3.0 and row["depth_contracts"] == 120.0
    assert row["spread_weight"] == 1.5
    assert row["session_weight"] == pytest.approx(1.33)
    assert row["stack_weight"] == 1.5


def test_v3_addon_first_requal_only(rec):
    rec.observe_window(model_version="m", window_key=900, close_time=9000.0,
                       slate=[_cap(ticker="RQ", yes_ask_cents=67.0)], now=1000.0)
    # 12M: re-passes the full rule -> one add-on at the 12M ask
    n = rec.observe_checkpoint(model_version="m", window_key=900, interval="12M",
                               close_time=9000.0,
                               slate=[_cap(ticker="RQ", yes_ask_cents=69.0)], now=1060.0)
    assert n == 1
    # repeat + later checkpoint: still exactly one add-on (first re-qual wins)
    assert rec.observe_checkpoint(model_version="m", window_key=900, interval="12M",
                                  close_time=9000.0,
                                  slate=[_cap(ticker="RQ", yes_ask_cents=69.0)], now=1070.0) == 0
    assert rec.observe_checkpoint(model_version="m", window_key=900, interval="11M",
                                  close_time=9000.0,
                                  slate=[_cap(ticker="RQ", yes_ask_cents=70.0)], now=1120.0) == 0
    rows = rec._conn.execute(
        "SELECT add_interval, ask_cents, base_ask_cents FROM drift_addons").fetchall()
    assert len(rows) == 1
    assert rows[0]["add_interval"] == "12M" and rows[0]["ask_cents"] == 69.0
    assert rows[0]["base_ask_cents"] == 67.0
    # resolve grades the add-on too: base pick + add-on both settle YES
    rec.resolve([{"ticker": "RQ", "result": "YES"}], now=2000.0)
    sb = rec.scoreboard()
    assert sb["book_addon_requal"]["n_resolved"] == 1
    assert sb["book_addon_requal"]["ev_cents"] == pytest.approx(29.0)  # 100-69-2


def test_v3_addon_needs_base_pick_and_band(rec):
    # qualifying cap at 12M with NO base pick and no watch -> nothing recorded
    assert rec.observe_checkpoint(model_version="m", window_key=901, interval="12M",
                                  close_time=9000.0,
                                  slate=[_cap(ticker="NOBASE", yes_ask_cents=69.0)],
                                  now=1000.0) == 0
    assert rec._conn.execute("SELECT COUNT(*) FROM drift_addons").fetchone()[0] == 0
    assert rec._conn.execute("SELECT COUNT(*) FROM drift_latequal").fetchone()[0] == 0
    # base pick exists but the later ask is in the 74-80 diagnostic band -> no add
    rec.observe_window(model_version="m", window_key=902, close_time=9000.0,
                       slate=[_cap(ticker="RQ2", yes_ask_cents=67.0)], now=1000.0)
    assert rec.observe_checkpoint(model_version="m", window_key=902, interval="12M",
                                  close_time=9000.0,
                                  slate=[_cap(ticker="RQ2", yes_ask_cents=76.0)],
                                  now=1060.0) == 0


def test_v3_latequal_watch_then_entry(rec):
    # 13M: clean signal but ask below the 60c floor -> watch, NOT a pick
    rec.observe_window(model_version="m", window_key=903, close_time=9000.0,
                       slate=[_cap(ticker="LQ1", yes_ask_cents=55.0),
                              _cap(ticker="DIRTY", yes_ask_cents=55.0, flip_probability=40.0)],
                       now=1000.0)
    assert rec._conn.execute("SELECT COUNT(*) FROM drift_picks").fetchone()[0] == 0
    watch = rec._conn.execute("SELECT ticker FROM drift_lq_watch").fetchall()
    assert [w["ticker"] for w in watch] == ["LQ1"]      # dirty 13M is never watched
    # 12M: market repriced INTO the band with gates clean -> entry recorded
    assert rec.observe_checkpoint(model_version="m", window_key=903, interval="12M",
                                  close_time=9000.0,
                                  slate=[_cap(ticker="LQ1", yes_ask_cents=64.0),
                                         _cap(ticker="DIRTY", yes_ask_cents=64.0)],
                                  now=1060.0) == 1
    row = rec._conn.execute(
        "SELECT entry_interval, ask_cents, ask13_cents FROM drift_latequal").fetchone()
    assert row["entry_interval"] == "12M" and row["ask_cents"] == 64.0
    assert row["ask13_cents"] == 55.0
    # 9M is NOT a late-qual entry checkpoint (12M/11M/10M only)
    rec.observe_window(model_version="m", window_key=904, close_time=9000.0,
                       slate=[_cap(ticker="LQ2", yes_ask_cents=55.0)], now=2000.0)
    assert rec.observe_checkpoint(model_version="m", window_key=904, interval="9M",
                                  close_time=9000.0,
                                  slate=[_cap(ticker="LQ2", yes_ask_cents=64.0)],
                                  now=2300.0) == 0
    # grading: LQ1 settles NO -> book_latequal shows the loss at the entry ask
    rec.resolve([{"ticker": "LQ1", "result": "NO"}], now=3000.0)
    sb = rec.scoreboard()
    assert sb["book_latequal"]["n_resolved"] == 1
    assert sb["book_latequal"]["ev_cents"] == pytest.approx(-66.0)  # -(64+2)


def test_v3_scoreboard_blueprint_and_tilts(rec):
    ts = 1783454400.0  # 20:00Z -> session 1.33
    rec.observe_window(model_version="m", window_key=905, close_time=9000.0,
                       slate=[_cap(ticker="T", yes_ask_cents=67.0, spread_cents=3.0,
                                   captured_at=ts)], now=ts)
    rec.resolve([{"ticker": "T", "result": "YES"}], now=ts + 900)
    sb = rec.scoreboard()
    bp = sb["full_enable_blueprint"]
    assert set(bp) >= {"base_book_60_73", "addon_requal_12m_7m",
                       "latequal_repriced_into_band", "sizing_tilts_recorded",
                       "execution_doctrine"}
    tv = sb["tilts_volume_book"]
    assert tv["n_resolved"] == 1 and tv["flat_pnl_cents"] == pytest.approx(31.0)
    assert tv["spread_weight"]["weighted_pnl_cents"] == pytest.approx(46.5)   # 1.5x
    assert tv["stack_weight"]["weighted_pnl_cents"] == pytest.approx(46.5)    # clipped 1.5
    assert sb["bars"]["addon_requal"].startswith("KILL n>=40")


def test_v3_migrates_v21_schema_in_place(tmp_path, monkeypatch):
    # a live DB from the v2.1 build (size_weight present, no v3 columns/tables)
    import sqlite3 as s3
    db = str(tmp_path / "v21.sqlite3")
    c = s3.connect(db)
    c.execute("""CREATE TABLE drift_picks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
        model_version TEXT, asset TEXT NOT NULL, ticker TEXT NOT NULL,
        window_key INTEGER NOT NULL, close_time REAL, side TEXT NOT NULL,
        ask_cents REAL NOT NULL, distance_sigma REAL, flip_probability REAL,
        calibrated_yes_probability REAL, side_prob REAL, disagreement REAL,
        slate_n INTEGER, pick_rank INTEGER, size_weight REAL,
        features_version TEXT NOT NULL, official_result TEXT, resolved_at REAL,
        correct INTEGER, pnl_cents REAL,
        UNIQUE(model_version, window_key, ticker))""")
    c.execute("INSERT INTO drift_picks (created_at, asset, ticker, window_key, side,"
              " ask_cents, pick_rank, size_weight, features_version, official_result,"
              " correct, pnl_cents)"
              " VALUES (1,'DOGE','OLD',1,'YES',67,1,1.5,'drift-shadow-v2','YES',1,31.0)")
    c.commit(); c.close()
    monkeypatch.setenv("Q15_DRIFT_SHADOW", "true")
    monkeypatch.setenv("Q15_DRIFT_SHADOW_DB", db)
    ds.reset_recorder()
    r = ds.DriftShadow()
    assert r.enabled
    cols = {row["name"] for row in r._conn.execute("PRAGMA table_info(drift_picks)")}
    assert {"spread_weight", "session_weight", "stack_weight",
            "spread_cents", "depth_contracts"} <= cols
    tables = {t[0] for t in r._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"drift_addons", "drift_lq_watch", "drift_latequal"} <= tables
    # old row: NULL tilt weights read as 1.0; existing size_weight preserved
    tv = r.scoreboard()["tilts_volume_book"]
    assert tv["spread_weight"]["weighted_pnl_cents"] == pytest.approx(31.0)
    assert tv["size_weight"]["weighted_pnl_cents"] == pytest.approx(46.5)


def test_picks_recorded_at_returns_only_new_rows(rec):
    slate = [_cap(ticker="N1"), _cap(ticker="N2", calibrated_yes_probability=0.75)]
    rec.observe_window(model_version="m", window_key=950, close_time=9000.0,
                       slate=slate, now=1000.0)
    new = rec.picks_recorded_at("m", 950, 1000.0)
    assert {p["ticker"] for p in new} == {"N1", "N2"}
    assert all("spread_weight" in p and "ask_cents" in p for p in new)
    # re-observation at a later time inserts nothing -> helper returns nothing
    rec.observe_window(model_version="m", window_key=950, close_time=9000.0,
                       slate=slate, now=1010.0)
    assert rec.picks_recorded_at("m", 950, 1010.0) == []


def test_health_reports_rows(rec):
    missing = rec.health(now=1000.0)
    assert missing["status"] == "empty" and missing["rows_written"] == 0
    rec.observe_window(model_version="m", window_key=300, close_time=9000.0,
                       slate=[_cap()], now=900.0)
    h = rec.health(now=1000.0)
    assert h["status"] == "ok" and h["rows_written"] == 1
    assert h["latest_age_seconds"] == pytest.approx(100.0)
