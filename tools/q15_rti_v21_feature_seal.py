"""Exclusive, outcome-blind earliest-180 feature seal for frozen V21."""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import rti_microstructure_v18 as v18
from q15_upgrade.strategy_bots import rti_microstructure_v19 as v19
from q15_upgrade.strategy_bots import rti_microstructure_v20_features as v20_features
from q15_upgrade.strategy_bots import rti_microstructure_v21 as v21
from q15_upgrade.strategy_bots import rti_microstructure_v21_features as v21_features
from q15_upgrade.strategy_bots import rti_microstructure_v21_identity as identity
from tools.q15_rti_microstructure_freeze import load_feature_rows_after
from tools.q15_rti_independent_path_audit import validate_exact_contract_identity
from tools.q15_rti_v17_development_seal import DEFAULT_DB
from tools.q15_rti_v18_prospective_seal import _complete_windows
from tools.q15_rti_v19_readiness import _linked_parent_id
from tools.q15_rti_v21_readiness import load_trajectory_feature_rows_after


ASSETS = frozenset({"BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP"})
SEAL_VERSION = "q15-rti-v21-exclusive-feature-seal-v1"
CONFIRMATION = "CREATE_V21_EXCLUSIVE_FEATURE_SEAL_ONCE"
DEFAULT_OUTPUT = ROOT / "reports" / "q15_rti_v21_audit" / "feature_seal.json"
FORBIDDEN_KEYS = frozenset({
    "official_result", "resolved", "settled", "outcome", "label", "correct",
    "won", "pnl", "profit", "settlement_value", "resolution_status",
})


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _walk_keys(value: Any):
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key).lower()
            yield from _walk_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_keys(child)


