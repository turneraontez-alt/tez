"""Exploratory V18 slate-context study on the already-open V17 dev population.

This is intentionally not a promotion audit. It may use V17 development labels
to choose one successor architecture, but it cannot read any other outcomes,
notify, create a paper artifact, promote, or trade. Any chosen design requires
new strictly prospective calibration and untouched-test windows.
"""
from __future__ import annotations

from collections import defaultdict
import argparse
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import q15_rti_v17_development_command as v17_command
from tools import q15_rti_v17_development_evaluator as v17_evaluator
from tools import q15_rti_v17_development_runner as v17_runner
from tools import q15_rti_v17_development_seal as v17_seal
from tools.q15_rti_microstructure_freeze import (
    apply_residual_trust,
    fit_residual_model,
    load_feature_rows,
    predict_probabilities,
    select_residual_trust_factor,
)


DEFAULT_RESULT = (
    ROOT / "reports" / "q15_rti_v17_development_runs"
    / "non_btc_transfer" / "development-reservation.result.json"
)
DEFAULT_RESERVATION = (
    ROOT / "reports" / "q15_rti_v17_development_runs"
    / "non_btc_transfer" / "development-reservation.json"
)
DEFAULT_OUTPUT = (
    ROOT / "reports" / "q15_rti_v18_exploration"
    / "v17-development-slate-context-v2.json"
)
SUPERSEDED_V1_SHA256 = (
    "a5404fc5f69ffe2bcad27e26eb07f9f419e1003a231e0856f3a586b6c39ad9e3"
)
ASSETS = frozenset({"BNB", "DOGE", "ETH", "HYPE", "SOL", "XRP"})
ASSET_FLAGS = ("DOGE", "ETH", "HYPE", "SOL", "XRP")
CORE_NAMES = (
    "yes_signed_distance_bps",
    "yes_acceleration_bps",
    "log1p_realized_volatility_bps",
    "trend_efficiency",
    "yes_persistence_signal",
    "log1p_strike_crossings",
    "seconds_since_crossing_fraction",
    "yes_distance_to_remaining_volatility",
    "kalshi_microprice_change_bounded_5s",
    "kalshi_microprice_change_bounded_60s",
)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _clip(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _feature_map(row: Mapping[str, Any]) -> dict[str, float]:
    names = tuple(row["v17_feature_names"])
    values = tuple(float(value) for value in row["v17_features"])
    if len(names) != len(values):
        raise ValueError("v18_exploration_feature_geometry_invalid")
    return dict(zip(names, values))


def _load_labeled_rows(
    *, strategy_db: Path, reservation_path: Path, result_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    reservation = v17_runner._read_sealed(reservation_path)
    result = v17_runner._read_sealed(result_path)
    v17_runner._validate_result(result, reservation)
    seal = v17_command.load_seal(v17_seal.DEFAULT_OUTPUT)
    rows = v17_seal.reconstruct_examples(load_feature_rows(strategy_db), seal)
    labels = {
        int(row["id"]): int(row["label_yes"])
        for row in result["development_label_rows"]
    }
    if (
        set(labels) != {int(row["id"]) for row in rows}
        or len(rows) != 1440
        or any(str(row["asset"]).upper() == "BTC" for row in rows)
    ):
        raise ValueError("v18_exploration_v17_label_binding_invalid")
    labeled = [
        {**dict(row), "label_yes": labels[int(row["id"])]}
        for row in rows
    ]
    return labeled, result


def build_slate_features(
    labeled: Sequence[Mapping[str, Any]], feature_set: str,
) -> list[dict[str, Any]]:
    groups: dict[float, list[Mapping[str, Any]]] = defaultdict(list)
    for row in labeled:
        groups[float(row["close_time"])].append(row)
    output: list[dict[str, Any]] = []
    for close_time in sorted(groups):
        rows = groups[close_time]
        if len(rows) != 6 or {str(row["asset"]).upper() for row in rows} != ASSETS:
            raise ValueError("v18_exploration_partial_close_window")
        maps = {int(row["id"]): _feature_map(row) for row in rows}
        for row in rows:
            row_id = int(row["id"])
            asset = str(row["asset"]).upper()
            peers = [other for other in rows if int(other["id"]) != row_id]
            target_probability = float(row["market_yes_probability"])
            target_signed = 2.0 * target_probability - 1.0
            peer_signed = [2.0 * float(other["market_yes_probability"]) - 1.0 for other in peers]
            peer_mean = _mean(peer_signed)
            peer_median = _median(peer_signed)
            peer_mad = _median([abs(value - peer_median) for value in peer_signed])
            target_map = maps[row_id]
            target_micro_5 = target_map["kalshi_microprice_change_bounded_5s"]
            target_micro_60 = target_map["kalshi_microprice_change_bounded_60s"]
            target_trade_60 = target_map["kalshi_trade_price_change_bounded_60s"]
            target_taker = target_map["kalshi_trade_imbalance_yes_30s"]
            peer_micro_5 = [maps[int(other["id"])]["kalshi_microprice_change_bounded_5s"] for other in peers]
            peer_micro_60 = [maps[int(other["id"])]["kalshi_microprice_change_bounded_60s"] for other in peers]
            peer_trade_60 = [maps[int(other["id"])]["kalshi_trade_price_change_bounded_60s"] for other in peers]
            peer_taker = [maps[int(other["id"])]["kalshi_trade_imbalance_yes_30s"] for other in peers]
            values = [
                peer_mean,
                peer_median,
                peer_mad,
                _mean([1.0 if value >= 0.0 else -1.0 for value in peer_signed]),
                _mean([1.0 if value >= 0.2 else 0.0 for value in peer_signed]),
                _mean([1.0 if value <= -0.2 else 0.0 for value in peer_signed]),
                _clip(target_signed - peer_mean),
                _clip(target_signed * peer_mean),
                _clip(abs(target_signed) * peer_mean),
                _clip(abs(peer_mean) - peer_mad),
                _mean(peer_micro_5),
                _mean(peer_micro_60),
                _mean([1.0 if value >= 0.0 else -1.0 for value in peer_micro_60]),
                _clip(target_micro_5 - _mean(peer_micro_5)),
                _clip(target_micro_60 - _mean(peer_micro_60)),
                _clip(target_micro_60 * peer_mean),
                _mean(peer_trade_60),
                _clip(target_trade_60 - _mean(peer_trade_60)),
                _mean(peer_taker),
                _clip(target_taker - _mean(peer_taker)),
                *[1.0 if asset == flag else 0.0 for flag in ASSET_FLAGS],
            ]
            names = [
                "peer_market_mean_signed",
                "peer_market_median_signed",
                "peer_market_mad_signed",
                "peer_market_breadth_signed",
                "peer_market_strong_yes_fraction",
                "peer_market_strong_no_fraction",
                "target_minus_peer_market_signed",
                "target_x_peer_market_consensus",
                "target_confidence_x_peer_market_consensus",
                "peer_market_consensus_minus_dispersion",
                "peer_microprice_change_mean_5s",
                "peer_microprice_change_mean_60s",
                "peer_microprice_change_breadth_60s",
                "target_minus_peer_microprice_change_5s",
                "target_minus_peer_microprice_change_60s",
                "target_microprice_change_x_peer_market_consensus",
                "peer_trade_price_change_mean_60s",
                "target_minus_peer_trade_price_change_60s",
                "peer_taker_imbalance_mean_30s",
                "target_minus_peer_taker_imbalance_30s",
                *[f"asset_is_{flag.lower()}" for flag in ASSET_FLAGS],
            ]
            if feature_set in {"SLATE_CORE", "SLATE_FULL"}:
                values.extend(target_map[name] for name in CORE_NAMES)
                names.extend(CORE_NAMES)
            if feature_set == "SLATE_FULL":
                values.extend(float(value) for value in row["v17_features"])
                names.extend(f"v17__{name}" for name in row["v17_feature_names"])
            if feature_set not in {"SLATE_ONLY", "SLATE_CORE", "SLATE_FULL"}:
                raise ValueError("v18_exploration_feature_set_invalid")
            if not all(math.isfinite(float(value)) for value in values):
                raise ValueError("v18_exploration_nonfinite_feature")
            output.append({
                **dict(row),
                "features": values,
                "feature_names": names,
            })
    return output


def _config(l2: float) -> dict[str, Any]:
    base = dict(v17_evaluator.load_protocol()["model"])
    base["model_l2"] = float(l2)
    return base


def _evaluate_candidate(
    rows: Sequence[Mapping[str, Any]], *, name: str, l2: float,
) -> dict[str, Any]:
    windows = tuple(sorted({float(row["close_time"]) for row in rows}))
    if len(windows) != 240:
        raise ValueError("v18_exploration_window_geometry_invalid")
    config = _config(l2)
    contract = v17_evaluator.load_contract()
    trust_protocol = v17_evaluator._trust_protocol(contract, "V17")
    safe_rows: list[Mapping[str, Any]] = []
    safe_probabilities: list[float] = []
    raw_probabilities: list[float] = []
    folds = []
    for index in range(4):
        split = 120 + index * 30
        train_times = set(windows[:split])
        validation_times = set(windows[split:split + 30])
        train = [row for row in rows if float(row["close_time"]) in train_times]
        validation = [row for row in rows if float(row["close_time"]) in validation_times]
        trust = select_residual_trust_factor(train, config, trust_protocol, "NON_BTC_TRANSFER")
        model = fit_residual_model(train, config)
        raw, diagnostics = predict_probabilities(model, validation, config)
        safe = apply_residual_trust(validation, raw, trust)
        safe_scores = v17_evaluator.score_utils.proper_scores(validation, safe)
        raw_scores = v17_evaluator.score_utils.proper_scores(validation, raw)
        market_scores = v17_evaluator.score_utils.proper_scores(
            validation, [float(row["market_yes_probability"]) for row in validation],
        )
        folds.append({
            "fold": index + 1,
            "selected_trust": float(trust["selected_factor"]),
            "safe_brier_delta": safe_scores["brier_score"] - market_scores["brier_score"],
            "safe_log_loss_delta": safe_scores["log_loss"] - market_scores["log_loss"],
            "raw_brier_delta": raw_scores["brier_score"] - market_scores["brier_score"],
            "raw_log_loss_delta": raw_scores["log_loss"] - market_scores["log_loss"],
            "raw_accuracy": raw_scores["accuracy"],
            "market_accuracy": market_scores["accuracy"],
            "out_of_distribution_rows": sum(bool(item["out_of_distribution"]) for item in diagnostics),
        })
        safe_rows.extend(validation)
        safe_probabilities.extend(float(value) for value in safe)
        raw_probabilities.extend(float(value) for value in raw)
    market = [float(row["market_yes_probability"]) for row in safe_rows]
    safe_scores = v17_evaluator.score_utils.proper_scores(safe_rows, safe_probabilities)
    raw_scores = v17_evaluator.score_utils.proper_scores(safe_rows, raw_probabilities)
    market_scores = v17_evaluator.score_utils.proper_scores(safe_rows, market)
    return {
        "name": name,
        "feature_count": len(rows[0]["features"]),
        "feature_names": list(rows[0]["feature_names"]),
        "model_l2": float(l2),
        "folds": folds,
        "selected_trust_factors": [fold["selected_trust"] for fold in folds],
        "safe_scores": safe_scores,
        "raw_full_trust_scores": raw_scores,
        "market_scores": market_scores,
        "safe_brier_delta": safe_scores["brier_score"] - market_scores["brier_score"],
        "safe_log_loss_delta": safe_scores["log_loss"] - market_scores["log_loss"],
        "raw_brier_delta": raw_scores["brier_score"] - market_scores["brier_score"],
        "raw_log_loss_delta": raw_scores["log_loss"] - market_scores["log_loss"],
        "every_safe_fold_not_worse": all(
            fold["safe_brier_delta"] <= 0.0 and fold["safe_log_loss_delta"] <= 0.0
            for fold in folds
        ),
    }


def build_report(
    *, strategy_db: Path, reservation_path: Path, result_path: Path,
) -> dict[str, Any]:
    labeled, v17_result = _load_labeled_rows(
        strategy_db=strategy_db,
        reservation_path=reservation_path,
        result_path=result_path,
    )
    candidates = []
    for feature_set, l2_values in (
        ("SLATE_ONLY", (10.0, 30.0, 100.0)),
        ("SLATE_CORE", (30.0, 100.0)),
        ("SLATE_FULL", (100.0,)),
    ):
        rows = build_slate_features(labeled, feature_set)
        for l2 in l2_values:
            candidates.append(_evaluate_candidate(
                rows, name=f"{feature_set}_L2_{int(l2)}", l2=l2,
            ))
    ranked = sorted(candidates, key=lambda candidate: (
        float(candidate["raw_brier_delta"]) / float(candidate["market_scores"]["brier_score"])
        + float(candidate["raw_log_loss_delta"]) / float(candidate["market_scores"]["log_loss"]),
        int(candidate["feature_count"]),
        float(candidate["model_l2"]),
    ))
    advancing = [
        candidate for candidate in ranked
        if float(candidate["safe_brier_delta"]) < 0.0
        and float(candidate["safe_log_loss_delta"]) < 0.0
        and any(float(value) > 0.0 for value in candidate["selected_trust_factors"])
        and candidate["every_safe_fold_not_worse"] is True
    ]
    return {
        "report_version": "q15-rti-v18-v17-development-slate-exploration-v2",
        "supersedes": {
            "report_version": "q15-rti-v18-v17-development-slate-exploration-v1",
            "file_sha256": SUPERSEDED_V1_SHA256,
            "reason": "v1 incorrectly named a best candidate when every safety-selected candidate was identical to market and every raw residual was worse",
        },
        "source_v17_result_state_sha256": v17_result["state_sha256"],
        "source_v17_result_status": v17_result["status"],
        "source_population": "ALREADY_OPEN_V17_DEVELOPMENT_ONLY",
        "source_rows": len(labeled),
        "source_close_windows": len({float(row["close_time"]) for row in labeled}),
        "candidate_selection_uses_v17_development_labels": True,
        "independent_confirmation": False,
        "exploratory_only": True,
        "future_v18_prospective_confirmation_required": True,
        "future_v17_calibration_allowed": False,
        "btc_labels_read": False,
        "new_population_outcomes_read": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
        "candidate_grid_fully_reported": True,
        "ranked_candidates": ranked,
        "least_bad_raw_candidate": ranked[0]["name"],
        "advancing_candidate": advancing[0]["name"] if advancing else None,
        "conclusion": (
            "CANDIDATE_ADVANCES_TO_PROSPECTIVE_DESIGN"
            if advancing else "NO_RESIDUAL_CANDIDATE_ADVANCES"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-db", default=str(v17_seal.DEFAULT_DB))
    parser.add_argument("--reservation", default=str(DEFAULT_RESERVATION))
    parser.add_argument("--result", default=str(DEFAULT_RESULT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    report = build_report(
        strategy_db=Path(args.strategy_db),
        reservation_path=Path(args.reservation),
        result_path=Path(args.result),
    )
    if args.write:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
    print(json.dumps({
        "report_version": report["report_version"],
        "least_bad_raw_candidate": report["least_bad_raw_candidate"],
        "advancing_candidate": report["advancing_candidate"],
        "conclusion": report["conclusion"],
        "ranked_candidates": [{
            key: candidate[key] for key in (
                "name", "feature_count", "model_l2", "selected_trust_factors",
                "safe_brier_delta", "safe_log_loss_delta", "raw_brier_delta",
                "raw_log_loss_delta", "every_safe_fold_not_worse",
            )
        } for candidate in report["ranked_candidates"]],
        "notification_eligible": False,
        "real_trading_allowed": False,
    }, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
