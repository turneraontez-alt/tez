from __future__ import annotations

import json
import sqlite3

import pytest

from q15_upgrade.strategy_bots.drift_evidence import (
    SOURCE_NAMES,
    enrich_drift_evidence,
)


DECISION_AT = 1_000.0
WINDOW_KEY = 77
MODEL_VERSION = "interval-research-v1"
ASSET = "SOL"
TICKER = "KXSOL15M-TEST"


def _row(**updates):
    row = {
        "created_at": DECISION_AT,
        "model_version": MODEL_VERSION,
        "asset": ASSET,
        "ticker": TICKER,
        "window_key": WINDOW_KEY,
        "predicted_side": "YES",
        "custom_input": "preserved",
    }
    row.update(updates)
    return row


def _paths(tmp_path):
    return {
        "interval": tmp_path / "interval.sqlite3",
        "v91": tmp_path / "v91.sqlite3",
        "v95": tmp_path / "v95.sqlite3",
        "spot": tmp_path / "spot.sqlite3",
    }


def _configure(monkeypatch, paths):
    monkeypatch.setenv("Q15_INTERVAL_RESEARCH_DB", str(paths["interval"]))
    monkeypatch.setenv("Q15_V91_SQLITE_PATH", str(paths["v91"]))
    monkeypatch.setenv("Q15_V95_LEDGER_DB", str(paths["v95"]))
    monkeypatch.setenv("Q15_SPOT_DEPTH_DB", str(paths["spot"]))
    monkeypatch.setenv("Q15_DRIFT_EVIDENCE_V91_BUCKET_SECONDS", "12")
    monkeypatch.setenv("Q15_DRIFT_EVIDENCE_V91_MIN_OBSERVATIONS", "3")
    monkeypatch.setenv("Q15_DRIFT_EVIDENCE_FLOW_BUCKET_MAX_LAG_SECONDS", "15")
    monkeypatch.setenv("Q15_DRIFT_EVIDENCE_FLOW_TRADE_MAX_AGE_SECONDS", "15")


def _create_schemas(paths):
    with sqlite3.connect(paths["interval"]) as connection:
        connection.execute(
            "CREATE TABLE interval_captures ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, model_version TEXT, ticker TEXT, "
            "asset TEXT, interval TEXT, window_key INTEGER, captured_at REAL, "
            "predicted_side TEXT, yes_ask_cents REAL, entry_ask_cents REAL, "
            "distance_from_strike REAL, flip_probability REAL)"
        )
    with sqlite3.connect(paths["v91"]) as connection:
        connection.execute(
            "CREATE TABLE q15_v91_observations ("
            "contract_key TEXT, asset TEXT, bucket INTEGER, observed_at REAL, "
            "side TEXT, p_yes REAL, model_version TEXT)"
        )
    with sqlite3.connect(paths["v95"]) as connection:
        connection.execute(
            "CREATE TABLE predictions ("
            "prediction_id TEXT, model_version TEXT, feature_schema_version TEXT, "
            "ticker TEXT, asset TEXT, checkpoint TEXT, created_at REAL, "
            "predicted_side TEXT, selected_probability REAL, feature_json TEXT)"
        )
    with sqlite3.connect(paths["spot"]) as connection:
        connection.execute(
            "CREATE TABLE spot_depth_snapshots ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL, asset TEXT, "
            "provider TEXT, symbol TEXT, source TEXT, trade_age_seconds REAL, "
            "trade_side_semantics TEXT, trade_buy_notional_60s REAL, "
            "trade_sell_notional_60s REAL, trade_net_notional_60s REAL)"
        )


def _insert_interval(
    connection,
    *,
    ticker,
    asset,
    interval,
    captured_at,
    side="YES",
    ask=None,
    distance=None,
    flip=None,
):
    connection.execute(
        "INSERT INTO interval_captures("
        "model_version,ticker,asset,interval,window_key,captured_at,predicted_side,"
        "yes_ask_cents,entry_ask_cents,distance_from_strike,flip_probability) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            MODEL_VERSION,
            ticker,
            asset,
            interval,
            WINDOW_KEY,
            captured_at,
            side,
            ask,
            ask,
            distance,
            flip,
        ),
    )


