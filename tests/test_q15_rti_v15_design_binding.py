from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v14 as v14
from q15_upgrade.strategy_bots import rti_microstructure_v15 as v15
from q15_upgrade.strategy_bots import rti_microstructure_v15_identity as identity
from q15_upgrade.strategy_bots.rti_independent_path import (
    DERIVED_FEATURE_KEYS,
    SCHEMA_VERSION as PATH_SCHEMA_VERSION,
    TIME_BASIS as PATH_TIME_BASIS,
)
from q15_upgrade.strategy_bots.rti_independent_path_identity import (
    DESIGN_ID as PATH_DESIGN_ID,
    DESIGN_SHA256 as PATH_DESIGN_SHA256,
)
from tools import q15_rti_v15_design_binding as binding
from tools.q15_rti_microstructure_preregister import design_fingerprint


ROOT = Path(__file__).resolve().parents[1]


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _base_vector() -> dict:
    return {
        "available": True,
        "features": [float(index) for index in range(20)],
        "feature_names": list(v14.FEATURE_NAMES),
        "market_yes_probability": 0.61,
        "design_id": v14.DESIGN_ID,
        "design_sha256": v14.DESIGN_SHA256,
    }


def _row() -> dict:
    close = v15.FIRST_ELIGIBLE_CLOSE_TIME
    captured = close - 780.0 + 0.25
    return {
        "asset": "BTC",
        "close_time": close,
        "source_captured_at": captured,
        "rti_independent_path_evidence_cutoff_at": captured,
        "rti_independent_path_design_id": PATH_DESIGN_ID,
        "rti_independent_path_design_sha256": PATH_DESIGN_SHA256,
        "rti_independent_path_schema_version": PATH_SCHEMA_VERSION,
        "rti_independent_path_time_basis": PATH_TIME_BASIS,
        "rti_independent_path_status": "ok",
        "rti_independent_path_available_count": 2,
        "rti_independent_path_evidence_sha256": "a" * 64,
        **{
            name: float(index + 1) / 10.0
            for index, name in enumerate(DERIVED_FEATURE_KEYS)
        },
    }


def _patch_valid_sources(monkeypatch):
    monkeypatch.setattr(v15.v14, "feature_vector", lambda row: _base_vector())
    monkeypatch.setattr(
        v15,
        "validate_persisted_independent_path",
        lambda row: {
            "valid": True,
            "errors": [],
            "prospective_credit_eligible": True,
        },
    )


def test_v15_binding_is_exact_and_outcome_blind():
    result = binding.validate_files()
    design = _read(binding.DEFAULT_DESIGN)
    assert design_fingerprint(design) == identity.DESIGN_SHA256
    assert result["status"] == (
        "V15_EXECUTABLE_FEATURE_DESIGN_BOUND_AND_VERIFIED"
    )
    assert result["feature_count"] == 25
    assert result["base_feature_count"] == 20
    assert result["path_feature_count"] == 5
    assert result["outcome_labels_read"] is False
    assert result["model_fit_performed"] is False
    assert result["probability_scoring_performed"] is False
    assert result["model_artifact_created"] is False
    assert result["notification_eligible"] is False
    assert result["real_trading_allowed"] is False


def test_v15_first_twenty_are_exact_v14_and_last_five_are_frozen_path(
    monkeypatch,
):
    _patch_valid_sources(monkeypatch)
    row = _row()
    result = v15.feature_vector(row)
    assert result["available"] is True
    assert tuple(result["feature_names"][:20]) == tuple(v14.FEATURE_NAMES)
    assert tuple(result["feature_names"][20:]) == tuple(DERIVED_FEATURE_KEYS)
    assert result["features"][:20] == _base_vector()["features"]
    assert result["features"][20:] == [
        row[name] for name in DERIVED_FEATURE_KEYS
    ]
    assert result["base_features_identical_to_v14"] is True
    assert result["path_features_recomputed_from_canonical_evidence"] is True
    assert result["missing_path_imputation_performed"] is False
    assert result["outcome_labels_read"] is False
    assert result["model_fit_performed"] is False
    assert result["probability_scoring_performed"] is False


