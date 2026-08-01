import sqlite3

from ladder_probe import LadderProbe, ladder_metrics


class FakeKalshi:
    def __init__(self):
        self.markets = [
            {
                "ticker": "KXBTC-LOW",
                "floor_strike": 100.0,
                "yes_bid": 70,
                "yes_ask": 72,
                "volume": 10,
            },
            {
                "ticker": "KXBTC-MID",
                "floor_strike": 110.0,
                "yes_bid": 50,
                "yes_ask": 52,
                "volume": 20,
            },
            {
                "ticker": "KXBTC-HIGH",
                "floor_strike": 120.0,
                "yes_bid": 30,
                "yes_ask": 32,
                "volume": 30,
            },
        ]

    def discover(self, series_ticker, **kwargs):
        assert series_ticker == "KXBTC15M"
        assert kwargs["min_close_ts"] == 1998
        assert kwargs["max_close_ts"] == 2002
        return self.markets

    def get_orderbook(self, ticker):
        raise AssertionError("market quotes should avoid orderbook fetch")


def test_ladder_metrics_from_cross_strike_spacing():
    rows = [
        {"strike": 100.0, "yes_bid": 70.0, "yes_ask": 72.0},
        {"strike": 110.0, "yes_bid": 50.0, "yes_ask": 52.0},
        {"strike": 120.0, "yes_bid": 30.0, "yes_ask": 32.0},
    ]
    out = ladder_metrics(rows)
    assert out["implied_sigma"] == 2.0
    assert out["ladder_skew"] == -40.0
    assert out["ladder_arb_flag"] == 0


def test_ladder_metrics_flags_adjacent_inversion():
    out = ladder_metrics([
        {"strike": 100.0, "yes_bid": 40.0, "yes_ask": 42.0},
        {"strike": 110.0, "yes_bid": 45.0, "yes_ask": 47.0},
    ])
    assert out["ladder_arb_flag"] == 1


def test_ladder_probe_captures_all_window_strikes(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_FEED_LADDER", "true")
    probe = LadderProbe(db_path=str(tmp_path / "ladder.sqlite3"), client=FakeKalshi())
    rows = probe.capture_job({
        "asset": "BTC",
        "series_ticker": "KXBTC15M",
        "close_time": 2000.0,
        "checkpoint": "10M",
        "ts": 1000.0,
    })
    assert [row["ticker"] for row in rows] == ["KXBTC-LOW", "KXBTC-MID", "KXBTC-HIGH"]
    assert {row["implied_sigma"] for row in rows} == {2.0}
    assert probe._insert_rows(rows) == 3

    conn = sqlite3.connect(str(tmp_path / "ladder.sqlite3"))
    try:
        count = conn.execute("SELECT COUNT(*) FROM ladder_captures").fetchone()[0]
        sample = conn.execute(
            "SELECT checkpoint, strike, yes_bid, yes_ask, volume, age_s FROM ladder_captures "
            "ORDER BY strike LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert count == 3
    assert sample[:5] == ("10M", 100.0, 70.0, 72.0, 10.0)
    assert sample[5] >= 0


def test_ladder_observe_dedups_checkpoint_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_FEED_LADDER", "true")
    probe = LadderProbe(db_path=str(tmp_path / "ladder.sqlite3"), client=FakeKalshi())
    probe.start = lambda: True

    assert probe.observe(
        asset="BTC",
        series_ticker="KXBTC15M",
        close_time=2000.0,
        seconds_remaining=600.0,
        now=1000.0,
    ) is True
    assert probe.observe(
        asset="BTC",
        series_ticker="KXBTC15M",
        close_time=2000.0,
        seconds_remaining=599.0,
        now=1001.0,
    ) is False
    assert probe._queue.qsize() == 1


def test_ladder_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_FEED_LADDER", "false")
    probe = LadderProbe(db_path=str(tmp_path / "ladder.sqlite3"), client=FakeKalshi())
    assert probe.observe(
        asset="BTC",
        series_ticker="KXBTC15M",
        close_time=2000.0,
        seconds_remaining=600.0,
        now=1000.0,
    ) is False


def test_ladder_watchdog_tracks_worker_not_sparse_capture_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_FEED_LADDER", "true")
    probe = LadderProbe(db_path=str(tmp_path / "ladder.sqlite3"), client=FakeKalshi())
    probe.start()
    try:
        health = probe.health()
        assert health["worker_alive"] is True
        assert health["last_capture_age_seconds"] is None
        assert health["watchdog_age_seconds"] == 0.0
        assert health["status"] == "running"
    finally:
        probe.stop()
