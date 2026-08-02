from __future__ import annotations

from copy import deepcopy
import json

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v18_audit_identity as identity
from tools import q15_rti_v18_first_review_command as command
from tools import q15_rti_v18_first_review_runner as runner


ASSETS = ("BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP")


def _rows() -> list[dict]:
    return [{
        "id": row_id,
        "ticker": f"TICKER-{row_id}",
        "asset": ASSETS[(row_id - 1) % len(ASSETS)],
        "close_time": float(1_900_000_000 + (row_id - 1) * 900),
        "side": "YES" if row_id % 2 else "NO",
        "entry_ask_cents": 50.0,
        "sim_full_fill_supported": True,
        "reversal_risk_class": "low" if row_id <= 30 else "medium",
        "settlement_average_risk_class": "low",
        "path_regime_class": "trend",
    } for row_id in range(1, 61)]


def _seal(rows: list[dict]) -> dict:
    candidate = [dict(row) for row in rows if int(row["id"]) <= 30]
    candidate_ids = tuple(range(1, 31))
    control_ids = tuple(range(1, 61))
    return {
        "seal_sha256": "synthetic-seal",
        "selected_complete_close_windows": 150,
        "selected_candidate_picks": 30,
        "selected_control_picks": 60,
        "selected_candidate_row_ids_sha256": runner._canonical_sha256(
            candidate_ids
        ),
        "selected_control_row_ids_sha256": runner._canonical_sha256(control_ids),
        "selected_candidate_feature_evidence_sha256": runner._canonical_sha256(
            candidate
        ),
        "selected_control_feature_evidence_sha256": runner._canonical_sha256(rows),
    }


def _fake_report(rows: list[dict], gate: bool = True) -> dict:
    return {
        "evaluator_version": identity.EVALUATOR_VERSION,
        "audit_contract_id": identity.AUDIT_CONTRACT_ID,
        "audit_contract_sha256": identity.AUDIT_CONTRACT_SHA256,
        "protocol_id": runner.v18_identity.PROTOCOL_ID,
        "protocol_sha256": runner.v18_identity.PROTOCOL_SHA256,
        "prospective_seal_sha256": "synthetic-seal",
        "cohort": runner.COHORT,
        "control_input_rows": 60,
        "candidate_input_rows": 30,
        "selected_complete_close_windows": 150,
        "gate_met": gate,
        "control_row_ids_sha256": runner._canonical_sha256(tuple(range(1, 61))),
        "candidate_row_ids_sha256": runner._canonical_sha256(tuple(range(1, 31))),
        "outcome_labels_read": True,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }


@pytest.fixture(autouse=True)
def _accept_synthetic_seal(monkeypatch):
    monkeypatch.setattr(runner.prospective_seal, "validate_seal", lambda value: None)


def test_wrong_confirmation_never_reserves_or_calls_labels(tmp_path):
    rows = _rows()
    called = False

    def read(ids):
        nonlocal called
        called = True
        return {}

    reservation = tmp_path / "reservation.json"
    with pytest.raises(ValueError, match="explicit_one_shot_confirmation"):
        runner.run_first_review_once(
            seal=_seal(rows), control_rows=rows, candidate_ids=range(1, 31),
            reservation_path=reservation, confirmation="wrong",
            read_control_labels=read,
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
        assert tuple(ids) == tuple(range(1, 61))
        return {row_id: row_id % 2 for row_id in ids}

    monkeypatch.setattr(
        runner.evaluator, "evaluate_first_review",
        lambda labeled, candidate_ids, seal, contract: _fake_report(rows),
    )
    first = runner.run_first_review_once(
        seal=_seal(rows), control_rows=rows, candidate_ids=range(1, 31),
        reservation_path=reservation, confirmation=identity.CONFIRMATION_PHRASE,
        read_control_labels=read, timestamp="2026-08-03T00:00:00Z",
    )
    assert first["status"] == runner.PASS_STATUS
    assert first["control_labels_read_this_call"] is True
    assert first["result"]["notification_eligible"] is False
    assert first["result"]["real_trading_allowed"] is False
    assert calls == 1

    second = runner.run_first_review_once(
        seal=_seal(rows), control_rows=rows, candidate_ids=range(1, 31),
        reservation_path=reservation, confirmation=identity.CONFIRMATION_PHRASE,
        read_control_labels=read,
    )
    assert second["status"] == "ALREADY_FINALIZED_NO_REREAD"
    assert second["control_labels_read_this_call"] is False
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
        runner.run_first_review_once(
            seal=_seal(rows), control_rows=rows, candidate_ids=range(1, 31),
            reservation_path=reservation,
            confirmation=identity.CONFIRMATION_PHRASE,
            read_control_labels=fail,
        )
    assert reservation.exists()
    assert runner.result_path_for(reservation).exists() is False

    result = runner.run_first_review_once(
        seal=_seal(rows), control_rows=rows, candidate_ids=range(1, 31),
        reservation_path=reservation, confirmation=identity.CONFIRMATION_PHRASE,
        read_control_labels=fail,
    )
    assert result["status"] == "AMBIGUOUS_RESERVED_NO_REREAD"
    assert result["control_labels_read_this_call"] is False
    assert calls == 1


def test_required_authoritative_evidence_rejects_plain_mapping(tmp_path):
    rows = _rows()
    reservation = tmp_path / "reservation.json"
    with pytest.raises(ValueError, match="label_evidence_required"):
        runner.run_first_review_once(
            seal=_seal(rows), control_rows=rows, candidate_ids=range(1, 31),
            reservation_path=reservation,
            confirmation=identity.CONFIRMATION_PHRASE,
            read_control_labels=lambda ids: {
                row_id: row_id % 2 for row_id in ids
            },
            require_label_evidence=True,
        )
    assert reservation.exists()
    assert runner.result_path_for(reservation).exists() is False


def test_tampered_contract_fails_before_reservation_or_labels(tmp_path):
    rows = _rows()
    contract = deepcopy(runner.prospective_seal.load_contract())
    contract["gate"]["candidate_resolved_picks_minimum"] = 1
    called = False

    def read(ids):
        nonlocal called
        called = True
        return {}

    reservation = tmp_path / "reservation.json"
    with pytest.raises(ValueError, match="contract_identity"):
        runner.run_first_review_once(
            seal=_seal(rows), control_rows=rows, candidate_ids=range(1, 31),
            reservation_path=reservation, confirmation=identity.CONFIRMATION_PHRASE,
            read_control_labels=read, contract=contract,
        )
    assert called is False
    assert reservation.exists() is False


def test_feature_tampering_fails_before_reservation_or_labels(tmp_path):
    rows = _rows()
    artifact = _seal(rows)
    rows[0]["entry_ask_cents"] = 51.0
    called = False

    def read(ids):
        nonlocal called
        called = True
        return {}

    reservation = tmp_path / "reservation.json"
    with pytest.raises(ValueError, match="seal_binding"):
        runner.run_first_review_once(
            seal=artifact, control_rows=rows, candidate_ids=range(1, 31),
            reservation_path=reservation, confirmation=identity.CONFIRMATION_PHRASE,
            read_control_labels=read,
        )
    assert called is False
    assert reservation.exists() is False


def test_command_rejects_unbound_seal(tmp_path, monkeypatch):
    monkeypatch.undo()
    path = tmp_path / "seal.json"
    path.write_text(json.dumps({"seal_sha256": "tampered"}), encoding="utf-8")
    with pytest.raises(ValueError, match="prospective_seal_invalid"):
        command.load_seal(path)
