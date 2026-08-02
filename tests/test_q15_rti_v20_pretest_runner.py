from __future__ import annotations

import json
from pathlib import Path

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v20_audit_identity as audit_identity
from tools import q15_rti_v20_modeling as modeling
from tools import q15_rti_v20_pretest_runner as runner
from test_q15_rti_v20_modeling import _pretest_labels, _sealed_population


@pytest.fixture(scope="module")
def population():
    return _sealed_population()


def _fake_modeled(seal, survival, *, passed=True):
    ids = tuple(sorted(int(value) for value in survival))
    report = {
        "modeling_version": audit_identity.MODELING_VERSION,
        "evaluator_contract_id": audit_identity.EVALUATOR_CONTRACT_ID,
        "evaluator_contract_sha256": audit_identity.EVALUATOR_CONTRACT_SHA256,
        "feature_seal_sha256": seal["seal_sha256"],
        "train_calibration_label_rows": len(ids),
        "train_calibration_label_ids_sha256": modeling._canonical_sha256(ids),
        "pretest_gate_met": passed,
        "outcome_labels_read": True,
        "untouched_test_labels_read": False,
    }
    return {
        "report": report,
        "artifacts": {
            "NON_BTC_TRANSFER": {"fake": "non_btc"},
            "BTC": {"fake": "btc"},
        } if passed else {
            "NON_BTC_TRANSFER": None,
            "BTC": None,
        },
    }


def _settlement_labels(payload, labels):
    # The synthetic helper's label is survival, so convert it back to the
    # authoritative YES-result representation expected by the runner.
    survival = _pretest_labels(payload, labels)
    output = {}
    for row in payload["rows"]:
        row_id = int(row["parent_id"])
        if row_id not in survival:
            continue
        output[row_id] = (
            survival[row_id]
            if row["side"] == "YES"
            else 1 - survival[row_id]
        )
    return output


def test_confirmation_is_required_before_reservation_or_callback(
    tmp_path, population,
):
    payload, labels = population
    reservation = tmp_path / "reservation.json"
    called = False

    def callback(_ids):
        nonlocal called
        called = True
        return _settlement_labels(payload, labels)

    with pytest.raises(ValueError, match="explicit_one_shot_confirmation"):
        runner.run_pretest_once(
            seal=payload,
            reservation_path=reservation,
            confirmation="WRONG",
            read_settlement_yes_labels=callback,
            require_label_evidence=False,
        )
    assert called is False
    assert reservation.exists() is False


def test_crash_after_reservation_is_permanently_ambiguous_and_never_rereads(
    tmp_path, population,
):
    payload, _labels = population
    reservation = tmp_path / "reservation.json"
    calls = 0

    def failing_callback(_ids):
        nonlocal calls
        calls += 1
        raise RuntimeError("simulated source interruption")

    with pytest.raises(RuntimeError, match="source interruption"):
        runner.run_pretest_once(
            seal=payload,
            reservation_path=reservation,
            confirmation=audit_identity.PRETEST_CONFIRMATION,
            read_settlement_yes_labels=failing_callback,
            require_label_evidence=False,
            timestamp="2026-08-01T16:00:00Z",
        )
    assert calls == 1
    assert reservation.exists()

    result = runner.run_pretest_once(
        seal=payload,
        reservation_path=reservation,
        confirmation=audit_identity.PRETEST_CONFIRMATION,
        read_settlement_yes_labels=lambda _ids: pytest.fail("labels reread"),
        require_label_evidence=False,
    )
    assert result["status"] == "AMBIGUOUS_RESERVED_NO_REREAD"
    assert result["train_calibration_labels_read_this_call"] is False
    assert calls == 1


def test_passed_pretest_writes_bound_model_once_and_replay_never_rereads(
    tmp_path, population, monkeypatch,
):
    payload, labels = population
    reservation = tmp_path / "reservation.json"
    calls = 0
    monkeypatch.setattr(
        modeling,
        "evaluate_pretest",
        lambda seal, survival: _fake_modeled(seal, survival, passed=True),
    )

    def callback(ids):
        nonlocal calls
        calls += 1
        assert len(ids) == 840
        return _settlement_labels(payload, labels)

    first = runner.run_pretest_once(
        seal=payload,
        reservation_path=reservation,
        confirmation=audit_identity.PRETEST_CONFIRMATION,
        read_settlement_yes_labels=callback,
        require_label_evidence=False,
        timestamp="2026-08-01T16:00:00Z",
    )
    assert first["status"] == runner.PASS_STATUS
    assert first["result"]["untouched_test_labels_read"] is False
    assert first["result"]["audit_model_bundle_created"] is True
    assert runner.artifact_path_for(reservation).exists()
    assert calls == 1

    second = runner.run_pretest_once(
        seal=payload,
        reservation_path=reservation,
        confirmation=audit_identity.PRETEST_CONFIRMATION,
        read_settlement_yes_labels=lambda _ids: pytest.fail("labels reread"),
        require_label_evidence=False,
    )
    assert second["status"] == "ALREADY_FINALIZED_NO_REREAD"
    assert second["train_calibration_labels_read_this_call"] is False
    assert calls == 1


