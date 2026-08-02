"""Exclusive, outcome-blind earliest-150 feature seal for frozen V20."""
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
from q15_upgrade.strategy_bots import rti_microstructure_v20 as v20
from q15_upgrade.strategy_bots import rti_microstructure_v20_features as v20_features
from q15_upgrade.strategy_bots import rti_microstructure_v20_identity as identity
from tools.q15_rti_microstructure_freeze import load_feature_rows_after
from tools.q15_rti_independent_path_audit import validate_exact_contract_identity
from tools.q15_rti_v17_development_seal import DEFAULT_DB
from tools.q15_rti_v18_prospective_seal import _complete_windows
from tools.q15_rti_v19_readiness import (
    _linked_parent_id,
    load_delayed_feature_rows_after,
)


SEAL_VERSION = "q15-rti-v20-exclusive-feature-seal-v2"
CONFIRMATION = "CREATE_V20_EXCLUSIVE_FEATURE_SEAL_ONCE"
DEFAULT_OUTPUT = ROOT / "reports" / "q15_rti_v20_audit" / "feature_seal.json"
ASSETS = frozenset({"BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP"})
FORBIDDEN_KEYS = frozenset({
    "official_result",
    "resolved_at",
    "correct",
    "hypothetical_pnl_cents",
    "outcome_label",
    "settlement_result",
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
            yield str(key)
            yield from _walk_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_keys(child)


def _seal_core(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in payload.items() if key != "seal_sha256"
    }


def collect_feature_windows(
    parent_rows: Sequence[Mapping[str, Any]],
    delayed_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collect complete point-in-time windows without selecting a label."""
    v20.load_protocol()
    parents = {
        close: rows
        for close, rows in _complete_windows(parent_rows).items()
        if close > identity.PROSPECTIVE_AFTER_CLOSE_TIME
    }
    delayed_by_parent: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in delayed_rows:
        if float(row.get("close_time") or 0.0) > identity.PROSPECTIVE_AFTER_CLOSE_TIME:
            delayed_by_parent[_linked_parent_id(row)].append(row)
    complete = []
    excluded = []
    for close_time, parent_window in sorted(parents.items()):
        window_rows = []
        failures = []
        for parent in sorted(
            parent_window, key=lambda row: str(row.get("asset") or "")
        ):
            matches = delayed_by_parent.get(int(parent["id"]), [])
            if len(matches) != 1:
                failures.append({
                    "asset": parent.get("asset"),
                    "parent_id": parent.get("id"),
                    "reason": (
                        "DELAYED_PAIR_MISSING"
                        if not matches
                        else "DELAYED_PAIR_DUPLICATE"
                    ),
                })
                continue
            delayed = matches[0]
            feature_result = v20.evaluate_pair(parent, delayed)
            source_result = v19.evaluate_delayed_source(parent, delayed)
            if (
                feature_result.get("eligible_for_v20_feature_credit")
                is not True
                or source_result.get("available") is not True
            ):
                failures.append({
                    "asset": parent.get("asset"),
                    "parent_id": parent.get("id"),
                    "delayed_id": delayed.get("id"),
                    "reason": "V20_FEATURE_SOURCE_INCOMPLETE",
                    "failures": list(feature_result.get("failures") or ())
                    + list(source_result.get("failures") or ()),
                })
                continue
            feature_evidence = dict(feature_result["evidence"])
            execution = dict(source_result["evidence"])
            v18_result = v18.evaluate_row(parent)
            v19_result = v19.evaluate_pair(parent, delayed)
            benchmark_evidence = {
                "matched_v18_eligible": v18_result.get("eligible") is True,
                "matched_v18_feature_evidence_sha256": v18_result[
                    "feature_evidence_sha256"
                ],
                "matched_v19_eligible": v19_result.get("eligible") is True,
                "matched_v19_feature_evidence_sha256": v19_result[
                    "feature_evidence_sha256"
                ],
            }
            window_rows.append({
                "parent_id": int(feature_evidence["parent_id"]),
                "delayed_id": int(feature_evidence["delayed_id"]),
                "asset": str(feature_evidence["asset"]),
                "cohort": str(feature_evidence["cohort"]),
                "ticker": str(feature_evidence["ticker"]),
                "close_time": float(feature_evidence["close_time"]),
                "side": str(feature_evidence["side"]),
                "entry_ask_cents": execution["entry_ask_cents"],
                "spread_cents": execution["spread_cents"],
                "depth_contracts": execution["depth_contracts"],
                "sim_contracts": execution["sim_contracts"],
                "sim_full_fill_supported": execution[
                    "sim_full_fill_supported"
                ],
                "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
                "feature_names_sha256": identity.FEATURE_NAMES_SHA256,
                "delayed_source_evidence_sha256": source_result[
                    "feature_evidence_sha256"
                ],
                "feature_evidence_sha256": feature_evidence[
                    "feature_evidence_sha256"
                ],
                "source_feature_evidence_sha256": feature_result[
                    "source_feature_evidence_sha256"
                ],
                **benchmark_evidence,
                "matched_benchmark_evidence_sha256": _canonical_sha256(
                    benchmark_evidence
                ),
                "features": list(feature_result["features"]),
            })
        if (
            not failures
            and len(window_rows) == 7
            and {row["asset"] for row in window_rows} == ASSETS
        ):
            complete.append({
                "close_time": float(close_time),
                "rows": window_rows,
            })
        else:
            excluded.append({
                "close_time": float(close_time),
                "feature_quality_failures": failures,
            })
    return complete, excluded


def build_seal(
    complete_windows: Sequence[Mapping[str, Any]],
    *,
    excluded_windows: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Seal the exclusive earliest 150 complete close clusters."""
    v20.load_protocol()
    ordered = sorted(
        (dict(window) for window in complete_windows),
        key=lambda window: float(window["close_time"]),
    )
    if len(ordered) < identity.MINIMUM_COMPLETE_CLOSE_WINDOWS:
        raise ValueError("v20_feature_seal_not_ready")
    selected = ordered[:identity.MINIMUM_COMPLETE_CLOSE_WINDOWS]
    rows = []
    partition_windows = {
        "TRAIN": [],
        "CALIBRATION": [],
        "UNTOUCHED_TEST": [],
    }
    for index, window in enumerate(selected):
        if index < identity.TRAIN_CLOSE_WINDOWS:
            partition = "TRAIN"
        elif index < (
            identity.TRAIN_CLOSE_WINDOWS
            + identity.CALIBRATION_CLOSE_WINDOWS
        ):
            partition = "CALIBRATION"
        else:
            partition = "UNTOUCHED_TEST"
        close_time = float(window["close_time"])
        partition_windows[partition].append(close_time)
        window_rows = sorted(
            (dict(row) for row in window["rows"]),
            key=lambda row: str(row["asset"]),
        )
        if (
            len(window_rows) != 7
            or {str(row["asset"]) for row in window_rows} != ASSETS
        ):
            raise ValueError("v20_feature_seal_window_geometry_invalid")
        for row in window_rows:
            if float(row["close_time"]) != close_time:
                raise ValueError("v20_feature_seal_cross_close_row")
            rows.append({**row, "partition": partition})
    payload = {
        "seal_version": SEAL_VERSION,
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
        "feature_count": identity.FEATURE_COUNT,
        "feature_names_sha256": identity.FEATURE_NAMES_SHA256,
        "prospective_after_close_time": identity.PROSPECTIVE_AFTER_CLOSE_TIME,
        "first_eligible_close_time": identity.FIRST_ELIGIBLE_CLOSE_TIME,
        "exclusive_selection": "EARLIEST_150_COMPLETE_SEVEN_ASSET_CLOSE_WINDOWS",
        "selected_complete_close_windows": len(selected),
        "selected_rows": len(rows),
        "first_selected_close_time": float(selected[0]["close_time"]),
        "last_selected_close_time": float(selected[-1]["close_time"]),
        "partition_windows": partition_windows,
        "excluded_feature_incomplete_windows_before_selection": [
            dict(window) for window in excluded_windows
            if float(window.get("close_time") or 0.0)
            <= float(selected[-1]["close_time"])
        ],
        "rows": rows,
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
    """Fail closed on identity, partition, feature, or safety mutation."""
    v20.load_protocol()
    seal = dict(payload)
    if any(key in FORBIDDEN_KEYS for key in _walk_keys(seal)):
        raise ValueError("v20_feature_seal_contains_outcome_field")
    if (
        seal.get("seal_version") != SEAL_VERSION
        or seal.get("protocol_id") != identity.PROTOCOL_ID
        or seal.get("protocol_sha256") != identity.PROTOCOL_SHA256
        or seal.get("feature_builder_version")
        != identity.FEATURE_BUILDER_VERSION
        or int(seal.get("feature_count") or 0) != identity.FEATURE_COUNT
        or seal.get("feature_names_sha256") != identity.FEATURE_NAMES_SHA256
        or int(seal.get("selected_complete_close_windows") or 0)
        != identity.MINIMUM_COMPLETE_CLOSE_WINDOWS
        or int(seal.get("selected_rows") or 0)
        != identity.MINIMUM_COMPLETE_CLOSE_WINDOWS * 7
        or seal.get("outcome_columns_selected") is not False
        or seal.get("outcome_labels_read") is not False
        or seal.get("model_fit_performed") is not False
        or seal.get("probability_scoring_performed") is not False
        or seal.get("paper_artifact_created") is not False
        or seal.get("notification_eligible") is not False
        or seal.get("automatic_promotion") is not False
        or seal.get("real_trading_allowed") is not False
    ):
        raise ValueError("v20_feature_seal_identity_or_safety_invalid")
    partitions = dict(seal.get("partition_windows") or {})
    expected_partition_sizes = {
        "TRAIN": identity.TRAIN_CLOSE_WINDOWS,
        "CALIBRATION": identity.CALIBRATION_CLOSE_WINDOWS,
        "UNTOUCHED_TEST": identity.UNTOUCHED_TEST_CLOSE_WINDOWS,
    }
    if (
        set(partitions) != set(expected_partition_sizes)
        or any(
            len(partitions[name]) != size
            for name, size in expected_partition_sizes.items()
        )
    ):
        raise ValueError("v20_feature_seal_partition_sizes_invalid")
    all_partition_closes = [
        float(close)
        for name in ("TRAIN", "CALIBRATION", "UNTOUCHED_TEST")
        for close in partitions[name]
    ]
    if (
        all_partition_closes != sorted(all_partition_closes)
        or len(set(all_partition_closes))
        != identity.MINIMUM_COMPLETE_CLOSE_WINDOWS
        or any(
            close <= identity.PROSPECTIVE_AFTER_CLOSE_TIME
            for close in all_partition_closes
        )
    ):
        raise ValueError("v20_feature_seal_partition_chronology_invalid")
    rows = list(seal.get("rows") or ())
    rows_by_close: dict[float, list[Mapping[str, Any]]] = defaultdict(list)
    parent_ids = set()
    delayed_ids = set()
    partition_by_close = {
        float(close): partition
        for partition, closes in partitions.items()
        for close in closes
    }
    for row in rows:
        close_time = float(row["close_time"])
        rows_by_close[close_time].append(row)
        if row.get("partition") != partition_by_close.get(close_time):
            raise ValueError("v20_feature_seal_cross_partition_row")
        features = list(row.get("features") or ())
        if (
            len(features) != identity.FEATURE_COUNT
            or any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                for value in features
            )
            or len(str(row.get("feature_evidence_sha256") or "")) != 64
            or len(str(row.get("delayed_source_evidence_sha256") or ""))
            != 64
            or len(str(row.get("source_feature_evidence_sha256") or ""))
            != 64
            or len(str(row.get("matched_v18_feature_evidence_sha256") or ""))
            != 64
            or len(str(row.get("matched_v19_feature_evidence_sha256") or ""))
            != 64
            or len(str(row.get("matched_benchmark_evidence_sha256") or ""))
            != 64
            or not isinstance(row.get("matched_v18_eligible"), bool)
            or not isinstance(row.get("matched_v19_eligible"), bool)
            or row.get("sim_full_fill_supported") is not True
            or str(row.get("cohort") or "")
            != (
                "BTC"
                if str(row.get("asset") or "") == "BTC"
                else "NON_BTC_TRANSFER"
            )
            or validate_exact_contract_identity(row).get("valid") is not True
        ):
            raise ValueError("v20_feature_seal_row_identity_invalid")
        expected_feature_hash = _canonical_sha256({
            "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
            "parent_id": row["parent_id"],
            "delayed_id": row["delayed_id"],
            "asset": row["asset"],
            "ticker": row["ticker"],
            "close_time": row["close_time"],
            "side": row["side"],
            "source_feature_evidence_sha256": row[
                "delayed_source_evidence_sha256"
            ],
            "feature_names": list(v20_features.FEATURE_NAMES),
            "features": features,
        })
        expected_source_hash = _canonical_sha256({
            "parent_id": row["parent_id"],
            "delayed_id": row["delayed_id"],
            "asset": row["asset"],
            "cohort": row["cohort"],
            "ticker": row["ticker"],
            "close_time": row["close_time"],
            "side": row["side"],
            "feature_builder_version": identity.FEATURE_BUILDER_VERSION,
            "feature_evidence_sha256": expected_feature_hash,
            "feature_count": identity.FEATURE_COUNT,
        })
        expected_benchmark_hash = _canonical_sha256({
            "matched_v18_eligible": row["matched_v18_eligible"],
            "matched_v18_feature_evidence_sha256": row[
                "matched_v18_feature_evidence_sha256"
            ],
            "matched_v19_eligible": row["matched_v19_eligible"],
            "matched_v19_feature_evidence_sha256": row[
                "matched_v19_feature_evidence_sha256"
            ],
        })
        if (
            row.get("feature_evidence_sha256") != expected_feature_hash
            or row.get("source_feature_evidence_sha256")
            != expected_source_hash
            or row.get("matched_benchmark_evidence_sha256")
            != expected_benchmark_hash
        ):
            raise ValueError("v20_feature_seal_row_hash_mismatch")
        parent_id = int(row["parent_id"])
        delayed_id = int(row["delayed_id"])
        if parent_id in parent_ids or delayed_id in delayed_ids:
            raise ValueError("v20_feature_seal_duplicate_row_identity")
        parent_ids.add(parent_id)
        delayed_ids.add(delayed_id)
    if set(rows_by_close) != set(all_partition_closes) or any(
        len(window) != 7
        or {str(row["asset"]) for row in window} != ASSETS
        for window in rows_by_close.values()
    ):
        raise ValueError("v20_feature_seal_row_geometry_invalid")
    expected_sha = _canonical_sha256(_seal_core(seal))
    if seal.get("seal_sha256") != expected_sha:
        raise ValueError("v20_feature_seal_hash_mismatch")
    return {
        "valid": True,
        "seal_sha256": expected_sha,
        "selected_complete_close_windows": len(rows_by_close),
        "selected_rows": len(rows),
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
    }


def create_or_validate_seal(
    candidate: Mapping[str, Any], output: Path,
) -> dict[str, Any]:
    """Create once, or require byte-equivalent immutable feature identity."""
    validate_seal(candidate)
    if output.exists():
        try:
            existing = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("v20_existing_feature_seal_unreadable") from exc
        validate_seal(existing)
        if _canonical_json(existing) != _canonical_json(candidate):
            raise ValueError("v20_exclusive_feature_seal_conflict")
        return {"created": False, "path": str(output), **validate_seal(existing)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(candidate, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"created": True, "path": str(output), **validate_seal(candidate)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-db", default=str(DEFAULT_DB))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    db_path = Path(args.strategy_db)
    parent_rows = load_feature_rows_after(
        db_path, identity.PROSPECTIVE_AFTER_CLOSE_TIME,
    )
    delayed_rows = load_delayed_feature_rows_after(
        db_path, identity.PROSPECTIVE_AFTER_CLOSE_TIME,
    )
    complete, excluded = collect_feature_windows(parent_rows, delayed_rows)
    preview = {
        "status": (
            "READY_FOR_EXCLUSIVE_FEATURE_SEAL"
            if len(complete) >= identity.MINIMUM_COMPLETE_CLOSE_WINDOWS
            else "COLLECTING_V20_PROSPECTIVE_FEATURES_NO_OUTCOMES"
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
        indent=2,
        sort_keys=True,
    ))


if __name__ == "__main__":
    main()
