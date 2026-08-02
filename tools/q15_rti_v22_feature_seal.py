"""Exclusive outcome-blind feature seal for the earliest 180 V22 windows."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import rti_microstructure_v22 as v22  # noqa: E402
from q15_upgrade.strategy_bots import rti_microstructure_v22_identity as identity  # noqa: E402
from q15_upgrade.strategy_bots import rti_microstructure_v22_top_book_features as features  # noqa: E402
from q15_upgrade.strategy_bots import rti_spot_rest_top_book_reservoir_identity as rest_identity  # noqa: E402
from tools.q15_rti_independent_path_audit import validate_exact_contract_identity  # noqa: E402
from tools.q15_rti_microstructure_freeze import load_feature_rows_after  # noqa: E402
from tools.q15_rti_spot_rest_top_book_readiness import load_rows as load_rest_rows  # noqa: E402
from tools.q15_rti_v17_development_seal import DEFAULT_DB as STRATEGY_DB  # noqa: E402
from tools.q15_rti_v21_readiness import load_trajectory_feature_rows_after  # noqa: E402
from tools.q15_rti_v22_readiness import collect_feature_windows  # noqa: E402


DEFAULT_OUTPUT = ROOT / "reports" / "q15_rti_v22_feature_seal.json"
CONFIRMATION = "SEAL_V22_EARLIEST_180_FEATURES_NO_LABELS"
SEAL_VERSION = "q15-rti-v22-earliest-180-outcome-blind-feature-seal-v1"
ASSETS = frozenset(rest_identity.SOURCE_IDENTITIES)
PARTITIONS = (
    ("TRAIN", 0, 104),
    ("PROBABILITY_CALIBRATION", 105, 129),
    ("EXECUTION_POLICY_SELECTION", 130, 154),
    ("UNTOUCHED_TEST", 155, 179),
)
FORBIDDEN_KEYS = frozenset({
    "outcome", "result", "result_yes", "resolved", "correct", "settlement",
    "settlement_result", "pnl", "profit", "label", "label_survives",
})


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _walk_keys(value: Any):
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key).lower()
            yield from _walk_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_keys(child)


def _core(seal: Mapping[str, Any]) -> dict[str, Any]:
    output = dict(seal)
    output.pop("seal_sha256", None)
    return output


def _partition(index: int) -> str:
    for name, first, last in PARTITIONS:
        if first <= index <= last:
            return name
    raise ValueError("v22_feature_seal_partition_index_invalid")


def _sealed_row(row: Mapping[str, Any], partition: str) -> dict[str, Any]:
    asset = str(row["asset"])
    return {
        "partition": partition,
        "parent_id": int(row["parent_id"]),
        "intermediate_id": int(row["intermediate_id"]),
        "delayed_id": int(row["delayed_id"]),
        "asset": asset,
        "cohort": "BTC" if asset == "BTC" else "NON_BTC_TRANSFER",
        "ticker": str(row["ticker"]),
        "close_time": float(row["close_time"]),
        "side": str(row["side"]),
        "execution_supported": row["execution_supported"] is True,
        "entry_ask_cents": float(row["entry_ask_cents"]),
        "spread_cents": float(row["spread_cents"]),
        "depth_contracts": float(row["depth_contracts"]),
        "sim_contracts": float(row["sim_contracts"]),
        "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
        "feature_names_sha256": identity.FEATURE_NAMES_SHA256,
        "feature_evidence_sha256": str(row["feature_evidence_sha256"]),
        "parent_source_evidence_sha256": str(
            row["parent_source_evidence_sha256"]
        ),
        "intermediate_source_evidence_sha256": str(
            row["intermediate_source_evidence_sha256"]
        ),
        "delayed_source_evidence_sha256": str(
            row["delayed_source_evidence_sha256"]
        ),
        "rest_evidence_sha256_by_stage": dict(
            row["rest_evidence_sha256_by_stage"]
        ),
        "matched_frozen_v21_eligible": (
            row["matched_frozen_v21_eligible"] is True
        ),
        "matched_frozen_v21_source_feature_evidence_sha256": str(
            row.get("matched_frozen_v21_source_feature_evidence_sha256") or ""
        ),
        "replaced_spot_source_failures": list(
            row["replaced_spot_source_failures"]
        ),
        "features": [float(value) for value in row["features"]],
    }


def build_seal(
    complete_windows: Sequence[Mapping[str, Any]],
    *, excluded_windows: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    v22.load_protocol()
    v22.load_evaluator_contract()
    ordered = sorted(
        (dict(window) for window in complete_windows),
        key=lambda window: float(window["close_time"]),
    )
    if len(ordered) < identity.MINIMUM_COMPLETE_COMMON_CLOSE_WINDOWS:
        raise ValueError("v22_feature_seal_insufficient_complete_windows")
    selected = ordered[:identity.MINIMUM_COMPLETE_COMMON_CLOSE_WINDOWS]
    closes = [float(window["close_time"]) for window in selected]
    if (
        closes != sorted(closes) or len(set(closes)) != len(closes)
        or closes[0] < identity.FIRST_ELIGIBLE_COMMON_CLOSE_TIME
    ):
        raise ValueError("v22_feature_seal_chronology_invalid")
    rows = []
    for index, window in enumerate(selected):
        window_rows = sorted(
            (dict(row) for row in window["rows"]), key=lambda row: row["asset"],
        )
        if len(window_rows) != 7 or {row["asset"] for row in window_rows} != ASSETS:
            raise ValueError("v22_feature_seal_window_geometry_invalid")
        rows.extend(_sealed_row(row, _partition(index)) for row in window_rows)
    partition_closes = {
        name: closes[first:last + 1] for name, first, last in PARTITIONS
    }
    excluded = [
        {
            "close_time": float(window["close_time"]),
            "failure_counts": dict(window.get("failure_counts") or {}),
        }
        for window in excluded_windows
        if float(window.get("close_time") or 0.0) <= closes[-1]
    ]
    seal = {
        "seal_version": SEAL_VERSION,
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "evaluator_contract_id": identity.EVALUATOR_CONTRACT_ID,
        "evaluator_contract_sha256": identity.EVALUATOR_CONTRACT_SHA256,
        "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
        "feature_count": identity.FEATURE_COUNT,
        "feature_names": list(features.FEATURE_NAMES),
        "feature_names_sha256": identity.FEATURE_NAMES_SHA256,
        "rest_protocol_id": rest_identity.PROTOCOL_ID,
        "rest_protocol_sha256": rest_identity.PROTOCOL_SHA256,
        "first_eligible_common_close_time": identity.FIRST_ELIGIBLE_COMMON_CLOSE_TIME,
        "selected_complete_close_windows": len(selected),
        "selected_feature_rows": len(rows),
        "selected_first_close_time": closes[0],
        "selected_last_close_time": closes[-1],
        "partitions": partition_closes,
        "excluded_preselection_windows": excluded,
        "rows": rows,
        "selected_feature_evidence_rollup_sha256": _sha256([
            [row["parent_id"], row["feature_evidence_sha256"]] for row in rows
        ]),
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }
    seal["seal_sha256"] = _sha256(_core(seal))
    validate_seal(seal)
    return seal


def validate_seal(seal: Mapping[str, Any]) -> dict[str, Any]:
    v22.load_protocol()
    v22.load_evaluator_contract()
    if FORBIDDEN_KEYS & set(_walk_keys(seal)):
        raise ValueError("v22_feature_seal_outcome_or_label_forbidden")
    partitions = dict(seal.get("partitions") or {})
    expected_partition_names = {name for name, _first, _last in PARTITIONS}
    if (
        seal.get("seal_version") != SEAL_VERSION
        or seal.get("protocol_id") != identity.PROTOCOL_ID
        or seal.get("protocol_sha256") != identity.PROTOCOL_SHA256
        or seal.get("evaluator_contract_id") != identity.EVALUATOR_CONTRACT_ID
        or seal.get("evaluator_contract_sha256")
        != identity.EVALUATOR_CONTRACT_SHA256
        or seal.get("feature_builder_version") != identity.FEATURE_BUILDER_VERSION
        or seal.get("feature_count") != identity.FEATURE_COUNT
        or seal.get("feature_names") != list(features.FEATURE_NAMES)
        or seal.get("feature_names_sha256") != identity.FEATURE_NAMES_SHA256
        or seal.get("rest_protocol_id") != rest_identity.PROTOCOL_ID
        or seal.get("rest_protocol_sha256") != rest_identity.PROTOCOL_SHA256
        or seal.get("first_eligible_common_close_time")
        != identity.FIRST_ELIGIBLE_COMMON_CLOSE_TIME
        or seal.get("selected_complete_close_windows") != 180
        or seal.get("selected_feature_rows") != 1260
        or set(partitions) != expected_partition_names
        or any(seal.get(key) is not False for key in (
            "outcome_columns_selected", "outcome_labels_read",
            "model_fit_performed", "probability_scoring_performed",
            "paper_artifact_created", "notification_eligible",
            "automatic_promotion", "real_trading_allowed",
        ))
    ):
        raise ValueError("v22_feature_seal_identity_invalid")
    all_closes = []
    for name, first, last in PARTITIONS:
        values = [float(value) for value in partitions[name]]
        if len(values) != last - first + 1:
            raise ValueError("v22_feature_seal_partition_geometry_invalid")
        all_closes.extend(values)
    if (
        all_closes != sorted(all_closes)
        or len(set(all_closes)) != 180
        or all_closes[0] < identity.FIRST_ELIGIBLE_COMMON_CLOSE_TIME
        or seal.get("selected_first_close_time") != all_closes[0]
        or seal.get("selected_last_close_time") != all_closes[-1]
    ):
        raise ValueError("v22_feature_seal_partition_chronology_invalid")
    rows = list(seal.get("rows") or ())
    rows_by_close: Counter[float] = Counter()
    assets_by_close: dict[float, set[str]] = {}
    parent_ids = set()
    intermediate_ids = set()
    delayed_ids = set()
    for row in rows:
        values = list(row.get("features") or ())
        close = float(row["close_time"])
        asset = str(row["asset"])
        rows_by_close[close] += 1
        assets_by_close.setdefault(close, set()).add(asset)
        ids = (
            int(row["parent_id"]), int(row["intermediate_id"]),
            int(row["delayed_id"]),
        )
        expected_partition = _partition(all_closes.index(close))
        rest_hashes = dict(row.get("rest_evidence_sha256_by_stage") or {})
        matched = row.get("matched_frozen_v21_eligible")
        matched_hash = str(
            row.get("matched_frozen_v21_source_feature_evidence_sha256") or ""
        )
        replaced_failures = row.get("replaced_spot_source_failures")
        if (
            row.get("partition") != expected_partition
            or len(values) != identity.FEATURE_COUNT
            or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) for value in values
            )
            or row.get("cohort")
            != ("BTC" if asset == "BTC" else "NON_BTC_TRANSFER")
            or row.get("sim_contracts") != 10.0
            or not isinstance(row.get("execution_supported"), bool)
            or set(rest_hashes) != set(features.STAGES)
            or any(len(str(value or "")) != 64 for value in rest_hashes.values())
            or not isinstance(matched, bool)
            or (matched and len(matched_hash) != 64)
            or (not matched and matched_hash and len(matched_hash) != 64)
            or not isinstance(replaced_failures, list)
            or not set(str(value) for value in replaced_failures).issubset(
                features.ALLOWED_REPLACED_SPOT_SOURCE_FAILURES
            )
            or any(len(str(row.get(key) or "")) != 64 for key in (
                "feature_evidence_sha256", "parent_source_evidence_sha256",
                "intermediate_source_evidence_sha256",
                "delayed_source_evidence_sha256",
            ))
            or validate_exact_contract_identity(row).get("valid") is not True
            or min(ids) <= 0 or len(set(ids)) != 3
            or ids[0] in parent_ids or ids[1] in intermediate_ids
            or ids[2] in delayed_ids
        ):
            raise ValueError("v22_feature_seal_row_identity_invalid")
        parent_ids.add(ids[0])
        intermediate_ids.add(ids[1])
        delayed_ids.add(ids[2])
        evidence_core = {
            "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
            "parent_id": ids[0],
            "asset": asset,
            "ticker": row["ticker"],
            "close_time": close,
            "side": row["side"],
            "parent_source_evidence_sha256": row[
                "parent_source_evidence_sha256"
            ],
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
            "features": values,
        }
        if row["feature_evidence_sha256"] != _sha256(evidence_core):
            raise ValueError("v22_feature_seal_row_hash_mismatch")
    if (
        set(rows_by_close) != set(all_closes)
        or any(rows_by_close[close] != 7 for close in all_closes)
        or any(assets_by_close[close] != ASSETS for close in all_closes)
        or seal.get("selected_feature_evidence_rollup_sha256") != _sha256([
            [row["parent_id"], row["feature_evidence_sha256"]] for row in rows
        ])
        or seal.get("seal_sha256") != _sha256(_core(seal))
    ):
        raise ValueError("v22_feature_seal_hash_or_geometry_invalid")
    return {
        "valid": True,
        "seal_sha256": seal["seal_sha256"],
        "selected_complete_close_windows": 180,
        "selected_feature_rows": 1260,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "real_trading_allowed": False,
    }


def create_or_validate_seal(
    candidate: Mapping[str, Any], output: Path,
) -> dict[str, Any]:
    validate_seal(candidate)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
        result = validate_seal(existing)
        if existing.get("seal_sha256") != candidate.get("seal_sha256"):
            raise ValueError("v22_feature_seal_existing_candidate_mismatch")
        return {"created": False, "path": str(output), **result}
    payload = json.dumps(candidate, indent=2, sort_keys=True) + "\n"
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            output.unlink(missing_ok=True)
        finally:
            raise
    return {"created": True, "path": str(output), **validate_seal(candidate)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-db", default=str(STRATEGY_DB))
    parser.add_argument("--rest-db", default=str(
        ROOT / rest_identity.DATABASE_RELATIVE_PATH
    ))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--confirmation", default="")
    args = parser.parse_args()
    strategy_db = Path(args.strategy_db)
    parents = load_feature_rows_after(
        strategy_db, rest_identity.PROSPECTIVE_AFTER_CLOSE_TIME,
    )
    trajectory = load_trajectory_feature_rows_after(
        strategy_db, rest_identity.PROSPECTIVE_AFTER_CLOSE_TIME,
    )
    complete, excluded, rest_failures = collect_feature_windows(
        parents, trajectory, load_rest_rows(Path(args.rest_db)),
    )
    if len(complete) < identity.MINIMUM_COMPLETE_COMMON_CLOSE_WINDOWS:
        print(json.dumps({
            **v22.status(),
            "complete_close_windows": len(complete),
            "remaining": identity.MINIMUM_COMPLETE_COMMON_CLOSE_WINDOWS
            - len(complete),
            "rest_quality_failure_counts": dict(sorted(rest_failures.items())),
            "feature_seal_created": False,
            "confirmation_required": CONFIRMATION,
            "status": "AWAITING_180_COMPLETE_V22_WINDOWS_NO_LABELS_OPENED",
        }, indent=2, sort_keys=True))
        return
    if args.confirmation != CONFIRMATION:
        raise ValueError("v22_feature_seal_exact_confirmation_required")
    candidate = build_seal(complete, excluded_windows=excluded)
    print(json.dumps(
        create_or_validate_seal(candidate, Path(args.output)),
        indent=2, sort_keys=True,
    ))


if __name__ == "__main__":
    main()