def _populate_complete(paths):
    with sqlite3.connect(paths["interval"]) as connection:
        _insert_interval(
            connection,
            ticker=TICKER,
            asset=ASSET,
            interval="15M",
            captured_at=880.0,
        )
        _insert_interval(
            connection,
            ticker="KXBTC15M-TEST",
            asset="BTC",
            interval="15M",
            captured_at=881.0,
        )
        # Future same-window rows must never replace the point-in-time rows.
        _insert_interval(
            connection,
            ticker=TICKER,
            asset=ASSET,
            interval="15M",
            captured_at=1_001.0,
            side="NO",
        )
        _insert_interval(
            connection,
            ticker="KXBTC15M-FUTURE",
            asset="BTC",
            interval="15M",
            captured_at=1_001.0,
            side="NO",
        )

        breadth_rows = (
            # core + asymmetric
            ("DOGE", 62.0, 2e-5),
            # asymmetric only: upper ask tier permits distance <=1e-4
            ("HYPE", 66.0, 5e-5),
            # core + asymmetric
            ("SOL", 70.0, 2e-5),
            # neither
            ("XRP", 70.0, 2e-4),
        )
        for asset, ask, distance in breadth_rows:
            _insert_interval(
                connection,
                ticker=f"KX{asset}15M-TEST",
                asset=asset,
                interval="13M",
                captured_at=999.0,
                ask=ask,
                distance=distance,
                flip=20.0,
            )
        # A future qualifying XRP must not inflate either breadth count.
        _insert_interval(
            connection,
            ticker="KXXRP15M-FUTURE",
            asset="XRP",
            interval="13M",
            captured_at=1_001.0,
            ask=62.0,
            distance=1e-5,
            flip=10.0,
        )

    sides = (
        "YES",
        "YES",
        "NO",
        "UNCERTAIN",
        "YES",
        "NO",
        "YES",
        "UNCERTAIN",
        "YES",
        "YES",
        "YES",
    )
    probabilities = (0.7, 0.8, 0.3, 0.5, 0.75, 0.2, 0.9, 0.55, 0.65, 0.8, 0.85)
    with sqlite3.connect(paths["v91"]) as connection:
        # Pre-capture evidence is excluded even though it matches the contract.
        connection.execute(
            "INSERT INTO q15_v91_observations VALUES(?,?,?,?,?,?,?)",
            (f"{TICKER}|15M", ASSET, 72, 868.0, "YES", 0.99, "v91"),
        )
        for index, (side, probability) in enumerate(zip(sides, probabilities)):
            observed_at = 880.0 + 12.0 * index
            connection.execute(
                "INSERT INTO q15_v91_observations VALUES(?,?,?,?,?,?,?)",
                (
                    f"{TICKER}|15M",
                    ASSET,
                    int(observed_at // 12),
                    observed_at,
                    side,
                    probability,
                    "v91",
                ),
            )
        connection.execute(
            "INSERT INTO q15_v91_observations VALUES(?,?,?,?,?,?,?)",
            (f"{TICKER}|15M", ASSET, 84, 1_012.0, "NO", 0.01, "v91"),
        )

    with sqlite3.connect(paths["v95"]) as connection:
        connection.execute(
            "INSERT INTO predictions VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "v95-past",
                "v95-model",
                "v95-schema",
                TICKER,
                ASSET,
                "15M",
                882.0,
                "YES",
                0.63,
                json.dumps({"flow": 0.25}),
            ),
        )
        connection.execute(
            "INSERT INTO predictions VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "v95-wrong-ticker",
                "v95-model",
                "v95-schema",
                "OTHER-TICKER",
                ASSET,
                "15M",
                999.0,
                "NO",
                0.99,
                json.dumps({"flow": -8.0}),
            ),
        )
        connection.execute(
            "INSERT INTO predictions VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "v95-future",
                "v95-model",
                "v95-schema",
                TICKER,
                ASSET,
                "15M",
                1_001.0,
                "NO",
                0.99,
                json.dumps({"flow": -9.0}),
            ),
        )

    # Newest -> oldest.  Each rolling-60s window ends exactly 60 seconds after
    # the next one, so the sums are non-overlapping.
    flows = (10.0, 10.0, -5.0, 10.0, -5.0, 10.0, 10.0, 10.0, -5.0, -5.0, 10.0, 10.0, 10.0)
    with sqlite3.connect(paths["spot"]) as connection:
        for index, flow in enumerate(flows):
            created_at = 995.0 - index * 60.0
            buy = max(flow, 0.0) + 20.0
            sell = max(-flow, 0.0) + 20.0
            connection.execute(
                "INSERT INTO spot_depth_snapshots("
                "created_at,asset,provider,symbol,source,trade_age_seconds,"
                "trade_side_semantics,trade_buy_notional_60s,"
                "trade_sell_notional_60s,trade_net_notional_60s) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    created_at,
                    ASSET,
                    "coinbase",
                    "SOL-USD",
                    "coinbase SOL-USD",
                    2.0,
                    "aggressor",
                    buy,
                    sell,
                    flow,
                ),
            )
        connection.execute(
            "INSERT INTO spot_depth_snapshots("
            "created_at,asset,provider,symbol,source,trade_age_seconds,"
            "trade_side_semantics,trade_buy_notional_60s,"
            "trade_sell_notional_60s,trade_net_notional_60s) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (1_001.0, ASSET, "coinbase", "SOL-USD", "coinbase SOL-USD", 0.0,
             "aggressor", 10_000.0, 0.0, 10_000.0),
        )


