"""Build an outcome-blind, cohort-specific execution seal for the V15 audit.

The seal commits to the exact earliest 60 non-BTC or 150 BTC complete windows,
their point-in-time feature evidence, immutable folds, and identical comparator
rows before any label may be opened.  This module deliberately has no outcome
loader, model fitter, scorer, notifier, promotion path, or trading path.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import rti_microstructure_v14 as v14
from q15_upgrade.strategy_bots import rti_microstructure_v15 as v15
from q15_upgrade.strategy_bots import (
    rti_microstructure_v15_audit_identity as audit_identity,
)
from q15_upgrade.strategy_bots.rti_microstructure_v15_identity import (
    DESIGN_ID,
    DESIGN_SHA256,
    EVALUATION_PROTOCOL_ID,
    EVALUATION_PROTOCOL_SHA256,
)
from tools import q15_rti_independent_path_geometry_freeze as geometry_freeze
from tools import q15_rti_independent_path_successor_preregister as preregister
from tools import q15_rti_v15_design_binding as binding
from tools.q15_rti_independent_path_audit import (
    CONTRACT_IDENTITY_VERSION,
    validate_exact_contract_identity,
)
from tools.q15_rti_market_prior_consistency_audit import (
    audit_rows as audit_market_prior_rows,
)
from tools.q15_rti_microstructure_freeze import (
    OUTCOME_COLUMNS,
    load_feature_rows,
)
from tools.q15_rti_microstructure_preregister import design_fingerprint


AUDIT_SEAL_VERSION = audit_identity.AUDIT_SEAL_VERSION
EXPECTED_ASSETS = frozenset({"BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP"})
COHORT_ASSETS = {
    "NON_BTC_TRANSFER": frozenset(
        {"BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP"}
    ),
    "BTC": frozenset({"BTC"}),
}
READY_STATUS = "READY_FOR_MANUAL_TRAIN_CAL_LABEL_ACCESS"
WAITING_STATUS = "WAITING_FOR_COMPLETE_OUTCOME_BLIND_WINDOWS"
EXPECTED_GEOMETRY_PAYLOAD_SHA256 = (
    "d4831b2a68a0af6cb73af721829d2a0a54df2786451063110fa9be4ce58afe7c"
)
EXPECTED_FOLD_GEOMETRY = {
    "NON_BTC_TRANSFER": {
        "minimum": 60,
        "rows": 360,
        "development": (36, 216),
        "calibration": (12, 72),
        "untouched_test": (12, 72),
        "outer_train": [24, 32, 40],
        "outer_validation": [8, 8, 8],
        "pretest": 48,
        "test": 12,
    },
    "BTC": {
        "minimum": 150,
        "rows": 150,
        "development": (90, 90),
        "calibration": (30, 30),
        "untouched_test": (30, 30),
        "outer_train": [60, 80, 100],
        "outer_validation": [20, 20, 20],
        "pretest": 120,
        "test": 30,
    },
}

DEFAULT_DB = ROOT / "data" / "q15_strategy_bots_v3.sqlite3"
DEFAULT_OUTPUT_DIR = ROOT / "reports" / "q15_rti_v15_audit_seals"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _valid_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(
        character in "0123456789abcdef" for character in text.lower()
    )


def seal_fingerprint(seal: Mapping[str, Any]) -> str:
    payload = dict(seal)
    payload.pop("seal_sha256", None)
    return canonical_sha256(payload)


def _number(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _load_inputs() -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    str,
]:
    binding.validate_files()
    design = binding._load(
        binding.DEFAULT_DESIGN, "v15_audit_seal_design_root_not_object",
    )
    v14_design = binding._load(
        binding.DEFAULT_V14_DESIGN,
        "v15_audit_seal_v14_design_root_not_object",
    )
    charter = binding._load(
        binding.DEFAULT_CHARTER, "v15_audit_seal_charter_root_not_object",
    )
    protocol = binding._load(
        binding.DEFAULT_PROTOCOL, "v15_audit_seal_protocol_root_not_object",
    )
    artifact = binding._load(
        binding.DEFAULT_GEOMETRY_ARTIFACT,
        "v15_audit_seal_geometry_root_not_object",
    )
    artifact_file_sha256 = binding._file_sha256(
        binding.DEFAULT_GEOMETRY_ARTIFACT
    )
    return (
        design,
        v14_design,
        charter,
        protocol,
        artifact,
        artifact_file_sha256,
    )


def _validate_inputs(
    *,
    design: Mapping[str, Any],
    v14_design: Mapping[str, Any],
    charter: Mapping[str, Any],
    protocol: Mapping[str, Any],
    geometry_artifact: Mapping[str, Any],
    geometry_artifact_file_sha256: str,
) -> None:
    binding.validate_design_binding(
        design,
        v14_design=v14_design,
        charter=charter,
        protocol=protocol,
        geometry_artifact=geometry_artifact,
        geometry_artifact_file_sha256=geometry_artifact_file_sha256,
    )
    preregister.validate_charter(charter)
    preregister.validate_protocol(protocol, charter)
    geometry_freeze.validate_artifact(geometry_artifact)
    if (
        design.get("design_id") != DESIGN_ID
        or design_fingerprint(design) != DESIGN_SHA256
        or protocol.get("protocol_id") != EVALUATION_PROTOCOL_ID
        or design_fingerprint(protocol) != EVALUATION_PROTOCOL_SHA256
        or dict(geometry_artifact.get("payload") or {}).get("decision")
        != "PASS_CONTINUE_PROSPECTIVE_COLLECTION_NO_MODEL"
    ):
        raise ValueError("v15_audit_seal_input_identity_mismatch")


def _complete_windows(
    rows: Sequence[Mapping[str, Any]],
) -> dict[float, list[dict[str, Any]]]:
    grouped: dict[float, list[dict[str, Any]]] = defaultdict(list)
    for raw in rows:
        if (
            str(raw.get("bot_name") or "") != "rti_path_13m"
            or str(raw.get("interval") or "").upper() != "13M"
            or str(raw.get("record_kind") or "").upper()
            != "RTI_PATH_13M_PROSPECTIVE_EXACT"
            or raw.get("kalshi_microstructure_schema_version")
            != v15.SOURCE_SCHEMA
        ):
            continue
        close = _number(raw.get("close_time"))
        if close is not None and close > v15.PROSPECTIVE_AFTER_CLOSE_TIME:
            grouped[close].append(dict(raw))

    complete: dict[float, list[dict[str, Any]]] = {}
    for close_time, window_rows in sorted(grouped.items()):
        assets = {
            str(row.get("asset") or "").upper() for row in window_rows
        }
        if len(window_rows) != 7 or assets != EXPECTED_ASSETS:
            continue
        vectors = [v15.feature_vector(row) for row in window_rows]
        if any(not vector.get("available") for vector in vectors):
            continue
        if any(
            not validate_exact_contract_identity(row).get("valid")
            for row in window_rows
        ):
            continue
        complete[close_time] = window_rows
    return complete


def complete_audit_windows(
    rows: Sequence[Mapping[str, Any]],
) -> dict[float, list[dict[str, Any]]]:
    """Return exact windows eligible for the frozen V15 audit population."""
    return _complete_windows(rows)


def _partition(
    close_times: Sequence[float],
    *,
    name: str,
    row_ids: Sequence[int],
) -> dict[str, Any]:
    times = tuple(float(value) for value in close_times)
    ids = tuple(sorted(int(value) for value in row_ids))
    return {
        "name": name,
        "close_windows": len(times),
        "row_count": len(ids),
        "first_close_time": min(times),
        "last_close_time": max(times),
        "close_times_sha256": canonical_sha256(times),
        "row_ids_sha256": canonical_sha256(ids),
    }


def _rows_for_times(
    rows: Sequence[Mapping[str, Any]],
    close_times: Sequence[float],
) -> list[Mapping[str, Any]]:
    selected = set(float(value) for value in close_times)
    return [
        row for row in rows
        if float(row["close_time"]) in selected
    ]


def _inner_fold_manifest(
    outer_train_times: Sequence[float],
    *,
    initial: int,
    block: int,
) -> list[dict[str, Any]]:
    times = tuple(float(value) for value in outer_train_times)
    if len(times) < initial + block or (len(times) - initial) % block != 0:
        raise ValueError("v15_audit_seal_inner_fold_geometry_invalid")
    folds = []
    for index, start in enumerate(range(initial, len(times), block), start=1):
        training = times[:start]
        validation = times[start:start + block]
        if not validation or max(training) >= min(validation):
            raise ValueError("v15_audit_seal_inner_chronology_invalid")
        folds.append({
            "fold": index,
            "train_close_windows": len(training),
            "validation_close_windows": len(validation),
            "train_close_times_sha256": canonical_sha256(training),
            "validation_close_times_sha256": canonical_sha256(validation),
        })
    return folds


def _fold_manifest(
    selected_times: Sequence[float],
    protocol: Mapping[str, Any],
    cohort: str,
) -> dict[str, Any]:
    rule = dict(dict(protocol["cohorts"])[cohort])
    initial = int(rule["initial_train_windows"])
    block = int(rule["validation_block_windows"])
    fold_count = int(rule["walk_forward_fold_count"])
    pretest = initial + block * fold_count
    times = tuple(float(value) for value in selected_times)
    if len(times) != int(rule["minimum_complete_close_windows"]):
        raise ValueError("v15_audit_seal_selected_window_count_mismatch")
    outer = []
    for index in range(fold_count):
        start = initial + index * block
        training = times[:start]
        validation = times[start:start + block]
        if (
            not training
            or len(validation) != block
            or max(training) >= min(validation)
        ):
            raise ValueError("v15_audit_seal_outer_chronology_invalid")
        outer.append({
            "fold": index + 1,
            "train_close_windows": len(training),
            "validation_close_windows": len(validation),
            "train_close_times_sha256": canonical_sha256(training),
            "validation_close_times_sha256": canonical_sha256(validation),
            "inner_folds": _inner_fold_manifest(
                training,
                initial=int(rule["inner_initial_train_windows"]),
                block=int(rule["inner_validation_block_windows"]),
            ),
        })
    validation = times[initial:pretest]
    if len(validation) != block * fold_count:
        raise ValueError("v15_audit_seal_outer_coverage_invalid")
    return {
        "outer_fold_count": fold_count,
        "outer_folds": outer,
        "pretest_close_windows": pretest,
        "pretest_close_times_sha256": canonical_sha256(times[:pretest]),
        "untouched_test_close_windows": len(times[pretest:]),
        "untouched_test_close_times_sha256": canonical_sha256(times[pretest:]),
        "same_close_assets_share_every_outer_and_inner_fold": True,
        "outer_validation_strictly_after_training": True,
        "inner_validation_strictly_after_training": True,
    }


def _project_evidence(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    projected: list[dict[str, Any]] = []
    contract_failures = 0
    for row in sorted(
        rows,
        key=lambda item: (
            float(item["close_time"]),
            str(item.get("asset") or ""),
            int(item["id"]),
        ),
    ):
        contract = validate_exact_contract_identity(row)
        if not contract.get("valid"):
            contract_failures += 1
        candidate = v15.feature_vector(row)
        control = v14.feature_vector(row)
        if not candidate.get("available") or not control.get("available"):
            raise ValueError("v15_audit_seal_feature_unavailable")
        candidate_features = [float(value) for value in candidate["features"]]
        control_features = [float(value) for value in control["features"]]
        if (
            candidate_features[:20] != control_features
            or tuple(candidate.get("feature_names") or ()) != v15.FEATURE_NAMES
            or tuple(control.get("feature_names") or ()) != v14.FEATURE_NAMES
        ):
            raise ValueError("v15_audit_seal_v14_feature_invariance_failure")
        spread_cents = _number(row.get("spread_cents"))
        if spread_cents is None or spread_cents < 0.0:
            raise ValueError(
                "v15_audit_seal_spread_missing_or_invalid"
            )
        item = {
            "id": int(row["id"]),
            "ticker": str(row.get("ticker") or ""),
            "asset": str(row.get("asset") or "").upper(),
            "side": str(row.get("side") or "").upper(),
            "close_time": float(row["close_time"]),
            "source_captured_at": float(row["source_captured_at"]),
            "evidence_as_of": float(row["evidence_as_of"]),
            "v15_feature_names": list(v15.FEATURE_NAMES),
            "v14_feature_names": list(v14.FEATURE_NAMES),
            "v15_features": candidate_features,
            "v14_features": control_features,
            "market_yes_probability": float(
                candidate["market_yes_probability"]
            ),
            "yes_ask_cents": float(candidate["yes_ask_cents"]),
            "no_ask_cents": float(candidate["no_ask_cents"]),
            "yes_depth_contracts": float(candidate["yes_depth_contracts"]),
            "no_depth_contracts": float(candidate["no_depth_contracts"]),
            "yes_depth_available": bool(candidate["yes_depth_available"]),
            "no_depth_available": bool(candidate["no_depth_available"]),
            # Spread is immutable decision-row evidence.  The frozen feature
            # vector contains its clipped transform but intentionally does not
            # expose it as a duplicate named metadata field.
            "spread_cents": spread_cents,
            "path_evidence_sha256": str(
                candidate.get("source_path_evidence_sha256") or ""
            ),
            "contract_identity_version": contract.get("version"),
            "contract_identity_valid": contract.get("valid"),
        }
        if OUTCOME_COLUMNS.intersection(item):
            raise AssertionError("v15_audit_seal_projection_contains_outcome")
        projected.append(item)
    return projected, contract_failures


def build_audit_seal(
    rows: Sequence[Mapping[str, Any]],
    *,
    cohort: str,
    design: Mapping[str, Any],
    v14_design: Mapping[str, Any],
    charter: Mapping[str, Any],
    protocol: Mapping[str, Any],
    geometry_artifact: Mapping[str, Any],
    geometry_artifact_file_sha256: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if cohort not in COHORT_ASSETS:
        raise ValueError("v15_audit_seal_unsupported_cohort")
    _validate_inputs(
        design=design,
        v14_design=v14_design,
        charter=charter,
        protocol=protocol,
        geometry_artifact=geometry_artifact,
        geometry_artifact_file_sha256=geometry_artifact_file_sha256,
    )
    complete = complete_audit_windows(rows)
    minimum = int(dict(protocol["cohorts"][cohort])[
        "minimum_complete_close_windows"
    ])
    common = {
        "seal_version": AUDIT_SEAL_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "design_id": DESIGN_ID,
        "design_sha256": DESIGN_SHA256,
        "evaluation_protocol_id": EVALUATION_PROTOCOL_ID,
        "evaluation_protocol_sha256": EVALUATION_PROTOCOL_SHA256,
        "geometry_payload_sha256": geometry_artifact["payload_sha256"],
        "cohort": cohort,
        "cohort_assets": sorted(COHORT_ASSETS[cohort]),
        "minimum_complete_close_windows": minimum,
        "complete_close_windows_available": len(complete),
        "windows_remaining": max(0, minimum - len(complete)),
        "selection": "EARLIEST_COMPLETE_RECONSTRUCTABLE_CLOSE_WINDOWS",
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "train_calibration_label_access_authorized": False,
        "untouched_test_label_access_authorized": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }
    if len(complete) < minimum:
        result = {**common, "status": WAITING_STATUS}
        result["seal_sha256"] = seal_fingerprint(result)
        validate_audit_seal(result)
        return result

    selected_times = tuple(sorted(complete)[:minimum])
    all_rows = [
        row for close_time in selected_times for row in complete[close_time]
    ]
    market_prior = audit_market_prior_rows(all_rows)
    if (
        market_prior.get("status") != "PASS"
        or int(market_prior.get("eligible_rows") or 0) != minimum * 7
        or int(market_prior.get("checked_rows") or 0) != minimum * 7
    ):
        raise ValueError("v15_audit_seal_market_prior_consistency_failure")
    selected_rows = [
        row for row in all_rows
        if str(row.get("asset") or "").upper() in COHORT_ASSETS[cohort]
    ]
    expected_rows = minimum * len(COHORT_ASSETS[cohort])
    if len(selected_rows) != expected_rows:
        raise ValueError("v15_audit_seal_cohort_rows_incomplete")
    projected, contract_failures = _project_evidence(selected_rows)
    if contract_failures:
        raise ValueError("v15_audit_seal_contract_identity_failure")

    rule = dict(protocol["cohorts"][cohort])
    development_count = int(rule["development_train_windows"])
    calibration_count = int(rule["calibration_windows"])
    test_count = int(rule["untouched_test_windows"])
    if development_count + calibration_count + test_count != minimum:
        raise ValueError("v15_audit_seal_partition_geometry_invalid")
    development_times = selected_times[:development_count]
    calibration_times = selected_times[
        development_count:development_count + calibration_count
    ]
    test_times = selected_times[-test_count:]
    partition_times = {
        "development": development_times,
        "calibration": calibration_times,
        "untouched_test": test_times,
    }
    partitions = {}
    for name, times in partition_times.items():
        partition_rows = _rows_for_times(projected, times)
        partitions[name] = _partition(
            times,
            name=name,
            row_ids=[int(row["id"]) for row in partition_rows],
        )
    row_ids = tuple(sorted(int(row["id"]) for row in projected))
    pretest_ids = tuple(sorted(
        int(row["id"]) for row in projected
        if float(row["close_time"]) not in set(test_times)
    ))
    test_ids = tuple(sorted(
        int(row["id"]) for row in projected
        if float(row["close_time"]) in set(test_times)
    ))
    result = {
        **common,
        "status": READY_STATUS,
        "complete_close_windows_available": len(complete),
        "windows_remaining": 0,
        "selected_close_windows": minimum,
        "selected_rows": expected_rows,
        "selected_all_seven_source_rows": minimum * 7,
        "first_selected_close_time": min(selected_times),
        "last_selected_close_time": max(selected_times),
        "selected_close_times_sha256": canonical_sha256(selected_times),
        "selected_row_ids_sha256": canonical_sha256(row_ids),
        "selected_feature_evidence_sha256": canonical_sha256(projected),
        "train_calibration_row_ids_sha256": canonical_sha256(pretest_ids),
        "untouched_test_row_ids_sha256": canonical_sha256(test_ids),
        "comparator_row_identity": {
            "v15_market_v14_use_identical_rows": True,
            "v14_receives_path_features": False,
            "row_ids_sha256": canonical_sha256(row_ids),
        },
        "contract_identity": {
            "version": CONTRACT_IDENTITY_VERSION,
            "rows": expected_rows,
            "mismatch_rows": 0,
            "outcome_labels_read": False,
        },
        "market_prior_consistency": {
            "audit_version": market_prior.get("audit_version"),
            "rows": int(market_prior["checked_rows"]),
            "maximum_absolute_delta": market_prior.get(
                "maximum_absolute_delta"
            ),
            "maximum_exact_capture_offset_seconds": market_prior.get(
                "maximum_exact_capture_offset_seconds"
            ),
            "maximum_kalshi_source_cutoff_delta_seconds": market_prior.get(
                "maximum_kalshi_source_cutoff_delta_seconds"
            ),
            "status": market_prior["status"],
            "outcome_labels_read": False,
        },
        "partitions": partitions,
        "fold_manifest": _fold_manifest(selected_times, protocol, cohort),
        "label_access_policy": {
            "manual_action_required": True,
            "only_train_and_calibration_ids_may_be_opened_first": True,
            "other_cohort_labels_remain_sealed": True,
            "untouched_test_labels_remain_sealed_until_every_prior_gate_passes": True,
            "untouched_test_requires_exclusive_one_shot_reservation": True,
            "seal_and_reconstructed_feature_evidence_must_match_before_any_read": True,
        },
    }
    result["seal_sha256"] = seal_fingerprint(result)
    validate_audit_seal(result)
    return result


def validate_audit_seal(seal: Mapping[str, Any]) -> None:
    try:
        generated_at = datetime.fromisoformat(
            str(seal.get("generated_at") or "").replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValueError("v15_audit_seal_timestamp_invalid") from exc
    if (
        seal.get("seal_version") != AUDIT_SEAL_VERSION
        or seal.get("seal_sha256") != seal_fingerprint(seal)
        or seal.get("design_id") != DESIGN_ID
        or seal.get("design_sha256") != DESIGN_SHA256
        or seal.get("evaluation_protocol_id") != EVALUATION_PROTOCOL_ID
        or seal.get("evaluation_protocol_sha256")
        != EVALUATION_PROTOCOL_SHA256
        or seal.get("cohort") not in COHORT_ASSETS
        or generated_at.tzinfo is None
        or seal.get("geometry_payload_sha256")
        != EXPECTED_GEOMETRY_PAYLOAD_SHA256
    ):
        raise ValueError("v15_audit_seal_identity_or_sha_invalid")
    if any(seal.get(key) is not False for key in (
        "outcome_columns_selected",
        "outcome_labels_read",
        "train_calibration_label_access_authorized",
        "untouched_test_label_access_authorized",
        "model_fit_performed",
        "probability_scoring_performed",
        "paper_artifact_created",
        "notification_eligible",
        "automatic_promotion",
        "real_trading_allowed",
    )):
        raise ValueError("v15_audit_seal_safety_invalid")
    status = seal.get("status")
    if status == WAITING_STATUS:
        if (
            int(seal.get("windows_remaining") or 0) <= 0
            or int(seal.get("complete_close_windows_available") or 0)
            >= int(seal.get("minimum_complete_close_windows") or 0)
            or int(seal.get("minimum_complete_close_windows") or 0)
            != EXPECTED_FOLD_GEOMETRY[str(seal["cohort"])]["minimum"]
        ):
            raise ValueError("v15_audit_seal_waiting_state_invalid")
        return
    if status != READY_STATUS:
        raise ValueError("v15_audit_seal_status_invalid")
    cohort = str(seal["cohort"])
    minimum = int(seal["minimum_complete_close_windows"])
    expected_rows = minimum * len(COHORT_ASSETS[cohort])
    expected = EXPECTED_FOLD_GEOMETRY[cohort]
    comparator = seal.get("comparator_row_identity")
    contract = seal.get("contract_identity")
    policy = seal.get("label_access_policy")
    market = seal.get("market_prior_consistency")
    partitions = seal.get("partitions")
    folds = seal.get("fold_manifest")
    hash_fields = (
        "selected_close_times_sha256",
        "selected_row_ids_sha256",
        "selected_feature_evidence_sha256",
        "train_calibration_row_ids_sha256",
        "untouched_test_row_ids_sha256",
    )
    if (
        int(seal.get("windows_remaining", -1)) != 0
        or int(seal.get("selected_close_windows") or 0) != minimum
        or int(seal.get("selected_rows") or 0) != expected_rows
        or minimum != int(expected["minimum"])
        or expected_rows != int(expected["rows"])
        or int(seal.get("selected_all_seven_source_rows") or 0)
        != minimum * 7
        or any(not _valid_sha256(seal.get(key)) for key in hash_fields)
        or not isinstance(comparator, Mapping)
        or comparator.get("v15_market_v14_use_identical_rows") is not True
        or comparator.get("v14_receives_path_features") is not False
        or comparator.get("row_ids_sha256")
        != seal.get("selected_row_ids_sha256")
        or not isinstance(contract, Mapping)
        or int(contract.get("rows") or 0) != expected_rows
        or int(contract.get("mismatch_rows", -1)) != 0
        or contract.get("outcome_labels_read") is not False
        or not isinstance(market, Mapping)
        or market.get("status") != "PASS"
        or int(market.get("rows") or 0) != minimum * 7
        or market.get("outcome_labels_read") is not False
        or not isinstance(partitions, Mapping)
        or set(partitions) != {
            "development", "calibration", "untouched_test",
        }
        or not isinstance(folds, Mapping)
        or int(folds.get("outer_fold_count") or 0) != 3
        or int(folds.get("pretest_close_windows") or 0)
        != int(expected["pretest"])
        or int(folds.get("untouched_test_close_windows") or 0)
        != int(expected["test"])
        or folds.get(
            "same_close_assets_share_every_outer_and_inner_fold"
        ) is not True
        or folds.get("outer_validation_strictly_after_training") is not True
        or folds.get("inner_validation_strictly_after_training") is not True
        or not isinstance(policy, Mapping)
        or any(policy.get(key) is not True for key in (
            "manual_action_required",
            "only_train_and_calibration_ids_may_be_opened_first",
            "other_cohort_labels_remain_sealed",
            "untouched_test_labels_remain_sealed_until_every_prior_gate_passes",
            "untouched_test_requires_exclusive_one_shot_reservation",
            "seal_and_reconstructed_feature_evidence_must_match_before_any_read",
        ))
    ):
        raise ValueError("v15_audit_seal_ready_state_invalid")
    for name in ("development", "calibration", "untouched_test"):
        partition = partitions.get(name)
        expected_windows, expected_partition_rows = expected[name]
        if not isinstance(partition, Mapping) or (
            partition.get("name") != name
            or int(partition.get("close_windows") or 0) != expected_windows
            or int(partition.get("row_count") or 0) != expected_partition_rows
            or not _valid_sha256(partition.get("close_times_sha256"))
            or not _valid_sha256(partition.get("row_ids_sha256"))
        ):
            raise ValueError("v15_audit_seal_partition_invalid")
    outer = folds.get("outer_folds")
    if not isinstance(outer, list) or len(outer) != 3 or (
        [int(dict(item).get("train_close_windows") or 0) for item in outer]
        != expected["outer_train"]
    ) or (
        [
            int(dict(item).get("validation_close_windows") or 0)
            for item in outer
        ]
        != expected["outer_validation"]
    ):
        raise ValueError("v15_audit_seal_fold_manifest_invalid")
    for index, fold in enumerate(outer, start=1):
        if (
            int(fold.get("fold") or 0) != index
            or not _valid_sha256(fold.get("train_close_times_sha256"))
            or not _valid_sha256(fold.get("validation_close_times_sha256"))
            or not isinstance(fold.get("inner_folds"), list)
            or not fold["inner_folds"]
        ):
            raise ValueError("v15_audit_seal_fold_manifest_invalid")


def write_seal_exclusive(path: Path, seal: Mapping[str, Any]) -> None:
    validate_audit_seal(seal)
    if seal.get("status") != READY_STATUS:
        raise ValueError("v15_audit_seal_refuses_persistent_waiting_state")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(dict(seal), handle, indent=2, sort_keys=True)
        handle.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cohort", choices=tuple(COHORT_ASSETS), required=True,
    )
    parser.add_argument("--strategy-db", default=str(DEFAULT_DB))
    parser.add_argument("--output")
    args = parser.parse_args()
    (
        design,
        v14_design,
        charter,
        protocol,
        artifact,
        artifact_file_sha256,
    ) = _load_inputs()
    rows = load_feature_rows(Path(args.strategy_db))
    seal = build_audit_seal(
        rows,
        cohort=args.cohort,
        design=design,
        v14_design=v14_design,
        charter=charter,
        protocol=protocol,
        geometry_artifact=artifact,
        geometry_artifact_file_sha256=artifact_file_sha256,
    )
    if args.output:
        write_seal_exclusive(Path(args.output), seal)
    print(json.dumps(seal, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