def _seal_core(seal: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(seal)
    value.pop("seal_sha256", None)
    return value


def collect_feature_windows(
    parent_rows: Sequence[Mapping[str, Any]],
    trajectory_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collect all valid triplet windows; never require all rows executable."""
    v21.load_protocol()
    v21.load_evaluator_contract()
    parents = {
        close: rows for close, rows in _complete_windows(parent_rows).items()
        if close > identity.PROSPECTIVE_AFTER_CLOSE_TIME
    }
    stages: dict[int, dict[str, list[Mapping[str, Any]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in trajectory_rows:
        if float(row.get("close_time") or 0.0) <= identity.PROSPECTIVE_AFTER_CLOSE_TIME:
            continue
        interval = str(row.get("interval") or "").upper()
        if interval in {"12M30S", "12M"}:
            stages[_linked_parent_id(row)][interval].append(row)
    complete = []
    excluded = []
    for close_time, parent_window in sorted(parents.items()):
        window_rows = []
        failures = []
        for parent in sorted(parent_window, key=lambda row: str(row.get("asset") or "")):
            parent_id = int(parent["id"])
            intermediate_matches = stages.get(parent_id, {}).get("12M30S", [])
            delayed_matches = stages.get(parent_id, {}).get("12M", [])
            if len(intermediate_matches) != 1 or len(delayed_matches) != 1:
                failures.append({
                    "asset": parent.get("asset"),
                    "parent_id": parent_id,
                    "reason": "V21_TRIPLET_MISSING_OR_DUPLICATE",
                    "intermediate_matches": len(intermediate_matches),
                    "delayed_matches": len(delayed_matches),
                })
                continue
            intermediate = intermediate_matches[0]
            delayed = delayed_matches[0]
            result = v21.evaluate_triplet(parent, intermediate, delayed)
            if result.get("eligible_for_v21_feature_credit") is not True:
                failures.append({
                    "asset": parent.get("asset"),
                    "parent_id": parent_id,
                    "intermediate_id": intermediate.get("id"),
                    "delayed_id": delayed.get("id"),
                    "reason": "V21_FEATURE_SOURCE_INCOMPLETE",
                    "failures": list(result.get("failures") or ()),
                })
                continue
            evidence = dict(result["evidence"])
            v18_result = v18.evaluate_row(parent)
            v19_result = v19.evaluate_pair(parent, delayed)
            benchmarks = {
                "matched_v18_eligible": v18_result.get("eligible") is True,
                "matched_v18_feature_evidence_sha256": v18_result[
                    "feature_evidence_sha256"
                ],
                "matched_v19_eligible": v19_result.get("eligible") is True,
                "matched_v19_feature_evidence_sha256": v19_result[
                    "feature_evidence_sha256"
                ],
                "v20_base_feature_evidence_sha256": evidence[
                    "base_feature_evidence_sha256"
                ],
            }
            window_rows.append({
                "parent_id": int(evidence["parent_id"]),
                "intermediate_id": int(evidence["intermediate_id"]),
                "delayed_id": int(evidence["delayed_id"]),
                "asset": str(evidence["asset"]),
                "cohort": str(evidence["cohort"]),
                "ticker": str(evidence["ticker"]),
                "close_time": float(evidence["close_time"]),
                "side": str(evidence["side"]),
                "execution_supported": evidence["execution_supported"] is True,
                "entry_ask_cents": evidence["entry_ask_cents"],
                "spread_cents": evidence["spread_cents"],
                "depth_contracts": evidence["depth_contracts"],
                "sim_contracts": evidence["sim_contracts"],
                "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
                "feature_names_sha256": identity.FEATURE_NAMES_SHA256,
                "intermediate_source_evidence_sha256": evidence[
                    "intermediate_source_evidence_sha256"
                ],
                "delayed_source_evidence_sha256": evidence[
                    "delayed_source_evidence_sha256"
                ],
                "feature_evidence_sha256": evidence["feature_evidence_sha256"],
                "source_feature_evidence_sha256": result[
                    "source_feature_evidence_sha256"
                ],
                **benchmarks,
                "matched_benchmark_evidence_sha256": _canonical_sha256(benchmarks),
                "features": list(result["features"]),
            })
        if (
            not failures and len(window_rows) == 7
            and {row["asset"] for row in window_rows} == ASSETS
        ):
            complete.append({"close_time": float(close_time), "rows": window_rows})
        else:
            excluded.append({
                "close_time": float(close_time),
                "feature_quality_failures": failures,
            })
    return complete, excluded


def build_seal(
    complete_windows: Sequence[Mapping[str, Any]],
    *, excluded_windows: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    v21.load_protocol()
    v21.load_evaluator_contract()
    ordered = sorted(
        (dict(window) for window in complete_windows),
        key=lambda window: float(window["close_time"]),
    )
    if len(ordered) < identity.MINIMUM_COMPLETE_CLOSE_WINDOWS:
        raise ValueError("v21_feature_seal_not_ready")
    selected = ordered[:identity.MINIMUM_COMPLETE_CLOSE_WINDOWS]
    partitions = {
        "TRAIN": [],
        "PROBABILITY_CALIBRATION": [],
        "EXECUTION_POLICY_SELECTION": [],
        "UNTOUCHED_TEST": [],
    }
    rows = []
    for index, window in enumerate(selected):
        if index < identity.TRAIN_CLOSE_WINDOWS:
            partition = "TRAIN"
        elif index < identity.TRAIN_CLOSE_WINDOWS + identity.PROBABILITY_CALIBRATION_CLOSE_WINDOWS:
            partition = "PROBABILITY_CALIBRATION"
        elif index < (
            identity.TRAIN_CLOSE_WINDOWS
            + identity.PROBABILITY_CALIBRATION_CLOSE_WINDOWS
            + identity.EXECUTION_POLICY_SELECTION_CLOSE_WINDOWS
        ):
            partition = "EXECUTION_POLICY_SELECTION"
        else:
            partition = "UNTOUCHED_TEST"
        close_time = float(window["close_time"])
        partitions[partition].append(close_time)
        window_rows = sorted(window["rows"], key=lambda row: str(row["asset"]))
        if len(window_rows) != 7 or {row["asset"] for row in window_rows} != ASSETS:
            raise ValueError("v21_feature_seal_window_geometry_invalid")
        for raw in window_rows:
            row = dict(raw)
            if float(row["close_time"]) != close_time:
                raise ValueError("v21_feature_seal_cross_close_row")
            rows.append({**row, "partition": partition})
    payload = {
        "seal_version": SEAL_VERSION,
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "evaluator_contract_id": identity.EVALUATOR_CONTRACT_ID,
        "evaluator_contract_sha256": identity.EVALUATOR_CONTRACT_SHA256,
        "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
        "feature_count": identity.FEATURE_COUNT,
        "feature_names_sha256": identity.FEATURE_NAMES_SHA256,
        "prospective_after_close_time": identity.PROSPECTIVE_AFTER_CLOSE_TIME,
        "first_eligible_close_time": identity.FIRST_ELIGIBLE_CLOSE_TIME,
        "exclusive_selection": "EARLIEST_180_COMPLETE_SEVEN_ASSET_TRIPLET_WINDOWS",
        "selected_complete_close_windows": len(selected),
        "selected_rows": len(rows),
        "first_selected_close_time": float(selected[0]["close_time"]),
        "last_selected_close_time": float(selected[-1]["close_time"]),
        "partition_windows": partitions,
        "excluded_feature_incomplete_windows_before_selection": [
            dict(window) for window in excluded_windows
            if float(window.get("close_time") or 0.0) <= float(selected[-1]["close_time"])
        ],
        "rows": rows,
        "feature_credit_requires_all_rows_executable": False,
        "pnl_credit_requires_row_level_execution_supported": True,
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }
    payload["seal_sha256"] = _canonical_sha256(payload)
    validate_seal(payload)
    return payload


def validate_seal(payload: Mapping[str, Any]) -> dict[str, Any]:
    v21.load_protocol()
    v21.load_evaluator_contract()
    seal = dict(payload)
    if any(key in FORBIDDEN_KEYS for key in _walk_keys(seal)):
        raise ValueError("v21_feature_seal_contains_outcome_field")
    expected_sizes = {
        "TRAIN": identity.TRAIN_CLOSE_WINDOWS,
        "PROBABILITY_CALIBRATION": identity.PROBABILITY_CALIBRATION_CLOSE_WINDOWS,
        "EXECUTION_POLICY_SELECTION": identity.EXECUTION_POLICY_SELECTION_CLOSE_WINDOWS,
        "UNTOUCHED_TEST": identity.UNTOUCHED_TEST_CLOSE_WINDOWS,
    }
    partitions = dict(seal.get("partition_windows") or {})
    if (
        seal.get("seal_version") != SEAL_VERSION
        or seal.get("protocol_id") != identity.PROTOCOL_ID
        or seal.get("protocol_sha256") != identity.PROTOCOL_SHA256
        or seal.get("evaluator_contract_id") != identity.EVALUATOR_CONTRACT_ID
        or seal.get("evaluator_contract_sha256") != identity.EVALUATOR_CONTRACT_SHA256
        or seal.get("feature_builder_version") != identity.FEATURE_BUILDER_VERSION
        or int(seal.get("feature_count") or 0) != identity.FEATURE_COUNT
        or seal.get("feature_names_sha256") != identity.FEATURE_NAMES_SHA256
        or int(seal.get("selected_complete_close_windows") or 0)
        != identity.MINIMUM_COMPLETE_CLOSE_WINDOWS
        or int(seal.get("selected_rows") or 0)
        != identity.MINIMUM_COMPLETE_CLOSE_WINDOWS * 7
        or set(partitions) != set(expected_sizes)
        or any(len(partitions[name]) != size for name, size in expected_sizes.items())
        or seal.get("feature_credit_requires_all_rows_executable") is not False
        or seal.get("pnl_credit_requires_row_level_execution_supported") is not True
        or any(seal.get(key) is not False for key in (
            "outcome_columns_selected", "outcome_labels_read", "model_fit_performed",
            "probability_scoring_performed", "paper_artifact_created",
            "notification_eligible", "automatic_promotion", "real_trading_allowed",
        ))
    ):
        raise ValueError("v21_feature_seal_identity_or_safety_invalid")
    ordered_partitions = (
        "TRAIN", "PROBABILITY_CALIBRATION", "EXECUTION_POLICY_SELECTION",
        "UNTOUCHED_TEST",
    )
    all_closes = [
        float(close) for name in ordered_partitions for close in partitions[name]
    ]
    if (
        all_closes != sorted(all_closes)
        or len(set(all_closes)) != identity.MINIMUM_COMPLETE_CLOSE_WINDOWS
        or any(close <= identity.PROSPECTIVE_AFTER_CLOSE_TIME for close in all_closes)
    ):
        raise ValueError("v21_feature_seal_partition_chronology_invalid")
    partition_by_close = {
        float(close): name for name, closes in partitions.items() for close in closes
    }
    rows = list(seal.get("rows") or ())
    rows_by_close: dict[float, list[Mapping[str, Any]]] = defaultdict(list)
    parent_ids = set()
    intermediate_ids = set()
    delayed_ids = set()
    for row in rows:
        close_time = float(row["close_time"])
        rows_by_close[close_time].append(row)
        values = list(row.get("features") or ())
        execution_supported = row.get("execution_supported")
        if (
            row.get("partition") != partition_by_close.get(close_time)
            or len(values) != identity.FEATURE_COUNT
            or any(
                not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(float(value)) for value in values
            )
            or not isinstance(execution_supported, bool)
            or row.get("sim_contracts") != 10.0
            or str(row.get("cohort") or "") != (
                "BTC" if str(row.get("asset") or "") == "BTC" else "NON_BTC_TRANSFER"
            )
            or validate_exact_contract_identity(row).get("valid") is not True
            or any(len(str(row.get(key) or "")) != 64 for key in (
                "intermediate_source_evidence_sha256",
                "delayed_source_evidence_sha256",
                "feature_evidence_sha256",
                "source_feature_evidence_sha256",
                "matched_v18_feature_evidence_sha256",
                "matched_v19_feature_evidence_sha256",
                "v20_base_feature_evidence_sha256",
                "matched_benchmark_evidence_sha256",
            ))
        ):
            raise ValueError("v21_feature_seal_row_identity_invalid")
        feature_hash = _canonical_sha256({
            "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
            "parent_id": row["parent_id"],
            "intermediate_id": row["intermediate_id"],
            "delayed_id": row["delayed_id"],
            "asset": row["asset"],
            "ticker": row["ticker"],
            "close_time": row["close_time"],
            "side": row["side"],
            "base_feature_evidence_sha256": row["v20_base_feature_evidence_sha256"],
            "feature_names": list(v21_features.FEATURE_NAMES),
            "features": values,
        })
        source_hash = _canonical_sha256({
            "parent_id": row["parent_id"],
            "intermediate_id": row["intermediate_id"],
            "delayed_id": row["delayed_id"],
            "asset": row["asset"],
            "cohort": row["cohort"],
            "ticker": row["ticker"],
            "close_time": row["close_time"],
            "side": row["side"],
            "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
            "feature_evidence_sha256": feature_hash,
            "base_feature_evidence_sha256": row["v20_base_feature_evidence_sha256"],
            "intermediate_source_evidence_sha256": row[
                "intermediate_source_evidence_sha256"
            ],
            "delayed_source_evidence_sha256": row[
                "delayed_source_evidence_sha256"
            ],
            "feature_count": identity.FEATURE_COUNT,
            "execution_supported": execution_supported,
            "entry_ask_cents": row["entry_ask_cents"],
            "spread_cents": row["spread_cents"],
            "depth_contracts": row["depth_contracts"],
            "sim_contracts": row["sim_contracts"],
        })
        benchmark_hash = _canonical_sha256({
            "matched_v18_eligible": row["matched_v18_eligible"],
            "matched_v18_feature_evidence_sha256": row[
                "matched_v18_feature_evidence_sha256"
            ],
            "matched_v19_eligible": row["matched_v19_eligible"],
            "matched_v19_feature_evidence_sha256": row[
                "matched_v19_feature_evidence_sha256"
            ],
            "v20_base_feature_evidence_sha256": row[
                "v20_base_feature_evidence_sha256"
            ],
        })
        if (
            row.get("feature_evidence_sha256") != feature_hash
            or row.get("source_feature_evidence_sha256") != source_hash
            or row.get("matched_benchmark_evidence_sha256") != benchmark_hash
        ):
            raise ValueError("v21_feature_seal_row_hash_mismatch")
        ids = (int(row["parent_id"]), int(row["intermediate_id"]), int(row["delayed_id"]))
        if ids[0] in parent_ids or ids[1] in intermediate_ids or ids[2] in delayed_ids:
            raise ValueError("v21_feature_seal_duplicate_row_identity")
        parent_ids.add(ids[0])
        intermediate_ids.add(ids[1])
        delayed_ids.add(ids[2])
    if set(rows_by_close) != set(all_closes) or any(
        len(window) != 7 or {str(row["asset"]) for row in window} != ASSETS
        for window in rows_by_close.values()
    ):
        raise ValueError("v21_feature_seal_row_geometry_invalid")
    expected_sha = _canonical_sha256(_seal_core(seal))
    if seal.get("seal_sha256") != expected_sha:
        raise ValueError("v21_feature_seal_hash_mismatch")
    return {
        "valid": True,
        "seal_sha256": expected_sha,
        "selected_complete_close_windows": len(rows_by_close),
        "selected_rows": len(rows),
        "executable_rows": sum(row["execution_supported"] is True for row in rows),
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
    }


def create_or_validate_seal(candidate: Mapping[str, Any], output: Path) -> dict[str, Any]:
    validate_seal(candidate)
    if output.exists():
        try:
            existing = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("v21_existing_feature_seal_unreadable") from exc
        validate_seal(existing)
        if _canonical_json(existing) != _canonical_json(candidate):
            raise ValueError("v21_exclusive_feature_seal_conflict")
        return {"created": False, "path": str(output), **validate_seal(existing)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(candidate, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {"created": True, "path": str(output), **validate_seal(candidate)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-db", default=str(DEFAULT_DB))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    db_path = Path(args.strategy_db)
    parents = load_feature_rows_after(db_path, identity.PROSPECTIVE_AFTER_CLOSE_TIME)
    trajectory = load_trajectory_feature_rows_after(
        db_path, identity.PROSPECTIVE_AFTER_CLOSE_TIME
    )
    complete, excluded = collect_feature_windows(parents, trajectory)
    preview = {
        "status": (
            "READY_FOR_EXCLUSIVE_FEATURE_SEAL"
            if len(complete) >= identity.MINIMUM_COMPLETE_CLOSE_WINDOWS
            else "COLLECTING_V21_PROSPECTIVE_FEATURES_NO_OUTCOMES"
        ),
        "complete_close_windows": len(complete),
        "complete_close_windows_remaining": max(
            0, identity.MINIMUM_COMPLETE_CLOSE_WINDOWS - len(complete)
        ),
        "excluded_feature_incomplete_windows": len(excluded),
        "confirmation_required": CONFIRMATION,
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "real_trading_allowed": False,
    }
    if len(complete) < identity.MINIMUM_COMPLETE_CLOSE_WINDOWS:
        print(json.dumps(preview, indent=2, sort_keys=True))
        return
    candidate = build_seal(complete, excluded_windows=excluded)
    if args.confirm != CONFIRMATION:
        print(json.dumps({
            **preview,
            "candidate_seal_sha256": candidate["seal_sha256"],
            "write_performed": False,
        }, indent=2, sort_keys=True))
        return
    print(json.dumps(
        create_or_validate_seal(candidate, Path(args.output)),
        indent=2, sort_keys=True,
    ))


if __name__ == "__main__":
    main()