def _complete_fixture(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _create_schemas(paths)
    _populate_complete(paths)
    _configure(monkeypatch, paths)
    return paths


def test_complete_evidence_is_point_in_time_and_has_expected_metrics(
    tmp_path, monkeypatch
):
    paths = _complete_fixture(tmp_path, monkeypatch)
    before_counts = {}
    for name, table in (
        ("interval", "interval_captures"),
        ("v91", "q15_v91_observations"),
        ("v95", "predictions"),
        ("spot", "spot_depth_snapshots"),
    ):
        with sqlite3.connect(paths[name]) as connection:
            before_counts[name] = connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]

    out = enrich_drift_evidence(_row())

    assert out["custom_input"] == "preserved"
    assert out["drift_evidence_status"] == "ok"
    assert out["drift_evidence_complete"] is True
    assert out["drift_interval_15m_side"] == "YES"
    assert out["drift_interval_15m_captured_at"] == 880.0
    assert out["drift_interval_15m_age_seconds"] == 120.0

    assert out["drift_v91_observation_count"] == 11
    assert out["drift_v91_directional_count"] == 9
    assert out["drift_v91_yes_count"] == 7
    assert out["drift_v91_no_count"] == 2
    assert out["drift_v91_uncertain_count"] == 2
    assert out["drift_v91_yes_fraction_all"] == pytest.approx(7 / 11)
    assert out["drift_v91_yes_fraction_directional"] == pytest.approx(7 / 9)
    assert out["drift_v91_expected_observation_count"] == 11
    assert out["drift_v91_coverage"] == 1.0
    assert out["drift_v91_first_observed_at"] == 880.0
    assert out["drift_v91_last_observed_at"] == 1_000.0

    assert out["drift_v95_15m_side"] == "YES"
    assert out["drift_v95_15m_flow_score"] == 0.25
    assert out["drift_v95_15m_age_seconds"] == 118.0
    assert out["drift_v95_15m_prediction_id"] == "v95-past"
    assert out["drift_btc_15m_side"] == "YES"
    assert out["drift_btc_15m_age_seconds"] == 119.0

    assert out["drift_core_breadth"] == 2
    assert out["drift_asymmetric_breadth"] == 3
    assert out["drift_core_breadth_assets"] == ["DOGE", "SOL"]
    assert out["drift_asymmetric_breadth_assets"] == ["DOGE", "HYPE", "SOL"]
    assert out["drift_breadth_missing_assets"] == []

    assert out["drift_flow_1m"] == 10.0
    assert out["drift_flow_3m"] == 15.0
    assert out["drift_flow_5m"] == 20.0
    assert out["drift_flow_13m"] == 70.0
    assert out["drift_flow_positive_bucket_fraction"] == pytest.approx(9 / 13)
    assert out["drift_flow_sign_flips"] == 6
    assert out["drift_flow_persistence"] == pytest.approx(3 / 13)
    assert out["drift_flow_coverage"] == 1.0
    assert out["drift_evidence_ages"]["spot_flow"] == 7.0

    for source in SOURCE_NAMES:
        evidence = out["drift_evidence_availability"][source]
        assert evidence["available"] is True
        assert evidence["status"] == "ok"
        assert evidence["missing_reason"] is None
        assert evidence["source_at"] is not None
        assert evidence["age_seconds"] is not None

    # Opening the ledgers read-only must not mutate rows or create helper tables.
    for name, table in (
        ("interval", "interval_captures"),
        ("v91", "q15_v91_observations"),
        ("v95", "predictions"),
        ("spot", "spot_depth_snapshots"),
    ):
        with sqlite3.connect(paths[name]) as connection:
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == before_counts[name]


