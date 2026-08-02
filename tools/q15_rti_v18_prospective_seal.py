"""Outcome-blind exclusive seal for V18's first prospective review."""
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
from q15_upgrade.strategy_bots import rti_microstructure_v18_audit_identity as audit_identity
from q15_upgrade.strategy_bots import rti_microstructure_v18_identity as identity
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


READY_STATUS = "V18_FIRST_PROSPECTIVE_FEATURES_SEALED_LABELS_CLOSED"
NOT_READY_STATUS = "V18_FIRST_PROSPECTIVE_FEATURES_NOT_READY"
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
    contract = _load(path, "v18_first_review_contract_unreadable")
    selection = dict(contract.get("prospective_selection") or {})
    access = dict(contract.get("label_access") or {})
    result = dict(contract.get("result_policy") or {})
    amendment = dict(
        contract.get("outcome_blind_source_integration_amendment") or {}
    )
    if (
        design_fingerprint(contract) != audit_identity.AUDIT_CONTRACT_SHA256
        or contract.get("contract_id") != audit_identity.AUDIT_CONTRACT_ID
        or contract.get("contract_status")
        != "FROZEN_BEFORE_ANY_V18_PROSPECTIVE_OUTCOME_ACCESS"
        or contract.get("protocol_id") != identity.PROTOCOL_ID
        or contract.get("protocol_sha256") != identity.PROTOCOL_SHA256
        or contract.get("cohort") != "NON_BTC_TRANSFER"
        or set(contract.get("assets") or ()) != NON_BTC_ASSETS
        or contract.get("btc_labels_forbidden") is not True
        or float(selection.get("prospective_after_close_time") or 0.0)
        != identity.PROSPECTIVE_AFTER_CLOSE_TIME
        or int(selection.get("minimum_complete_close_windows") or 0) != 150
        or int(selection.get("minimum_candidate_picks") or 0) != 30
        or selection.get("complete_window_definition")
        != "ALL_SEVEN_V18_SOURCE_QUALITY_COMPLETE_EXACT_13M_ROWS"
        or selection.get("source_quality_rule_version")
        != "q15-rti-v18-all-seven-exact-source-quality-v1"
        or selection.get("unrelated_v17_model_feature_availability_required")
        is not False
        or selection.get("same_close_rows_never_split") is not True
        or selection.get("outcomes_or_resolution_status_used_for_selection")
        is not False
        or access.get("confirmation_phrase")
        != audit_identity.CONFIRMATION_PHRASE
        or access.get("exclusive_reservation_before_callback") is not True
        or access.get("fresh_authoritative_kalshi_finalized_evidence_required")
        is not True
        or access.get("btc_labels_forbidden") is not True
        or result.get("paper_artifact_allowed_by_this_command") is not False
        or result.get("notifications_allowed_by_this_command") is not False
        or result.get("telegram_allowed_by_this_command") is not False
        or result.get("automatic_promotion_allowed") is not False
        or result.get("real_trading_allowed") is not False
        or amendment.get("prospective_outcomes_or_resolution_status_inspected")
        is not False
        or amendment.get("entry_side_price_spread_depth_or_risk_threshold_changed")
        is not False
        or amendment.get("candidate_or_control_rule_changed") is not False
        or amendment.get("historical_credit_claimed") is not False
        or contract.get("outcome_labels_used_to_create_contract") is not False
        or contract.get("prospective_resolution_status_inspected_to_create_contract")
        is not False
    ):
        raise ValueError("v18_first_review_contract_identity_or_safety_invalid")
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


def _complete_windows(
    rows: Sequence[Mapping[str, Any]],
) -> dict[float, list[dict[str, Any]]]:
    """Return all-seven exact windows with frozen V18 source quality."""
    grouped: dict[float, list[dict[str, Any]]] = defaultdict(list)
    for raw in rows:
        if (
            str(raw.get("bot_name") or "") != "rti_path_13m"
            or str(raw.get("interval") or "").upper() != "13M"
            or str(raw.get("record_kind") or "").upper()
            != "RTI_PATH_13M_PROSPECTIVE_EXACT"
        ):
            continue
        try:
            close_time = float(raw["close_time"])
        except (KeyError, TypeError, ValueError):
            continue
        if close_time > identity.PROSPECTIVE_AFTER_CLOSE_TIME:
            grouped[close_time].append(dict(raw))
    output: dict[float, list[dict[str, Any]]] = {}
    for close_time, window_rows in sorted(grouped.items()):
        assets = {str(row.get("asset") or "").upper() for row in window_rows}
        if len(window_rows) != 7 or assets != EXPECTED_ASSETS:
            continue
        if any(
            not validate_exact_contract_identity(row).get("valid")
            or v18.evaluate_source_row(row).get("available") is not True
            for row in window_rows
        ):
            continue
        output[float(close_time)] = [dict(row) for row in window_rows]
    return output


