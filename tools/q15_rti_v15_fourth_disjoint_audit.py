"""Build V15's fourth NON-BTC seal, disjoint from 144 authorized windows."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import (
    rti_microstructure_v15_fourth_disjoint_identity as identity,
)
from tools import q15_rti_v15_audit_seal as audit_seal
from tools import q15_rti_v15_final_disjoint_audit as prior
from tools import q15_rti_v15_pretest as pretest
from tools import q15_rti_v15_pretest_command as pretest_command
from tools import q15_rti_v15_untouched_test as untouched
from tools.q15_rti_microstructure_freeze import load_feature_rows
from tools.q15_rti_microstructure_preregister import design_fingerprint


COHORT = "NON_BTC_TRANSFER"
DEFAULT_PROTOCOL = ROOT / identity.PROTOCOL_RELATIVE_PATH
DEFAULT_OUTPUT = (
    ROOT / "reports" / "q15_rti_v15_audit_seals"
    / "non_btc_transfer-fourth-disjoint-60-v1.json"
)
DEFAULT_PRETEST_STATE = (
    ROOT / "reports" / "q15_rti_v15_fourth_disjoint_audit_runs"
    / "non_btc_transfer" / "pretest-reservation.json"
)
DEFAULT_TEST_STATE = (
    ROOT / "reports" / "q15_rti_v15_fourth_disjoint_audit_runs"
    / "non_btc_transfer" / "untouched-test-reservation.json"
)


def _load(path: Path, error: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(error) from exc
    if not isinstance(data, Mapping):
        raise ValueError(error)
    return dict(data)


def load_protocol(path: Path = DEFAULT_PROTOCOL) -> dict[str, Any]:
    result = _load(path, "v15_fourth_protocol_unreadable")
    if (
        design_fingerprint(result) != identity.PROTOCOL_SHA256
        or result.get("protocol_id") != identity.PROTOCOL_ID
        or result.get("selection_rule") != identity.SELECTION_RULE
        or int(result.get("excluded_authorized_close_windows") or 0) != 144
        or int(result.get("excluded_authorized_rows") or 0) != 864
        or int(result.get("minimum_complete_close_windows") or 0) != 60
        or result.get(
            "all_previously_authorized_close_windows_excluded_regardless_of_outcome"
        ) is not True
        or result.get("features_models_thresholds_costs_and_gates_unchanged")
        is not True
        or result.get("outcome_values_used_for_selection") is not False
        or result.get("fourth_population_labels_read_before_freeze") is not False
        or result.get("automatic_promotion") is not False
        or result.get("real_trading_allowed") is not False
    ):
        raise ValueError("v15_fourth_protocol_invalid")
    return result


def _excluded(
    rows: Sequence[Mapping[str, Any]],
    protocol: Mapping[str, Any],
) -> tuple[tuple[float, ...], tuple[int, ...]]:
    times0, ids0 = prior._excluded_population(rows, prior.load_protocol())
    seal3 = prior.load_seal()
    reservation3 = pretest._read_sealed(prior.DEFAULT_PRETEST_STATE)
    if pretest.result_path_for(prior.DEFAULT_PRETEST_STATE).exists():
        raise ValueError("v15_fourth_parent_result_exists")
    if (
        reservation3.get("status") != pretest.RESERVED_STATUS
        or reservation3.get("audit_seal_sha256") != seal3.get("seal_sha256")
        or reservation3.get("state_sha256")
        not in protocol.get("ambiguous_parent_pretest_reservation_sha256", ())
        or reservation3.get("walk_forward_scoring_performed") is not False
        or reservation3.get("calibration_scoring_performed") is not False
        or reservation3.get("untouched_test_labels_read") is not False
    ):
        raise ValueError("v15_fourth_parent_invalid")
    selected3 = prior.select_rows(rows, seal3)
    _design, _v14, _charter, evaluation, _geometry, _sha = (
        audit_seal._load_inputs()
    )
    _, pretest3, _test3 = untouched._prepare_projected_rows(
        selected3, seal=seal3, cohort=COHORT, protocol=evaluation,
    )
    times3 = tuple(sorted({float(row["close_time"]) for row in pretest3}))
    ids3 = tuple(sorted(int(row["id"]) for row in pretest3))
    if (
        audit_seal.canonical_sha256(times3)
        != reservation3.get("pretest_close_times_sha256")
        or audit_seal.canonical_sha256(ids3)
        != reservation3.get("pretest_row_ids_sha256")
    ):
        raise ValueError("v15_fourth_parent_partition_invalid")
    times = tuple(sorted(set(times0).union(times3)))
    ids = tuple(sorted(set(ids0).union(ids3)))
    if (
        len(times) != 144
        or len(ids) != 864
        or audit_seal.canonical_sha256(times)
        != protocol.get("excluded_authorized_close_times_sha256")
        or audit_seal.canonical_sha256(ids)
        != protocol.get("excluded_authorized_row_ids_sha256")
    ):
        raise ValueError("v15_fourth_union_invalid")
    return times, ids


def _filtered(
    rows: Sequence[Mapping[str, Any]], times: Sequence[float],
) -> list[dict[str, Any]]:
    excluded = set(times)
    return [
        dict(row) for row in rows
        if float(row.get("close_time") or 0.0) not in excluded
    ]


def build_seal(
    rows: Sequence[Mapping[str, Any]],
    *, generated_at: str | None = None,
) -> dict[str, Any]:
    protocol = load_protocol()
    times, ids = _excluded(rows, protocol)
    design, v14, charter, evaluation, geometry, geometry_sha = (
        audit_seal._load_inputs()
    )
    result = audit_seal.build_audit_seal(
        _filtered(rows, times),
        cohort=COHORT,
        design=design,
        v14_design=v14,
        charter=charter,
        protocol=evaluation,
        geometry_artifact=geometry,
        geometry_artifact_file_sha256=geometry_sha,
        generated_at=generated_at,
    )
    if result.get("status") != audit_seal.READY_STATUS:
        raise ValueError("v15_fourth_population_not_ready")
    result.update({
        "selection": identity.SELECTION_RULE,
        "fourth_protocol_id": identity.PROTOCOL_ID,
        "fourth_protocol_sha256": identity.PROTOCOL_SHA256,
        "excluded_authorized_close_windows": len(times),
        "excluded_authorized_rows": len(ids),
        "excluded_authorized_close_times_sha256": (
            audit_seal.canonical_sha256(times)
        ),
        "excluded_authorized_row_ids_sha256": (
            audit_seal.canonical_sha256(ids)
        ),
        "fourth_population_labels_read_before_freeze": False,
        "outcome_values_used_for_selection": False,
        "features_models_thresholds_costs_and_gates_unchanged": True,
    })
    result["seal_sha256"] = audit_seal.seal_fingerprint(result)
    validate_seal(result)
    return result


def validate_seal(
    seal: Mapping[str, Any],
    protocol: Mapping[str, Any] | None = None,
) -> None:
    audit_seal.validate_audit_seal(seal)
    protocol = dict(protocol or load_protocol())
    if (
        seal.get("selection") != identity.SELECTION_RULE
        or seal.get("fourth_protocol_id") != identity.PROTOCOL_ID
        or seal.get("fourth_protocol_sha256") != identity.PROTOCOL_SHA256
        or int(seal.get("excluded_authorized_close_windows") or 0) != 144
        or int(seal.get("excluded_authorized_rows") or 0) != 864
        or seal.get("excluded_authorized_close_times_sha256")
        != protocol.get("excluded_authorized_close_times_sha256")
        or seal.get("excluded_authorized_row_ids_sha256")
        != protocol.get("excluded_authorized_row_ids_sha256")
        or seal.get("fourth_population_labels_read_before_freeze") is not False
        or seal.get("outcome_values_used_for_selection") is not False
        or seal.get("features_models_thresholds_costs_and_gates_unchanged")
        is not True
    ):
        raise ValueError("v15_fourth_seal_invalid")


def select_rows(
    rows: Sequence[Mapping[str, Any]],
    seal: Mapping[str, Any],
) -> list[dict[str, Any]]:
    validate_seal(seal)
    times, _ids = _excluded(rows, load_protocol())
    selected = pretest_command.select_sealed_feature_rows(
        _filtered(rows, times), seal,
    )
    if any(float(row["close_time"]) in set(times) for row in selected):
        raise ValueError("v15_fourth_overlap")
    return selected


def load_seal(path: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    result = _load(path, "v15_fourth_seal_unreadable")
    validate_seal(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-db", default=str(audit_seal.DEFAULT_DB))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    result = build_seal(load_feature_rows(Path(args.strategy_db)))
    audit_seal.write_seal_exclusive(Path(args.output), result)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