def test_v15_outcome_fields_cannot_change_features(monkeypatch):
    _patch_valid_sources(monkeypatch)
    yes = {**_row(), "official_result": "YES", "label_yes": 1}
    no = {**_row(), "official_result": "NO", "label_yes": 0}
    assert v15.feature_vector(yes)["features"] == (
        v15.feature_vector(no)["features"]
    )


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (
            lambda row: row.pop(DERIVED_FEATURE_KEYS[0]),
            "path_feature_missing_or_nonfinite",
        ),
        (
            lambda row: row.__setitem__(
                "rti_independent_path_available_count", 2.5,
            ),
            "path_source_identity_mismatch",
        ),
        (
            lambda row: row.__setitem__(
                "rti_independent_path_evidence_cutoff_at",
                row["source_captured_at"] + 0.01,
            ),
            "path_cutoff_not_source_capture",
        ),
        (
            lambda row: row.__setitem__(
                "source_captured_at", row["close_time"] - 780.01,
            ),
            "path_cutoff_not_source_capture",
        ),
        (
            lambda row: (
                row.__setitem__(
                    "source_captured_at", row["close_time"] - 777.9,
                ),
                row.__setitem__(
                    "rti_independent_path_evidence_cutoff_at",
                    row["close_time"] - 777.9,
                ),
            ),
            "path_not_exact_13m",
        ),
    ],
)
def test_v15_missing_or_misaligned_path_fails_closed(
    monkeypatch, mutation, error,
):
    _patch_valid_sources(monkeypatch)
    row = _row()
    mutation(row)
    result = v15.feature_vector(row)
    assert result["available"] is False
    assert error in result["error"]


def test_v15_recomputed_path_integrity_failure_fails_closed(monkeypatch):
    monkeypatch.setattr(v15.v14, "feature_vector", lambda row: _base_vector())
    monkeypatch.setattr(
        v15,
        "validate_persisted_independent_path",
        lambda row: {
            "valid": False,
            "errors": ["evidence_sha256_mismatch"],
            "prospective_credit_eligible": True,
        },
    )
    result = v15.feature_vector(_row())
    assert result["available"] is False
    assert result["error"] == (
        "path_evidence_invalid:evidence_sha256_mismatch"
    )


def test_v15_boundary_is_frozen_before_first_source_row(monkeypatch):
    called = False

    def _base(row):
        nonlocal called
        called = True
        return _base_vector()

    monkeypatch.setattr(v15.v14, "feature_vector", _base)
    result = v15.feature_vector({
        "close_time": v15.PROSPECTIVE_AFTER_CLOSE_TIME,
    })
    assert result == {
        "available": False,
        "error": "pre_v15_source_boundary",
    }
    assert called is False


def test_rehashed_design_feature_tamper_fails_semantic_validation(monkeypatch):
    design = _read(binding.DEFAULT_DESIGN)
    v14_design = _read(binding.DEFAULT_V14_DESIGN)
    charter = _read(binding.DEFAULT_CHARTER)
    protocol = _read(binding.DEFAULT_PROTOCOL)
    artifact = _read(binding.DEFAULT_GEOMETRY_ARTIFACT)
    tampered = copy.deepcopy(design)
    tampered["feature_names"][-1], tampered["feature_names"][-2] = (
        tampered["feature_names"][-2],
        tampered["feature_names"][-1],
    )
    monkeypatch.setattr(binding, "DESIGN_SHA256", design_fingerprint(tampered))
    with pytest.raises(
        ValueError, match="v15_design_binding_feature_projection_mismatch",
    ):
        binding.validate_design_binding(
            tampered,
            v14_design=v14_design,
            charter=charter,
            protocol=protocol,
            geometry_artifact=artifact,
            geometry_artifact_file_sha256=binding._file_sha256(
                binding.DEFAULT_GEOMETRY_ARTIFACT
            ),
        )


def test_geometry_artifact_byte_tamper_blocks_binding():
    design = _read(binding.DEFAULT_DESIGN)
    v14_design = _read(binding.DEFAULT_V14_DESIGN)
    charter = _read(binding.DEFAULT_CHARTER)
    protocol = _read(binding.DEFAULT_PROTOCOL)
    artifact = _read(binding.DEFAULT_GEOMETRY_ARTIFACT)
    with pytest.raises(
        ValueError, match="v15_design_binding_geometry_lineage_mismatch",
    ):
        binding.validate_design_binding(
            design,
            v14_design=v14_design,
            charter=charter,
            protocol=protocol,
            geometry_artifact=artifact,
            geometry_artifact_file_sha256="0" * 64,
        )
