from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time

import pytest

from q15_upgrade.rti_spot_rest_top_book import (
    SpotRESTTopBookReservoir,
    load_protocol,
)
from q15_upgrade.strategy_bots import (
    rti_spot_rest_top_book_reservoir_identity as identity,
)
from tools import q15_rti_spot_rest_top_book_readiness as readiness


class _Response:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _coinbase_get(*_args, **_kwargs):
    return _Response({
        "sequence": 12345,
        "bids": [["100.0", "4.0", 2]],
        "asks": [["100.1", "6.0", 3]],
    })


def _wait_for_rows(db_path: Path, expected: int = 1) -> None:
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if db_path.exists():
            with sqlite3.connect(str(db_path)) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM spot_rest_top_book"
                ).fetchone()[0]
            if count >= expected:
                return
        time.sleep(0.02)
    raise AssertionError("reservoir row was not persisted")


def test_protocol_is_hash_bound_outcome_blind_and_v21_inert():
    protocol = load_protocol()
    assert protocol["protocol_id"] == identity.PROTOCOL_ID
    assert protocol["noninterference"]["used_by_v21"] is False
    assert protocol["usage"]["outcome_access_allowed"] is False
    assert protocol["usage"]["real_trading_allowed"] is False
    frozen = protocol["capture_contract"]["source_identity_by_asset"]
    assert {
        asset: (
            values["provider"], values["symbol"], values["quote_currency"]
        )
        for asset, values in frozen.items()
    } == dict(identity.SOURCE_IDENTITIES)
    requests = protocol["capture_contract"]["request_contract_by_provider"]
    assert requests["coinbase"]["query"] == {"level": "1"}
    assert requests["okx"]["query"] == {"instId": "{symbol}", "sz": "1"}
    assert protocol["capture_contract"][
        "maximum_exchange_clock_lead_seconds"
    ] == 5.0
    assert protocol["v1_terminal_exclusion"]["rows_receive_v2_credit"] is False
    report = json.loads((
        Path(__file__).resolve().parents[1]
        / "reports/q15_rti_spot_rest_top_book_v1_terminal_exclusion.json"
    ).read_text(encoding="utf-8"))
    assert report["row_count"] == 28
    assert report["outcome_labels_read"] is False
    assert report["rows_receive_v2_credit"] is False


def test_async_capture_is_idempotent_hash_bound_and_complete(tmp_path):
    db_path = tmp_path / "rest.sqlite3"
    target = identity.FIRST_ELIGIBLE_CLOSE_TIME - 780.0
    reservoir = SpotRESTTopBookReservoir(
        db_path=db_path,
        worker_count=1,
        request_get=_coinbase_get,
        clock=lambda: target + 0.1,
    )
    assert reservoir.start()
    assert reservoir.submit(
        asset="BTC",
        ticker="KXBTC15M-TEST",
        close_time=identity.FIRST_ELIGIBLE_CLOSE_TIME,
        stage="13M",
        target_at=target,
        submitted_at=target + 0.05,
    )
    _wait_for_rows(db_path)
    assert not reservoir.submit(
        asset="BTC",
        ticker="KXBTC15M-TEST",
        close_time=identity.FIRST_ELIGIBLE_CLOSE_TIME,
        stage="13M",
        target_at=target,
        submitted_at=target + 0.2,
    )
    rows = readiness.load_rows(db_path)
    report = readiness.build_readiness(rows)
    assert report["eligible_rows"] == 1
    assert report["quality_failure_counts"] == {}
    assert report["complete_all_four_stage_close_windows"] == 0
    assert reservoir.health()["duplicate_submissions"] == 1
    reservoir.stop()


def test_okx_clock_lead_is_provenance_and_never_receipt_freshness(tmp_path):
    target = identity.FIRST_ELIGIBLE_CLOSE_TIME - 780.0

    def okx_get(*_args, **_kwargs):
        return _Response({
            "code": "0",
            "data": [{
                "ts": str(int((target + 1.4) * 1000)),
                "seqId": 42,
                "bids": [["500.0", "7.0", "0", "1"]],
                "asks": [["500.2", "3.0", "0", "1"]],
            }],
        })

    db_path = tmp_path / "okx.sqlite3"
    reservoir = SpotRESTTopBookReservoir(
        db_path=db_path, worker_count=1, request_get=okx_get,
        clock=lambda: target + 0.2,
    )
    reservoir.start()
    assert reservoir.submit(
        asset="BNB", ticker="KXBNB15M-TEST",
        close_time=identity.FIRST_ELIGIBLE_CLOSE_TIME,
        stage="13M", target_at=target, submitted_at=target + 0.1,
    )
    _wait_for_rows(db_path)
    row = readiness.load_rows(db_path)[0]
    assert row["status"] == "OK"
    assert row["source_mutation_age_seconds"] == pytest.approx(-1.2)
    assert readiness.build_readiness([row])["quality_failure_counts"] == {}
    reservoir.stop()