def select_prefix(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    complete = _complete_windows(rows)
    future = tuple(
        close_time for close_time in sorted(complete)
        if close_time > identity.PROSPECTIVE_AFTER_CLOSE_TIME
    )
    candidate_by_close: dict[float, list[dict[str, Any]]] = {}
    control_by_close: dict[float, list[dict[str, Any]]] = {}
    candidate_total = 0
    terminal_index: int | None = None
    for index, close_time in enumerate(future):
        candidates = []
        controls = []
        for row in complete[close_time]:
            if str(row.get("asset") or "").upper() == "BTC":
                continue
            control = v18.evaluate_strict_control_row(row)
            candidate = v18.evaluate_row(row)
            if candidate["eligible"] and not control["eligible"]:
                raise ValueError("v18_candidate_not_control_subset")
            if control["eligible"]:
                controls.append(dict(row))
            if candidate["eligible"]:
                candidates.append(dict(row))
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
            "candidate_picks": sum(
                len(candidate_by_close[close_time]) for close_time in future
            ),
            "candidate_picks_required": 30,
            "candidate_picks_remaining": max(0, 30 - sum(
                len(candidate_by_close[close_time]) for close_time in future
            )),
            "outcome_labels_read": False,
        }
    selected = future[:terminal_index + 1]
    source_rows = [row for close_time in selected for row in complete[close_time]]
    candidate_rows = [
        row for close_time in selected for row in candidate_by_close[close_time]
    ]
    control_rows = [
        row for close_time in selected for row in control_by_close[close_time]
    ]
    return {
        "status": READY_STATUS,
        "selected_close_times": selected,
        "source_rows": source_rows,
        "candidate_rows": candidate_rows,
        "control_rows": control_rows,
        "outcome_labels_read": False,
    }


