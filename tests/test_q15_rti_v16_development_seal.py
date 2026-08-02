from __future__ import annotations

import json
from pathlib import Path

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v16 as v16
from q15_upgrade.strategy_bots import rti_microstructure_v16_identity as identity
from tools import q15_rti_v16_development_seal as seal
from tools.q15_rti_microstructure_preregister import design_fingerprint


ROOT = Path(__file__).resolve().parents[1]


def test_protocol_and_development_fold_geometry_are_frozen():
    protocol = seal.load_protocol()
    assert design_fingerprint(protocol) == identity.PROTOCOL_SHA256
    times = tuple(float(index * 900) for index in range(240))
    folds = seal._fold_manifest(times)
    assert [item["train_close_windows"] for item in folds["outer_folds"]] == [
        120, 150, 180, 210,
    ]
    assert [len(item["inner_folds"]) for item in folds["outer_folds"]] == [
        2, 3, 4, 5,
    ]
    assert folds["calibration_rows_are_not_in_walk_forward_validation"] is True


def test_projection_cannot_contain_outcome_columns(monkeypatch):
    base = [{
        "id": 1,
        "asset": "ETH",
        "close_time": identity.PROSPECTIVE_AFTER_CLOSE_TIME - 900,
    }]
    monkeypatch.setattr(
        seal.v15_seal,
        "_project_evidence",
        lambda rows: ([{"id": 1, "asset": "ETH", "close_time": rows[0]["close_time"]}], 0),
    )
    monkeypatch.setattr(
        seal.v16,
        "feature_vector",
        lambda row: {
            "available": True,
            "feature_names": list(v16.FEATURE_NAMES),
            "features": [0.0] * len(v16.FEATURE_NAMES),
        },
    )
    assert seal._project_v16_evidence(base)[0]["v16_protocol_sha256"] == (
        identity.PROTOCOL_SHA256
    )

    monkeypatch.setattr(
        seal.v15_seal,
        "_project_evidence",
        lambda rows: ([{"id": 1, "official_result": "YES"}], 0),
    )
    with pytest.raises(AssertionError, match="projection_contains_outcome"):
        seal._project_v16_evidence(base)


def test_waiting_seal_does_not_fit_score_notify_or_trade(monkeypatch):
    protocol = json.loads(
        (ROOT / identity.PROTOCOL_RELATIVE_PATH).read_text(encoding="utf-8")
    )
    monkeypatch.setattr(seal, "_selected_times", lambda rows: ((), []))
    monkeypatch.setattr(seal, "_complete_v16_windows", lambda rows: {})
    result = seal.build_development_seal([], protocol=protocol, generated_at="fixed")
    assert result["status"] == seal.WAITING_STATUS
    assert result["development_windows_remaining"] == 240
    for key in (
        "outcome_columns_selected", "outcome_labels_read", "btc_labels_read",
        "model_fit_performed", "probability_scoring_performed",
        "paper_artifact_created", "notification_eligible", "automatic_promotion",
        "real_trading_allowed",
    ):
        assert result[key] is False


def test_prospective_readiness_uses_only_strictly_future_complete_windows(
    monkeypatch,
):
    boundary = identity.PROSPECTIVE_AFTER_CLOSE_TIME
    monkeypatch.setattr(
        seal,
        "_complete_v16_windows",
        lambda rows: {
            boundary: [],
            boundary + 900: [],
            boundary + 1800: [],
        },
    )
    result = seal.prospective_readiness([])
    assert result["successor_audit_complete_close_windows"] == 2
    assert result["calibration_windows_remaining"] == 58
    assert result["outcome_labels_read"] is False


def test_reconstruction_rejects_feature_hash_mismatch(monkeypatch):
    fake_seal = {"selected_close_times_sha256": "times"}
    monkeypatch.setattr(seal, "validate_development_seal", lambda value: None)
    monkeypatch.setattr(seal, "_selected_times", lambda rows: ((), []))
    monkeypatch.setattr(
        seal,
        "_complete_v16_windows",
        lambda rows: {float(index): [] for index in range(1, 241)},
    )
    monkeypatch.setattr(
        seal.v15_seal,
        "canonical_sha256",
        lambda value: "not-times",
    )
    with pytest.raises(ValueError, match="selected_window_identity"):
        seal.reconstruct_development_examples([], fake_seal)
