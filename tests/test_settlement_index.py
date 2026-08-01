import json
import sqlite3

from settlement_index import DEFAULT_INDEX_IDS, SettlementIndexCollector, settlement_index_context
from q15_upgrade.interval_research.capture import build_capture_row
from q15_upgrade.interval_research.ledger import IntervalResearchLedger
from q15_upgrade.ultoim_v2.ledger import UltoimV2Ledger


def _cf_message(index_id="BRTI", value="68000.12", source_ms=1_710_000_000_123):
    return json.dumps({
        "type": "cfbenchmarks_value",
        "sid": 1,
        "seq": 42,
        "msg": {
            "index_id": index_id,
            "received_at": source_ms,
            "data": json.dumps({
                "type": "value",
                "id": index_id,
                "time": source_ms,
                "value": value,
            }),
            "avg_60s_data": {"value": "67999.50", "window_size": 60},
            "last_60s_windowed_average_15min": {"value": "68001.25", "window_size": 14},
        },
    })


def test_settlement_index_records_cfbenchmark_tick(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_FEED_SETTLE_INDEX", "true")
    db = tmp_path / "settle.sqlite3"
    feed = SettlementIndexCollector(db_path=str(db), index_ids={"BTC": "BRTI"})

    feed._handle_message(_cf_message())
    feed._writer_loop_once_for_tests()

    ctx = feed.context("BTC", spot_px=68001.00, now=1_710_000_001.123)
    assert ctx["index_px"] == 68000.12
    assert round(ctx["basis_cents"], 3) == 88.0
    assert round(ctx["index_age_s"], 3) == 1.0

    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT * FROM settlement_index_ticks").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[2] == "BTC"


def test_settlement_index_stale_context_is_null(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_FEED_SETTLE_INDEX", "true")
    feed = SettlementIndexCollector(db_path=str(tmp_path / "settle.sqlite3"), index_ids={"BTC": "BRTI"})
    feed._handle_message(_cf_message(source_ms=1_710_000_000_000))

    stale = feed.context("BTC", spot_px=68001.0, now=1_710_000_100.0)
    assert stale["index_px"] is None
    assert stale["index_status"] == "stale"
    assert stale["index_missing_reason"] == "settlement_index_tick_stale"


def test_strict_path_requires_all_61_fresh_seconds(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_FEED_SETTLE_INDEX", "true")
    base = 1_710_000_000
    clock = [base + 940.2]
    monkeypatch.setattr("settlement_index.time.time", lambda: clock[0])
    feed = SettlementIndexCollector(
        db_path=str(tmp_path / "settle.sqlite3"), index_ids={"BTC": "BRTI"}
    )
    for second in range(940, 1001):
        clock[0] = base + second + 0.2
        feed._handle_message(
            _cf_message(
                value=str(68_000 + second - 940),
                source_ms=(base + second) * 1000,
            )
        )

    complete = feed.path(
        "BTC",
        start_ts=base + 940,
        end_ts=base + 1000,
        now=base + 1000.5,
        max_age_s=2.0,
    )
    assert complete["status"] == "ok"
    assert complete["complete"] is True
    assert complete["count"] == 61
    assert complete["missing_seconds"] == []

    feed._history["BTC"] = type(feed._history["BTC"])(
        (row for row in feed._history["BTC"] if round(row["ts"]) != base + 970),
        maxlen=900,
    )
    missing = feed.path(
        "BTC",
        start_ts=base + 940,
        end_ts=base + 1000,
        now=base + 1000.5,
        max_age_s=2.0,
    )
    assert missing["complete"] is False
    assert missing["missing_reason"] == "settlement_index_path_incomplete"
    assert missing["missing_seconds"] == [base + 970]


def test_default_index_ids_use_live_kalshi_identifiers():
    assert DEFAULT_INDEX_IDS == {
        "BTC": "BRTI",
        "ETH": "ETHUSD_RTI",
        "SOL": "SOLUSD_RTI",
        "XRP": "XRPUSD_RTI",
        "DOGE": "DOGEUSD_RTI",
        "BNB": "BNBUSD_RTI",
        "HYPE": "HYPEUSD_RTI",
    }


def test_health_requires_every_configured_asset_to_be_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_FEED_SETTLE_INDEX", "true")
    monkeypatch.setattr("settlement_index.time.time", lambda: 1_710_000_020.0)
    feed = SettlementIndexCollector(
        db_path=str(tmp_path / "settle.sqlite3"),
        index_ids={"BTC": "BRTI", "ETH": "ETHUSD_RTI"},
    )
    feed._connected = True
    feed._connected_at = 1_710_000_000.0
    feed._handle_message(json.dumps({
        "id": 1,
        "type": "subscribed",
        "msg": {"channel": "cfbenchmarks_value", "sid": 4},
    }))
    feed._handle_message(_cf_message(source_ms=1_710_000_019_000))

    health = feed.health()
    assert health["status"] == "degraded_missing"
    assert health["fresh_assets"] == ["BTC"]
    assert health["missing_assets"] == ["ETH"]
    assert health["all_assets_ready"] is False
    assert health["fresh_coverage_ratio"] == 0.5
    assert health["watchdog_age_seconds"] == 20.0


def test_indexlist_validates_requested_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_FEED_SETTLE_INDEX", "true")
    feed = SettlementIndexCollector(
        db_path=str(tmp_path / "settle.sqlite3"),
        index_ids={"BTC": "BRTI", "ETH": "BAD_ETH_ID"},
    )
    feed._handle_message(json.dumps({
        "type": "cfbenchmarks_value_indexlist",
        "id": 2,
        "sid": 1,
        "msg": {"index_ids": ["BRTI", "ETHUSD_RTI"]},
    }))

    health = feed.health()
    assert health["available_index_ids"] == ["BRTI", "ETHUSD_RTI"]
    assert health["unsupported_assets"] == ["ETH"]
    assert health["subscription_error"] == "unsupported_index_ids:BAD_ETH_ID"


def test_all_seven_live_ids_are_parsed_and_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("Q15_FEED_SETTLE_INDEX", "true")
    monkeypatch.setattr("settlement_index.time.time", lambda: 1_710_000_001.0)
    feed = SettlementIndexCollector(db_path=str(tmp_path / "settle.sqlite3"))
    feed._connected = True
    feed._connected_at = 1_710_000_000.0
    feed._handle_message(json.dumps({
        "id": 1,
        "type": "subscribed",
        "msg": {"channel": "cfbenchmarks_value", "sid": 1},
    }))
    feed._handle_message(json.dumps({
        "type": "cfbenchmarks_value_indexlist",
        "id": 2,
        "sid": 1,
        "msg": {"index_ids": list(DEFAULT_INDEX_IDS.values())},
    }))
    for index_id in DEFAULT_INDEX_IDS.values():
        feed._handle_message(_cf_message(index_id=index_id, source_ms=1_710_000_000_500))

    health = feed.health()
    assert health["status"] == "connected"
    assert health["all_assets_ready"] is True
    assert health["missing_assets"] == []
    assert health["unsupported_assets"] == []
    assert set(health["messages_by_asset"]) == set(DEFAULT_INDEX_IDS)
    assert all(count == 1 for count in health["messages_by_asset"].values())


def test_global_context_is_null_when_collector_disabled(monkeypatch):
    monkeypatch.setenv("Q15_FEED_SETTLE_INDEX", "false")
    context = settlement_index_context("BTC", spot_px=1.0)
    assert context["index_px"] is None
    assert context["index_status"] == "disabled"
    assert context["index_missing_reason"] == "settlement_index_disabled"


def test_ultoim_v2_settlement_index_columns_migrate_and_record(tmp_path):
    led = UltoimV2Ledger(str(tmp_path / "u2.sqlite3"))
    cols = {row["name"] for row in led._conn.execute("PRAGMA table_info(ultoim_v2_predictions)")}
    assert {"index_px", "basis_cents", "index_age_s"}.issubset(cols)

    base = {
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
        "index_px": 68000.12,
        "basis_cents": 88.0,
        "index_age_s": 1.0,
    }
    assert led.record_decision(base) is not None
    row = led._conn.execute(
        "SELECT index_px,basis_cents,index_age_s FROM ultoim_v2_predictions"
    ).fetchone()
    assert tuple(row) == (68000.12, 88.0, 1.0)


def test_interval_research_settlement_index_columns_migrate_and_record(tmp_path):
    led = IntervalResearchLedger(str(tmp_path / "ir.sqlite3"))
    cols = {
        row[1]
        for row in sqlite3.connect(str(tmp_path / "ir.sqlite3")).execute(
            "PRAGMA table_info(interval_captures)"
        )
    }
    assert {"index_px", "basis_cents", "index_age_s"}.issubset(cols)

    class Canon:
        ticker = "KXBTC"
        seconds_remaining = 600.0
        settlement_time = 2000.0

    row = build_capture_row(
        model_version="ir-test",
        interval="10M",
        mark_seconds=600,
        asset="BTC",
        analysis={
            "prediction_available": True,
            "prediction_side": "NO",
            "yes_probability": 0.4,
            "quote": {"ask_cents": 62.0},
            "settlement_index": {"index_px": 68000.12, "basis_cents": 88.0, "index_age_s": 1.0},
        },
        canonical=Canon(),
        window_key=1,
        now=1000.0,
    )
    assert led.record_capture(row) is True
    stored = sqlite3.connect(str(tmp_path / "ir.sqlite3")).execute(
        "SELECT index_px,basis_cents,index_age_s FROM interval_captures"
    ).fetchone()
    assert tuple(stored) == (68000.12, 88.0, 1.0)
