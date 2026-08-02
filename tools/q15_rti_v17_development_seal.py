"""Create V17's outcome-blind development seal on wholly disjoint windows."""
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

from q15_upgrade.strategy_bots import rti_microstructure_v17 as v17
from q15_upgrade.strategy_bots import rti_microstructure_v17_identity as identity
from tools import q15_rti_v15_audit_seal as v15_seal
from tools import q15_rti_v16_development_seal as v16_seal
from tools.q15_rti_market_prior_consistency_audit import (
    audit_rows as audit_market_prior_rows,
)
from tools.q15_rti_microstructure_freeze import OUTCOME_COLUMNS, load_feature_rows
from tools.q15_rti_microstructure_preregister import design_fingerprint


SEAL_VERSION = "q15-rti-v17-development-outcome-blind-seal-v1"
READY_STATUS = "V17_DEVELOPMENT_SEALED_PROSPECTIVE_CALIBRATION_COLLECTING"
NON_BTC_ASSETS = frozenset({"BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP"})
DEFAULT_DB = ROOT / "data" / "q15_strategy_bots_v3.sqlite3"
DEFAULT_PROTOCOL = ROOT / identity.PROTOCOL_RELATIVE_PATH
DEFAULT_V16_SEAL = v16_seal.DEFAULT_OUTPUT
DEFAULT_OUTPUT = (
    ROOT / "reports" / "q15_rti_v17_development"
    / "non_btc_transfer-development-240-v1.json"
)


