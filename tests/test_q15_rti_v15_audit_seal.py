from __future__ import annotations

import copy
from datetime import datetime
import inspect
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from q15_upgrade.strategy_bots import rti_microstructure_v14 as v14
from q15_upgrade.strategy_bots import rti_microstructure_v15 as v15
from tools import q15_rti_v15_audit_seal as seal


EASTERN = ZoneInfo("America/New_York")
ASSETS = ("BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP")


def _ticker(asset: str, close_time: float) -> str:
    timestamp = datetime.fromtimestamp(close_time, EASTERN)
    return (
        f"KX{asset}15M-{timestamp:%y%b%d%H%M}-"
        f"{timestamp.minute}"
    ).upper()


def _rows(windows: int) -> list[dict]:
    output = []
    row_id = 0
    start = v15.FIRST_ELIGIBLE_CLOSE_TIME
    for window in range(windows):
        close = start + 900.0 * window
        captured = close - 780.0 + 0.25
        for asset_index, asset in enumerate(ASSETS):
            row_id += 1
            output.append({
                "id": row_id,
                "bot_name": "rti_path_13m",
                "record_kind": "RTI_PATH_13M_PROSPECTIVE_EXACT",
                "interval": "13M",
                "ticker": _ticker(asset, close),
                "asset": asset,
                "side": "YES",
                "close_time": close,
                "source_captured_at": captured,
                "evidence_as_of": captured + 0.5,
                "kalshi_microstructure_captured_at": captured,
                "kalshi_microstructure_schema_version": v15.SOURCE_SCHEMA,
                "entry_ask_cents": 50.0,
                "spread_cents": 1.0,
                "threshold_json": {
                    "rti_opposite_ask_cents": 51.0,
                    "rti_market_mid_probability": 0.495,
                },
                "synthetic_window": window,
                "synthetic_asset": asset_index,
            })
    return output


def _feature_values(row: dict) -> list[float]:
    window = float(row["synthetic_window"])
    asset = float(row["synthetic_asset"])
    return [
        (window + 1.0) / 1000.0 + asset / 10000.0 + index / 100000.0
        for index in range(25)
    ]


def _patch_features(monkeypatch):
    def candidate(row):
        values = _feature_values(row)
        return {
            "available": True,
            "feature_names": list(v15.FEATURE_NAMES),
            "features": values,
            "market_yes_probability": 0.495,
            "yes_ask_cents": 50.0,
            "no_ask_cents": 51.0,
            "yes_depth_contracts": 100.0,
            "no_depth_contracts": 100.0,
            "yes_depth_available": True,
            "no_depth_available": True,
            "source_path_evidence_sha256": (
                f"{int(row['id']):064x}"
            ),
        }

    def control(row):
        return {
            "available": True,
            "feature_names": list(v14.FEATURE_NAMES),
            "features": _feature_values(row)[:20],
        }

    monkeypatch.setattr(seal.v15, "feature_vector", candidate)
    monkeypatch.setattr(seal.v14, "feature_vector", control)


def _inputs():
    return seal._load_inputs()


def _build(rows, cohort, *, generated_at="2026-07-23T04:00:00+00:00"):
    design, v14_design, charter, protocol, artifact, artifact_sha = _inputs()
    return seal.build_audit_seal(
        rows,
        cohort=cohort,
        design=design,
        v14_design=v14_design,
        charter=charter,
        protocol=protocol,
        geometry_artifact=artifact,
        geometry_artifact_file_sha256=artifact_sha,
        generated_at=generated_at,
    )


def test_non_btc_waiting_seal_cannot_authorize_any_label_access(monkeypatch):
    _patch_features(monkeypatch)
    result = _build(_rows(59), "NON_BTC_TRANSFER")
    assert result["status"] == seal.WAITING_STATUS
    assert result["complete_close_windows_available"] == 59
    assert result["windows_remaining"] == 1
    assert result["outcome_columns_selected"] is False
    assert result["outcome_labels_read"] is False
    assert result["train_calibration_label_access_authorized"] is False
    assert result["untouched_test_label_access_authorized"] is False
    assert result["model_fit_performed"] is False
    assert result["probability_scoring_performed"] is False
    seal.validate_audit_seal(result)


def test_seal_builder_has_no_label_model_scoring_or_delivery_capability():
    parameters = inspect.signature(seal.build_audit_seal).parameters
    assert "read_labels" not in parameters
    assert "labels_are_available" not in parameters
    source = Path(seal.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "official_result",
        "import sqlite3",
        "from sqlite3",
        "fit_residual_model(",
        "predict_probabilities(",
        "V3Telegram",
        "place_order(",
    ):
        assert forbidden not in source


