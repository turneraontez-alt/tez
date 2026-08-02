from __future__ import annotations

from copy import deepcopy
import hashlib

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v22_identity as identity
from q15_upgrade.strategy_bots import rti_microstructure_v22_top_book_features as features
from tests.test_q15_rti_v21_trajectory import _ticker
from tools import q15_rti_v22_feature_seal as seal
from tools import q15_rti_v22_pretest_binding as pretest_binding


ASSETS = ("BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _row(asset: str, close: float, ordinal: int) -> dict:
    parent_id = 1_000_000 + ordinal * 3
    source_hashes = {
        "parent": _digest(f"parent:{ordinal}"),
        "intermediate": _digest(f"intermediate:{ordinal}"),
        "delayed": _digest(f"delayed:{ordinal}"),
    }
    rest_hashes = {
        stage: _digest(f"rest:{ordinal}:{stage}") for stage in features.STAGES
    }
    values = [float((ordinal + index) % 17) / 10.0 for index in range(91)]
    side = "YES" if ordinal % 2 else "NO"
    core = {
        "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
        "parent_id": parent_id,
        "asset": asset,
        "ticker": _ticker(asset, close),
        "close_time": close,
        "side": side,
        "parent_source_evidence_sha256": source_hashes["parent"],
        "intermediate_source_evidence_sha256": source_hashes["intermediate"],
        "delayed_source_evidence_sha256": source_hashes["delayed"],
        "rest_evidence_sha256_by_stage": rest_hashes,
        "feature_names": list(features.FEATURE_NAMES),
        "features": values,
    }
    return {
        **core,
        "intermediate_id": parent_id + 1,
        "delayed_id": parent_id + 2,
        "execution_supported": ordinal % 5 != 0,
        "entry_ask_cents": 55.0,
        "spread_cents": 1.0,
        "depth_contracts": 20.0,
        "sim_contracts": 10.0,
        "feature_evidence_sha256": seal._sha256(core),
        "matched_frozen_v21_eligible": ordinal % 7 != 0,
        "matched_frozen_v21_source_feature_evidence_sha256": _digest(
            f"v21:{ordinal}"
        ),
        "replaced_spot_source_failures": [],
    }


def _windows(count: int = 180):
    output = []
    ordinal = 0
    for index in range(count):
        close = identity.FIRST_ELIGIBLE_COMMON_CLOSE_TIME + index * 900.0
        rows = []
        for asset in ASSETS:
            ordinal += 1
            rows.append(_row(asset, close, ordinal))
        output.append({
            "close_time": close,
            "rows": rows,
            "matched_frozen_v21_rows": sum(
                row["matched_frozen_v21_eligible"] for row in rows
            ),
            "row_level_executable_rows": sum(
                row["execution_supported"] for row in rows
            ),
        })
    return output


def test_v22_feature_seal_refuses_less_than_180_complete_windows():
    with pytest.raises(ValueError, match="insufficient_complete_windows"):
        seal.build_seal(_windows(179))


def test_v22_feature_seal_freezes_exact_chronological_partitions_without_labels():
    candidate = seal.build_seal(_windows())
    checked = seal.validate_seal(candidate)
    assert checked["valid"] is True
    assert candidate["selected_complete_close_windows"] == 180
    assert candidate["selected_feature_rows"] == 1260
    assert len(candidate["partitions"]["TRAIN"]) == 105
    assert len(candidate["partitions"]["PROBABILITY_CALIBRATION"]) == 25
    assert len(candidate["partitions"]["EXECUTION_POLICY_SELECTION"]) == 25
    assert len(candidate["partitions"]["UNTOUCHED_TEST"]) == 25
    assert candidate["outcome_labels_read"] is False
    assert candidate["model_fit_performed"] is False
    assert candidate["real_trading_allowed"] is False


def test_v22_feature_seal_rejects_feature_and_outcome_tampering():
    candidate = seal.build_seal(_windows())
    feature_tamper = deepcopy(candidate)
    feature_tamper["rows"][0]["features"][0] += 1.0
    feature_tamper["seal_sha256"] = seal._sha256(seal._core(feature_tamper))
    with pytest.raises(ValueError, match="row_hash_mismatch"):
        seal.validate_seal(feature_tamper)

    outcome_tamper = deepcopy(candidate)
    outcome_tamper["rows"][0]["label_survives"] = 1
    outcome_tamper["seal_sha256"] = seal._sha256(seal._core(outcome_tamper))
    with pytest.raises(ValueError, match="outcome_or_label_forbidden"):
        seal.validate_seal(outcome_tamper)


def test_v22_feature_seal_rejects_fully_rehashed_identity_tampering():
    candidate = seal.build_seal(_windows())
    metadata = deepcopy(candidate)
    metadata["selected_first_close_time"] += 900.0
    metadata["seal_sha256"] = seal._sha256(seal._core(metadata))
    with pytest.raises(ValueError, match="partition_chronology_invalid"):
        seal.validate_seal(metadata)

    evidence = deepcopy(candidate)
    row = evidence["rows"][0]
    row["rest_evidence_sha256_by_stage"][features.STAGES[0]] = "bad"
    core = {
        "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
        "parent_id": row["parent_id"],
        "asset": row["asset"],
        "ticker": row["ticker"],
        "close_time": row["close_time"],
        "side": row["side"],
        "parent_source_evidence_sha256": row["parent_source_evidence_sha256"],
        "intermediate_source_evidence_sha256": row[
            "intermediate_source_evidence_sha256"
        ],
        "delayed_source_evidence_sha256": row[
            "delayed_source_evidence_sha256"
        ],
        "rest_evidence_sha256_by_stage": row[
            "rest_evidence_sha256_by_stage"
        ],
        "feature_names": list(features.FEATURE_NAMES),
        "features": row["features"],
    }
    row["feature_evidence_sha256"] = seal._sha256(core)
    evidence["selected_feature_evidence_rollup_sha256"] = seal._sha256([
        [item["parent_id"], item["feature_evidence_sha256"]]
        for item in evidence["rows"]
    ])
    evidence["seal_sha256"] = seal._sha256(seal._core(evidence))
    with pytest.raises(ValueError, match="row_identity_invalid"):
        seal.validate_seal(evidence)


def test_v22_feature_seal_exclusive_write_is_idempotent_and_mismatch_fails(tmp_path):
    candidate = seal.build_seal(_windows())
    path = tmp_path / "seal.json"
    first = seal.create_or_validate_seal(candidate, path)
    second = seal.create_or_validate_seal(candidate, path)
    assert first["created"] is True
    assert second["created"] is False
    alternative = seal.build_seal(
        _windows(), excluded_windows=[{
            "close_time": identity.FIRST_ELIGIBLE_COMMON_CLOSE_TIME,
            "failure_counts": {"DIAGNOSTIC": 1},
        }],
    )
    with pytest.raises(ValueError, match="existing_candidate_mismatch"):
        seal.create_or_validate_seal(alternative, path)


def test_v22_pretest_binding_excludes_test_and_nonexecutable_policy_labels():
    candidate = seal.build_seal(_windows())
    binding = pretest_binding.expected_binding(candidate)
    checked = pretest_binding.validate_binding(binding, candidate)
    pretest_rows = pretest_binding.required_pretest_rows(candidate)
    test_rows = pretest_binding.untouched_test_rows(candidate)
    assert checked["valid"] is True
    assert binding["pretest_label_rows"] == len(pretest_rows) == 1050
    assert binding["untouched_test_rows"] == len(test_rows) == 175
    assert not {
        row["parent_id"] for row in pretest_rows
    } & {row["parent_id"] for row in test_rows}
    assert all(
        row["execution_supported"] is True
        for row in pretest_rows
        if row["partition"] == "EXECUTION_POLICY_SELECTION"
    )
    assert binding["outcome_labels_read"] is False
    assert binding["reservation_created"] is False
    assert binding["real_trading_allowed"] is False


def test_v22_pretest_binding_requires_authoritative_evidence_and_rejects_tamper():
    candidate = seal.build_seal(_windows())
    with pytest.raises(ValueError, match="authoritative_label_evidence_required"):
        pretest_binding.expected_binding(
            candidate, label_evidence_required=False,
        )
    binding = pretest_binding.expected_binding(candidate)
    binding["pretest_label_rows"] -= 1
    binding["binding_sha256"] = pretest_binding._sha256({
        key: value for key, value in binding.items() if key != "binding_sha256"
    })
    with pytest.raises(ValueError, match="binding_invalid"):
        pretest_binding.validate_binding(binding, candidate)
