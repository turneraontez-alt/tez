"""Outcome-blind exclusive seal for V19's first prospective review."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import rti_microstructure_v18 as v18
from q15_upgrade.strategy_bots import rti_microstructure_v18_identity as v18_identity
from q15_upgrade.strategy_bots import rti_microstructure_v19 as v19
from q15_upgrade.strategy_bots import rti_microstructure_v19_audit_identity as audit_identity
from q15_upgrade.strategy_bots import rti_microstructure_v19_identity as identity
from tools.q15_rti_independent_path_audit import (
    CONTRACT_IDENTITY_VERSION,
    validate_exact_contract_identity,
)
from tools.q15_rti_microstructure_freeze import (
    OUTCOME_COLUMNS,
    load_feature_rows_after,
)
from tools.q15_rti_microstructure_preregister import design_fingerprint
from tools.q15_rti_v17_development_seal import DEFAULT_DB
from tools.q15_rti_v18_prospective_seal import _complete_windows
from tools.q15_rti_v19_readiness import (
    _linked_parent_id,
    load_delayed_feature_rows_after,
)


READY_STATUS = "V19_FIRST_PROSPECTIVE_FEATURES_SEALED_LABELS_CLOSED"
NOT_READY_STATUS = "V19_FIRST_PROSPECTIVE_FEATURES_NOT_READY"
NON_BTC_ASSETS = frozenset({"BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP"})
EXPECTED_ASSETS = NON_BTC_ASSETS | {"BTC"}
DEFAULT_CONTRACT = ROOT / audit_identity.AUDIT_CONTRACT_RELATIVE_PATH
DEFAULT_OUTPUT = ROOT / audit_identity.PROSPECTIVE_SEAL_RELATIVE_PATH


def _load(path: Path, error: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(error) from exc
    if not isinstance(value, Mapping):
        raise ValueError(error)
    return dict(value)


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def load_contract(path: Path = DEFAULT_CONTRACT) -> dict[str, Any]:
    contract = _load(path, "v19_first_review_contract_unreadable")
    parent = dict(contract.get("parent_control") or {})
    selection = dict(contract.get("prospective_selection") or {})
    execution = dict(contract.get("execution_simulation") or {})
    access = dict(contract.get("label_access") or {})
    metrics = dict(contract.get("metrics") or {})
    gate = dict(contract.get("gate") or {})
    result = dict(contract.get("result_policy") or {})
    distance = dict(metrics.get("by_distance_tier") or {})
    volatility = dict(metrics.get("by_volatility_tier") or {})
    if (
        design_fingerprint(contract) != audit_identity.AUDIT_CONTRACT_SHA256
        or contract.get("contract_id") != audit_identity.AUDIT_CONTRACT_ID
        or contract.get("contract_status")
        != "FROZEN_BEFORE_ANY_V19_PROSPECTIVE_OUTCOME_ACCESS"
        or contract.get("protocol_id") != identity.PROTOCOL_ID
        or contract.get("protocol_sha256") != identity.PROTOCOL_SHA256
        or contract.get("cohort") != "NON_BTC_TRANSFER"
        or set(contract.get("assets") or ()) != NON_BTC_ASSETS
        or contract.get("btc_labels_forbidden") is not True
        or parent.get("protocol_id") != v18_identity.PROTOCOL_ID
        or parent.get("protocol_sha256") != v18_identity.PROTOCOL_SHA256
        or parent.get("candidate_must_be_subset_of_parent_control") is not True
        or float(selection.get("prospective_after_close_time") or 0.0)
        != identity.PROSPECTIVE_AFTER_CLOSE_TIME
        or float(selection.get("first_eligible_close_time") or 0.0)
        != identity.FIRST_ELIGIBLE_CLOSE_TIME
        or int(selection.get("minimum_complete_close_windows") or 0) != 150
        or int(selection.get("minimum_candidate_picks") or 0) != 30
        or selection.get("complete_window_definition")
        != "ALL_SEVEN_V18_PARENT_ROWS_EACH_WITH_EXACTLY_ONE_LINEAGE_MATCHED_V19_FRESH_60S_SOURCE_QUALITY_COMPLETE_DELAYED_ROW"
        or selection.get("source_quality_rule_version")
        != "q15-rti-v19-all-seven-parent-fresh-60s-paired-source-quality-v1"
        or selection.get("same_close_rows_never_split") is not True
        or selection.get("partial_or_duplicate_pair_windows_forbidden") is not True
        or selection.get("candidate_must_be_subset_of_control") is not True
        or selection.get("outcomes_or_resolution_status_used_for_selection")
        is not False
        or selection.get("feature_seal_must_be_exclusive_before_label_access")
        is not True
        or execution.get("candidate_may_not_reuse_parent_entry_price") is not True
        or access.get("confirmation_phrase")
        != audit_identity.CONFIRMATION_PHRASE
        or access.get("exclusive_reservation_before_callback") is not True
        or access.get("candidate_delayed_ids_and_pair_hashes_must_match_seal")
        is not True
        or access.get("fresh_authoritative_kalshi_finalized_evidence_required")
        is not True
        or access.get("btc_labels_forbidden") is not True
        or distance.get("cut_points_bps") != [1.0, 3.0]
        or volatility.get("cut_points_bps") != [1.0, 3.0]
        or gate.get("candidate_resolved_picks_minimum") != 30
        or gate.get("complete_close_windows_minimum") != 150
        or gate.get("automatic_promotion") is not False
        or gate.get("manual_promotion_only") is not True
        or result.get("paper_artifact_allowed_by_this_command") is not False
        or result.get("notifications_allowed_by_this_command") is not False
        or result.get("telegram_allowed_by_this_command") is not False
        or result.get("automatic_promotion_allowed") is not False
        or result.get("real_trading_allowed") is not False
        or contract.get("outcome_labels_used_to_create_contract") is not False
        or contract.get("prospective_resolution_status_inspected_to_create_contract")
        is not False
        or contract.get("model_fit_performed_before_contract_freeze") is not False
        or contract.get("probability_scoring_performed_before_contract_freeze")
        is not False
    ):
        raise ValueError("v19_first_review_contract_identity_or_safety_invalid")
    return contract


def _profile(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("threshold_json")
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        value = json.loads(str(raw or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _matched_complete_windows(
    parent_rows: Sequence[Mapping[str, Any]],
    delayed_rows: Sequence[Mapping[str, Any]],
) -> dict[float, list[tuple[dict[str, Any], dict[str, Any]]]]:
    complete_parents = _complete_windows(parent_rows)
    delayed_by_parent: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for raw in delayed_rows:
        parent_id = _linked_parent_id(raw)
        if parent_id > 0:
            delayed_by_parent[parent_id].append(dict(raw))
    output: dict[float, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    used_delayed_ids: set[int] = set()
    for close_time, parents in sorted(complete_parents.items()):
        if close_time <= identity.PROSPECTIVE_AFTER_CLOSE_TIME:
            continue
        pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        failed = False
        for parent in sorted(parents, key=lambda row: str(row.get("asset") or "")):
            matches = delayed_by_parent.get(int(parent["id"]), [])
            if len(matches) != 1:
                failed = True
                break
            delayed = matches[0]
            delayed_id = int(delayed["id"])
            if delayed_id in used_delayed_ids:
                raise ValueError("v19_delayed_row_reused_across_parents")
            source = v19.evaluate_delayed_source(parent, delayed)
            if source.get("available") is not True:
                failed = True
                break
            pairs.append((dict(parent), dict(delayed)))
        if failed or len(pairs) != 7:
            continue
        if {str(parent.get("asset") or "").upper() for parent, _ in pairs} != EXPECTED_ASSETS:
            continue
        used_delayed_ids.update(int(delayed["id"]) for _, delayed in pairs)
        output[float(close_time)] = pairs
    return output


def select_prefix(
    parent_rows: Sequence[Mapping[str, Any]],
    delayed_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    complete = _matched_complete_windows(parent_rows, delayed_rows)
    future = tuple(sorted(complete))
    candidate_by_close: dict[
        float, list[tuple[dict[str, Any], dict[str, Any]]]
    ] = {}
    control_by_close: dict[
        float, list[tuple[dict[str, Any], dict[str, Any]]]
    ] = {}
    candidate_total = 0
    terminal_index: int | None = None
    for index, close_time in enumerate(future):
        candidates = []
        controls = []
        for parent, delayed in complete[close_time]:
            if str(parent.get("asset") or "").upper() == "BTC":
                continue
            control = v18.evaluate_row(parent)
            candidate = v19.evaluate_pair(parent, delayed)
            if candidate["eligible"] and not control["eligible"]:
                raise ValueError("v19_candidate_not_v18_control_subset")
            if control["eligible"]:
                controls.append((dict(parent), dict(delayed)))
            if candidate["eligible"]:
                candidates.append((dict(parent), dict(delayed)))
        candidate_by_close[close_time] = candidates
        control_by_close[close_time] = controls
        candidate_total += len(candidates)
        if index + 1 >= 150 and candidate_total >= 30:
            terminal_index = index
            break
    if terminal_index is None:
        return {
            "status": NOT_READY_STATUS,
            "future_complete_close_windows": len(future),
            "complete_close_windows_required": 150,
            "complete_close_windows_remaining": max(0, 150 - len(future)),
            "candidate_picks": sum(len(candidate_by_close[close]) for close in future),
            "candidate_picks_required": 30,
            "candidate_picks_remaining": max(0, 30 - sum(
                len(candidate_by_close[close]) for close in future
            )),
            "outcome_labels_read": False,
        }
    selected = future[:terminal_index + 1]
    source_pairs = [pair for close in selected for pair in complete[close]]
    candidate_pairs = [
        pair for close in selected for pair in candidate_by_close[close]
    ]
    control_pairs = [
        pair for close in selected for pair in control_by_close[close]
    ]
    return {
        "status": READY_STATUS,
        "selected_close_times": selected,
        "source_pairs": source_pairs,
        "candidate_pairs": candidate_pairs,
        "control_pairs": control_pairs,
        "outcome_labels_read": False,
    }


def _tier(value: float, *, name: str) -> str:
    if value < 1.0:
        return f"{name}_UNDER_1_BPS"
    if value < 3.0:
        return f"{name}_1_TO_UNDER_3_BPS"
    return f"{name}_3_BPS_PLUS"


def _project_pair(
    parent: Mapping[str, Any], delayed: Mapping[str, Any],
) -> dict[str, Any]:
    contract_identity = validate_exact_contract_identity(parent)
    parent_source = v18.evaluate_source_row(parent)
    control = v18.evaluate_row(parent)
    delayed_source = v19.evaluate_delayed_source(parent, delayed)
    candidate = v19.evaluate_pair(parent, delayed)
    if (
        contract_identity.get("valid") is not True
        or parent_source.get("available") is not True
        or delayed_source.get("available") is not True
    ):
        raise ValueError("v19_prospective_pair_source_or_contract_failure")
    parent_profile = _profile(parent)
    delayed_profile = _profile(delayed)
    evidence = dict(delayed_source["evidence"])
    distance = abs(float(parent_profile.get("rti_signed_distance_bps") or 0.0))
    volatility = float(
        parent_profile.get("rti_path_realized_volatility_bps") or 0.0
    )
    projected = {
        "parent_id": int(parent["id"]),
        "delayed_id": int(delayed["id"]),
        "ticker": str(parent.get("ticker") or ""),
        "asset": str(parent.get("asset") or "").upper(),
        "side": str(parent.get("side") or "").upper(),
        "close_time": float(parent["close_time"]),
        "parent_source_captured_at": float(parent["source_captured_at"]),
        "parent_evidence_as_of": float(parent["evidence_as_of"]),
        "delayed_quote_captured_at": float(evidence["quote_captured_at"]),
        "delayed_evaluated_at": float(evidence["evaluated_at"]),
        "capture_gap_from_parent_seconds": float(
            evidence["capture_gap_from_parent_seconds"]
        ),
        "parent_entry_ask_cents": float(parent["entry_ask_cents"]),
        "parent_spread_cents": float(parent["spread_cents"]),
        "parent_depth_contracts": float(parent.get("depth_contracts") or 0.0),
        "parent_sim_full_fill_supported": (
            v18._flag(parent_profile.get("sim_full_fill_supported")) is True
        ),
        "delayed_entry_ask_cents": float(delayed["entry_ask_cents"]),
        "delayed_spread_cents": float(delayed["spread_cents"]),
        "delayed_depth_contracts": float(delayed.get("depth_contracts") or 0.0),
        "delayed_sim_contracts": int(float(evidence["sim_contracts"] or 0.0)),
        "delayed_sim_full_fill_supported": (
            evidence["sim_full_fill_supported"] is True
        ),
        "contract_identity_version": (
            contract_identity.get("version") or CONTRACT_IDENTITY_VERSION
        ),
        "contract_identity_valid": True,
        "parent_v18_eligible": control["eligible"] is True,
        "v19_candidate_eligible": candidate["eligible"] is True,
        "parent_source_feature_evidence_sha256": parent_source[
            "feature_evidence_sha256"
        ],
        "parent_control_feature_evidence_sha256": control[
            "feature_evidence_sha256"
        ],
        "delayed_source_feature_evidence_sha256": delayed_source[
            "feature_evidence_sha256"
        ],
        "v19_candidate_feature_evidence_sha256": candidate[
            "feature_evidence_sha256"
        ],
        "reversal_risk_class": str(
            parent_profile.get("rti_reversal_risk_class") or ""
        ),
        "settlement_average_risk_class": str(
            parent_profile.get("rti_settlement_average_risk_class") or ""
        ),
        "path_regime_class": str(
            parent_profile.get("rti_path_regime_class") or ""
        ),
        "absolute_distance_bps": distance,
        "absolute_distance_tier": _tier(distance, name="DISTANCE"),
        "realized_volatility_bps": volatility,
        "realized_volatility_tier": _tier(volatility, name="VOLATILITY"),
        "path_strike_crossings": int(
            parent_profile.get("rti_path_strike_crossings") or 0
        ),
        "parent_protocol_sha256": v18_identity.PROTOCOL_SHA256,
        "audit_contract_sha256": audit_identity.AUDIT_CONTRACT_SHA256,
    }
    if OUTCOME_COLUMNS.intersection(projected):
        raise AssertionError("v19_prospective_projection_contains_outcome")
    return projected


def _project_pairs(
    pairs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    return [
        _project_pair(parent, delayed)
        for parent, delayed in sorted(pairs, key=lambda pair: (
            float(pair[0]["close_time"]),
            str(pair[0].get("asset") or ""),
            int(pair[0]["id"]),
            int(pair[1]["id"]),
        ))
    ]


def _fingerprint(seal: Mapping[str, Any]) -> str:
    value = dict(seal)
    value.pop("seal_sha256", None)
    return _canonical_sha256(value)


def build_seal(
    parent_rows: Sequence[Mapping[str, Any]],
    delayed_rows: Sequence[Mapping[str, Any]],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    contract = load_contract()
    selection = select_prefix(parent_rows, delayed_rows)
    if selection["status"] != READY_STATUS:
        raise ValueError("v19_first_prospective_population_not_ready")
    selected = tuple(float(value) for value in selection["selected_close_times"])
    source = _project_pairs(selection["source_pairs"])
    candidate_keys = {
        (int(parent["id"]), int(delayed["id"]))
        for parent, delayed in selection["candidate_pairs"]
    }
    control_parent_ids = {
        int(parent["id"]) for parent, _ in selection["control_pairs"]
    }
    candidate = [
        row for row in source
        if (int(row["parent_id"]), int(row["delayed_id"])) in candidate_keys
    ]
    control = [
        row for row in source if int(row["parent_id"]) in control_parent_ids
    ]
    candidate_parent_ids = tuple(sorted(int(row["parent_id"]) for row in candidate))
    candidate_delayed_ids = tuple(sorted(int(row["delayed_id"]) for row in candidate))
    candidate_pair_ids = tuple(sorted(
        (int(row["parent_id"]), int(row["delayed_id"])) for row in candidate
    ))
    control_parent_ids_sorted = tuple(sorted(int(row["parent_id"]) for row in control))
    source_parent_ids = tuple(sorted(int(row["parent_id"]) for row in source))
    source_delayed_ids = tuple(sorted(int(row["delayed_id"]) for row in source))
    if (
        len(selected) < 150
        or len(candidate) < 30
        or not set(candidate_parent_ids).issubset(control_parent_ids_sorted)
        or len(source) != len(selected) * 7
        or len(source_parent_ids) != len(set(source_parent_ids))
        or len(source_delayed_ids) != len(set(source_delayed_ids))
        or any(not row["parent_sim_full_fill_supported"] for row in control)
        or any(not row["delayed_sim_full_fill_supported"] for row in candidate)
        or any(row["delayed_sim_contracts"] != 10 for row in candidate)
    ):
        raise ValueError("v19_first_prospective_geometry_invalid")
    result = {
        "seal_version": audit_identity.PROSPECTIVE_SEAL_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "status": READY_STATUS,
        "design_id": identity.DESIGN_ID,
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "parent_protocol_id": v18_identity.PROTOCOL_ID,
        "parent_protocol_sha256": v18_identity.PROTOCOL_SHA256,
        "audit_contract_id": audit_identity.AUDIT_CONTRACT_ID,
        "audit_contract_sha256": audit_identity.AUDIT_CONTRACT_SHA256,
        "cohort": "NON_BTC_TRANSFER",
        "cohort_assets": sorted(NON_BTC_ASSETS),
        "selection": contract["prospective_selection"]["selected_prefix"],
        "selected_complete_close_windows": len(selected),
        "selected_candidate_picks": len(candidate),
        "selected_control_picks": len(control),
        "selected_all_seven_parent_delayed_pairs": len(source),
        "first_selected_close_time": min(selected),
        "last_selected_close_time": max(selected),
        "selected_close_times_sha256": _canonical_sha256(selected),
        "selected_candidate_parent_ids_sha256": _canonical_sha256(
            candidate_parent_ids
        ),
        "selected_candidate_delayed_ids_sha256": _canonical_sha256(
            candidate_delayed_ids
        ),
        "selected_candidate_pair_ids_sha256": _canonical_sha256(
            candidate_pair_ids
        ),
        "selected_control_parent_ids_sha256": _canonical_sha256(
            control_parent_ids_sorted
        ),
        "selected_source_parent_ids_sha256": _canonical_sha256(
            source_parent_ids
        ),
        "selected_source_delayed_ids_sha256": _canonical_sha256(
            source_delayed_ids
        ),
        "selected_candidate_feature_evidence_sha256": _canonical_sha256(
            candidate
        ),
        "selected_control_feature_evidence_sha256": _canonical_sha256(control),
        "selected_source_feature_evidence_sha256": _canonical_sha256(source),
        "candidate_is_subset_of_control": True,
        "same_close_rows_never_split": True,
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "btc_labels_read": False,
        "resolution_status_inspected": False,
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
    if (
        seal.get("seal_sha256") != _fingerprint(seal)
        or seal.get("seal_version") != audit_identity.PROSPECTIVE_SEAL_VERSION
        or seal.get("status") != READY_STATUS
        or seal.get("design_id") != identity.DESIGN_ID
        or seal.get("protocol_id") != identity.PROTOCOL_ID
        or seal.get("protocol_sha256") != identity.PROTOCOL_SHA256
        or seal.get("parent_protocol_id") != v18_identity.PROTOCOL_ID
        or seal.get("parent_protocol_sha256") != v18_identity.PROTOCOL_SHA256
        or seal.get("audit_contract_id") != audit_identity.AUDIT_CONTRACT_ID
        or seal.get("audit_contract_sha256")
        != audit_identity.AUDIT_CONTRACT_SHA256
        or seal.get("cohort") != "NON_BTC_TRANSFER"
        or int(seal.get("selected_complete_close_windows") or 0) < 150
        or int(seal.get("selected_candidate_picks") or 0) < 30
        or int(seal.get("selected_control_picks") or 0)
        < int(seal.get("selected_candidate_picks") or 0)
        or int(seal.get("selected_all_seven_parent_delayed_pairs") or 0)
        != int(seal.get("selected_complete_close_windows") or 0) * 7
        or seal.get("candidate_is_subset_of_control") is not True
        or seal.get("same_close_rows_never_split") is not True
        or any(seal.get(key) is not False for key in (
            "outcome_columns_selected", "outcome_labels_read", "btc_labels_read",
            "resolution_status_inspected", "model_fit_performed",
            "probability_scoring_performed", "paper_artifact_created",
            "notification_eligible", "automatic_promotion", "real_trading_allowed",
        ))
    ):
        raise ValueError("v19_first_prospective_seal_invalid")


def reconstruct_examples(
    parent_rows: Sequence[Mapping[str, Any]],
    delayed_rows: Sequence[Mapping[str, Any]],
    seal: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    validate_seal(seal)
    selection = select_prefix(parent_rows, delayed_rows)
    if selection["status"] != READY_STATUS:
        raise ValueError("v19_first_prospective_population_not_ready")
    selected = tuple(float(value) for value in selection["selected_close_times"])
    source = _project_pairs(selection["source_pairs"])
    candidate_keys = {
        (int(parent["id"]), int(delayed["id"]))
        for parent, delayed in selection["candidate_pairs"]
    }
    control_parent_ids = {
        int(parent["id"]) for parent, _ in selection["control_pairs"]
    }
    candidate = [
        row for row in source
        if (int(row["parent_id"]), int(row["delayed_id"])) in candidate_keys
    ]
    control = [
        row for row in source if int(row["parent_id"]) in control_parent_ids
    ]
    if (
        _canonical_sha256(selected) != seal.get("selected_close_times_sha256")
        or _canonical_sha256(tuple(sorted(
            int(row["parent_id"]) for row in candidate
        ))) != seal.get("selected_candidate_parent_ids_sha256")
        or _canonical_sha256(tuple(sorted(
            int(row["delayed_id"]) for row in candidate
        ))) != seal.get("selected_candidate_delayed_ids_sha256")
        or _canonical_sha256(tuple(sorted(
            (int(row["parent_id"]), int(row["delayed_id"]))
            for row in candidate
        ))) != seal.get("selected_candidate_pair_ids_sha256")
        or _canonical_sha256(tuple(sorted(
            int(row["parent_id"]) for row in control
        ))) != seal.get("selected_control_parent_ids_sha256")
        or _canonical_sha256(candidate)
        != seal.get("selected_candidate_feature_evidence_sha256")
        or _canonical_sha256(control)
        != seal.get("selected_control_feature_evidence_sha256")
        or _canonical_sha256(source)
        != seal.get("selected_source_feature_evidence_sha256")
    ):
        raise ValueError("v19_first_prospective_feature_evidence_mismatch")
    return {"candidate": candidate, "control": control, "source": source}


def write_exclusive(path: Path, seal: Mapping[str, Any]) -> None:
    validate_seal(seal)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(dict(seal), handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-db", default=str(DEFAULT_DB))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    database = Path(args.strategy_db)
    parent_rows = load_feature_rows_after(
        database, identity.PROSPECTIVE_AFTER_CLOSE_TIME,
    )
    delayed_rows = load_delayed_feature_rows_after(
        database, identity.PROSPECTIVE_AFTER_CLOSE_TIME,
    )
    readiness = select_prefix(parent_rows, delayed_rows)
    if readiness["status"] != READY_STATUS:
        print(json.dumps(readiness, indent=2, sort_keys=True, allow_nan=False))
        raise SystemExit(2)
    seal = build_seal(parent_rows, delayed_rows)
    if args.write:
        write_exclusive(Path(args.output), seal)
    print(json.dumps(seal, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