def test_non_btc_ready_seal_freezes_exact_rows_partitions_and_folds(
    monkeypatch,
):
    _patch_features(monkeypatch)
    result = _build(_rows(63), "NON_BTC_TRANSFER")
    assert result["status"] == seal.READY_STATUS
    assert result["selected_close_windows"] == 60
    assert result["selected_rows"] == 360
    assert result["selected_all_seven_source_rows"] == 420
    assert result["partitions"]["development"]["close_windows"] == 36
    assert result["partitions"]["development"]["row_count"] == 216
    assert result["partitions"]["calibration"]["close_windows"] == 12
    assert result["partitions"]["calibration"]["row_count"] == 72
    assert result["partitions"]["untouched_test"]["close_windows"] == 12
    assert result["partitions"]["untouched_test"]["row_count"] == 72
    assert result["fold_manifest"]["outer_fold_count"] == 3
    assert result["fold_manifest"]["pretest_close_windows"] == 48
    assert result["fold_manifest"]["untouched_test_close_windows"] == 12
    assert [
        fold["train_close_windows"]
        for fold in result["fold_manifest"]["outer_folds"]
    ] == [24, 32, 40]
    assert [
        fold["validation_close_windows"]
        for fold in result["fold_manifest"]["outer_folds"]
    ] == [8, 8, 8]
    assert result["comparator_row_identity"][
        "v15_market_v14_use_identical_rows"
    ] is True
    assert result["comparator_row_identity"][
        "v14_receives_path_features"
    ] is False
    assert result["contract_identity"]["mismatch_rows"] == 0
    assert result["market_prior_consistency"]["status"] == "PASS"
    assert result["label_access_policy"][
        "other_cohort_labels_remain_sealed"
    ] is True
    assert result["outcome_labels_read"] is False
    projected, failures = seal._project_evidence([
        row for row in _rows(60) if row["asset"] != "BTC"
    ])
    assert failures == 0
    assert all(
        row["yes_depth_available"] is True
        and row["no_depth_available"] is True
        and tuple(row["v14_feature_names"]) == v14.FEATURE_NAMES
        for row in projected
    )
    seal.validate_audit_seal(result)


def test_btc_has_independent_150_window_fold_geometry(monkeypatch):
    _patch_features(monkeypatch)
    result = _build(_rows(150), "BTC")
    assert result["status"] == seal.READY_STATUS
    assert result["cohort_assets"] == ["BTC"]
    assert result["selected_rows"] == 150
    assert result["selected_all_seven_source_rows"] == 1050
    assert result["partitions"]["development"]["close_windows"] == 90
    assert result["partitions"]["calibration"]["close_windows"] == 30
    assert result["partitions"]["untouched_test"]["close_windows"] == 30
    assert [
        fold["train_close_windows"]
        for fold in result["fold_manifest"]["outer_folds"]
    ] == [60, 80, 100]
    assert [
        fold["validation_close_windows"]
        for fold in result["fold_manifest"]["outer_folds"]
    ] == [20, 20, 20]
    assert result["untouched_test_label_access_authorized"] is False


def test_missing_same_close_asset_prevents_window_credit(monkeypatch):
    _patch_features(monkeypatch)
    rows = _rows(60)
    last_close = max(float(row["close_time"]) for row in rows)
    rows = [
        row for row in rows
        if not (
            float(row["close_time"]) == last_close
            and row["asset"] == "HYPE"
        )
    ]
    result = _build(rows, "NON_BTC_TRANSFER")
    assert result["status"] == seal.WAITING_STATUS
    assert result["complete_close_windows_available"] == 59
    assert result["windows_remaining"] == 1


def test_wrong_kalshi_contract_prevents_window_credit(monkeypatch):
    _patch_features(monkeypatch)
    rows = _rows(60)
    rows[-1]["ticker"] = rows[-1]["ticker"].replace("XRP", "BTC")
    result = _build(rows, "NON_BTC_TRANSFER")
    assert result["status"] == seal.WAITING_STATUS
    assert result["complete_close_windows_available"] == 59


def test_outcome_values_cannot_change_ready_seal_identity(monkeypatch):
    _patch_features(monkeypatch)
    yes_rows = [
        {**row, "official_result": "YES", "correct": 1}
        for row in _rows(60)
    ]
    no_rows = [
        {**row, "official_result": "NO", "correct": 0}
        for row in _rows(60)
    ]
    yes = _build(yes_rows, "NON_BTC_TRANSFER")
    no = _build(no_rows, "NON_BTC_TRANSFER")
    assert yes["seal_sha256"] == no["seal_sha256"]
    assert yes["selected_feature_evidence_sha256"] == (
        no["selected_feature_evidence_sha256"]
    )