def test_implausible_exchange_clock_lead_fails_closed(tmp_path):
    target = identity.FIRST_ELIGIBLE_CLOSE_TIME - 780.0

    def okx_get(*_args, **_kwargs):
        return _Response({
            "code": "0",
            "data": [{
                "ts": str(int((target + 8.0) * 1000)),
                "seqId": 43,
                "bids": [["500.0", "7.0", "0", "1"]],
                "asks": [["500.2", "3.0", "0", "1"]],
            }],
        })

    db_path = tmp_path / "okx-future.sqlite3"
    reservoir = SpotRESTTopBookReservoir(
        db_path=db_path, worker_count=1, request_get=okx_get,
        clock=lambda: target + 0.2,
    )
    reservoir.start()
    reservoir.submit(
        asset="BNB", ticker="KXBNB15M-FUTURE",
        close_time=identity.FIRST_ELIGIBLE_CLOSE_TIME,
        stage="13M", target_at=target, submitted_at=target + 0.1,
    )
    _wait_for_rows(db_path)
    row = readiness.load_rows(db_path)[0]
    assert row["status"] == "FAILED"
    assert row["failure_reason"] == "SOURCE_TIMESTAMP_IN_FUTURE"
    reservoir.stop()


def test_tampered_evidence_fails_closed(tmp_path):
    db_path = tmp_path / "tamper.sqlite3"
    target = identity.FIRST_ELIGIBLE_CLOSE_TIME - 780.0
    reservoir = SpotRESTTopBookReservoir(
        db_path=db_path, worker_count=1, request_get=_coinbase_get,
        clock=lambda: target + 0.1,
    )
    reservoir.start()
    reservoir.submit(
        asset="BTC", ticker="KXBTC15M-TAMPER",
        close_time=identity.FIRST_ELIGIBLE_CLOSE_TIME,
        stage="13M", target_at=target, submitted_at=target + 0.05,
    )
    _wait_for_rows(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        evidence = json.loads(conn.execute(
            "SELECT evidence_json FROM spot_rest_top_book"
        ).fetchone()[0])
        evidence["best_bid"] = 1.0
        conn.execute(
            "UPDATE spot_rest_top_book SET evidence_json=?",
            (json.dumps(evidence, sort_keys=True, separators=(",", ":")),),
        )
    report = readiness.build_readiness(readiness.load_rows(db_path))
    assert report["quality_failure_counts"][
        "EVIDENCE_HASH_OR_CANONICAL_MISMATCH"
    ] == 1
    reservoir.stop()
    restarted = SpotRESTTopBookReservoir(
        db_path=db_path, worker_count=1, request_get=_coinbase_get,
        clock=lambda: target + 0.2,
    )
    with pytest.raises(
        ValueError, match="existing_spot_rest_book_evidence_invalid"
    ):
        restarted.start()


def test_missing_database_unique_identity_constraint_fails_closed(tmp_path):
    db_path = tmp_path / "no-unique.sqlite3"
    target = identity.FIRST_ELIGIBLE_CLOSE_TIME - 780.0
    reservoir = SpotRESTTopBookReservoir(
        db_path=db_path, worker_count=1, request_get=_coinbase_get,
        clock=lambda: target + 0.1,
    )
    reservoir.start()
    reservoir.submit(
        asset="BTC", ticker="KXBTC15M-NOUNIQUE",
        close_time=identity.FIRST_ELIGIBLE_CLOSE_TIME,
        stage="13M", target_at=target, submitted_at=target + 0.05,
    )
    _wait_for_rows(db_path)
    reservoir.stop()
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript("""
            ALTER TABLE spot_rest_top_book RENAME TO old_spot_rest_top_book;
            CREATE TABLE spot_rest_top_book AS
                SELECT * FROM old_spot_rest_top_book;
            DROP TABLE old_spot_rest_top_book;
        """)
    with pytest.raises(
        ValueError, match="spot_rest_book_unique_identity_constraint_missing"
    ):
        readiness.load_rows(db_path)
    restarted = SpotRESTTopBookReservoir(
        db_path=db_path, worker_count=1, request_get=_coinbase_get,
        clock=lambda: target + 0.2,
    )
    with pytest.raises(
        ValueError, match="spot_rest_book_unique_identity_constraint_missing"
    ):
        restarted.start()


def test_duplicate_asset_stage_rows_cannot_receive_geometry_credit(tmp_path):
    db_path = tmp_path / "duplicate.sqlite3"
    target = identity.FIRST_ELIGIBLE_CLOSE_TIME - 780.0
    reservoir = SpotRESTTopBookReservoir(
        db_path=db_path, worker_count=1, request_get=_coinbase_get,
        clock=lambda: target + 0.1,
    )
    reservoir.start()
    reservoir.submit(
        asset="BTC", ticker="KXBTC15M-DUPLICATE",
        close_time=identity.FIRST_ELIGIBLE_CLOSE_TIME,
        stage="13M", target_at=target, submitted_at=target + 0.05,
    )
    _wait_for_rows(db_path)
    rows = readiness.load_rows(db_path)
    report = readiness.build_readiness([rows[0], dict(rows[0])])
    assert report["quality_failure_counts"]["DUPLICATE_EXACT_IDENTITY"] == 2
    assert report["quality_failure_counts"][
        "DUPLICATE_ASSET_STAGE_IDENTITY"
    ] == 2
    assert report["valid_all_seven_stage_windows"] == 0
    reservoir.stop()


def test_preboundary_wrong_target_and_late_submission_are_rejected(tmp_path):
    target = identity.FIRST_ELIGIBLE_CLOSE_TIME - 780.0
    reservoir = SpotRESTTopBookReservoir(
        db_path=tmp_path / "reject.sqlite3", worker_count=1,
        request_get=_coinbase_get, clock=lambda: target + 0.1,
    )
    reservoir.start()
    base = dict(
        asset="BTC", ticker="KXBTC15M-REJECT", stage="13M",
        target_at=target, submitted_at=target + 0.1,
    )
    assert not reservoir.submit(
        close_time=identity.PROSPECTIVE_AFTER_CLOSE_TIME, **base
    )
    assert not reservoir.submit(
        close_time=identity.FIRST_ELIGIBLE_CLOSE_TIME,
        **{**base, "target_at": target + 1.0},
    )
    assert not reservoir.submit(
        close_time=identity.FIRST_ELIGIBLE_CLOSE_TIME,
        **{**base, "submitted_at": target + 3.0},
    )
    assert reservoir.health()["rejected_submissions"] == 2
    reservoir.stop()


def test_http_failure_retains_status_and_redirects_are_forbidden(tmp_path):
    target = identity.FIRST_ELIGIBLE_CLOSE_TIME - 780.0
    calls = []

    def failed_get(*args, **kwargs):
        calls.append((args, kwargs))
        return _Response({}, status_code=503)

    db_path = tmp_path / "http-failure.sqlite3"
    reservoir = SpotRESTTopBookReservoir(
        db_path=db_path, worker_count=1, request_get=failed_get,
        clock=lambda: target + 0.1,
    )
    reservoir.start()
    reservoir.submit(
        asset="BTC", ticker="KXBTC15M-HTTPFAIL",
        close_time=identity.FIRST_ELIGIBLE_CLOSE_TIME,
        stage="13M", target_at=target, submitted_at=target + 0.05,
    )
    _wait_for_rows(db_path)
    row = readiness.load_rows(db_path)[0]
    assert row["status"] == "FAILED"
    assert row["http_status"] == 503
    assert "coinbase_http_503" in row["failure_reason"]
    assert calls[0][1]["allow_redirects"] is False
    reservoir.stop()


def test_wall_clock_discontinuity_fails_closed(tmp_path):
    target = identity.FIRST_ELIGIBLE_CLOSE_TIME - 780.0
    wall_values = iter([target + 0.1, target - 0.9, target - 0.9])
    monotonic_values = iter([10.0, 10.1])
    db_path = tmp_path / "clock-jump.sqlite3"
    reservoir = SpotRESTTopBookReservoir(
        db_path=db_path, worker_count=1, request_get=_coinbase_get,
        clock=lambda: next(wall_values),
        monotonic_clock=lambda: next(monotonic_values),
    )
    reservoir.start()
    reservoir.submit(
        asset="BTC", ticker="KXBTC15M-CLOCKJUMP",
        close_time=identity.FIRST_ELIGIBLE_CLOSE_TIME,
        stage="13M", target_at=target, submitted_at=target + 0.05,
    )
    _wait_for_rows(db_path)
    row = readiness.load_rows(db_path)[0]
    assert row["status"] == "FAILED"
    assert row["failure_reason"] == "LOCAL_CLOCK_DISCONTINUITY"
    assert row["response_latency_seconds"] < 0.0
    report = readiness.build_readiness([row])
    assert report["quality_failure_counts"]["TIMESTAMP_ALIGNMENT_INVALID"] == 1
    reservoir.stop()
