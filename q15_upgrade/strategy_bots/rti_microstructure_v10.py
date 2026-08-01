"""Outcome-blind compact successor to frozen RTI feature design V9.

V10 removes only two V8 fields that a 30-window, outcome-blind feature audit
proved are exact duplicates.  All retained values and all V9 integrity guards
remain unchanged.  This module cannot fit, notify, promote, or trade.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from . import rti_microstructure as v1
from . import rti_microstructure_v9 as v9


FEATURE_SCHEMA_VERSION = "rti-probability-microstructure-features-v10"
MODEL_FAMILY = v9.MODEL_FAMILY
DESIGN_ID = "q15-rti-market-residual-independent-microstructure-compact-v10"
DESIGN_SHA256 = "bc329e1a563a3bb5d7e703ad9584c076bf7ab12db1ce0bc791a1699eaf1a47ce"
SOURCE_SCHEMA = v9.SOURCE_SCHEMA
SOURCE_TIME_BASIS = v9.SOURCE_TIME_BASIS
NON_BTC_ASSETS = v9.NON_BTC_ASSETS
EXPECTED_ASSETS = v9.EXPECTED_ASSETS
MICROSTRUCTURE_SCHEMA_VERSION = v9.MICROSTRUCTURE_SCHEMA_VERSION
MICROSTRUCTURE_TIME_BASIS = v9.MICROSTRUCTURE_TIME_BASIS
KRAKEN_PARTIAL_FILL_FLOW_SCHEMA_VERSION = (
    v9.KRAKEN_PARTIAL_FILL_FLOW_SCHEMA_VERSION
)

# The V8 30-window feature review and V9's first fold were inspected before
# this compact schema was frozen.  V10 therefore takes no credit through the
# 16:15 ET close and starts at the following complete close window.
PROSPECTIVE_AFTER_CLOSE_TIME = 1784664900.0
FIRST_ELIGIBLE_CLOSE_TIME = 1784665800.0

DROP_FEATURES = frozenset({
    "kalshi_queue_pressure_yes_5s",
    "kalshi_queue_pressure_yes_30s",
})
FEATURE_NAMES = tuple(
    name for name in v9.FEATURE_NAMES if name not in DROP_FEATURES
)
if len(FEATURE_NAMES) != 63 or len(v9.FEATURE_NAMES) - len(FEATURE_NAMES) != 2:
    raise RuntimeError("v10_compact_feature_schema_mismatch")


def feature_vector(row: Mapping[str, Any]) -> dict[str, Any]:
    profile = v1._profile(row)
    close = v1._num(v1._value(row, profile, "close_time"))
    if close is None:
        return {"available": False, "error": "close_time_missing"}
    if close <= PROSPECTIVE_AFTER_CLOSE_TIME:
        return {"available": False, "error": "pre_v10_prospective_boundary"}
    if close < FIRST_ELIGIBLE_CLOSE_TIME:
        return {"available": False, "error": "before_first_eligible_close"}
    base = v9.feature_vector(row)
    if not base.get("available"):
        return {
            "available": False,
            "error": f"base:{base.get('error') or 'unavailable'}",
        }
    by_name = dict(zip(v9.FEATURE_NAMES, base["features"]))
    vector = [by_name[name] for name in FEATURE_NAMES]
    return {
        **base,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "features": vector,
        "design_id": DESIGN_ID,
        "design_sha256": DESIGN_SHA256,
        "prospective_after_close_time": PROSPECTIVE_AFTER_CLOSE_TIME,
        "first_eligible_close_time": FIRST_ELIGIBLE_CLOSE_TIME,
    }


def model_feature_window_coverage(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    eligible = [
        row for row in rows
        if v1._num(row.get("close_time")) is not None
        and float(v1._num(row.get("close_time")))
        > PROSPECTIVE_AFTER_CLOSE_TIME
        and row.get("rti_independent_microstructure_schema_version")
        == MICROSTRUCTURE_SCHEMA_VERSION
    ]
    return v1.model_feature_window_coverage(
        eligible,
        feature_builder=feature_vector,
        source_schema=SOURCE_SCHEMA,
    )
