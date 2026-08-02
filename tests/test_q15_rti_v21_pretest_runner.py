from __future__ import annotations

import json

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v21_audit_identity as audit_identity
from tools import q15_rti_v21_modeling as modeling
from tools import q15_rti_v21_pretest_runner as runner
from test_q15_rti_v21_modeling import _population


@pytest.fixture(scope="module")
def population():
    return _population()


def _settlement_labels(seal, survival_labels):
    required = modeling.required_pretest_label_ids(seal)
    output = {}
    for row in seal["rows"]:
        row_id = int(row["parent_id"])
        if row_id not in required:
            continue
        survival = int(survival_labels[row_id])
        output[row_id] = survival if row["side"] == "YES" else 1 - survival
    return output


def _fake_modeled(seal, survival, *, passed=True):
    ids = tuple(sorted(int(value) for value in survival))
    return {
        "report": {
            "modeling_version": audit_identity.MODELING_VERSION,
            "evaluator_contract_sha256": audit_identity.EVALUATOR_CONTRACT_SHA256,
            "feature_seal_sha256": seal["seal_sha256"],
            "train_calibration_policy_label_rows": len(ids),
            "train_calibration_policy_label_ids_sha256": (
                modeling._canonical_sha256(ids)
            ),
            "outcome_labels_read": True,
            "untouched_test_labels_read": False,
            "pretest_gate_met": passed,
        },
        "artifacts": {
            cohort: ({"cohort": cohort} if passed else None)
            for cohort in modeling.COHORTS
        },
    }


def test_v21_confirmation_required_before_reservation_or_callback(
    tmp_path, population,
):
    seal, labels = population
    reservation = tmp_path / "reservation.json"
    called = False

    def callback(_ids):
        nonlocal called
        called = True
        return _settlement_labels(seal, labels)

    with pytest.raises(ValueError, match="explicit_one_shot_confirmation"):
        runner.run_pretest_once(
            seal=seal,
            reservation_path=reservation,
            confirmation="WRONG",
            read_settlement_yes_labels=callback,
            require_label_evidence=False,
        )
    assert called is False
    assert reservation.exists() is False


def test_v21_callback_receives_only_minimal_pretest_ids(
    tmp_path, population, monkeypatch,
):
    seal, labels = population
    reservation = tmp_path / "reservation.json"
    expected_ids = modeling.required_pretest_label_ids(seal)
    included_calibration_nonfill = {
        int(row["parent_id"]) for row in seal["rows"]
        if row["partition"] == modeling.CALIBRATION_PARTITION
        and row["execution_supported"] is False
    }
    excluded_policy_nonfill = {
        int(row["parent_id"]) for row in seal["rows"]
        if row["partition"] == modeling.POLICY_PARTITION
        and row["execution_supported"] is False
    }
    test_ids = {
        int(row["parent_id"]) for row in seal["rows"]
        if row["partition"] == modeling.TEST_PARTITION
    }
    observed = None

    def callback(ids):
        nonlocal observed
        observed = set(int(value) for value in ids)
        return _settlement_labels(seal, labels)

    monkeypatch.setattr(
        modeling, "evaluate_pretest",
        lambda payload, survival: _fake_modeled(payload, survival, passed=True),
    )
    result = runner.run_pretest_once(
        seal=seal,
        reservation_path=reservation,
        confirmation=audit_identity.PRETEST_CONFIRMATION,
        read_settlement_yes_labels=callback,
        require_label_evidence=False,
    )
    assert result["status"] == runner.PASS_STATUS
    assert observed == expected_ids
    assert included_calibration_nonfill.issubset(observed)
    assert observed.isdisjoint(excluded_policy_nonfill)
    assert observed.isdisjoint(test_ids)
    assert result["reservation"]["pretest_label_rows"] == len(expected_ids)
    assert result["reservation"]["untouched_test_rows"] == 175


def test_v21_crash_after_reservation_is_ambiguous_and_never_rereads(
    tmp_path, population,
):
    seal, _labels = population
    reservation = tmp_path / "reservation.json"

    def failing(_ids):
        raise RuntimeError("simulated interruption")

    with pytest.raises(RuntimeError, match="simulated interruption"):
        runner.run_pretest_once(
            seal=seal,
            reservation_path=reservation,
            confirmation=audit_identity.PRETEST_CONFIRMATION,
            read_settlement_yes_labels=failing,
            require_label_evidence=False,
        )
    assert reservation.exists()
    replay = runner.run_pretest_once(
        seal=seal,
        reservation_path=reservation,
        confirmation=audit_identity.PRETEST_CONFIRMATION,
        read_settlement_yes_labels=lambda _ids: pytest.fail("labels reread"),
        require_label_evidence=False,
    )
    assert replay["status"] == "AMBIGUOUS_RESERVED_NO_REREAD"
    assert replay["train_calibration_policy_labels_read_this_call"] is False


def test_v21_finalized_replay_and_artifact_tamper_fail_closed(
    tmp_path, population, monkeypatch,
):
    seal, labels = population
    reservation = tmp_path / "reservation.json"
    monkeypatch.setattr(
        modeling, "evaluate_pretest",
        lambda payload, survival: _fake_modeled(payload, survival, passed=True),
    )
    first = runner.run_pretest_once(
        seal=seal,
        reservation_path=reservation,
        confirmation=audit_identity.PRETEST_CONFIRMATION,
        read_settlement_yes_labels=lambda _ids: _settlement_labels(seal, labels),
        require_label_evidence=False,
    )
    assert first["result"]["audit_model_bundle_created"] is True
    artifact = runner.artifact_path_for(reservation)
    assert artifact.exists()
    replay = runner.run_pretest_once(
        seal=seal,
        reservation_path=reservation,
        confirmation=audit_identity.PRETEST_CONFIRMATION,
        read_settlement_yes_labels=lambda _ids: pytest.fail("labels reread"),
        require_label_evidence=False,
    )
    assert replay["status"] == "ALREADY_FINALIZED_NO_REREAD"

    with artifact.open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(ValueError, match="artifact_file_invalid"):
        runner.run_pretest_once(
            seal=seal,
            reservation_path=reservation,
            confirmation=audit_identity.PRETEST_CONFIRMATION,
            read_settlement_yes_labels=lambda _ids: pytest.fail("labels reread"),
            require_label_evidence=False,
        )


def test_v21_survival_tamper_recomputed_from_settlement_and_side(
    tmp_path, population, monkeypatch,
):
    seal, labels = population
    reservation = tmp_path / "reservation.json"
    monkeypatch.setattr(
        modeling, "evaluate_pretest",
        lambda payload, survival: _fake_modeled(payload, survival, passed=False),
    )
    runner.run_pretest_once(
        seal=seal,
        reservation_path=reservation,
        confirmation=audit_identity.PRETEST_CONFIRMATION,
        read_settlement_yes_labels=lambda _ids: _settlement_labels(seal, labels),
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
    stored = runner._sealed(stored)
    result_path.write_text(json.dumps(stored), encoding="utf-8")
    with pytest.raises(ValueError, match="result_invalid"):
        runner.run_pretest_once(
            seal=seal,
            reservation_path=reservation,
            confirmation=audit_identity.PRETEST_CONFIRMATION,
            read_settlement_yes_labels=lambda _ids: pytest.fail("labels reread"),
            require_label_evidence=False,
        )
