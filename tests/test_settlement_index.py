import json
import sqlite3

from settlement_index import SettlementIndexCollector, settlement_index_context
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