def test_artifact_tamper_fails_closed(tmp_path, population, monkeypatch):
    payload, labels = population
    reservation = tmp_path / "reservation.json"
    monkeypatch.setattr(
        modeling,
        "evaluate_pretest",
        lambda seal, survival: _fake_modeled(seal, survival, passed=True),
    )
    runner.run_pretest_once(
        seal=payload,
        reservation_path=reservation,
        confirmation=audit_identity.PRETEST_CONFIRMATION,
        read_settlement_yes_labels=lambda _ids: _settlement_labels(
            payload, labels
        ),
        require_label_evidence=False,
    )
    artifact = runner.artifact_path_for(reservation)
    with artifact.open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(ValueError, match="artifact_file_invalid"):
        runner.run_pretest_once(
            seal=payload,
            reservation_path=reservation,
            confirmation=audit_identity.PRETEST_CONFIRMATION,
            read_settlement_yes_labels=lambda _ids: pytest.fail("labels reread"),
            require_label_evidence=False,
        )


def test_survival_label_tamper_is_recomputed_from_settlement_and_sealed_side(
    tmp_path, population, monkeypatch,
):
    payload, labels = population
    reservation = tmp_path / "reservation.json"
    monkeypatch.setattr(
        modeling,
        "evaluate_pretest",
        lambda seal, survival: _fake_modeled(seal, survival, passed=True),
    )
    runner.run_pretest_once(
        seal=payload,
        reservation_path=reservation,
        confirmation=audit_identity.PRETEST_CONFIRMATION,
        read_settlement_yes_labels=lambda _ids: _settlement_labels(
            payload, labels
        ),
        require_label_evidence=False,
    )
    result_path = runner.result_path_for(reservation)
    stored = json.loads(result_path.read_text(encoding="utf-8"))
    stored["survival_label_rows"][0]["label_survives"] ^= 1
    pairs = sorted(
        [int(row["parent_id"]), int(row["label_survives"])]
        for row in stored["survival_label_rows"]
    )
    stored["survival_labels_sha256"] = runner._canonical_sha256(pairs)
    stored.pop("state_sha256")
    stored["state_sha256"] = runner._canonical_sha256(stored)
    result_path.write_text(json.dumps(stored), encoding="utf-8")
    with pytest.raises(ValueError, match="result_invalid"):
        runner.run_pretest_once(
            seal=payload,
            reservation_path=reservation,
            confirmation=audit_identity.PRETEST_CONFIRMATION,
            read_settlement_yes_labels=lambda _ids: pytest.fail("labels reread"),
            require_label_evidence=False,
        )


def test_failed_pretest_never_creates_model_bundle(tmp_path, population, monkeypatch):
    payload, labels = population
    reservation = tmp_path / "reservation.json"
    monkeypatch.setattr(
        modeling,
        "evaluate_pretest",
        lambda seal, survival: _fake_modeled(seal, survival, passed=False),
    )
    result = runner.run_pretest_once(
        seal=payload,
        reservation_path=reservation,
        confirmation=audit_identity.PRETEST_CONFIRMATION,
        read_settlement_yes_labels=lambda _ids: _settlement_labels(
            payload, labels
        ),
        require_label_evidence=False,
    )
    assert result["status"] == runner.REJECT_STATUS
    assert result["result"]["manual_untouched_test_eligible"] is False
    assert result["result"]["audit_model_bundle_created"] is False
    assert runner.artifact_path_for(reservation).exists() is False


def test_settlement_yes_is_converted_to_original_side_survival(population):
    payload, labels = population
    rows = [
        row for row in payload["rows"]
        if row["partition"] in {"TRAIN", "CALIBRATION"}
    ]
    settlements = _settlement_labels(payload, labels)
    survival = runner._survival_labels(rows, settlements)
    assert survival == _pretest_labels(payload, labels)
