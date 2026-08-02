"""One-shot scoring of V20's sealed untouched test.

The selected models, Platt calibration, and edge margins come only from the
validated pretest artifact.  This module reserves the exact 210 untouched rows
before invoking a supplied authoritative-settlement callback.  It cannot tune,
refit, recalibrate, notify, promote, or trade.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import joblib
import numpy as np

from q15_upgrade.strategy_bots import rti_microstructure_v20_audit_identity as audit_identity
from q15_upgrade.strategy_bots import rti_microstructure_v20_features as v20_features
from q15_upgrade.strategy_bots import rti_microstructure_v20_identity as identity
from q15_upgrade.strategy_bots.costs import (
    rti_simulated_execution,
    rti_simulated_net_pnl_cents,
)
from tools import q15_rti_v20_feature_seal as feature_seal
from tools import q15_rti_v20_modeling as modeling
from tools import q15_rti_v20_pretest_runner as pretest
from tools.q15_rti_v15_label_evidence import validate_label_evidence


RESERVED_STATUS = "V20_UNTOUCHED_TEST_LABEL_ACCESS_RESERVED"
PASS_STATUS = "V20_HISTORICAL_GATES_PASSED_MANUAL_PAPER_CONSIDERATION_ONLY"
REJECT_STATUS = "V20_UNTOUCHED_TEST_GATE_FAILED_NO_PAPER_CHALLENGER"


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _sealed(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.pop("state_sha256", None)
    result["state_sha256"] = _canonical_sha256(result)
    return result


def _validate_sealed(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    expected = str(result.pop("state_sha256", ""))
    if expected != _canonical_sha256(result):
        raise ValueError("v20_untouched_test_state_sha256_invalid")
    result["state_sha256"] = expected
    return result


def _read_sealed(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("v20_untouched_test_state_unreadable") from exc
    if not isinstance(value, Mapping):
        raise ValueError("v20_untouched_test_state_root_not_object")
    return _validate_sealed(value)


def _write_exclusive(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    result = _sealed(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
    return result


def _now_iso(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("v20_untouched_test_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("v20_untouched_test_timestamp_invalid")
    return parsed.astimezone(timezone.utc).isoformat()


def result_path_for(reservation_path: Path) -> Path:
    suffix = reservation_path.suffix or ".json"
    return reservation_path.with_name(
        f"{reservation_path.stem}.result{suffix}"
    )


def _row_ids(rows: Sequence[Mapping[str, Any]]) -> tuple[int, ...]:
    return tuple(sorted(int(row["parent_id"]) for row in rows))


def _feature_identity(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    return tuple(sorted(({
        "parent_id": int(row["parent_id"]),
        "delayed_id": int(row["delayed_id"]),
        "feature_evidence_sha256": str(row["feature_evidence_sha256"]),
        "source_feature_evidence_sha256": str(
            row["source_feature_evidence_sha256"]
        ),
        "matched_benchmark_evidence_sha256": str(
            row["matched_benchmark_evidence_sha256"]
        ),
    } for row in rows), key=lambda value: value["parent_id"]))


def _load_passing_pretest(
    seal: Mapping[str, Any], reservation_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], Path]:
    if not reservation_path.exists():
        raise ValueError("v20_untouched_test_pretest_reservation_missing")
    reservation = pretest._read_sealed(reservation_path)
    expected = pretest._expected_binding(
        seal,
        label_evidence_required=bool(reservation.get("label_evidence_required")),
    )
    pretest._validate_reservation(reservation, expected)
    result_path = pretest.result_path_for(reservation_path)
    artifact_path = pretest.artifact_path_for(reservation_path)
    if not result_path.exists():
        raise ValueError("v20_untouched_test_pretest_ambiguous")
    result = pretest._read_sealed(result_path)
    pretest._validate_result(result, reservation, artifact_path, seal)
    if (
        result.get("status") != pretest.PASS_STATUS
        or result.get("manual_untouched_test_eligible") is not True
        or result.get("audit_model_bundle_created") is not True
        or result.get("untouched_test_labels_read") is not False
    ):
        raise ValueError("v20_untouched_test_pretest_gate_not_passed")
    try:
        bundle = joblib.load(artifact_path)
    except Exception as exc:  # trusted locally-created, hash-validated artifact
        raise ValueError("v20_untouched_test_model_bundle_unreadable") from exc
    return reservation, result, dict(bundle), artifact_path


def _expected_binding(
    seal: Mapping[str, Any],
    pretest_result: Mapping[str, Any],
    *,
    label_evidence_required: bool,
) -> dict[str, Any]:
    test_rows = [
        dict(row) for row in seal["rows"]
        if row["partition"] == modeling.TEST_PARTITION
    ]
    return {
        "untouched_test_runner_version": audit_identity.UNTOUCHED_TEST_RUNNER_VERSION,
        "modeling_version": audit_identity.MODELING_VERSION,
        "evaluator_contract_id": audit_identity.EVALUATOR_CONTRACT_ID,
        "evaluator_contract_sha256": audit_identity.EVALUATOR_CONTRACT_SHA256,
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "feature_seal_sha256": str(seal["seal_sha256"]),
        "pretest_result_state_sha256": str(pretest_result["state_sha256"]),
        "pretest_report_sha256": str(pretest_result["pretest_report_sha256"]),
        "audit_model_bundle_sha256": str(
            pretest_result["audit_model_bundle_sha256"]
        ),
        "untouched_test_rows": len(test_rows),
        "untouched_test_row_ids_sha256": _canonical_sha256(_row_ids(test_rows)),
        "untouched_test_feature_identity_sha256": _canonical_sha256(
            _feature_identity(test_rows)
        ),
        "label_evidence_required": bool(label_evidence_required),
    }


def _validate_reservation(
    reservation: Mapping[str, Any], expected: Mapping[str, Any],
) -> None:
    if (
        reservation.get("state_version")
        != audit_identity.UNTOUCHED_TEST_STATE_VERSION
        or reservation.get("status") != RESERVED_STATUS
    ):
        raise ValueError("v20_untouched_test_reservation_status_invalid")
    for key, value in expected.items():
        if reservation.get(key) != value:
            raise ValueError(
                f"v20_untouched_test_reservation_binding_mismatch:{key}"
            )
    if (
        int(reservation.get("untouched_test_rows") or 0) != 210
        or any(reservation.get(key) is not False for key in (
            "untouched_test_labels_read",
            "untouched_test_scoring_performed",
            "model_refit_performed",
            "recalibration_performed",
            "margin_selection_performed",
            "paper_artifact_created",
            "notification_eligible",
            "telegram_allowed",
            "automatic_promotion",
            "real_trading_allowed",
        ))
    ):
        raise ValueError("v20_untouched_test_reservation_safety_invalid")


def _normalize_settlement_labels(
    raw_labels: Mapping[int, int], expected_ids: Sequence[int],
) -> dict[int, int]:
    if not isinstance(raw_labels, Mapping):
        raise ValueError("v20_untouched_test_settlement_labels_invalid")
    labels = {}
    for key, value in raw_labels.items():
        try:
            row_id = int(key)
        except (TypeError, ValueError) as exc:
            raise ValueError("v20_untouched_test_settlement_labels_invalid") from exc
        if isinstance(value, bool) or value not in (0, 1):
            raise ValueError("v20_untouched_test_settlement_labels_invalid")
        labels[row_id] = int(value)
    if tuple(sorted(labels)) != tuple(sorted(int(value) for value in expected_ids)):
        raise ValueError("v20_untouched_test_settlement_label_identity_invalid")
    return labels


def _survival_labels(
    rows: Sequence[Mapping[str, Any]], settlement_yes: Mapping[int, int],
) -> dict[int, int]:
    return pretest._survival_labels(rows, settlement_yes)


def _wilson(successes: int, total: int, z: float) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    probability = successes / total
    denominator = 1.0 + z * z / total
    center = (probability + z * z / (2.0 * total)) / denominator
    half = z * math.sqrt(
        probability * (1.0 - probability) / total
        + z * z / (4.0 * total * total)
    ) / denominator
    return max(0.0, center - half), min(1.0, center + half)


def _maximum_drawdown(records: Sequence[Mapping[str, Any]]) -> float:
    cumulative = 0.0
    peak = 0.0
    maximum = 0.0
    for row in sorted(records, key=lambda item: (
        float(item["close_time"]), str(item["asset"]), int(item["parent_id"]),
    )):
        cumulative += float(row["pnl_cents_10"])
        peak = max(peak, cumulative)
        maximum = max(maximum, peak - cumulative)
    return maximum


def _basic_metrics(
    records: Sequence[Mapping[str, Any]], complete_windows: int,
) -> dict[str, Any]:
    count = len(records)
    correct = sum(int(row["correct"]) for row in records)
    lower, upper = _wilson(correct, count, 1.959963984540054)
    pnl = sum(float(row["pnl_cents_10"]) for row in records)
    return {
        "picks": count,
        "yes_picks": sum(row["side"] == "YES" for row in records),
        "no_picks": sum(row["side"] == "NO" for row in records),
        "correct": correct,
        "accuracy": correct / count if count else None,
        "wilson_95_low": lower,
        "wilson_95_high": upper,
        "trade_frequency_per_complete_window": count / complete_windows,
        "average_fee_slippage_adjusted_break_even": (
            sum(float(row["break_even_probability"]) for row in records) / count
            if count else None
        ),
        "fee_slippage_adjusted_pnl_cents_10_contracts": pnl,
        "ev_cents_per_10_contract_pick": pnl / count if count else None,
        "maximum_drawdown_cents_10_contracts": _maximum_drawdown(records),
        "parent_ids_sha256": _canonical_sha256(tuple(sorted(
            int(row["parent_id"]) for row in records
        ))),
    }


def _trade_record(
    row: Mapping[str, Any],
    probability: float,
    quoted_ask_cents: float,
) -> dict[str, Any]:
    execution = rti_simulated_execution(quoted_ask_cents, 10, 2.0)
    pnl_per_contract = rti_simulated_net_pnl_cents(
        quoted_ask_cents,
        bool(int(row["label_survives"])),
        10,
        2.0,
    )
    if execution is None or pnl_per_contract is None:
        raise ValueError("v20_untouched_test_execution_invalid")
    return {
        **dict(row),
        "probability": float(probability),
        "quoted_ask_cents": float(quoted_ask_cents),
        "simulated_fill_cents": float(execution["simulated_fill_cents"]),
        "break_even_probability": float(
            execution["fee_slippage_breakeven_rate"]
        ),
        "correct": int(row["label_survives"]),
        "pnl_cents_10": float(pnl_per_contract) * 10.0,
    }


def _cluster_bootstrap(
    records: Sequence[Mapping[str, Any]], *, seed: int,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    if not records:
        return {
            "clusters": 0,
            "resamples": 0,
            "mean_pnl_cents_per_pick_95": [None, None],
            "accuracy_minus_break_even_95": [None, None],
        }
    test_contract = dict(contract["untouched_test_scoring"])
    grouped: dict[float, list[Mapping[str, Any]]] = defaultdict(list)
    for row in records:
        grouped[float(row["close_time"])].append(row)
    clusters = list(grouped.values())
    resamples = int(test_contract["cluster_bootstrap_resamples"])
    low_q, high_q = test_contract["cluster_bootstrap_two_sided_quantiles"]
    rng = np.random.default_rng(seed)
    pnl_samples = np.empty(resamples, dtype=float)
    accuracy_edge_samples = np.empty(resamples, dtype=float)
    for index in range(resamples):
        selected = rng.integers(0, len(clusters), size=len(clusters))
        sample = [
            row for cluster_index in selected
            for row in clusters[int(cluster_index)]
        ]
        pnl_samples[index] = float(np.mean([
            float(row["pnl_cents_10"]) for row in sample
        ]))
        accuracy_edge_samples[index] = float(np.mean([
            float(row["correct"]) - float(row["break_even_probability"])
            for row in sample
        ]))
    return {
        "clusters": len(clusters),
        "resamples": resamples,
        "mean_pnl_cents_per_pick_95": [
            float(np.quantile(pnl_samples, low_q, method="linear")),
            float(np.quantile(pnl_samples, high_q, method="linear")),
        ],
        "accuracy_minus_break_even_95": [
            float(np.quantile(accuracy_edge_samples, low_q, method="linear")),
            float(np.quantile(accuracy_edge_samples, high_q, method="linear")),
        ],
    }


def _tier(value: float, tiers: Sequence[Mapping[str, Any]]) -> str:
    for tier in tiers:
        minimum = float(tier["minimum_inclusive"])
        maximum = tier.get("maximum_exclusive")
        if value >= minimum and (
            maximum is None or value < float(maximum)
        ):
            return str(tier["name"])
    raise ValueError("v20_untouched_test_subgroup_tier_invalid")


def _subgroup_values(
    row: Mapping[str, Any], probability: float,
    contract: Mapping[str, Any],
) -> dict[str, str]:
    subgroup = dict(contract["reporting_subgroups"])
    feature_map = dict(zip(v20_features.FEATURE_NAMES, row["features"], strict=True))
    distance = abs(float(feature_map[subgroup["distance_feature"]]))
    volatility = math.expm1(max(0.0, float(
        feature_map[subgroup["volatility_feature"]]
    )))
    efficiency = float(feature_map["spot_fast_trend_efficiency_60s"])
    agreement = float(feature_map["kalshi_spot_direction_agreement_60s"])
    regime = (
        "CHOP" if efficiency < 0.30
        else "TREND_ALIGNED" if efficiency >= 0.50 and agreement > 0.0
        else "MIXED"
    )
    settlement_ratio = abs(float(
        feature_map[subgroup["settlement_average_risk_feature"]]
    ))
    return {
        "ASSET": str(row["asset"]),
        "RTI_SIDE": str(row["side"]),
        "DISTANCE_TIER": _tier(
            distance, subgroup["distance_absolute_bps_tiers"]
        ),
        "VOLATILITY_TIER": _tier(
            volatility, subgroup["volatility_raw_bps_tiers_after_expm1"]
        ),
        "MARKET_REGIME": regime,
        "REVERSAL_RISK": _tier(
            float(probability),
            subgroup["reversal_risk_from_calibrated_survival_probability"],
        ),
        "SETTLEMENT_AVERAGE_RISK": _tier(
            settlement_ratio,
            subgroup["settlement_average_risk_absolute_ratio_tiers"],
        ),
    }


def _subgroup_report(
    records: Sequence[Mapping[str, Any]], complete_windows: int,
) -> dict[str, Any]:
    output = {}
    keys = (
        "ASSET", "RTI_SIDE", "DISTANCE_TIER", "VOLATILITY_TIER",
        "MARKET_REGIME", "REVERSAL_RISK", "SETTLEMENT_AVERAGE_RISK",
    )
    for key in keys:
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in records:
            grouped[str(row["subgroups"][key])].append(row)
        output[key] = {
            name: _basic_metrics(values, complete_windows)
            for name, values in sorted(grouped.items())
        }
    return output


def _score_cohort(
    rows: Sequence[Mapping[str, Any]],
    cohort: str,
    artifact: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    cohort_rows = [dict(row) for row in rows if row["cohort"] == cohort]
    expected_rows = 180 if cohort == "NON_BTC_TRANSFER" else 30
    if (
        len(cohort_rows) != expected_rows
        or len({float(row["close_time"]) for row in cohort_rows}) != 30
        or artifact.get("cohort") != cohort
        or artifact.get("selected_margin") is None
    ):
        raise ValueError("v20_untouched_test_cohort_geometry_invalid")
    base_probabilities = modeling._predict(
        artifact["base_model"], cohort_rows, contract,
    )
    probabilities = modeling._platt_predict(
        artifact["platt_calibrator"], base_probabilities, contract,
    )
    labels = modeling._labels(cohort_rows)
    market_index = v20_features.FEATURE_NAMES.index(
        "delayed_market_side_probability"
    )
    market_probabilities = modeling._clip_probability(np.asarray([
        float(row["features"][market_index]) for row in cohort_rows
    ]), contract)
    model_scores = modeling._proper_scores(labels, probabilities)
    market_scores = modeling._proper_scores(labels, market_probabilities)
    candidate_records = []
    rejected_records = []
    all_source_records = []
    matched_v18 = []
    matched_v19 = []
    parent_ask_index = v20_features.FEATURE_NAMES.index("parent_ask_cents")
    margin = float(artifact["selected_margin"])
    for row, probability in zip(cohort_rows, probabilities, strict=True):
        candidate = _trade_record(
            row, float(probability), float(row["entry_ask_cents"])
        )
        candidate["subgroups"] = _subgroup_values(
            row, float(probability), contract
        )
        edge = float(probability) - float(candidate["break_even_probability"])
        candidate["edge"] = edge
        all_source_records.append(candidate)
        if edge >= margin:
            candidate_records.append(candidate)
        else:
            rejected_records.append(candidate)
        if row["matched_v18_eligible"] is True:
            matched_v18.append(_trade_record(
                row,
                float(probability),
                float(row["features"][parent_ask_index]),
            ))
        if row["matched_v19_eligible"] is True:
            matched_v19.append(_trade_record(
                row, float(probability), float(row["entry_ask_cents"])
            ))
    candidate_metrics = _basic_metrics(candidate_records, 30)
    control_metrics = _basic_metrics(all_source_records, 30)
    checks = {
        "fee_slippage_adjusted_pnl_strictly_positive": (
            candidate_metrics["fee_slippage_adjusted_pnl_cents_10_contracts"]
            > 0.0
        ),
        "wilson_95_lower_strictly_exceeds_average_break_even": bool(
            candidate_metrics["wilson_95_low"] is not None
            and candidate_metrics["average_fee_slippage_adjusted_break_even"]
            is not None
            and candidate_metrics["wilson_95_low"]
            > candidate_metrics["average_fee_slippage_adjusted_break_even"]
        ),
        "all_row_log_loss_strictly_beats_market": (
            model_scores["log_loss"] < market_scores["log_loss"]
        ),
        "all_row_brier_strictly_beats_market": (
            model_scores["brier_score"] < market_scores["brier_score"]
        ),
        "maximum_drawdown_strictly_below_all_source_control": (
            candidate_metrics["maximum_drawdown_cents_10_contracts"]
            < control_metrics["maximum_drawdown_cents_10_contracts"]
        ),
        "minimum_five_yes_and_five_no_picks": (
            candidate_metrics["yes_picks"] >= 5
            and candidate_metrics["no_picks"] >= 5
        ),
    }
    passed = all(checks.values())
    seed = int(dict(contract["untouched_test_scoring"])[
        "non_btc_random_seed" if cohort == "NON_BTC_TRANSFER"
        else "btc_random_seed"
    ])
    return {
        "cohort": cohort,
        "selected_model_id": str(artifact["selected_model_id"]),
        "selected_margin": margin,
        "complete_close_windows": 30,
        "all_source_complete_rows": len(cohort_rows),
        "model_probability_metrics": {
            **model_scores,
            "market_log_loss": market_scores["log_loss"],
            "market_brier_score": market_scores["brier_score"],
        },
        "candidate": {
            "metrics": candidate_metrics,
            "close_cluster_bootstrap": _cluster_bootstrap(
                candidate_records, seed=seed, contract=contract,
            ),
            "subgroups": _subgroup_report(candidate_records, 30),
        },
        "all_source_complete_12m_side_follow_control": {
            "metrics": control_metrics,
        },
        "matched_v18_selection": {
            "metrics": _basic_metrics(matched_v18, 30),
        },
        "matched_v19_selection": {
            "metrics": _basic_metrics(matched_v19, 30),
        },
        "rejected_trade_counterfactual": {
            "metrics": _basic_metrics(rejected_records, 30),
        },
        "gate_checks": checks,
        "gate_met": passed,
        "model_refit_performed": False,
        "recalibration_performed": False,
        "margin_selection_performed": False,
    }


def evaluate_untouched_test(
    seal: Mapping[str, Any],
    survival_labels: Mapping[int, int],
    bundle: Mapping[str, Any],
    *,
    contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    contract = dict(contract or modeling.load_contract())
    if contract != modeling.load_contract():
        raise ValueError("v20_untouched_test_contract_override_forbidden")
    test_rows = [
        dict(row) for row in seal["rows"]
        if row["partition"] == modeling.TEST_PARTITION
    ]
    expected_ids = {int(row["parent_id"]) for row in test_rows}
    if (
        set(int(key) for key in survival_labels) != expected_ids
        or len(test_rows) != 210
        or bundle.get("feature_seal_sha256") != seal["seal_sha256"]
        or set(bundle.get("cohorts") or {}) != set(modeling.COHORTS)
    ):
        raise ValueError("v20_untouched_test_input_identity_invalid")
    labeled = []
    for row in test_rows:
        value = survival_labels[int(row["parent_id"])]
        if isinstance(value, bool) or value not in (0, 1):
            raise ValueError("v20_untouched_test_label_invalid")
        labeled.append({**row, "label_survives": int(value)})
    cohorts = {
        cohort: _score_cohort(
            labeled, cohort, bundle["cohorts"][cohort], contract,
        ) for cohort in modeling.COHORTS
    }
    passed = all(report["gate_met"] for report in cohorts.values())
    return {
        "untouched_test_runner_version": audit_identity.UNTOUCHED_TEST_RUNNER_VERSION,
        "modeling_version": audit_identity.MODELING_VERSION,
        "evaluator_contract_id": audit_identity.EVALUATOR_CONTRACT_ID,
        "evaluator_contract_sha256": audit_identity.EVALUATOR_CONTRACT_SHA256,
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "feature_seal_sha256": seal["seal_sha256"],
        "untouched_test_label_rows": len(survival_labels),
        "cohorts": cohorts,
        "historical_gate_met": passed,
        "status": PASS_STATUS if passed else REJECT_STATUS,
        "independent_final_historical_confirmation": True,
        "test_guided_refit_recalibration_or_margin_selection": False,
        "untouched_test_labels_read": True,
        "untouched_test_scoring_performed": True,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "telegram_allowed": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }


def _validate_result(
    result: Mapping[str, Any],
    reservation: Mapping[str, Any],
    seal: Mapping[str, Any],
) -> None:
    report = result.get("untouched_test_report")
    settlement_rows = result.get("settlement_label_rows")
    survival_rows = result.get("survival_label_rows")
    if (
        not isinstance(report, Mapping)
        or not isinstance(settlement_rows, list)
        or not isinstance(survival_rows, list)
    ):
        raise ValueError("v20_untouched_test_result_invalid")
    settlement_pairs = sorted(
        [int(row["parent_id"]), int(row["result_yes"])]
        for row in settlement_rows
    )
    survival_pairs = sorted(
        [int(row["parent_id"]), int(row["label_survives"])]
        for row in survival_rows
    )
    ids = [row_id for row_id, _label in settlement_pairs]
    test_rows = [
        dict(row) for row in seal["rows"]
        if row["partition"] == modeling.TEST_PARTITION
    ]
    expected_survival = sorted(
        [int(row_id), int(label)]
        for row_id, label in _survival_labels(
            test_rows, dict(settlement_pairs)
        ).items()
    )
    passed = report.get("historical_gate_met") is True
    expected_status = PASS_STATUS if passed else REJECT_STATUS
    evidence = result.get("label_read_evidence")
    if reservation.get("label_evidence_required") is True:
        if not isinstance(evidence, Mapping):
            raise ValueError("v20_untouched_test_result_invalid")
        stored = type(
            "_StoredVerifiedLabels", (dict,),
            {"audit_evidence": dict(evidence)},
        )(dict(settlement_pairs))
        verified = validate_label_evidence(
            stored, dict(settlement_pairs), ids,
            required=True, stage="v20_untouched_test",
        )
        if (
            verified is None
            or result.get("label_read_evidence_sha256")
            != verified.get("evidence_sha256")
        ):
            raise ValueError("v20_untouched_test_result_invalid")
    elif evidence is not None or result.get("label_read_evidence_sha256") is not None:
        raise ValueError("v20_untouched_test_result_invalid")
    if (
        result.get("state_version")
        != audit_identity.UNTOUCHED_TEST_STATE_VERSION
        or result.get("untouched_test_runner_version")
        != audit_identity.UNTOUCHED_TEST_RUNNER_VERSION
        or result.get("status") != expected_status
        or result.get("reservation_state_sha256")
        != reservation.get("state_sha256")
        or result.get("feature_seal_sha256")
        != reservation.get("feature_seal_sha256")
        or len(settlement_pairs) != 210
        or len(survival_pairs) != 210
        or len(set(ids)) != 210
        or tuple(sorted(ids)) != tuple(row_id for row_id, _ in survival_pairs)
        or survival_pairs != expected_survival
        or _canonical_sha256(tuple(sorted(ids)))
        != reservation.get("untouched_test_row_ids_sha256")
        or result.get("settlement_labels_sha256")
        != _canonical_sha256(settlement_pairs)
        or result.get("survival_labels_sha256")
        != _canonical_sha256(survival_pairs)
        or result.get("untouched_test_report_sha256")
        != _canonical_sha256(dict(report))
        or report.get("feature_seal_sha256")
        != reservation.get("feature_seal_sha256")
        or report.get("evaluator_contract_sha256")
        != audit_identity.EVALUATOR_CONTRACT_SHA256
        or report.get("untouched_test_label_rows") != 210
        or report.get("test_guided_refit_recalibration_or_margin_selection")
        is not False
        or result.get("untouched_test_labels_read_once") is not True
        or result.get("model_refit_performed") is not False
        or result.get("recalibration_performed") is not False
        or result.get("margin_selection_performed") is not False
        or result.get("manual_paper_challenger_eligible") != passed
        or any(result.get(key) is not False for key in (
            "paper_artifact_created",
            "notification_eligible",
            "telegram_allowed",
            "automatic_promotion",
            "real_trading_allowed",
        ))
    ):
        raise ValueError("v20_untouched_test_result_invalid")


def run_untouched_test_once(
    *,
    seal: Mapping[str, Any],
    pretest_reservation_path: Path,
    reservation_path: Path,
    confirmation: str,
    read_settlement_yes_labels: Callable[
        [Sequence[int]], Mapping[int, int]
    ],
    require_label_evidence: bool = True,
    timestamp: str | None = None,
) -> dict[str, Any]:
    feature_seal.validate_seal(seal)
    modeling.load_contract()
    _pretest_reservation, pretest_result, bundle, _artifact_path = (
        _load_passing_pretest(seal, pretest_reservation_path)
    )
    expected = _expected_binding(
        seal,
        pretest_result,
        label_evidence_required=require_label_evidence,
    )
    result_path = result_path_for(reservation_path)
    if reservation_path.exists():
        reservation = _read_sealed(reservation_path)
        _validate_reservation(reservation, expected)
        if result_path.exists():
            result = _read_sealed(result_path)
            _validate_result(result, reservation, seal)
            return {
                "status": "ALREADY_FINALIZED_NO_REREAD",
                "untouched_test_labels_read_this_call": False,
                "reservation": reservation,
                "result": result,
            }
        return {
            "status": "AMBIGUOUS_RESERVED_NO_REREAD",
            "untouched_test_labels_read_this_call": False,
            "reservation": reservation,
            "result": None,
        }
    if result_path.exists():
        raise ValueError("v20_untouched_test_result_exists_without_reservation")
    if confirmation != audit_identity.UNTOUCHED_TEST_CONFIRMATION:
        raise ValueError(
            "v20_untouched_test_explicit_one_shot_confirmation_required"
        )
    reservation = _write_exclusive(reservation_path, {
        **expected,
        "state_version": audit_identity.UNTOUCHED_TEST_STATE_VERSION,
        "status": RESERVED_STATUS,
        "reserved_at": _now_iso(timestamp),
        "untouched_test_labels_read": False,
        "untouched_test_scoring_performed": False,
        "model_refit_performed": False,
        "recalibration_performed": False,
        "margin_selection_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "telegram_allowed": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    })
    rows = [
        dict(row) for row in seal["rows"]
        if row["partition"] == modeling.TEST_PARTITION
    ]
    ids = _row_ids(rows)
    raw_labels = read_settlement_yes_labels(ids)
    settlement_yes = _normalize_settlement_labels(raw_labels, ids)
    label_evidence = validate_label_evidence(
        raw_labels, settlement_yes, ids,
        required=require_label_evidence,
        stage="v20_untouched_test",
    )
    survival = _survival_labels(rows, settlement_yes)
    report = evaluate_untouched_test(seal, survival, bundle)
    passed = report["historical_gate_met"] is True
    result = _write_exclusive(result_path, {
        "state_version": audit_identity.UNTOUCHED_TEST_STATE_VERSION,
        "untouched_test_runner_version": audit_identity.UNTOUCHED_TEST_RUNNER_VERSION,
        "status": PASS_STATUS if passed else REJECT_STATUS,
        "finalized_at": _now_iso(timestamp),
        "reservation_state_sha256": reservation["state_sha256"],
        "feature_seal_sha256": reservation["feature_seal_sha256"],
        "pretest_result_state_sha256": reservation[
            "pretest_result_state_sha256"
        ],
        "audit_model_bundle_sha256": reservation[
            "audit_model_bundle_sha256"
        ],
        "untouched_test_row_ids_sha256": reservation[
            "untouched_test_row_ids_sha256"
        ],
        "settlement_labels_sha256": _canonical_sha256(sorted(
            [int(row_id), int(label)]
            for row_id, label in settlement_yes.items()
        )),
        "survival_labels_sha256": _canonical_sha256(sorted(
            [int(row_id), int(label)]
            for row_id, label in survival.items()
        )),
        "settlement_label_rows": [
            {"parent_id": int(row_id), "result_yes": int(label)}
            for row_id, label in sorted(settlement_yes.items())
        ],
        "survival_label_rows": [
            {"parent_id": int(row_id), "label_survives": int(label)}
            for row_id, label in sorted(survival.items())
        ],
        "label_read_evidence": label_evidence,
        "label_read_evidence_sha256": (
            label_evidence["evidence_sha256"]
            if label_evidence is not None else None
        ),
        "untouched_test_report": report,
        "untouched_test_report_sha256": _canonical_sha256(report),
        "untouched_test_labels_read_once": True,
        "untouched_test_scoring_performed": True,
        "model_refit_performed": False,
        "recalibration_performed": False,
        "margin_selection_performed": False,
        "manual_paper_challenger_eligible": passed,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "telegram_allowed": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    })
    _validate_result(result, reservation, seal)
    return {
        "status": result["status"],
        "untouched_test_labels_read_this_call": True,
        "reservation": reservation,
        "result": result,
    }