def _project(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for raw in sorted(rows, key=lambda row: (
        float(row["close_time"]), str(row.get("asset") or ""), int(row["id"]),
    )):
        contract = validate_exact_contract_identity(raw)
        source = v18.evaluate_source_row(raw)
        if (
            contract.get("valid") is not True
            or source.get("available") is not True
        ):
            raise ValueError("v18_prospective_source_or_contract_identity_failure")
        profile = _profile(raw)
        candidate = v18.evaluate_row(raw)
        control = v18.evaluate_strict_control_row(raw)
        source_evidence = dict(source["evidence"])
        projected = {
            "id": int(raw["id"]),
            "ticker": str(raw.get("ticker") or ""),
            "asset": str(raw.get("asset") or "").upper(),
            "side": str(raw.get("side") or "").upper(),
            "close_time": float(raw["close_time"]),
            "source_captured_at": float(raw["source_captured_at"]),
            "evidence_as_of": float(raw["evidence_as_of"]),
            "entry_ask_cents": float(raw["entry_ask_cents"]),
            "spread_cents": float(raw["spread_cents"]),
            "depth_contracts": float(raw.get("depth_contracts") or 0.0),
            "sim_full_fill_supported": (
                v18._flag(profile.get("sim_full_fill_supported")) is True
            ),
            "contract_identity_version": (
                contract.get("version") or CONTRACT_IDENTITY_VERSION
            ),
            "contract_identity_valid": True,
            "v18_source_quality_rule_version": source["rule_version"],
            "v18_source_quality_evidence_sha256": source[
                "feature_evidence_sha256"
            ],
            "strict_control_eligible": control["eligible"] is True,
            "v18_candidate_eligible": candidate["eligible"] is True,
            "strict_control_feature_evidence_sha256": control[
                "feature_evidence_sha256"
            ],
            "v18_feature_evidence_sha256": candidate["feature_evidence_sha256"],
            "strict_rule_version": source_evidence["strict_rule_version"],
            "risk_policy_version": source_evidence["risk_policy_version"],
            "reversal_risk_class": source_evidence["reversal_risk_class"],
            "settlement_average_risk_class": str(
                profile.get("rti_settlement_average_risk_class") or ""
            ),
            "path_regime_class": str(profile.get("rti_path_regime_class") or ""),
            "path_strike_crossings": int(
                profile.get("rti_path_strike_crossings") or 0
            ),
            "path_persistence": float(profile.get("rti_path_persistence") or 0.0),
            "path_trend_efficiency": float(
                profile.get("rti_path_trend_efficiency") or 0.0
            ),
            "signed_distance_bps": float(
                profile.get("rti_signed_distance_bps") or 0.0
            ),
            "audit_contract_sha256": audit_identity.AUDIT_CONTRACT_SHA256,
        }
        if OUTCOME_COLUMNS.intersection(projected):
            raise AssertionError("v18_prospective_projection_contains_outcome")
        output.append(projected)
    return output


def _fingerprint(seal: Mapping[str, Any]) -> str:
    value = dict(seal)
    value.pop("seal_sha256", None)
    return _canonical_sha256(value)


def build_seal(
    rows: Sequence[Mapping[str, Any]], *, generated_at: str | None = None,
) -> dict[str, Any]:
    contract = load_contract()
    selection = select_prefix(rows)
    if selection["status"] != READY_STATUS:
        raise ValueError("v18_first_prospective_population_not_ready")
    selected = tuple(float(value) for value in selection["selected_close_times"])
    source_rows = list(selection["source_rows"])
    candidate_raw = list(selection["candidate_rows"])
    control_raw = list(selection["control_rows"])
    source_projected = _project(source_rows)
    projected_by_id = {int(row["id"]): row for row in source_projected}
    candidate = [projected_by_id[int(row["id"])] for row in candidate_raw]
    control = [projected_by_id[int(row["id"])] for row in control_raw]
    candidate_ids = tuple(sorted(int(row["id"]) for row in candidate))
    control_ids = tuple(sorted(int(row["id"]) for row in control))
    source_ids = tuple(sorted(int(row["id"]) for row in source_projected))
    if (
        len(selected) < 150
        or len(candidate_ids) < 30
        or not set(candidate_ids).issubset(control_ids)
        or len(source_ids) != len(selected) * 7
        or any(not row["sim_full_fill_supported"] for row in control)
    ):
        raise ValueError("v18_first_prospective_geometry_invalid")
    result = {
        "seal_version": audit_identity.PROSPECTIVE_SEAL_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "status": READY_STATUS,
        "design_id": identity.DESIGN_ID,
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "audit_contract_id": audit_identity.AUDIT_CONTRACT_ID,
        "audit_contract_sha256": audit_identity.AUDIT_CONTRACT_SHA256,
        "cohort": "NON_BTC_TRANSFER",
        "cohort_assets": sorted(NON_BTC_ASSETS),
        "selection": contract["prospective_selection"]["selected_prefix"],
        "selected_complete_close_windows": len(selected),
        "selected_candidate_picks": len(candidate_ids),
        "selected_control_picks": len(control_ids),
        "selected_all_seven_source_rows": len(source_ids),
        "first_selected_close_time": min(selected),
        "last_selected_close_time": max(selected),
        "selected_close_times_sha256": _canonical_sha256(selected),
        "selected_candidate_row_ids_sha256": _canonical_sha256(candidate_ids),
        "selected_control_row_ids_sha256": _canonical_sha256(control_ids),
        "selected_source_row_ids_sha256": _canonical_sha256(source_ids),
        "selected_candidate_feature_evidence_sha256": _canonical_sha256(candidate),
        "selected_control_feature_evidence_sha256": _canonical_sha256(control),
        "selected_source_feature_evidence_sha256": _canonical_sha256(source_projected),
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
        or seal.get("audit_contract_id") != audit_identity.AUDIT_CONTRACT_ID
        or seal.get("audit_contract_sha256")
        != audit_identity.AUDIT_CONTRACT_SHA256
        or seal.get("cohort") != "NON_BTC_TRANSFER"
        or int(seal.get("selected_complete_close_windows") or 0) < 150
        or int(seal.get("selected_candidate_picks") or 0) < 30
        or int(seal.get("selected_control_picks") or 0)
        < int(seal.get("selected_candidate_picks") or 0)
        or int(seal.get("selected_all_seven_source_rows") or 0)
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
        raise ValueError("v18_first_prospective_seal_invalid")


def reconstruct_examples(
    rows: Sequence[Mapping[str, Any]], seal: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    validate_seal(seal)
    selection = select_prefix(rows)
    if selection["status"] != READY_STATUS:
        raise ValueError("v18_first_prospective_population_not_ready")
    selected = tuple(float(value) for value in selection["selected_close_times"])
    projected = _project(selection["source_rows"])
    by_id = {int(row["id"]): row for row in projected}
    candidate = [by_id[int(row["id"])] for row in selection["candidate_rows"]]
    control = [by_id[int(row["id"])] for row in selection["control_rows"]]
    if (
        _canonical_sha256(selected) != seal.get("selected_close_times_sha256")
        or _canonical_sha256(tuple(sorted(int(row["id"]) for row in candidate)))
        != seal.get("selected_candidate_row_ids_sha256")
        or _canonical_sha256(tuple(sorted(int(row["id"]) for row in control)))
        != seal.get("selected_control_row_ids_sha256")
        or _canonical_sha256(candidate)
        != seal.get("selected_candidate_feature_evidence_sha256")
        or _canonical_sha256(control)
        != seal.get("selected_control_feature_evidence_sha256")
    ):
        raise ValueError("v18_first_prospective_feature_evidence_mismatch")
    return {"candidate": candidate, "control": control, "source": projected}


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
    rows = load_feature_rows_after(
        Path(args.strategy_db), identity.PROSPECTIVE_AFTER_CLOSE_TIME,
    )
    readiness = select_prefix(rows)
    if readiness["status"] != READY_STATUS:
        print(json.dumps(readiness, indent=2, sort_keys=True, allow_nan=False))
        raise SystemExit(2)
    seal = build_seal(rows)
    if args.write:
        write_exclusive(Path(args.output), seal)
    print(json.dumps(seal, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