def _load(path: Path, error: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(error) from exc
    if not isinstance(value, Mapping):
        raise ValueError(error)
    return dict(value)


def load_protocol(path: Path = DEFAULT_PROTOCOL) -> dict[str, Any]:
    protocol = _load(path, "v17_protocol_unreadable")
    population = dict(protocol.get("population") or {})
    safety = dict(protocol.get("safety") or {})
    if (
        design_fingerprint(protocol) != identity.PROTOCOL_SHA256
        or protocol.get("protocol_id") != identity.PROTOCOL_ID
        or protocol.get("design_id") != identity.DESIGN_ID
        or protocol.get("protocol_status")
        != "FROZEN_BEFORE_ANY_V17_POPULATION_OUTCOME_ACCESS"
        or population.get("cohort") != "NON_BTC_TRANSFER"
        or set(population.get("assets") or ()) != NON_BTC_ASSETS
        or population.get("btc_labels_forbidden") is not True
        or int(population.get("development_close_windows") or 0) != 240
        or int(population.get("outcome_blind_complete_disjoint_windows_available_at_freeze") or 0)
        != 244
        or int(population.get("unselected_historical_complete_windows") or 0) != 4
        or float(population.get("prospective_after_close_time") or 0.0)
        != identity.PROSPECTIVE_AFTER_CLOSE_TIME
        or any(safety.get(key) is not False for key in (
            "outcome_access_allowed_now", "model_fit_allowed_now",
            "probability_scoring_allowed_now", "paper_artifact_allowed_now",
            "notifications_allowed_now", "automatic_promotion_allowed",
            "real_trading_allowed",
        ))
    ):
        raise ValueError("v17_protocol_identity_or_safety_invalid")
    return protocol


def _complete_windows(
    rows: Sequence[Mapping[str, Any]],
) -> dict[float, list[dict[str, Any]]]:
    base = v15_seal.complete_audit_windows(rows)
    output: dict[float, list[dict[str, Any]]] = {}
    for close_time, window_rows in sorted(base.items()):
        if close_time <= identity.FEATURE_SOURCE_AFTER_CLOSE_TIME:
            continue
        if any(not v17.feature_vector(row).get("available") for row in window_rows):
            continue
        output[float(close_time)] = [dict(row) for row in window_rows]
    return output


def _excluded_times(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[tuple[float, ...], dict[str, Any]]:
    v15_times, v15_manifests = v16_seal._selected_times(rows)
    v16_artifact = _load(DEFAULT_V16_SEAL, "v17_v16_seal_unreadable")
    v16_seal.validate_development_seal(v16_artifact)
    v16_examples = v16_seal.reconstruct_development_examples(rows, v16_artifact)
    v16_times = tuple(sorted({float(row["close_time"]) for row in v16_examples}))
    if (
        len(v16_times) != 240
        or v15_seal.canonical_sha256(v16_times)
        != v16_artifact.get("selected_close_times_sha256")
        or set(v15_times).intersection(v16_times)
    ):
        raise ValueError("v17_v16_exclusion_identity_invalid")
    union = tuple(sorted(set(v15_times).union(v16_times)))
    if len(union) != len(v15_times) + len(v16_times):
        raise ValueError("v17_prior_population_overlap_invalid")
    return union, {
        "v15_selected_unique_close_windows": len(v15_times),
        "v15_selected_close_times_sha256": v15_seal.canonical_sha256(v15_times),
        "v15_seal_manifests": v15_manifests,
        "v16_development_close_windows": len(v16_times),
        "v16_development_close_times_sha256": v15_seal.canonical_sha256(v16_times),
        "v16_development_seal_sha256": v16_artifact["seal_sha256"],
        "all_prior_selected_unique_close_windows": len(union),
        "all_prior_selected_close_times_sha256": v15_seal.canonical_sha256(union),
    }


def _project(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    base, failures = v15_seal._project_evidence(rows)
    if failures:
        raise ValueError("v17_contract_identity_failure")
    raw_by_id = {int(row["id"]): row for row in rows}
    output = []
    for item in base:
        raw = raw_by_id[int(item["id"])]
        feature16 = v16_seal.v16.feature_vector(raw)
        feature17 = v17.feature_vector(raw)
        if (
            not feature16.get("available")
            or not feature17.get("available")
            or tuple(feature17.get("feature_names") or ()) != v17.FEATURE_NAMES
            or [float(value) for value in feature17["features"][:45]]
            != [float(value) for value in feature16["features"]]
        ):
            raise ValueError("v17_feature_projection_invalid")
        projected = {
            **item,
            "v16_feature_names": list(v16_seal.v16.FEATURE_NAMES),
            "v16_features": [float(value) for value in feature16["features"]],
            "v17_feature_names": list(v17.FEATURE_NAMES),
            "v17_features": [float(value) for value in feature17["features"]],
            "v17_protocol_sha256": identity.PROTOCOL_SHA256,
        }
        if OUTCOME_COLUMNS.intersection(projected):
            raise AssertionError("v17_projection_contains_outcome")
        output.append(projected)
    return output


def _fingerprint(seal: Mapping[str, Any]) -> str:
    payload = dict(seal)
    payload.pop("seal_sha256", None)
    return v15_seal.canonical_sha256(payload)


def build_seal(
    rows: Sequence[Mapping[str, Any]],
    *,
    protocol: Mapping[str, Any] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    protocol = dict(protocol or load_protocol())
    excluded, manifests = _excluded_times(rows)
    complete = _complete_windows(rows)
    candidates = tuple(
        close_time for close_time in sorted(complete)
        if close_time <= identity.DEVELOPMENT_BEFORE_OR_AT_CLOSE_TIME
        and close_time not in set(excluded)
    )
    if len(candidates) != 244:
        raise ValueError("v17_outcome_blind_population_count_mismatch")
    selected = candidates[:240]
    source_rows = [row for close_time in selected for row in complete[close_time]]
    market = audit_market_prior_rows(source_rows)
    if (
        market.get("status") != "PASS"
        or int(market.get("checked_rows") or 0) != 1680
        or int(market.get("eligible_rows") or 0) != 1680
    ):
        raise ValueError("v17_market_prior_consistency_failure")
    cohort_rows = [
        row for row in source_rows
        if str(row.get("asset") or "").upper() in NON_BTC_ASSETS
    ]
    projected = _project(cohort_rows)
    if len(projected) != 1440:
        raise ValueError("v17_cohort_rows_incomplete")
    row_ids = tuple(sorted(int(row["id"]) for row in projected))
    result = {
        "seal_version": SEAL_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "status": READY_STATUS,
        "design_id": identity.DESIGN_ID,
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "cohort": "NON_BTC_TRANSFER",
        "cohort_assets": sorted(NON_BTC_ASSETS),
        "selection": protocol["population"]["development_selection"],
        "prior_population_exclusion": manifests,
        "complete_disjoint_windows_available": len(candidates),
        "selected_development_close_windows": 240,
        "unselected_historical_complete_windows": 4,
        "selected_development_rows": 1440,
        "selected_all_seven_source_rows": 1680,
        "first_selected_close_time": min(selected),
        "last_selected_close_time": max(selected),
        "selected_close_times_sha256": v15_seal.canonical_sha256(selected),
        "selected_row_ids_sha256": v15_seal.canonical_sha256(row_ids),
        "selected_feature_evidence_sha256": v15_seal.canonical_sha256(projected),
        "feature_schema_version": v17.FEATURE_SCHEMA_VERSION,
        "feature_count": len(v17.FEATURE_NAMES),
        "feature_names_sha256": v15_seal.canonical_sha256(v17.FEATURE_NAMES),
        "market_prior_consistency": {
            "audit_version": market.get("audit_version"),
            "rows": int(market["checked_rows"]),
            "status": market["status"],
            "outcome_labels_read": False,
        },
        "fold_manifest": v16_seal._fold_manifest(selected),
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
    result["seal_sha256"] = _fingerprint(result)
    validate_seal(result)
    return result


def validate_seal(seal: Mapping[str, Any]) -> None:
    folds = seal.get("fold_manifest")
    exclusion = seal.get("prior_population_exclusion")
    if (
        seal.get("seal_sha256") != _fingerprint(seal)
        or seal.get("seal_version") != SEAL_VERSION
        or seal.get("status") != READY_STATUS
        or seal.get("design_id") != identity.DESIGN_ID
        or seal.get("protocol_id") != identity.PROTOCOL_ID
        or seal.get("protocol_sha256") != identity.PROTOCOL_SHA256
        or int(seal.get("complete_disjoint_windows_available") or 0) != 244
        or int(seal.get("selected_development_close_windows") or 0) != 240
        or int(seal.get("unselected_historical_complete_windows") or 0) != 4
        or int(seal.get("selected_development_rows") or 0) != 1440
        or int(seal.get("feature_count") or 0) != 81
        or seal.get("feature_names_sha256")
        != v15_seal.canonical_sha256(v17.FEATURE_NAMES)
        or not isinstance(exclusion, Mapping)
        or int(exclusion.get("v15_selected_unique_close_windows") or 0) != 204
        or int(exclusion.get("v16_development_close_windows") or 0) != 240
        or int(exclusion.get("all_prior_selected_unique_close_windows") or 0)
        != 444
        or not isinstance(folds, Mapping)
        or int(folds.get("outer_fold_count") or 0) != 4
        or any(seal.get(key) is not False for key in (
            "outcome_columns_selected", "outcome_labels_read", "btc_labels_read",
            "model_fit_performed", "probability_scoring_performed",
            "paper_artifact_created", "notification_eligible",
            "automatic_promotion", "real_trading_allowed",
        ))
    ):
        raise ValueError("v17_development_seal_invalid")


def reconstruct_examples(
    rows: Sequence[Mapping[str, Any]], seal: Mapping[str, Any],
) -> list[dict[str, Any]]:
    validate_seal(seal)
    excluded, _manifests = _excluded_times(rows)
    complete = _complete_windows(rows)
    selected = tuple(
        close_time for close_time in sorted(complete)
        if close_time <= identity.DEVELOPMENT_BEFORE_OR_AT_CLOSE_TIME
        and close_time not in set(excluded)
    )[:240]
    if v15_seal.canonical_sha256(selected) != seal.get("selected_close_times_sha256"):
        raise ValueError("v17_selected_window_identity_mismatch")
    raw = [
        row for close_time in selected for row in complete[close_time]
        if str(row.get("asset") or "").upper() in NON_BTC_ASSETS
    ]
    projected = _project(raw)
    if (
        v15_seal.canonical_sha256(tuple(sorted(int(row["id"]) for row in projected)))
        != seal.get("selected_row_ids_sha256")
        or v15_seal.canonical_sha256(projected)
        != seal.get("selected_feature_evidence_sha256")
    ):
        raise ValueError("v17_feature_evidence_mismatch")
    return projected


def prospective_readiness(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    complete = _complete_windows(rows)
    future = [
        close_time for close_time in sorted(complete)
        if close_time > identity.PROSPECTIVE_AFTER_CLOSE_TIME
    ]
    return {
        "protocol_id": identity.PROTOCOL_ID,
        "successor_audit_complete_close_windows": len(future),
        "calibration_close_windows_required": 60,
        "calibration_windows_remaining": max(0, 60 - len(future)),
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "notification_eligible": False,
        "real_trading_allowed": False,
    }


def write_exclusive(path: Path, seal: Mapping[str, Any]) -> None:
    validate_seal(seal)
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
    seal = build_seal(rows)
    if args.write:
        write_exclusive(Path(args.output), seal)
    print(json.dumps({
        "development_seal": seal,
        "prospective_readiness": prospective_readiness(rows),
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
