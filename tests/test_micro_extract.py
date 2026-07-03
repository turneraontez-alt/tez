"""Tests for tools/micro_extract.py — synthetic source DBs, deterministic."""
from __future__ import annotations

import gzip
import json
import sqlite3

from tools import micro_extract as me

CLOSE = 1_800_000_000.0


def _mk_sources(tmp_path, points=40):
    spot = str(tmp_path / "spot.sqlite3")
    c = sqlite3.connect(spot)
    c.execute("""CREATE TABLE spot_depth_snapshots (created_at REAL, asset TEXT,
        mid REAL, spread_bps REAL, depth_imbalance REAL, bid_depth_top REAL,
        ask_depth_top REAL, trade_net_qty_5s REAL, trade_buy_notional_5s REAL,
        trade_sell_notional_5s REAL, bid_levels_json TEXT, ask_levels_json TEXT)""")
    levels = json.dumps([[100.0 - i * 0.1, 5.0 if i != 3 else 50.0] for i in range(10)])
    for i in range(points):
        c.execute("INSERT INTO spot_depth_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (CLOSE - 900 + i * (900 / points), "BTC", 100.0, 2.0, 0.1,
                   10.0, 11.0, 0.5, 100.0, 90.0, levels, levels))
    c.commit(); c.close()
    kraken = str(tmp_path / "kraken.sqlite3")
    k = sqlite3.connect(kraken)
    k.execute("""CREATE TABLE kraken_l3_summaries (created_at REAL, symbol TEXT,
        mid REAL, spread_bps REAL, depth_imbalance REAL, cancel_to_add_5s REAL,
        add_count_5s INTEGER, delete_count_5s INTEGER, trade_count_5s INTEGER,
        avg_bid_order_size_levels REAL, avg_ask_order_size_levels REAL,
        net_matched_buy_notional_60s REAL, bid_levels_json TEXT, ask_levels_json TEXT)""")
    for i in range(points):
        k.execute("INSERT INTO kraken_l3_summaries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (CLOSE - 900 + i * (900 / points), "BTC/USD", 100.0, 2.0, -0.05,
                   0.8, 10, 8, 3, 0.4, 0.5, 1000.0, None, None))
    k.commit(); k.close()
    v95 = str(tmp_path / "v95.sqlite3")
    v = sqlite3.connect(v95)
    v.execute("CREATE TABLE predictions (asset TEXT, close_time REAL, official_result TEXT)")
    v.execute("INSERT INTO predictions VALUES ('BTC', ?, 'YES')", (CLOSE,))
    v.commit(); v.close()
    return spot, kraken, v95


def test_extract_writes_one_gz_path_per_window(tmp_path):
    spot, kraken, v95 = _mk_sources(tmp_path)
    out = str(tmp_path / "out.sqlite3")
    res = me.run(spot, kraken, v95, out, hours=26, now=CLOSE + 3600)
    assert res == {"windows": 1, "written": 1}
    c = sqlite3.connect(out)
    r = c.execute("SELECT asset, spot_points, kraken_points, official_result, blob_gz"
                  " FROM micro_paths").fetchone()
    assert r[0] == "BTC" and r[1] == 40 and r[2] == 40 and r[3] == "YES"
    blob = json.loads(gzip.decompress(r[4]))
    assert len(blob["spot"]) == 40 and len(blob["kraken"]) == 40
    # wall metric: level 3 is 10x median -> ratio ~10, present in spot points
    assert blob["spot"][0][9] and blob["spot"][0][9] > 5


def test_idempotent_rerun_writes_nothing(tmp_path):
    spot, kraken, v95 = _mk_sources(tmp_path)
    out = str(tmp_path / "out.sqlite3")
    me.run(spot, kraken, v95, out, now=CLOSE + 3600)
    res2 = me.run(spot, kraken, v95, out, now=CLOSE + 3600)
    assert res2["written"] == 0
    c = sqlite3.connect(out)
    assert c.execute("SELECT COUNT(*) FROM micro_paths").fetchone()[0] == 1


def test_downsample_caps_points(tmp_path):
    spot, kraken, v95 = _mk_sources(tmp_path, points=900)
    out = str(tmp_path / "out.sqlite3")
    me.run(spot, kraken, v95, out, now=CLOSE + 3600)
    c = sqlite3.connect(out)
    sp, kp = c.execute("SELECT spot_points, kraken_points FROM micro_paths").fetchone()
    assert sp == me.MAX_POINTS_PER_SOURCE and kp == me.MAX_POINTS_PER_SOURCE


def test_missing_sources_noop(tmp_path):
    _, _, v95 = _mk_sources(tmp_path)
    out = str(tmp_path / "out.sqlite3")
    res = me.run(str(tmp_path / "nope1.sqlite3"), str(tmp_path / "nope2.sqlite3"),
                 v95, out, now=CLOSE + 3600)
    assert res == {"windows": 0, "written": 0}


def test_wall_metric_shapes():
    levels = json.dumps([[100 - i, 2.0] for i in range(10)])
    ratio, dist = me.wall_metric(levels, 100.0)
    assert ratio == 1.0 and dist is not None
    assert me.wall_metric(None, 100.0) == (None, None)
    assert me.wall_metric("not json", 100.0) == (None, None)
