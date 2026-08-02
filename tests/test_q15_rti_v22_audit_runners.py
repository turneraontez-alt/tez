from __future__ import annotations

import pytest

from q15_upgrade.strategy_bots import (
    rti_microstructure_v22_audit_identity as audit_identity,
)
from tests.test_q15_rti_v22_modeling import _population
from tools import q15_rti_v15_label_evidence as label_evidence
from tools import q15_rti_v22_modeling as modeling
from tools import q15_rti_v22_pretest_binding as pretest_binding
from tools import q15_rti_v22_pretest_runner as pretest
from tools import q15_rti_v22_untouched_test_runner as untouched


@pytest.fixture(scope="module")
def population():
    return _population()


def _settlements(seal, survival, ids):
    wanted = {int(value) for value in ids}
    output = {}
    for row in seal["rows"]:
        row_id = int(row["parent_id"])
        if row_id not in wanted:
            continue
        value = int(survival[row_id])
        output[row_id] = value if row["side"] == "YES" else 1 - value
    return output


def _verified(seal, labels):
    by_id = {int(row["parent_id"]): row for row in seal["rows"]}
    contracts = []
    for row_id, result_yes in sorted(labels.items()):
        row = by_id[int(row_id)]
        close = float(row["close_time"])
        contracts.append({
            "ticker": str(row["ticker"]),
            "row_ids": [int(row_id)],
            "result_yes": int(result_yes),
            "status": "finalized",
            "expected_close_time": close,
            "kalshi_close_time": close,
            "kalshi_settled_time": close + 1.0,
            "kalshi_expiration_time": None,
            "local_cache_status": "MATCHED",
            "local_resolved_row_count": 1,
            "local_unresolved_row_count": 0,
            "local_invalid_row_count": 0,
            "local_resolved_labels_match_api": True,
            "fetched_at": "2026-08-01T00:00:00+00:00",
        })
    ids = tuple(sorted(int(value) for value in labels))
    pairs = sorted([int(row_id), int(value)] for row_id, value in labels.items())
    fee = modeling.load_contract()["fee_schedule_verification"]
    fee_rows = [{
        "series_ticker": ticker,
        "fee_type": fee["required_fee_type"],
        "fee_multiplier": float(fee["required_fee_multiplier"]),
        "series_last_updated_ts": "2026-08-01T00:00:00Z",
        "fetched_at": "2026-08-01T00:00:00+00:00",
        "source_url": f"https://external-api.kalshi.com/trade-api/v2/series/{ticker}",
    } for ticker in sorted(fee["series_tickers"])]
    evidence = label_evidence.seal_evidence({
        "evidence_version": label_evidence.EVIDENCE_VERSION,
        "verification_status": label_evidence.PASS_STATUS,
        "source_id": label_evidence.SOURCE_ID,
        "source_base_url": "https://external-api.kalshi.com/trade-api/v2",
        "verification_started_at": "2026-08-01T00:00:00+00:00",
        "verification_completed_at": "2026-08-01T00:00:01+00:00",
        "row_count": len(ids),
        "unique_contracts": len(contracts),
        "requested_row_ids_sha256": label_evidence.canonical_sha256(ids),
        "labels_sha256": label_evidence.canonical_sha256(pairs),
        "requested_contracts_sha256": label_evidence.canonical_sha256(
            tuple(sorted(item["ticker"] for item in contracts))
        ),
        "contracts": contracts,
        "fee_schedule_verification": {
            "verification_status": (
                "OFFICIAL_KALSHI_SERIES_FEE_METADATA_VERIFIED"
            ),
            "verified_at": "2026-08-01T00:00:00+00:00",
            "fee_schedule_version": fee["fee_schedule_version"],
            "execution_cost_model_version": fee["execution_cost_model_version"],
            "general_taker_fee_rate": float(fee["general_taker_fee_rate"]),
            "series": fee_rows,
        },
    })
    return label_evidence.VerifiedLabelMapping(labels, evidence)


def _fake_pretest(seal, survival, *, passed=True):
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


