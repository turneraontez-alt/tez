import gzip
import json
import sqlite3

from path_recorder import PathRecorder


def test_path_recorder_samples_and_flushes_compressed_window(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_FEED_PATH_RECORDER", "true")
    monkeypatch.setenv("Q15_FEED_PATH_RECORDER_SAMPLE_SECONDS", "1")
    rec = PathRecorder(db_path=str(tmp_path / "paths.sqlite3"))
    rec.start = lambda: True

    assert rec.observe(
        asset="BTC",
        close_time=2000.0,
        seconds_remaining=900.0,
        index_px=68000.0,
        spot_px=67999.0,
        yes_bid=48.0,
        yes_ask=52.0,
        now=1000.0,
    ) is True
    assert rec.observe(
        asset="BTC",
        close_time=2000.0,
        seconds_remaining=899.5,
        index_px=68001.0,
        yes_bid=50.0,
        yes_ask=54.0,
        now=1000.5,
    ) is False
    assert rec.observe(
        asset="BTC",
        close_time=2000.0,
        seconds_remaining=899.0,
        index_px=None,
        spot_px=68002.0,
        yes_bid=50.0,
        yes_ask=54.0,
        now=1001.0,
    ) is True

    assert rec.flush_window(asset="BTC", close_time=2000.0, now=2000.0) is True
    assert rec._drain_once_for_tests() is True

    conn = sqlite3.connect(str(tmp_path / "paths.sqlite3"))
    try:
        row = conn.execute(
            "SELECT asset,close_time,point_count,path_json_gz FROM window_paths"
        ).fetchone()
    finally:
        conn.close()

    assert row[:3] == ("BTC", 2000.0, 2)
    points = json.loads(gzip.decompress(row[3]).decode("utf-8"))
    assert points[0]["px"] == 68000.0
    assert points[0]["px_source"] == "index"
    assert points[0]["yes_mid"] == 50.0
    assert points[1]["px"] == 68002.0
    assert points[1]["px_source"] == "spot"


def test_path_recorder_caps_points_per_window(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_FEED_PATH_RECORDER", "true")
    monkeypatch.setenv("Q15_FEED_PATH_RECORDER_MAX_POINTS", "2")
    monkeypatch.setenv("Q15_FEED_PATH_RECORDER_SAMPLE_SECONDS", "0.1")
    rec = PathRecorder(db_path=str(tmp_path / "paths.sqlite3"))
    rec.start = lambda: True
    for idx in range(3):
        rec.observe(
            asset="BTC",
            close_time=2000.0,
            seconds_remaining=900.0 - idx,
            spot_px=100.0 + idx,
            now=1000.0 + idx,
        )
    health = rec.health()
    assert health["points_in_memory"] == 2


def test_path_recorder_flush_expired_once(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_FEED_PATH_RECORDER", "true")
    rec = PathRecorder(db_path=str(tmp_path / "paths.sqlite3"))
    rec.start = lambda: True
    rec.observe(asset="BTC", close_time=1000.0, seconds_remaining=1.0, spot_px=1.0, now=999.0)

    assert rec.flush_expired(now=1000.0) == 1
    assert rec.flush_expired(now=1001.0) == 0


def test_path_recorder_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_FEED_PATH_RECORDER", "false")
    rec = PathRecorder(db_path=str(tmp_path / "paths.sqlite3"))
    assert rec.observe(
        asset="BTC",
        close_time=2000.0,
        seconds_remaining=900.0,
        spot_px=1.0,
        now=1000.0,
    ) is False
