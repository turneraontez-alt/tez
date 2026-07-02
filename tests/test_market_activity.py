import sqlite3

from market_activity import MarketActivityRecorder, asleep_score, book_changed
from q15_upgrade.ultoim_v2.ledger import UltoimV2Ledger


def test_asleep_score_combines_low_volume_and_stale_book():
    assert asleep_score(
        cumulative_volume=0.0,
        open_interest=0.0,
        seconds_since_last_book_change=60.0,
    ) == 100.0
    assert asleep_score(
        cumulative_volume=500.0,
        open_interest=1000.0,
        seconds_since_last_book_change=0.0,
    ) == 0.0
    assert asleep_score(
        cumulative_volume=10.0,
        open_interest=0.0,
        seconds_since_last_book_change=30.0,
    ) == 64.0


def test_book_changed_detects_depth_delta():
    assert book_changed({"bid_liquidity_added": 0, "ask_liquidity_removed": 1}) is True
    assert book_changed({"imbalance_flip": True}) is True
    assert book_changed({"bid_liquidity_added": 0}) is False


def test_market_activity_records_and_exposes_context(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_FEED_MARKET_ACTIVITY", "true")
    rec = MarketActivityRecorder(db_path=str(tmp_path / "activity.sqlite3"))
    rec.start = lambda: True

    row1 = rec.observe(
        asset="BTC",
        ticker="KXBTC",
        market={"volume": 5, "open_interest": 8},
        orderbook_changed=True,
        now=1000.0,
    )
    row2 = rec.observe(
        asset="BTC",
        ticker="KXBTC",
        market={"volume": 5, "open_interest": 8},
        orderbook_changed=False,
        now=1030.0,
    )
    assert row1["seconds_since_last_book_change"] == 0.0
    assert row2["seconds_since_last_book_change"] == 30.0
    ctx = rec.context(ticker="KXBTC", now=1030.0)
    assert ctx["market_volume"] == 5.0
    assert ctx["open_interest"] == 8.0
    assert ctx["seconds_since_last_book_change"] == 30.0
    assert ctx["asleep_score"] > 0

    assert rec._drain_once_for_tests() is True
    conn = sqlite3.connect(str(tmp_path / "activity.sqlite3"))
    try:
        stored = conn.execute(
            "SELECT asset,ticker,cumulative_volume,open_interest,age_s FROM market_activity_samples"
        ).fetchone()
    finally:
        conn.close()
    assert stored == ("BTC", "KXBTC", 5.0, 8.0, 0.0)


def test_market_activity_stale_context_nulls(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_FEED_MARKET_ACTIVITY", "true")
    monkeypatch.setenv("Q15_FEED_MARKET_ACTIVITY_STALE_SECONDS", "10")
    rec = MarketActivityRecorder(db_path=str(tmp_path / "activity.sqlite3"))
    rec.start = lambda: True
    rec.observe(
        asset="BTC",
        ticker="KXBTC",
        market={"volume": 5},
        orderbook_changed=True,
        now=1000.0,
    )
    assert rec.context(ticker="KXBTC", now=1011.0)["asleep_score"] is None


def test_market_activity_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_FEED_MARKET_ACTIVITY", "false")
    rec = MarketActivityRecorder(db_path=str(tmp_path / "activity.sqlite3"))
    assert rec.observe(
        asset="BTC",
        ticker="KXBTC",
        market={"volume": 5},
        orderbook_changed=True,
        now=1000.0,
    ) is None


def test_ultoim_v2_market_activity_columns_migrate_and_record(tmp_path):
    led = UltoimV2Ledger(str(tmp_path / "u2.sqlite3"))
    cols = {row["name"] for row in led._conn.execute("PRAGMA table_info(ultoim_v2_predictions)")}
    assert {
        "market_volume",
        "open_interest",
        "seconds_since_last_book_change",
        "asleep_score",
        "market_activity_age_s",
    }.issubset(cols)
    row = {
        "created_at": 1000.0,
        "model_version": "test",
        "asset": "BTC",
        "ticker": "KXBTC",
        "interval": "10M",
        "window_key": 1,
        "mark_seconds": 600.0,
        "fired": 0,
        "close_time": 2000.0,
        "delivery_status": "RECORDED",
        "market_volume": 5.0,
        "open_interest": 8.0,
        "seconds_since_last_book_change": 30.0,
        "asleep_score": 64.0,
        "market_activity_age_s": 1.0,
    }
    assert led.record_decision(row) is not None
    stored = led._conn.execute(
        "SELECT market_volume,open_interest,seconds_since_last_book_change,asleep_score,market_activity_age_s "
        "FROM ultoim_v2_predictions"
    ).fetchone()
    assert tuple(stored) == (5.0, 8.0, 30.0, 64.0, 1.0)
