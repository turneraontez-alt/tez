from __future__ import annotations

import json

import pytest

from tests.test_q15_rti_v22_modeling import _population
from tools import q15_rti_v22_modeling as modeling
from tools import q15_rti_v22_pretest_command as command


def test_v22_command_loads_exact_seal_and_projects_no_outcomes(tmp_path):
    seal, _labels = _population()
    path = tmp_path / "feature-seal.json"
    path.write_text(json.dumps(seal), encoding="utf-8")
    loaded = command.load_feature_seal(path)
    rows = command.expected_database_rows(loaded)
    assert loaded["seal_sha256"] == seal["seal_sha256"]
    assert len(rows) == 180 * 7
    assert set(rows[0]) == {"id", "ticker", "asset", "close_time"}
    forbidden = {
        "official_result", "resolved_at", "correct", "label_survives",
        "result_yes", "settlement_result",
    }
    assert not any(forbidden.intersection(row) for row in rows)


def test_v22_fee_reader_verifies_every_frozen_series(tmp_path):
    seal, _labels = _population()
    expected = command.expected_database_rows(seal)
    calls = []

    def get_series(ticker):
        calls.append(ticker)
        return {
            "ticker": ticker,
            "fee_type": "quadratic",
            "fee_multiplier": 1,
            "last_updated_ts": "2026-08-01T00:00:00Z",
        }

    reader = command.KalshiFeeVerifiedSQLiteLabelReader(
        tmp_path / "unused.sqlite3", expected_rows=expected,
        get_market=lambda _ticker: None, get_series=get_series,
    )
    with pytest.raises(ValueError, match="fee_precondition_not_verified"):
        reader([expected[0]["id"]])
    evidence = reader.verify_fee_precondition()
    frozen = modeling.load_contract()["fee_schedule_verification"]
    assert calls == sorted(frozen["series_tickers"])
    assert evidence["verification_status"] == (
        "OFFICIAL_KALSHI_SERIES_FEE_METADATA_VERIFIED"
    )
    assert len(evidence["series"]) == 7


def test_v22_fee_reader_fails_closed_on_changed_series(tmp_path):
    seal, _labels = _population()

    def changed(ticker):
        return {
            "ticker": ticker,
            "fee_type": "quadratic",
            "fee_multiplier": 2 if ticker == "KXBTC15M" else 1,
        }

    reader = command.KalshiFeeVerifiedSQLiteLabelReader(
        tmp_path / "unused.sqlite3",
        expected_rows=command.expected_database_rows(seal),
        get_market=lambda _ticker: None, get_series=changed,
    )
    with pytest.raises(ValueError, match="series_fee_mismatch"):
        reader._verify_series_fees()