def _fake_untouched(seal, survival, bundle, *, passed=False):
    assert bundle["feature_seal_sha256"] == seal["seal_sha256"]
    assert set(int(value) for value in survival) == {
        int(row["parent_id"]) for row in seal["rows"]
        if row["partition"] == modeling.TEST_PARTITION
    }
    return {
        "evaluator_contract_sha256": audit_identity.EVALUATOR_CONTRACT_SHA256,
        "feature_seal_sha256": seal["seal_sha256"],
        "untouched_test_label_rows": 175,
        "untouched_test_labels_read": True,
        "untouched_test_scoring_performed": True,
        "historical_gate_met": passed,
    }


def test_v22_runner_binding_matches_frozen_outcome_blind_binding(population):
    seal, _labels = population
    expected = pretest._expected_binding(seal, label_evidence_required=True)
    frozen = pretest_binding.expected_binding(seal)
    for key in (
        "pretest_runner_version", "evaluator_contract_sha256",
        "protocol_sha256", "feature_seal_sha256", "feature_names_sha256",
        "pretest_label_rows", "untouched_test_rows",
        "pretest_label_row_ids_sha256", "untouched_test_row_ids_sha256",
        "pretest_feature_identity_sha256", "untouched_test_feature_identity_sha256",
    ):
        assert expected[key] == frozen[key]


def test_v22_runners_reject_duplicate_integer_label_aliases():
    raw = {1: 1, "1": 1}
    with pytest.raises(ValueError, match="settlement_labels_invalid"):
        pretest._normalize_settlement_labels(raw, [1])
    with pytest.raises(ValueError, match="settlement_labels_invalid"):
        untouched._normalize_settlement_labels(raw, [1])


def test_v22_runner_rejects_nonfrozen_kalshi_evidence_origin(population):
    seal, survival = population
    row_id = next(iter(modeling.required_pretest_label_ids(seal)))
    result_yes = _settlements(seal, survival, [row_id])
    verified = _verified(seal, result_yes)
    wrong_base = dict(verified.audit_evidence)
    wrong_base["source_base_url"] = "https://example.invalid/trade-api/v2"
    with pytest.raises(ValueError, match="fee_evidence_invalid"):
        pretest._validate_fee_evidence(wrong_base)

    wrong_url = dict(verified.audit_evidence)
    wrong_url["fee_schedule_verification"] = dict(
        wrong_url["fee_schedule_verification"]
    )
    wrong_url["fee_schedule_verification"]["series"] = [
        dict(row) for row in wrong_url["fee_schedule_verification"]["series"]
    ]
    wrong_url["fee_schedule_verification"]["series"][0]["source_url"] = (
        "https://example.invalid/series/" + wrong_url[
            "fee_schedule_verification"
        ]["series"][0]["series_ticker"]
    )
    with pytest.raises(ValueError, match="fee_evidence_invalid"):
        pretest._validate_fee_evidence(wrong_url)


def test_v22_pretest_requires_evidence_and_confirmation_before_reservation(
    tmp_path, population,
):
    seal, _labels = population
    reservation = tmp_path / "pretest.json"
    called = False

    def callback(_ids):
        nonlocal called
        called = True
        return {}

    with pytest.raises(ValueError, match="authoritative_label_evidence_required"):
        pretest.run_pretest_once(
            seal=seal, reservation_path=reservation,
            confirmation=audit_identity.PRETEST_CONFIRMATION,
            read_settlement_yes_labels=callback, require_label_evidence=False,
        )
    with pytest.raises(ValueError, match="explicit_one_shot_confirmation"):
        pretest.run_pretest_once(
            seal=seal, reservation_path=reservation, confirmation="WRONG",
            read_settlement_yes_labels=callback, require_label_evidence=True,
        )
    assert called is False
    assert reservation.exists() is False