def test_required_scalar_contract_and_source_contract(tmp_path, monkeypatch):
    _complete_fixture(tmp_path, monkeypatch)
    out = enrich_drift_evidence(_row())
    required = {
        "drift_v91_yes_fraction_all",
        "drift_v91_yes_fraction_directional",
        "drift_v91_observation_count",
        "drift_v95_15m_side",
        "drift_v95_15m_flow_score",
        "drift_v95_15m_age_seconds",
        "drift_btc_15m_side",
        "drift_core_breadth",
        "drift_asymmetric_breadth",
        "drift_flow_1m",
        "drift_flow_3m",
        "drift_flow_5m",
        "drift_flow_13m",
        "drift_flow_positive_bucket_fraction",
        "drift_flow_sign_flips",
        "drift_flow_persistence",
        "drift_flow_coverage",
        "drift_evidence_availability",
        "drift_evidence_ages",
    }
    assert required <= out.keys()
    assert set(out["drift_evidence_availability"]) == set(SOURCE_NAMES)
    assert set(out["drift_evidence_ages"]) == set(SOURCE_NAMES)
    for source in SOURCE_NAMES:
        assert {
            "available",
            "status",
            "missing_reason",
            "source_at",
            "age_seconds",
        } <= out["drift_evidence_availability"][source].keys()