def test_quote_availability_flags_change_the_sealed_execution_evidence(
    monkeypatch,
):
    _patch_features(monkeypatch)
    rows = _rows(60)
    both_sides = _build(rows, "NON_BTC_TRANSFER")
    original = seal.v15.feature_vector

    def one_side_missing(row):
        result = dict(original(row))
        if int(row["id"]) == 420:
            result["no_depth_available"] = False
        return result

    monkeypatch.setattr(seal.v15, "feature_vector", one_side_missing)
    one_side = _build(rows, "NON_BTC_TRANSFER")
    assert one_side["selected_feature_evidence_sha256"] != (
        both_sides["selected_feature_evidence_sha256"]
    )
    assert one_side["seal_sha256"] != both_sides["seal_sha256"]


def test_spread_is_sealed_from_raw_decision_evidence_and_fails_closed(
    monkeypatch,
):
    _patch_features(monkeypatch)
    rows = _rows(60)
    result = _build(rows, "NON_BTC_TRANSFER")
    assert result["status"] == seal.READY_STATUS

    invalid = _rows(60)
    invalid[-2]["spread_cents"] = float("nan")
    with pytest.raises(
        ValueError, match="v15_audit_seal_spread_missing_or_invalid",
    ):
        seal._project_evidence([
            row for row in invalid if row["asset"] != "BTC"
        ])


def test_rehashed_safety_tamper_is_rejected(monkeypatch):
    _patch_features(monkeypatch)
    result = _build(_rows(60), "NON_BTC_TRANSFER")
    tampered = copy.deepcopy(result)
    tampered["outcome_labels_read"] = True
    tampered["seal_sha256"] = seal.seal_fingerprint(tampered)
    with pytest.raises(ValueError, match="v15_audit_seal_safety_invalid"):
        seal.validate_audit_seal(tampered)


def test_rehashed_partition_and_comparator_tampering_is_rejected(monkeypatch):
    _patch_features(monkeypatch)
    result = _build(_rows(60), "NON_BTC_TRANSFER")
    partition_tamper = copy.deepcopy(result)
    partition_tamper["partitions"]["untouched_test"]["row_count"] = 71
    partition_tamper["seal_sha256"] = seal.seal_fingerprint(partition_tamper)
    with pytest.raises(ValueError, match="v15_audit_seal_partition_invalid"):
        seal.validate_audit_seal(partition_tamper)

    comparator_tamper = copy.deepcopy(result)
    comparator_tamper["comparator_row_identity"]["row_ids_sha256"] = "f" * 64
    comparator_tamper["seal_sha256"] = seal.seal_fingerprint(
        comparator_tamper
    )
    with pytest.raises(ValueError, match="v15_audit_seal_ready_state_invalid"):
        seal.validate_audit_seal(comparator_tamper)


def test_rehashed_waiting_minimum_tamper_is_rejected(monkeypatch):
    _patch_features(monkeypatch)
    result = _build(_rows(59), "NON_BTC_TRANSFER")
    tampered = copy.deepcopy(result)
    tampered["minimum_complete_close_windows"] = 61
    tampered["windows_remaining"] = 2
    tampered["seal_sha256"] = seal.seal_fingerprint(tampered)
    with pytest.raises(ValueError, match="v15_audit_seal_waiting_state_invalid"):
        seal.validate_audit_seal(tampered)


def test_seal_write_is_exclusive_and_idempotency_fails_closed(
    monkeypatch, tmp_path,
):
    _patch_features(monkeypatch)
    waiting = _build(_rows(59), "NON_BTC_TRANSFER")
    path = tmp_path / "nested" / "seal.json"
    with pytest.raises(
        ValueError, match="refuses_persistent_waiting_state",
    ):
        seal.write_seal_exclusive(path, waiting)
    assert not path.exists()

    result = _build(_rows(60), "NON_BTC_TRANSFER")
    seal.write_seal_exclusive(path, result)
    assert path.exists()
    with pytest.raises(FileExistsError):
        seal.write_seal_exclusive(path, result)


def test_pre_v15_rows_are_skipped_before_feature_reconstruction(monkeypatch):
    _patch_features(monkeypatch)
    original = seal.v15.feature_vector
    called_close_times = []

    def guarded(row):
        close_time = float(row["close_time"])
        assert close_time > seal.v15.PROSPECTIVE_AFTER_CLOSE_TIME
        called_close_times.append(close_time)
        return original(row)

    monkeypatch.setattr(seal.v15, "feature_vector", guarded)
    early = _rows(1)
    for row in early:
        row["close_time"] = seal.v15.PROSPECTIVE_AFTER_CLOSE_TIME
    result = _build([*early, *_rows(60)], "NON_BTC_TRANSFER")
    assert result["status"] == seal.READY_STATUS
    assert called_close_times
