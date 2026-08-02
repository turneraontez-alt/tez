"""Outcome-blind structural QA for the accumulating frozen V22 features.

This diagnostic never loads a result, settlement, label, model, or score.  It
independently revalidates row hashes and reports feature variation/redundancy so
collector defects can be found before the earliest-180 historical seal exists.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import (  # noqa: E402
    rti_microstructure_v22_identity as identity,
)
from q15_upgrade.strategy_bots import (  # noqa: E402
    rti_microstructure_v22_top_book_features as feature_builder,
)
from q15_upgrade.strategy_bots import (  # noqa: E402
    rti_spot_rest_top_book_reservoir_identity as rest_identity,
)
from tools.q15_rti_microstructure_freeze import (  # noqa: E402
    load_feature_rows_after,
)
from tools.q15_rti_spot_rest_top_book_readiness import (  # noqa: E402
    load_rows as load_rest_rows,
)
from tools.q15_rti_v17_development_seal import (  # noqa: E402
    DEFAULT_DB as STRATEGY_DB,
)
from tools.q15_rti_v21_readiness import (  # noqa: E402
    load_trajectory_feature_rows_after,
)
from tools.q15_rti_v22_readiness import collect_feature_windows  # noqa: E402


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _row_evidence_core(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "feature_builder_version": row.get("feature_builder_version"),
        "parent_id": row.get("parent_id"),
        "asset": row.get("asset"),
        "ticker": row.get("ticker"),
        "close_time": row.get("close_time"),
        "side": row.get("side"),
        "parent_source_evidence_sha256": row.get(
            "parent_source_evidence_sha256"
        ),
        "intermediate_source_evidence_sha256": row.get(
            "intermediate_source_evidence_sha256"
        ),
        "delayed_source_evidence_sha256": row.get(
            "delayed_source_evidence_sha256"
        ),
        "rest_evidence_sha256_by_stage": row.get(
            "rest_evidence_sha256_by_stage"
        ),
        "feature_names": row.get("feature_names"),
        "features": row.get("features"),
    }


def _correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    left_delta = [value - left_mean for value in left]
    right_delta = [value - right_mean for value in right]
    left_ss = sum(value * value for value in left_delta)
    right_ss = sum(value * value for value in right_delta)
    if left_ss <= 1e-24 or right_ss <= 1e-24:
        return None
    return sum(a * b for a, b in zip(left_delta, right_delta)) / math.sqrt(
        left_ss * right_ss
    )


def build_quality_report(
    complete_windows: Sequence[Mapping[str, Any]],
    *,
    excluded_windows: Sequence[Mapping[str, Any]] = (),
    rest_failure_counts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    names = tuple(feature_builder.FEATURE_NAMES)
    failures: Counter[str] = Counter()
    rows: list[Mapping[str, Any]] = []
    closes: list[float] = []
    row_identities: set[tuple[Any, ...]] = set()
    evidence_hashes: set[str] = set()
    expected_assets = set(rest_identity.SOURCE_IDENTITIES)

    for window in complete_windows:
        try:
            close = float(window.get("close_time"))
        except (TypeError, ValueError):
            failures["WINDOW_CLOSE_INVALID"] += 1
            continue
        window_rows = list(window.get("rows") or ())
        assets = {str(row.get("asset") or "").upper() for row in window_rows}
        if len(window_rows) != 7 or assets != expected_assets:
            failures["WINDOW_SEVEN_ASSET_GEOMETRY_INVALID"] += 1
            continue
        closes.append(close)
        rows.extend(window_rows)

    if closes != sorted(closes) or len(closes) != len(set(closes)):
        failures["WINDOW_CHRONOLOGY_INVALID"] += 1

    matrix: list[list[float]] = []
    assets_by_row: list[str] = []
    for row in rows:
        feature_names = tuple(row.get("feature_names") or ())
        raw_values = list(row.get("features") or ())
        if (
            feature_names != names
            or len(raw_values) != identity.FEATURE_COUNT
            or row.get("feature_names_sha256") != identity.FEATURE_NAMES_SHA256
            or row.get("feature_builder_version") != identity.FEATURE_BUILDER_VERSION
            or row.get("protocol_id") != identity.PROTOCOL_ID
            or row.get("protocol_sha256") != identity.PROTOCOL_SHA256
        ):
            failures["ROW_FEATURE_IDENTITY_INVALID"] += 1
            continue
        try:
            values = [float(value) for value in raw_values]
        except (TypeError, ValueError):
            failures["ROW_FEATURE_NONNUMERIC"] += 1
            continue
        if not all(math.isfinite(value) for value in values):
            failures["ROW_FEATURE_NONFINITE"] += 1
            continue
        evidence_hash = str(row.get("feature_evidence_sha256") or "")
        if (
            len(evidence_hash) != 64
            or _sha256(_row_evidence_core(row)) != evidence_hash
        ):
            failures["ROW_FEATURE_EVIDENCE_HASH_INVALID"] += 1
            continue
        identity_key = (
            row.get("parent_id"),
            str(row.get("asset") or "").upper(),
            str(row.get("ticker") or ""),
            float(row.get("close_time") or 0.0),
        )
        if identity_key in row_identities:
            failures["ROW_IDENTITY_DUPLICATE"] += 1
            continue
        if evidence_hash in evidence_hashes:
            failures["ROW_EVIDENCE_HASH_DUPLICATE"] += 1
            continue
        row_identities.add(identity_key)
        evidence_hashes.add(evidence_hash)
        matrix.append(values)
        assets_by_row.append(str(row.get("asset") or "").upper())

    feature_stats: dict[str, dict[str, Any]] = {}
    constant_features: list[str] = []
    columns: list[list[float]] = []
    for index, name in enumerate(names):
        values = [row[index] for row in matrix]
        columns.append(values)
        rounded_unique = len({round(value, 12) for value in values})
        if values and rounded_unique == 1:
            constant_features.append(name)
        feature_stats[name] = {
            "count": len(values),
            "finite_count": sum(math.isfinite(value) for value in values),
            "unique_rounded_12": rounded_unique,
            "zero_fraction": (
                sum(abs(value) <= 1e-12 for value in values) / len(values)
                if values else None
            ),
            "minimum": min(values) if values else None,
            "maximum": max(values) if values else None,
            "mean": statistics.fmean(values) if values else None,
            "population_stddev": statistics.pstdev(values) if values else None,
        }

    exact_duplicate_pairs: list[list[str]] = []
    near_duplicate_pairs: list[dict[str, Any]] = []
    for left_index, left_name in enumerate(names):
        for right_index in range(left_index + 1, len(names)):
            right_name = names[right_index]
            left = columns[left_index]
            right = columns[right_index]
            if not left:
                continue
            if all(abs(a - b) <= 1e-12 for a, b in zip(left, right)):
                exact_duplicate_pairs.append([left_name, right_name])
                continue
            correlation = _correlation(left, right)
            if correlation is not None and abs(correlation) >= 0.999999:
                near_duplicate_pairs.append({
                    "left": left_name,
                    "right": right_name,
                    "correlation": correlation,
                })

    by_asset_indices: dict[str, list[int]] = defaultdict(list)
    for index, asset in enumerate(assets_by_row):
        by_asset_indices[asset].append(index)
    constant_feature_count_by_asset = {}
    for asset, indices in sorted(by_asset_indices.items()):
        constant_feature_count_by_asset[asset] = sum(
            len({round(columns[column][index], 12) for index in indices}) <= 1
            for column in range(len(names))
        )

    excluded_failures: Counter[str] = Counter()
    for window in excluded_windows:
        excluded_failures.update(dict(window.get("failure_counts") or {}))
    rest_failures = Counter(rest_failure_counts or {})
    structural_ok = (
        bool(matrix)
        and not failures
        and not excluded_failures
        and not rest_failures
        and len(matrix) == len(rows) == len(closes) * 7
    )
    report = {
        "status": (
            "PASS_OUTCOME_BLIND_V22_FEATURE_STRUCTURE"
            if structural_ok
            else "FAIL_OUTCOME_BLIND_V22_FEATURE_STRUCTURE"
        ),
        "complete_close_windows": len(closes),
        "feature_rows": len(matrix),
        "feature_count": len(names),
        "feature_names_sha256": feature_builder.FEATURE_NAMES_SHA256,
        "structural_failure_counts": dict(sorted(failures.items())),
        "excluded_window_failure_counts": dict(sorted(excluded_failures.items())),
        "rest_quality_failure_counts": dict(sorted(rest_failures.items())),
        "constant_features_diagnostic_only": constant_features,
        "constant_feature_count_by_asset_diagnostic_only": (
            constant_feature_count_by_asset
        ),
        "exact_duplicate_feature_pairs_diagnostic_only": exact_duplicate_pairs,
        "near_duplicate_feature_pairs_diagnostic_only": near_duplicate_pairs,
        "feature_stats_outcome_blind": feature_stats,
        "variation_gate_applied": False,
        "variation_reason": (
            "INFORMATIONAL_UNTIL_MORE_WINDOWS;NO_LABEL_OR_SCORE_ACCESS"
        ),
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "probability_scoring_performed": False,
        "paper_artifact_created": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }
    report["quality_report_sha256"] = _sha256(report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-db", default=str(STRATEGY_DB))
    parser.add_argument(
        "--rest-db",
        default=str(ROOT / rest_identity.DATABASE_RELATIVE_PATH),
    )
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()
    strategy_db = Path(args.strategy_db)
    parent_rows = load_feature_rows_after(
        strategy_db, rest_identity.PROSPECTIVE_AFTER_CLOSE_TIME
    )
    trajectory_rows = load_trajectory_feature_rows_after(
        strategy_db, rest_identity.PROSPECTIVE_AFTER_CLOSE_TIME
    )
    complete, excluded, rest_failures = collect_feature_windows(
        parent_rows,
        trajectory_rows,
        load_rest_rows(Path(args.rest_db)),
    )
    report = build_quality_report(
        complete,
        excluded_windows=excluded,
        rest_failure_counts=rest_failures,
    )
    if args.summary_only:
        report = {
            key: value
            for key, value in report.items()
            if key != "feature_stats_outcome_blind"
        }
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
