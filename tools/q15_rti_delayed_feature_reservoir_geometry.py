"""Outcome-blind observability audit for the delayed RTI feature reservoir."""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import (
    rti_delayed_feature_reservoir_identity as identity,
)
from tools.q15_rti_delayed_feature_reservoir_readiness import (
    ASSETS,
    REQUIRED_PERSISTED_KEYS,
    _profile,
    load_protocol,
)
from tools.q15_rti_v17_development_seal import DEFAULT_DB
from tools.q15_rti_v19_readiness import load_delayed_feature_rows_after


EXPECTED_CONSTANT_NUMERIC_KEYS = frozenset({
    "rti_confirm_path_count",
    "sim_contracts",
    "kalshi_book_event_retention_seconds",
    "kalshi_trade_retention_seconds",
    "spot_mid_history_retention_seconds",
    "spot_mid_record_interval_seconds",
    "spot_fast_mid_history_retention_seconds",
    "spot_fast_mid_record_interval_seconds",
})
MINIMUM_COMPLETE_WINDOWS = 3


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _complete_rows(
    rows: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    grouped: dict[float, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        try:
            close_time = float(row["close_time"])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            close_time > identity.PROSPECTIVE_AFTER_CLOSE_TIME
            and str(row.get("interval") or "").upper() == "12M"
            and str(row.get("record_kind") or "").upper()
            == "RTI_PATH_12M_CONFIRM_PROSPECTIVE"
        ):
            grouped[close_time].append(row)
    complete = []
    for _, window in sorted(grouped.items()):
        if (
            len(window) == 7
            and {str(row.get("asset") or "").upper() for row in window}
            == ASSETS
        ):
            complete.extend(window)
    return complete


def build_geometry(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Describe feature variation without selecting any outcome field."""
    load_protocol()
    complete = _complete_rows(rows)
    profiles = [
        (
            str(row.get("asset") or "").upper(),
            float(row["close_time"]),
            _profile(row),
        )
        for row in complete
    ]
    numeric = []
    for key in REQUIRED_PERSISTED_KEYS:
        values = [
            float(profile[key])
            for _, _, profile in profiles
            if _finite_number(profile.get(key))
        ]
        if not values:
            continue
        unique_values = len({round(value, 12) for value in values})
        values_by_asset: dict[str, set[float]] = defaultdict(set)
        for asset, _, profile in profiles:
            value = profile.get(key)
            if _finite_number(value):
                values_by_asset[asset].add(round(float(value), 12))
        numeric.append({
            "feature": key,
            "observations": len(values),
            "unique_values": unique_values,
            "assets_with_temporal_variation": sum(
                len(asset_values) > 1
                for asset_values in values_by_asset.values()
            ),
            "minimum": min(values),
            "maximum": max(values),
        })

    constants = [item for item in numeric if item["unique_values"] == 1]
    unexpected_constants = [
        item for item in constants
        if item["feature"] not in EXPECTED_CONSTANT_NUMERIC_KEYS
    ]
    complete_windows = len({close for _, close, _ in profiles})
    enough_windows = complete_windows >= MINIMUM_COMPLETE_WINDOWS
    return {
        "geometry_version": (
            "q15-rti-delayed-feature-reservoir-outcome-blind-geometry-v1"
        ),
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "complete_close_windows": complete_windows,
        "rows_audited": len(profiles),
        "minimum_complete_windows": MINIMUM_COMPLETE_WINDOWS,
        "enough_windows_for_observability_audit": enough_windows,
        "numeric_required_features": len(numeric),
        "variable_numeric_features": sum(
            item["unique_values"] > 1 for item in numeric
        ),
        "temporally_variable_numeric_features": sum(
            item["assets_with_temporal_variation"] > 0 for item in numeric
        ),
        "constant_numeric_features": len(constants),
        "expected_constant_numeric_features": [
            item["feature"] for item in constants
            if item["feature"] in EXPECTED_CONSTANT_NUMERIC_KEYS
        ],
        "unexpected_constant_numeric_features": [
            item["feature"] for item in unexpected_constants
        ],
        "numeric_feature_geometry": numeric,
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "threshold_selection_performed": False,
        "probability_scoring_performed": False,
        "notification_eligible": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
        "status": (
            "COLLECTING_OUTCOME_BLIND_FEATURE_GEOMETRY"
            if not enough_windows
            else (
                "FEATURE_OBSERVABILITY_WARNING"
                if unexpected_constants
                else "FEATURE_OBSERVABILITY_OK"
            )
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-db", default=str(DEFAULT_DB))
    args = parser.parse_args()
    rows = load_delayed_feature_rows_after(
        Path(args.strategy_db), identity.PROSPECTIVE_AFTER_CLOSE_TIME,
    )
    print(json.dumps(build_geometry(rows), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