def test_missing_databases_fail_closed_without_creating_files(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _configure(monkeypatch, paths)

    out = enrich_drift_evidence(_row())

    assert out["drift_evidence_status"] == "missing"
    assert out["drift_evidence_complete"] is False
    assert out["drift_v91_yes_fraction_directional"] is None
    assert out["drift_v95_15m_side"] is None
    assert out["drift_btc_15m_side"] is None
    assert out["drift_core_breadth"] is None
    assert out["drift_asymmetric_breadth"] is None
    assert out["drift_flow_1m"] is None
    assert out["drift_flow_coverage"] == 0.0
    for source in SOURCE_NAMES:
        evidence = out["drift_evidence_availability"][source]
        assert evidence["available"] is False
        assert evidence["status"] == "missing"
    assert not any(path.exists() for path in paths.values())


def test_future_only_rows_are_never_used_by_any_source(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    _create_schemas(paths)
    _configure(monkeypatch, paths)
    with sqlite3.connect(paths["interval"]) as connection:
        for asset in (ASSET, "BTC", *[item for item in ("DOGE", "HYPE", "XRP")]):
            _insert_interval(
                connection,
                ticker=f"KX{asset}15M-FUTURE",
                asset=asset,
                interval="15M" if asset in {ASSET, "BTC"} else "13M",
                captured_at=1_001.0,
                side="NO",
                ask=62.0,
                distance=1e-5,
                flip=10.0,
            )
    with sqlite3.connect(paths["v91"]) as connection:
        connection.execute(
            "INSERT INTO q15_v91_observations VALUES(?,?,?,?,?,?,?)",
            (f"{TICKER}|15M", ASSET, 84, 1_001.0, "YES", 0.99, "v91"),
        )
    with sqlite3.connect(paths["v95"]) as connection:
        connection.execute(
            "INSERT INTO predictions VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("future", "v95", "schema", TICKER, ASSET, "15M", 1_001.0,
             "YES", 0.99, json.dumps({"flow": 99.0})),
        )
    with sqlite3.connect(paths["spot"]) as connection:
        connection.execute(
            "INSERT INTO spot_depth_snapshots("
            "created_at,asset,provider,symbol,source,trade_age_seconds,"
            "trade_side_semantics,trade_buy_notional_60s,trade_sell_notional_60s) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (1_001.0, ASSET, "coinbase", "SOL-USD", "coinbase SOL-USD", 0.0,
             "aggressor", 999.0, 0.0),
        )

    out = enrich_drift_evidence(_row())

    assert out["drift_interval_15m_side"] is None
    assert out["drift_v91_observation_count"] == 0
    assert out["drift_v95_15m_side"] is None
    assert out["drift_btc_15m_side"] is None
    assert out["drift_core_breadth"] is None
    assert out["drift_flow_1m"] is None
    assert out["drift_evidence_complete"] is False


def test_spot_horizons_require_nonoverlap_and_normalize_old_coinbase_labels(
    tmp_path, monkeypatch
):
    paths = _paths(tmp_path)
    _create_schemas(paths)
    _configure(monkeypatch, paths)
    with sqlite3.connect(paths["spot"]) as connection:
        # Old Coinbase semantics: stored buy-sell is maker flow, so aggressor flow
        # is inverted to -70.
        connection.execute(
            "INSERT INTO spot_depth_snapshots("
            "created_at,asset,provider,symbol,source,trade_age_seconds,"
            "trade_side_semantics,trade_buy_notional_60s,trade_sell_notional_60s) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (995.0, ASSET, "coinbase", "SOL-USD", "coinbase SOL-USD", 2.0,
             None, 100.0, 30.0),
        )
        # Only 59 seconds older.  Its rolling window overlaps the first and must
        # not be used as a second minute.
        connection.execute(
            "INSERT INTO spot_depth_snapshots("
            "created_at,asset,provider,symbol,source,trade_age_seconds,"
            "trade_side_semantics,trade_buy_notional_60s,trade_sell_notional_60s) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (936.0, ASSET, "coinbase", "SOL-USD", "coinbase SOL-USD", 2.0,
             None, 500.0, 0.0),
        )
        connection.execute(
            "INSERT INTO spot_depth_snapshots("
            "created_at,asset,provider,symbol,source,trade_age_seconds,"
            "trade_side_semantics,trade_buy_notional_60s,trade_sell_notional_60s) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (1_001.0, ASSET, "coinbase", "SOL-USD", "coinbase SOL-USD", 0.0,
             "aggressor", 50_000.0, 0.0),
        )

    out = enrich_drift_evidence(_row())

    assert out["drift_flow_1m"] == -70.0
    assert out["drift_flow_3m"] is None
    assert out["drift_flow_5m"] is None
    assert out["drift_flow_13m"] is None
    assert out["drift_flow_bucket_count"] == 1
    assert out["drift_flow_coverage"] == pytest.approx(1 / 13)
    assert out["drift_flow_positive_bucket_fraction"] == 0.0
    assert out["drift_flow_sign_flips"] == 0
    assert out["drift_flow_persistence"] == 1.0
    assert out["drift_flow_trade_side_semantics"] == "aggressor"
    source = out["drift_evidence_availability"]["spot_flow"]
    assert source["available"] is False
    assert source["status"] == "partial"
    assert source["horizons_available"] == {
        "1m": True,
        "3m": False,
        "5m": False,
        "13m": False,
    }


