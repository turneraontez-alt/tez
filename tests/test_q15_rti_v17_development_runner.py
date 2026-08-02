from __future__ import annotations

import json
from copy import deepcopy

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v17_audit_identity as identity
from tools import q15_rti_v17_development_command as command
from tools import q15_rti_v17_development_runner as runner


ASSETS = ("BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP")


def _rows() -> list[dict]:
    rows = []
    row_id = 1
    for window in range(240):
        for asset in ASSETS:
            rows.append({
                "id": row_id,
                "asset": asset,
                "close_time": float(1_800_000_000 + window * 900),
            })
            row_id += 1
    return rows


def _seal(rows: list[dict]) -> dict:
    return {
        "seal_sha256": identity.DEVELOPMENT_SEAL_SHA256,
        "selected_feature_evidence_sha256": "evidence",
        "selected_row_ids_sha256": runner._row_ids_sha256(rows),
        "selected_close_times_sha256": runner._close_times_sha256(rows),
    }


def _fake_report(rows: list[dict], gate: bool = True) -> dict:
    return {
        "evaluator_version": identity.EVALUATOR_VERSION,
        "evaluator_contract_id": identity.EVALUATOR_CONTRACT_ID,
        "evaluator_contract_sha256": identity.EVALUATOR_CONTRACT_SHA256,
        "protocol_id": runner.v17_identity.PROTOCOL_ID,
        "protocol_sha256": runner.v17_identity.PROTOCOL_SHA256,
        "development_seal_sha256": identity.DEVELOPMENT_SEAL_SHA256,
        "cohort": "NON_BTC_TRANSFER",
        "input_rows": 1440,
        "input_close_windows": 240,
        "walk_forward_validation_rows": 720,
        "walk_forward_validation_close_windows": 120,
        "candidate_market_v16_v15_v14_identical_rows": True,
        "same_close_assets_share_every_fold": True,
        "accuracy_is_report_only": True,
        "gate_met": gate,
        "input_row_ids_sha256": runner._row_ids_sha256(rows),
        "input_close_times_sha256": runner._close_times_sha256(rows),
        "future_calibration_rows_used": 0,
        "future_test_rows_used": 0,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }


def test_wrong_confirmation_never_reserves_or_calls_labels(tmp_path):
    rows = _rows()
    called = False

    def read(ids):
        nonlocal called
        called = True
        return {}

    reservation = tmp_path / "reservation.json"
    with pytest.raises(ValueError, match="explicit_one_shot_confirmation"):
        runner.run_development_once(
            seal=_seal(rows), development_rows=rows,
            reservation_path=reservation, confirmation="wrong",
            read_development_labels=read,
        )
    assert called is False
    assert reservation.exists() is False


def test_reservation_precedes_callback_and_result_is_idempotent(tmp_path, monkeypatch):
    rows = _rows()
    reservation = tmp_path / "reservation.json"
    calls = 0

    def read(ids):
        nonlocal calls
        calls += 1
        assert reservation.exists()
        assert len(ids) == 1440
        return {row_id: row_id % 2 for row_id in ids}

    monkeypatch.setattr(
        runner.evaluator, "evaluate_development",
        lambda labeled, contract, protocol: _fake_report(rows),
    )
    first = runner.run_development_once(
        seal=_seal(rows), development_rows=rows,
        reservation_path=reservation,
        confirmation=identity.CONFIRMATION_PHRASE,
        read_development_labels=read, timestamp="2026-08-01T08:00:00Z",
    )
    assert first["status"] == runner.PASS_STATUS
    assert first["development_labels_read_this_call"] is True
    assert calls == 1

    second = runner.run_development_once(
        seal=_seal(rows), development_rows=rows,
        reservation_path=reservation,
        confirmation=identity.CONFIRMATION_PHRASE,
        read_development_labels=read,
    )
    assert second["status"] == "ALREADY_FINALIZED_NO_REREAD"
    assert second["development_labels_read_this_call"] is False
    assert calls == 1


def test_callback_failure_leaves_permanent_ambiguous_reservation(tmp_path):
    rows = _rows()
    reservation = tmp_path / "reservation.json"
    calls = 0

    def fail(ids):
        nonlocal calls
        calls += 1
        raise RuntimeError("network failed")

    with pytest.raises(RuntimeError, match="network failed"):
        runner.run_development_once(
            seal=_seal(rows), development_rows=rows,
            reservation_path=reservation,
            confirmation=identity.CONFIRMATION_PHRASE,
            read_development_labels=fail,
        )
    assert reservation.exists()
    assert runner.result_path_for(reservation).exists() is False

    result = runner.run_development_once(
        seal=_seal(rows), development_rows=rows,
        reservation_path=reservation,
        confirmation=identity.CONFIRMATION_PHRASE,
        read_development_labels=fail,
    )
    assert result["status"] == "AMBIGUOUS_RESERVED_NO_REREAD"
    assert result["development_labels_read_this_call"] is False
    assert calls == 1


def test_required_authoritative_evidence_rejects_plain_mapping(tmp_path):
    rows = _rows()
    reservation = tmp_path / "reservation.json"
    with pytest.raises(ValueError, match="label_evidence_required"):
        runner.run_development_once(
            seal=_seal(rows), development_rows=rows,
            reservation_path=reservation,
            confirmation=identity.CONFIRMATION_PHRASE,
            read_development_labels=lambda ids: {
                row_id: row_id % 2 for row_id in ids
            },
            require_label_evidence=True,
        )
    assert reservation.exists()
    assert runner.result_path_for(reservation).exists() is False


def test_command_loads_only_the_bound_development_seal(tmp_path):
    real = command.load_seal(command.ROOT / identity.DEVELOPMENT_SEAL_RELATIVE_PATH)
    tampered = dict(real)
    tampered["seal_sha256"] = "0" * 64
    path = tmp_path / "seal.json"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="development_seal_invalid"):
        command.load_seal(path)


def test_tampered_contract_fails_before_reservation_or_labels(tmp_path):
    rows = _rows()
    contract = deepcopy(runner.evaluator.load_contract())
    contract["trust_selection"]["fallback_factor"] = 1.0
    called = False

    def read(ids):
        nonlocal called
        called = True
        return {}

    reservation = tmp_path / "reservation.json"
    with pytest.raises(ValueError, match="contract_identity"):
        runner.run_development_once(
            seal=_seal(rows), development_rows=rows,
            reservation_path=reservation,
            confirmation=identity.CONFIRMATION_PHRASE,
            read_development_labels=read, contract=contract,
        )
    assert called is False
    assert reservation.exists() is False
