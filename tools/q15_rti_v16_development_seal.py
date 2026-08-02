"""Create V16's immutable, outcome-blind development-population seal.

The seal excludes every close window selected by any of the four V15 audit
seals, commits to the earliest 240 remaining complete NON_BTC_TRANSFER
windows, and fixes the nested walk-forward chronology.  Settlement, P/L,
model fitting, probability scoring, notification, promotion, and trading are
deliberately outside this module.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import rti_microstructure_v16 as v16
from q15_upgrade.strategy_bots import rti_microstructure_v16_identity as identity
from tools import q15_rti_v15_audit_seal as v15_seal
from tools import q15_rti_v15_final_disjoint_audit as v15_final
from tools import q15_rti_v15_fourth_disjoint_audit as v15_fourth
from tools import q15_rti_v15_pretest_command as v15_pretest_command
from tools import q15_rti_v15_recovery_audit as v15_recovery
from tools.q15_rti_market_prior_consistency_audit import (
    audit_rows as audit_market_prior_rows,
)
from tools.q15_rti_microstructure_freeze import OUTCOME_COLUMNS, load_feature_rows
from tools.q15_rti_microstructure_preregister import design_fingerprint


SEAL_VERSION = "q15-rti-v16-development-outcome-blind-seal-v1"
READY_STATUS = "DEVELOPMENT_SEALED_PROSPECTIVE_CALIBRATION_COLLECTING"
WAITING_STATUS = "WAITING_FOR_OUTCOME_BLIND_DEVELOPMENT_WINDOWS"
NON_BTC_ASSETS = frozenset({"BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP"})
EXPECTED_ASSETS = frozenset({*NON_BTC_ASSETS, "BTC"})

DEFAULT_DB = ROOT / "data" / "q15_strategy_bots_v3.sqlite3"
DEFAULT_PROTOCOL = ROOT / identity.PROTOCOL_RELATIVE_PATH
DEFAULT_OUTPUT = (
    ROOT / "reports" / "q15_rti_v16_development"
    / "non_btc_transfer-development-240-v1.json"
)
FIRST_V15_SEAL = (
    ROOT / "reports" / "q15_rti_v15_audit_seals"
    / "non_btc_transfer-earliest-60-v3.json"
)


def _load_mapping(path: Path, error: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(error) from exc
    if not isinstance(value, Mapping):
        raise ValueError(error)
    return dict(value)


def load_protocol(path: Path = DEFAULT_PROTOCOL) -> dict[str, Any]:
    protocol = _load_mapping(path, "v16_protocol_unreadable")
    population = dict(protocol.get("population") or {})
    chronology = dict(protocol.get("chronology") or {})
    safety = dict(protocol.get("safety") or {})
    if (
        design_fingerprint(protocol) != identity.PROTOCOL_SHA256
        or protocol.get("protocol_id") != identity.PROTOCOL_ID
        or protocol.get("design_id") != identity.DESIGN_ID
        or protocol.get("protocol_status")
        != "FROZEN_BEFORE_ANY_V16_POPULATION_OUTCOME_ACCESS"
        or population.get("cohort") != "NON_BTC_TRANSFER"
        or set(population.get("assets") or ()) != NON_BTC_ASSETS
        or population.get("btc_labels_forbidden") is not True
        or int(population.get("development_close_windows") or 0) != 240
        or int(population.get("calibration_close_windows") or 0) != 60
        or int(population.get("untouched_test_close_windows") or 0) != 60
        or float(population.get("development_feature_source_after_close_time") or 0)
        != identity.FEATURE_SOURCE_AFTER_CLOSE_TIME
        or float(population.get("prospective_after_close_time") or 0)
        != identity.PROSPECTIVE_AFTER_CLOSE_TIME
        or int(chronology.get("development_outer_initial_train_windows") or 0)
        != 120
        or int(chronology.get("development_outer_validation_block_windows") or 0)
        != 30
        or int(chronology.get("development_outer_fold_count") or 0) != 4
        or int(chronology.get("inner_initial_train_windows") or 0) != 60
        or int(chronology.get("inner_validation_block_windows") or 0) != 30
        or safety.get("outcome_access_allowed_now") is not False
        or safety.get("model_fit_allowed_now") is not False
        or safety.get("probability_scoring_allowed_now") is not False
        or safety.get("paper_artifact_allowed_now") is not False
        or safety.get("notifications_allowed_now") is not False
        or safety.get("automatic_promotion_allowed") is not False
        or safety.get("real_trading_allowed") is not False
    ):
        raise ValueError("v16_protocol_identity_or_safety_invalid")
    return protocol


def _selected_times(rows: Sequence[Mapping[str, Any]]) -> tuple[
    tuple[float, ...], list[dict[str, Any]]
]:
    selections: list[tuple[str, Mapping[str, Any], list[dict[str, Any]]]] = []

    first = v15_pretest_command.load_ready_seal(FIRST_V15_SEAL)
    selections.append((
        "earliest_60_v3",
        first,
        v15_pretest_command.select_sealed_feature_rows(rows, first),
    ))
    recovery = v15_recovery.load_recovery_seal()
    selections.append((
        "disjoint_recovery_60_v1",
        recovery,
        v15_recovery.select_recovery_feature_rows(rows, recovery),
    ))
    final = v15_final.load_seal()
    selections.append((
        "final_disjoint_60_v1",
        final,
        v15_final.select_rows(rows, final),
    ))
    fourth = v15_fourth.load_seal()
    selections.append((
        "fourth_disjoint_60_v1",
        fourth,
        v15_fourth.select_rows(rows, fourth),
    ))

    manifests: list[dict[str, Any]] = []
    union: set[float] = set()
    for name, seal, selected in selections:
        times = tuple(sorted({float(row["close_time"]) for row in selected}))
        row_ids = tuple(sorted(int(row["id"]) for row in selected))
        if (
            len(times) != 60
            or len(row_ids) != 360
            or v15_seal.canonical_sha256(times)
            != seal.get("selected_close_times_sha256")
            or v15_seal.canonical_sha256(row_ids)
            != seal.get("selected_row_ids_sha256")
        ):
            raise ValueError("v16_v15_exclusion_reconstruction_mismatch")
        manifests.append({
            "name": name,
            "seal_sha256": seal["seal_sha256"],
            "selected_close_windows": 60,
            "selected_rows": 360,
            "selected_close_times_sha256": seal["selected_close_times_sha256"],
            "selected_row_ids_sha256": seal["selected_row_ids_sha256"],
        })
        union.update(times)
    return tuple(sorted(union)), manifests


def _complete_v16_windows(
    rows: Sequence[Mapping[str, Any]],
) -> dict[float, list[dict[str, Any]]]:
    base = v15_seal.complete_audit_windows(rows)
    output: dict[float, list[dict[str, Any]]] = {}
    for close_time, window_rows in sorted(base.items()):
        if close_time <= identity.FEATURE_SOURCE_AFTER_CLOSE_TIME:
            continue
        if {str(row.get("asset") or "").upper() for row in window_rows} != EXPECTED_ASSETS:
            continue
        if any(not v16.feature_vector(row).get("available") for row in window_rows):
            continue
        output[float(close_time)] = [dict(row) for row in window_rows]
    return output


def _inner_folds(times: Sequence[float]) -> list[dict[str, Any]]:
    values = tuple(float(value) for value in times)
    folds = []
    for fold, start in enumerate(range(60, len(values), 30), start=1):
        train = values[:start]
        validation = values[start:start + 30]
        if len(validation) != 30 or max(train) >= min(validation):
            raise ValueError("v16_inner_fold_chronology_invalid")
        folds.append({
            "fold": fold,
            "train_close_windows": len(train),
            "validation_close_windows": len(validation),
            "train_close_times_sha256": v15_seal.canonical_sha256(train),
            "validation_close_times_sha256": v15_seal.canonical_sha256(validation),
        })
    return folds


def _fold_manifest(times: Sequence[float]) -> dict[str, Any]:
    values = tuple(float(value) for value in times)
    if len(values) != 240 or tuple(sorted(values)) != values:
        raise ValueError("v16_development_fold_population_invalid")
    outer = []
    for index in range(4):
        start = 120 + index * 30
        train = values[:start]
        validation = values[start:start + 30]
        if len(validation) != 30 or max(train) >= min(validation):
            raise ValueError("v16_outer_fold_chronology_invalid")
        outer.append({
            "fold": index + 1,
            "train_close_windows": len(train),
            "validation_close_windows": len(validation),
            "train_close_times_sha256": v15_seal.canonical_sha256(train),
            "validation_close_times_sha256": v15_seal.canonical_sha256(validation),
            "inner_folds": _inner_folds(train),
        })
    return {
        "outer_fold_count": 4,
        "outer_folds": outer,
        "initial_train_close_windows": 120,
        "walk_forward_validation_close_windows": 120,
        "same_close_assets_share_every_outer_and_inner_fold": True,
        "outer_validation_strictly_after_training": True,
        "inner_validation_strictly_after_training": True,
        "calibration_rows_are_not_in_walk_forward_validation": True,
    }


def _project_v16_evidence(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    base, contract_failures = v15_seal._project_evidence(rows)
    if contract_failures:
        raise ValueError("v16_development_contract_identity_failure")
    raw_by_id = {int(row["id"]): row for row in rows}
    projected = []
    for item in base:
        candidate = v16.feature_vector(raw_by_id[int(item["id"])])
        if (
            not candidate.get("available")
            or tuple(candidate.get("feature_names") or ()) != v16.FEATURE_NAMES
            or len(candidate.get("features") or ()) != len(v16.FEATURE_NAMES)
        ):
            raise ValueError("v16_development_feature_unavailable")
        output = {
            **item,
            "v16_feature_names": list(v16.FEATURE_NAMES),
            "v16_features": [float(value) for value in candidate["features"]],
            "v16_protocol_sha256": identity.PROTOCOL_SHA256,
        }
        if OUTCOME_COLUMNS.intersection(output):
            raise AssertionError("v16_development_projection_contains_outcome")
        projected.append(output)
    return projected


def _seal_fingerprint(seal: Mapping[str, Any]) -> str:
    payload = dict(seal)
    payload.pop("seal_sha256", None)
    return v15_seal.canonical_sha256(payload)


def build_development_seal(
    rows: Sequence[Mapping[str, Any]],
    *,
    protocol: Mapping[str, Any] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    protocol = dict(protocol or load_protocol())
    excluded_times, v15_manifests = _selected_times(rows)
    complete = _complete_v16_windows(rows)
    candidates = tuple(
        close_time for close_time in sorted(complete)
        if close_time <= identity.PROSPECTIVE_AFTER_CLOSE_TIME
        and close_time not in set(excluded_times)
    )
    common = {
        "seal_version": SEAL_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "status": WAITING_STATUS,
        "design_id": identity.DESIGN_ID,
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "cohort": "NON_BTC_TRANSFER",
        "cohort_assets": sorted(NON_BTC_ASSETS),
        "selection": (
            "EARLIEST_240_COMPLETE_V16_WINDOWS_BEFORE_PROSPECTIVE_BOUNDARY_"
            "DISJOINT_FROM_EVERY_SELECTED_WINDOW_IN_ALL_FOUR_V15_SEALS"
        ),
        "v15_seal_manifests": v15_manifests,
        "excluded_v15_selected_close_windows_unique": len(excluded_times),
        "excluded_v15_selected_close_times_sha256": (
            v15_seal.canonical_sha256(excluded_times)
        ),
        "complete_disjoint_development_windows_available": len(candidates),
        "development_windows_required": 240,
        "development_windows_remaining": max(0, 240 - len(candidates)),
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "btc_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }
    if len(candidates) < 240:
        result = dict(common)
        result["seal_sha256"] = _seal_fingerprint(result)
        return result

    selected_times = candidates[:240]
    all_source_rows = [
        row for close_time in selected_times for row in complete[close_time]
    ]
    market = audit_market_prior_rows(all_source_rows)
    if (
        market.get("status") != "PASS"
        or int(market.get("checked_rows") or 0) != 1680
        or int(market.get("eligible_rows") or 0) != 1680
    ):
        raise ValueError("v16_development_market_prior_consistency_failure")
    cohort_rows = [
        row for row in all_source_rows
        if str(row.get("asset") or "").upper() in NON_BTC_ASSETS
    ]
    if len(cohort_rows) != 1440:
        raise ValueError("v16_development_cohort_rows_incomplete")
    projected = _project_v16_evidence(cohort_rows)
    row_ids = tuple(sorted(int(row["id"]) for row in projected))
    result = {
        **common,
        "status": READY_STATUS,
        "development_windows_remaining": 0,
        "selected_development_close_windows": 240,
        "selected_development_rows": 1440,
        "selected_all_seven_source_rows": 1680,
        "first_selected_close_time": min(selected_times),
        "last_selected_close_time": max(selected_times),
        "development_strictly_before_prospective_calibration": (
            max(selected_times) <= identity.PROSPECTIVE_AFTER_CLOSE_TIME
        ),
        "selected_close_times_sha256": v15_seal.canonical_sha256(selected_times),
        "selected_row_ids_sha256": v15_seal.canonical_sha256(row_ids),
        "selected_feature_evidence_sha256": v15_seal.canonical_sha256(projected),
        "feature_schema_version": v16.FEATURE_SCHEMA_VERSION,
        "feature_count": len(v16.FEATURE_NAMES),
        "feature_names_sha256": v15_seal.canonical_sha256(v16.FEATURE_NAMES),
        "contract_identity_mismatch_rows": 0,
        "market_prior_consistency": {
            "audit_version": market.get("audit_version"),
            "rows": int(market["checked_rows"]),
            "status": market["status"],
            "outcome_labels_read": False,
        },
        "fold_manifest": _fold_manifest(selected_times),
    }
    result["seal_sha256"] = _seal_fingerprint(result)
    validate_development_seal(result)
    return result


def validate_development_seal(seal: Mapping[str, Any]) -> None:
    if seal.get("seal_sha256") != _seal_fingerprint(seal):
        raise ValueError("v16_development_seal_hash_mismatch")
    if seal.get("status") != READY_STATUS:
        raise ValueError("v16_development_seal_not_ready")
    folds = seal.get("fold_manifest")
    manifests = seal.get("v15_seal_manifests")
    if (
        seal.get("seal_version") != SEAL_VERSION
        or seal.get("design_id") != identity.DESIGN_ID
        or seal.get("protocol_id") != identity.PROTOCOL_ID
        or seal.get("protocol_sha256") != identity.PROTOCOL_SHA256
        or seal.get("cohort") != "NON_BTC_TRANSFER"
        or set(seal.get("cohort_assets") or ()) != NON_BTC_ASSETS
        or int(seal.get("selected_development_close_windows") or 0) != 240
        or int(seal.get("selected_development_rows") or 0) != 1440
        or int(seal.get("selected_all_seven_source_rows") or 0) != 1680
        or int(seal.get("feature_count") or 0) != len(v16.FEATURE_NAMES)
        or seal.get("feature_names_sha256")
        != v15_seal.canonical_sha256(v16.FEATURE_NAMES)
        or seal.get("development_strictly_before_prospective_calibration")
        is not True
        or int(seal.get("contract_identity_mismatch_rows", -1)) != 0
        or not isinstance(manifests, list)
        or len(manifests) != 4
        or not isinstance(folds, Mapping)
        or int(folds.get("outer_fold_count") or 0) != 4
        or int(folds.get("walk_forward_validation_close_windows") or 0) != 120
        or folds.get("calibration_rows_are_not_in_walk_forward_validation")
        is not True
        or any(seal.get(key) is not False for key in (
            "outcome_columns_selected", "outcome_labels_read", "btc_labels_read",
            "model_fit_performed", "probability_scoring_performed",
            "paper_artifact_created", "notification_eligible",
            "automatic_promotion", "real_trading_allowed",
        ))
    ):
        raise ValueError("v16_development_seal_invalid")
    outer = folds.get("outer_folds")
    if (
        not isinstance(outer, list)
        or len(outer) != 4
        or [int(item.get("train_close_windows") or 0) for item in outer]
        != [120, 150, 180, 210]
        or [int(item.get("validation_close_windows") or 0) for item in outer]
        != [30, 30, 30, 30]
        or [len(item.get("inner_folds") or ()) for item in outer]
        != [2, 3, 4, 5]
    ):
        raise ValueError("v16_development_fold_manifest_invalid")


def validate_against_rows(
    seal: Mapping[str, Any], rows: Sequence[Mapping[str, Any]],
) -> None:
    validate_development_seal(seal)
    rebuilt = build_development_seal(
        rows,
        generated_at=str(seal["generated_at"]),
    )
    if rebuilt != dict(seal):
        raise ValueError("v16_development_seal_reconstruction_mismatch")


def reconstruct_development_examples(
    rows: Sequence[Mapping[str, Any]],
    seal: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Rebuild exactly the feature-only rows committed by a ready seal."""
    validate_development_seal(seal)
    excluded_times, _manifests = _selected_times(rows)
    complete = _complete_v16_windows(rows)
    selected_times = tuple(
        close_time for close_time in sorted(complete)
        if close_time <= identity.PROSPECTIVE_AFTER_CLOSE_TIME
        and close_time not in set(excluded_times)
    )[:240]
    if (
        len(selected_times) != 240
        or v15_seal.canonical_sha256(selected_times)
        != seal.get("selected_close_times_sha256")
    ):
        raise ValueError("v16_development_selected_window_identity_mismatch")
    raw = [
        row for close_time in selected_times for row in complete[close_time]
        if str(row.get("asset") or "").upper() in NON_BTC_ASSETS
    ]
    projected = _project_v16_evidence(raw)
    row_ids = tuple(sorted(int(row["id"]) for row in projected))
    if (
        len(projected) != 1440
        or v15_seal.canonical_sha256(row_ids)
        != seal.get("selected_row_ids_sha256")
        or v15_seal.canonical_sha256(projected)
        != seal.get("selected_feature_evidence_sha256")
        or any(float(row["close_time"]) in set(excluded_times) for row in projected)
    ):
        raise ValueError("v16_development_feature_evidence_mismatch")
    return projected


def prospective_readiness(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    complete = _complete_v16_windows(rows)
    future = tuple(
        close_time for close_time in sorted(complete)
        if close_time > identity.PROSPECTIVE_AFTER_CLOSE_TIME
    )
    available = len(future)
    return {
        "protocol_id": identity.PROTOCOL_ID,
        "cohort": "NON_BTC_TRANSFER",
        "successor_audit_complete_close_windows": available,
        "calibration_close_windows_required": 60,
        "calibration_windows_remaining": max(0, 60 - available),
        "calibration_reservation_ready": available >= 60,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "notification_eligible": False,
        "real_trading_allowed": False,
    }


def write_exclusive(path: Path, seal: Mapping[str, Any]) -> None:
    validate_development_seal(seal)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(dict(seal), handle, indent=2, sort_keys=True)
        handle.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-db", default=str(DEFAULT_DB))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    rows = load_feature_rows(Path(args.strategy_db))
    seal = build_development_seal(rows)
    if args.write:
        write_exclusive(Path(args.output), seal)
    print(json.dumps({
        "development_seal": seal,
        "prospective_readiness": prospective_readiness(rows),
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