def test_present_v95_without_flow_is_partial_and_unavailable(tmp_path, monkeypatch):
    paths = _complete_fixture(tmp_path, monkeypatch)
    with sqlite3.connect(paths["v95"]) as connection:
        connection.execute(
            "UPDATE predictions SET feature_json='{}' WHERE prediction_id='v95-past'"
        )

    out = enrich_drift_evidence(_row())

    assert out["drift_v95_15m_side"] == "YES"
    assert out["drift_v95_15m_flow_score"] is None
    source = out["drift_evidence_availability"]["v95_15m"]
    assert source["available"] is False
    assert source["status"] == "partial"
    assert source["missing_reason"] == "v95_flow_feature_missing"
    assert out["drift_evidence_complete"] is False


def test_bad_schemas_cannot_raise_into_the_caller(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    for path in paths.values():
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE wrong_schema (value TEXT)")
    _configure(monkeypatch, paths)

    out = enrich_drift_evidence(_row())

    assert out["drift_evidence_complete"] is False
    assert out["drift_evidence_status"] == "missing"
    assert out["drift_evidence_availability"]["interval_15m"]["status"] == "error"
    assert out["drift_evidence_availability"]["v95_15m"]["status"] == "error"
    assert out["drift_evidence_availability"]["btc_15m"]["status"] == "error"
    assert out["drift_evidence_availability"]["breadth_13m"]["status"] == "error"
    assert out["drift_evidence_availability"]["spot_flow"]["status"] == "error"
    # V91 is intentionally blocked before querying when its 15M lower bound is
    # unavailable, which is safer than falling back to an unbounded history.
    assert out["drift_evidence_availability"]["v91"]["status"] == "missing"


def test_old_exact_rows_are_reported_stale_not_available(tmp_path, monkeypatch):
    paths = _complete_fixture(tmp_path, monkeypatch)
    with sqlite3.connect(paths["interval"]) as connection:
        connection.execute("DELETE FROM interval_captures WHERE captured_at>1000")
    with sqlite3.connect(paths["v91"]) as connection:
        connection.execute("DELETE FROM q15_v91_observations WHERE observed_at>1000")
    with sqlite3.connect(paths["v95"]) as connection:
        connection.execute("DELETE FROM predictions WHERE created_at>1000")
    with sqlite3.connect(paths["spot"]) as connection:
        connection.execute("DELETE FROM spot_depth_snapshots WHERE created_at>1000")

    out = enrich_drift_evidence(_row(created_at=1_200.0))

    assert out["drift_interval_15m_side"] == "YES"
    assert out["drift_v95_15m_side"] == "YES"
    assert out["drift_btc_15m_side"] == "YES"
    assert out["drift_core_breadth"] == 2
    for source in ("interval_15m", "v91", "v95_15m", "btc_15m", "breadth_13m"):
        evidence = out["drift_evidence_availability"][source]
        assert evidence["available"] is False
        assert evidence["status"] == "stale"
        assert evidence["age_seconds"] is not None
    assert out["drift_evidence_complete"] is False


def test_invalid_input_never_raises_and_marks_every_source_unavailable():
    out = enrich_drift_evidence({"ticker": "NO-TIME"})
    assert out["drift_evidence_complete"] is False
    assert out["drift_evidence_status"] == "missing"
    for source in SOURCE_NAMES:
        evidence = out["drift_evidence_availability"][source]
        assert evidence["available"] is False
        assert evidence["status"] == "invalid"
        assert evidence["missing_reason"] == "missing_decision_timestamp_or_asset"
