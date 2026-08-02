"""Pure in-memory evaluator for V18's first prospective manual review."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.entry_economics.costs import kalshi_fee_cents
from q15_upgrade.strategy_bots import rti_microstructure_v18_audit_identity as identity
from tools import q15_rti_v18_prospective_seal as prospective_seal


CONTRACTS = 10
SLIPPAGE_CENTS = 2.0


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _wilson(correct: int, count: int) -> tuple[float | None, float | None]:
    if count <= 0:
        return None, None
    z = 1.959963984540054
    rate = correct / count
    denominator = 1.0 + z * z / count
    centre = rate + z * z / (2.0 * count)
    margin = z * math.sqrt(
        rate * (1.0 - rate) / count + z * z / (4.0 * count * count)
    )
    return (
        max(0.0, (centre - margin) / denominator),
        min(1.0, (centre + margin) / denominator),
    )


def _scored(row: Mapping[str, Any]) -> dict[str, Any]:
    side = str(row.get("side") or "").upper()
    label = int(row.get("label_yes", -1))
    ask = float(row["entry_ask_cents"])
    if (
        side not in {"YES", "NO"}
        or label not in {0, 1}
        or not 0.0 <= ask <= 97.0
        or row.get("sim_full_fill_supported") is not True
    ):
        raise ValueError("v18_first_review_execution_evidence_invalid")
    fill = min(99.0, ask + SLIPPAGE_CENTS)
    fee = float(kalshi_fee_cents(
        fill, contracts=CONTRACTS, rate=0.07, ceil=True,
    ))
    correct = (side == "YES") == bool(label)
    pnl_per_contract = (100.0 - fill - fee) if correct else (-fill - fee)
    return {
        **dict(row),
        "correct": bool(correct),
        "fill_cents": fill,
        "fee_cents_per_contract": fee,
        "break_even_probability": (fill + fee) / 100.0,
        "pnl_cents_10_contracts": pnl_per_contract * CONTRACTS,
    }


def _metrics(rows: Sequence[Mapping[str, Any]], complete_windows: int) -> dict[str, Any]:
    scored = sorted(
        [_scored(row) for row in rows],
        key=lambda row: (float(row["close_time"]), int(row["id"])),
    )
    count = len(scored)
    correct = sum(bool(row["correct"]) for row in scored)
    low, high = _wilson(correct, count)
    pnl = sum(float(row["pnl_cents_10_contracts"]) for row in scored)
    cumulative = 0.0
    peak = 0.0
    drawdown = 0.0
    for row in scored:
        cumulative += float(row["pnl_cents_10_contracts"])
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    break_even = (
        sum(float(row["break_even_probability"]) for row in scored) / count
        if count else None
    )
    return {
        "picks": count,
        "correct": correct,
        "accuracy": correct / count if count else None,
        "wilson_95_low": low,
        "wilson_95_high": high,
        "average_fee_slippage_adjusted_break_even": break_even,
        "fee_slippage_adjusted_pnl_cents_10_contracts": pnl,
        "fee_slippage_adjusted_pnl_dollars_10_contracts": pnl / 100.0,
        "ev_cents_per_pick_10_contracts": pnl / count if count else None,
        "maximum_drawdown_cents_10_contracts": drawdown,
        "maximum_drawdown_dollars_10_contracts": drawdown / 100.0,
        "complete_close_windows": int(complete_windows),
        "pick_frequency_per_complete_window": (
            count / complete_windows if complete_windows else None
        ),
        "estimated_picks_per_96_window_day": (
            count / complete_windows * 96.0 if complete_windows else None
        ),
        "yes_picks": sum(str(row["side"]).upper() == "YES" for row in scored),
        "no_picks": sum(str(row["side"]).upper() == "NO" for row in scored),
        "pick_close_windows": len({float(row["close_time"]) for row in scored}),
    }


def _subgroups(
    rows: Sequence[Mapping[str, Any]], complete_windows: int,
) -> dict[str, Any]:
    fields = {
        "by_asset": "asset",
        "by_side": "side",
        "by_reversal_risk": "reversal_risk_class",
        "by_settlement_average_risk": "settlement_average_risk_class",
        "by_path_regime": "path_regime_class",
    }
    output = {}
    for output_name, field in fields.items():
        values: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            values[str(row.get(field) or "UNKNOWN")].append(row)
        output[output_name] = {
            key: _metrics(group, complete_windows)
            for key, group in sorted(values.items())
        }
    return output


def _chronological_halves(
    rows: Sequence[Mapping[str, Any]], complete_windows: int,
) -> dict[str, Any]:
    close_times = tuple(sorted({float(row["close_time"]) for row in rows}))
    split = len(close_times) // 2
    first_times = set(close_times[:split])
    second_times = set(close_times[split:])
    return {
        "same_close_rows_never_split": True,
        "first_half": _metrics([
            row for row in rows if float(row["close_time"]) in first_times
        ], complete_windows),
        "second_half": _metrics([
            row for row in rows if float(row["close_time"]) in second_times
        ], complete_windows),
    }


def clustered_bootstrap(
    rows: Sequence[Mapping[str, Any]], *, resamples: int, seed: int,
) -> dict[str, Any]:
    scored = [_scored(row) for row in rows]
    clustered: dict[float, list[dict[str, Any]]] = defaultdict(list)
    for row in scored:
        clustered[float(row["close_time"])].append(row)
    close_times = tuple(sorted(clustered))
    if not close_times or resamples != 10000:
        raise ValueError("v18_first_review_bootstrap_geometry_invalid")
    # Resample whole close-window clusters, then recompute the pick-level mean
    # from each sampled cluster's totals and pick count.  Averaging the cluster
    # means would over-weight windows containing one pick and under-weight
    # windows containing multiple simultaneous picks.
    cluster_totals = np.asarray([[
        sum(float(row["pnl_cents_10_contracts"]) for row in clustered[close]),
        sum(
            (1.0 if row["correct"] else 0.0)
            - float(row["break_even_probability"])
            for row in clustered[close]
        ),
        len(clustered[close]),
    ] for close in close_times], dtype=float)
    rng = np.random.default_rng(int(seed))
    indexes = rng.integers(
        0, len(close_times), size=(resamples, len(close_times)), endpoint=False,
    )
    sampled_totals = cluster_totals[indexes].sum(axis=1)
    means = sampled_totals[:, :2] / sampled_totals[:, 2, np.newaxis]

    def summary(index: int) -> dict[str, float]:
        samples = means[:, index]
        return {
            "observed_mean": float(
                cluster_totals[:, index].sum() / cluster_totals[:, 2].sum()
            ),
            "one_sided_lower_90": float(np.quantile(samples, 0.1)),
            "two_sided_lower_90": float(np.quantile(samples, 0.05)),
            "two_sided_upper_90": float(np.quantile(samples, 0.95)),
            "probability_above_zero": float(np.mean(samples > 0.0)),
        }

    return {
        "version": "q15-rti-v18-candidate-close-cluster-bootstrap-v2",
        "cluster_key": "close_time",
        "same_close_picks_resampled_together": True,
        "pick_level_mean_recomputed_after_cluster_resampling": True,
        "close_windows_with_candidate_picks": len(close_times),
        "picks": len(rows),
        "resamples": resamples,
        "confidence_level": 0.9,
        "random_seed": int(seed),
        "mean_pnl_cents_10_contracts": summary(0),
        "accuracy_minus_break_even": summary(1),
    }


def evaluate_first_review(
    labeled_control: Sequence[Mapping[str, Any]],
    *,
    candidate_ids: Sequence[int],
    seal: Mapping[str, Any],
    contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    prospective_seal.validate_seal(seal)
    contract = dict(contract or prospective_seal.load_contract())
    if contract != prospective_seal.load_contract():
        raise ValueError("v18_first_review_contract_identity_invalid")
    control_ids = tuple(sorted(int(row["id"]) for row in labeled_control))
    candidate_id_set = {int(value) for value in candidate_ids}
    if (
        len(control_ids) != len(set(control_ids))
        or _canonical_sha256(control_ids)
        != seal.get("selected_control_row_ids_sha256")
        or _canonical_sha256(tuple(sorted(candidate_id_set)))
        != seal.get("selected_candidate_row_ids_sha256")
        or not candidate_id_set.issubset(control_ids)
        or any(str(row.get("asset") or "").upper() == "BTC" for row in labeled_control)
        or any(int(row.get("label_yes", -1)) not in {0, 1} for row in labeled_control)
    ):
        raise ValueError("v18_first_review_input_identity_invalid")
    candidate = [
        row for row in labeled_control if int(row["id"]) in candidate_id_set
    ]
    rejected = [
        row for row in labeled_control if int(row["id"]) not in candidate_id_set
    ]
    complete_windows = int(seal["selected_complete_close_windows"])
    candidate_metrics = _metrics(candidate, complete_windows)
    control_metrics = _metrics(labeled_control, complete_windows)
    rejected_metrics = _metrics(rejected, complete_windows)
    halves = _chronological_halves(candidate, complete_windows)
    bootstrap_config = dict(dict(contract["metrics"])["same_close_clustered_bootstrap"])
    bootstrap = clustered_bootstrap(
        candidate,
        resamples=int(bootstrap_config["resamples"]),
        seed=int(bootstrap_config["random_seed"]),
    )
    checks = {
        "candidate_resolved_picks_minimum": candidate_metrics["picks"] >= 30,
        "complete_close_windows_minimum": complete_windows >= 150,
        "candidate_fee_slippage_adjusted_pnl_positive": (
            candidate_metrics["fee_slippage_adjusted_pnl_cents_10_contracts"] > 0.0
        ),
        "candidate_wilson_lower_exceeds_average_break_even": bool(
            candidate_metrics["wilson_95_low"] is not None
            and candidate_metrics["average_fee_slippage_adjusted_break_even"] is not None
            and candidate_metrics["wilson_95_low"]
            > candidate_metrics["average_fee_slippage_adjusted_break_even"]
        ),
        "candidate_accuracy_not_below_control": bool(
            candidate_metrics["accuracy"] is not None
            and control_metrics["accuracy"] is not None
            and candidate_metrics["accuracy"] >= control_metrics["accuracy"]
        ),
        "candidate_maximum_drawdown_below_control": bool(
            candidate_metrics["maximum_drawdown_cents_10_contracts"]
            < control_metrics["maximum_drawdown_cents_10_contracts"]
        ),
        "candidate_yes_picks_minimum": candidate_metrics["yes_picks"] >= 5,
        "candidate_no_picks_minimum": candidate_metrics["no_picks"] >= 5,
        "candidate_first_half_pnl_positive": (
            halves["first_half"]["fee_slippage_adjusted_pnl_cents_10_contracts"] > 0.0
        ),
        "candidate_second_half_pnl_positive": (
            halves["second_half"]["fee_slippage_adjusted_pnl_cents_10_contracts"] > 0.0
        ),
        "candidate_clustered_mean_pnl_lower_above_zero": (
            bootstrap["mean_pnl_cents_10_contracts"]["one_sided_lower_90"] > 0.0
        ),
        "candidate_clustered_accuracy_minus_break_even_lower_above_zero": (
            bootstrap["accuracy_minus_break_even"]["one_sided_lower_90"] > 0.0
        ),
    }
    passed = all(checks.values())
    return {
        "evaluator_version": identity.EVALUATOR_VERSION,
        "audit_contract_id": identity.AUDIT_CONTRACT_ID,
        "audit_contract_sha256": identity.AUDIT_CONTRACT_SHA256,
        "protocol_id": seal["protocol_id"],
        "protocol_sha256": seal["protocol_sha256"],
        "prospective_seal_sha256": seal["seal_sha256"],
        "cohort": "NON_BTC_TRANSFER",
        "control_input_rows": len(labeled_control),
        "candidate_input_rows": len(candidate),
        "selected_complete_close_windows": complete_windows,
        "candidate": {
            "metrics": candidate_metrics,
            "chronological_halves": halves,
            "subgroups": _subgroups(candidate, complete_windows),
            "clustered_bootstrap": bootstrap,
        },
        "strict_control": {
            "metrics": control_metrics,
            "subgroups": _subgroups(labeled_control, complete_windows),
        },
        "rejected_trade_counterfactual": {
            "metrics": rejected_metrics,
            "subgroups": _subgroups(rejected, complete_windows),
        },
        "gate_checks": checks,
        "gate_met": passed,
        "outcome_labels_read": True,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
        "control_row_ids_sha256": _canonical_sha256(control_ids),
        "candidate_row_ids_sha256": _canonical_sha256(tuple(sorted(candidate_id_set))),
        "scored_rows_sha256": _canonical_sha256(sorted([
            int(row["id"]), int(row["label_yes"]),
            float(_scored(row)["fill_cents"]),
            float(_scored(row)["fee_cents_per_contract"]),
            float(_scored(row)["pnl_cents_10_contracts"]),
        ] for row in labeled_control)),
    }