def test_v22_pretest_reads_only_bound_ids_and_replay_never_rereads(
    tmp_path, population, monkeypatch,
):
    seal, survival = population
    ids = modeling.required_pretest_label_ids(seal)
    settlement = _settlements(seal, survival, ids)
    observed = None
    reservation = tmp_path / "pretest.json"
    monkeypatch.setattr(
        modeling, "evaluate_pretest",
        lambda payload, labels: _fake_pretest(payload, labels, passed=True),
    )

    def callback(requested):
        nonlocal observed
        observed = tuple(int(value) for value in requested)
        return _verified(seal, settlement)

    first = pretest.run_pretest_once(
        seal=seal, reservation_path=reservation,
        confirmation=audit_identity.PRETEST_CONFIRMATION,
        read_settlement_yes_labels=callback, require_label_evidence=True,
    )
    assert first["status"] == pretest.PASS_STATUS
    assert set(observed) == ids
    assert first["result"]["audit_model_bundle_created"] is True
    replay = pretest.run_pretest_once(
        seal=seal, reservation_path=reservation,
        confirmation=audit_identity.PRETEST_CONFIRMATION,
        read_settlement_yes_labels=lambda _ids: pytest.fail("labels reread"),
        require_label_evidence=True,
    )
    assert replay["status"] == "ALREADY_FINALIZED_NO_REREAD"


def test_v22_crash_after_pretest_reservation_never_rereads(
    tmp_path, population,
):
    seal, _labels = population
    reservation = tmp_path / "pretest.json"
    with pytest.raises(RuntimeError, match="interruption"):
        pretest.run_pretest_once(
            seal=seal, reservation_path=reservation,
            confirmation=audit_identity.PRETEST_CONFIRMATION,
            read_settlement_yes_labels=lambda _ids: (_ for _ in ()).throw(
                RuntimeError("interruption")
            ),
            require_label_evidence=True,
        )
    replay = pretest.run_pretest_once(
        seal=seal, reservation_path=reservation,
        confirmation=audit_identity.PRETEST_CONFIRMATION,
        read_settlement_yes_labels=lambda _ids: pytest.fail("labels reread"),
        require_label_evidence=True,
    )
    assert replay["status"] == "AMBIGUOUS_RESERVED_NO_REREAD"


def test_v22_untouched_reads_exact_test_once_without_refit(
    tmp_path, population, monkeypatch,
):
    seal, survival = population
    pretest_ids = modeling.required_pretest_label_ids(seal)
    pretest_settlement = _settlements(seal, survival, pretest_ids)
    pretest_path = tmp_path / "pretest.json"
    monkeypatch.setattr(
        modeling, "evaluate_pretest",
        lambda payload, labels: _fake_pretest(payload, labels, passed=True),
    )
    pretest.run_pretest_once(
        seal=seal, reservation_path=pretest_path,
        confirmation=audit_identity.PRETEST_CONFIRMATION,
        read_settlement_yes_labels=lambda _ids: _verified(
            seal, pretest_settlement
        ),
        require_label_evidence=True,
    )
    test_ids = {
        int(row["parent_id"]) for row in seal["rows"]
        if row["partition"] == modeling.TEST_PARTITION
    }
    test_settlement = _settlements(seal, survival, test_ids)
    observed = None
    monkeypatch.setattr(
        modeling, "evaluate_untouched_test",
        lambda payload, labels, bundle: _fake_untouched(
            payload, labels, bundle, passed=False
        ),
    )

    def callback(requested):
        nonlocal observed
        observed = {int(value) for value in requested}
        return _verified(seal, test_settlement)

    reservation = tmp_path / "untouched.json"
    first = untouched.run_untouched_test_once(
        seal=seal, pretest_reservation_path=pretest_path,
        reservation_path=reservation,
        confirmation=audit_identity.UNTOUCHED_TEST_CONFIRMATION,
        read_settlement_yes_labels=callback, require_label_evidence=True,
    )
    assert first["status"] == untouched.REJECT_STATUS
    assert observed == test_ids
    assert first["result"]["model_refit_performed"] is False
    assert first["result"]["recalibration_performed"] is False
    assert first["result"]["margin_selection_performed"] is False
    assert first["result"]["paper_artifact_created"] is False
    replay = untouched.run_untouched_test_once(
        seal=seal, pretest_reservation_path=pretest_path,
        reservation_path=reservation,
        confirmation=audit_identity.UNTOUCHED_TEST_CONFIRMATION,
        read_settlement_yes_labels=lambda _ids: pytest.fail("labels reread"),
        require_label_evidence=True,
    )
    assert replay["status"] == "ALREADY_FINALIZED_NO_REREAD"
