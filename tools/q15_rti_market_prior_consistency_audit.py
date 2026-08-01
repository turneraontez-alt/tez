"""Outcome-blind consistency audit for the point-in-time Kalshi prior."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots.rti_independent_path_identity import (
    FIRST_ELIGIBLE_CLOSE_TIME,
)
from tools.q15_rti_microstructure_freeze import load_feature_rows


AUDIT_VERSION = "q15-rti-market-prior-outcome-blind-consistency-v1"
DEFAULT_DB = ROOT / "data" / "q15_strategy_bots_v3.sqlite3"
MAX_ABSOLUTE_DELTA = 1e-9
MAX_EXACT_CAPTURE_OFFSET_SECONDS = 2.0
MAX_CUTOFF_DELTA_SECONDS = 1e-6


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _profile(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("threshold_json")
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        decoded = json.loads(str(raw or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def reconstruct_market_yes(row: Mapping[str, Any]) -> dict[str, Any]:
    profile = _profile(row)
    side = str(row.get("side") or profile.get("rti_side") or "").upper()
    side_ask = _num(row.get("entry_ask_cents"))
    spread = _num(row.get("spread_cents"))
    opposite_ask = _num(profile.get("rti_opposite_ask_cents"))
    selected_mid = _num(profile.get("rti_market_mid_probability"))
    if side not in {"YES", "NO"}:
        return {"available": False, "error": "side_missing"}
    if side_ask is None or spread is None:
        return {"available": False, "error": "selected_quote_missing"}
    if opposite_ask is None:
        opposite_ask = 100.0 - (side_ask - spread)
    yes_ask, no_ask = (
        (side_ask, opposite_ask)
        if side == "YES" else (opposite_ask, side_ask)
    )
    quote_yes = (yes_ask + (100.0 - no_ask)) / 200.0
    if selected_mid is None:
        return {
            "available": True,
            "stored_mid_available": False,
            "quote_yes_probability": quote_yes,
            "stored_yes_probability": None,
            "absolute_delta": None,
        }
    stored_yes = selected_mid if side == "YES" else 1.0 - selected_mid
    return {
        "available": True,
        "stored_mid_available": True,
        "quote_yes_probability": quote_yes,
        "stored_yes_probability": stored_yes,
        "absolute_delta": abs(stored_yes - quote_yes),
    }


def audit_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    errors: Counter[str] = Counter()
    checked: list[dict[str, Any]] = []
    eligible = []
    exact_offsets: list[float] = []
    cutoff_deltas: list[float] = []
    for row in rows:
        close = _num(row.get("close_time"))
        if close is None or close < FIRST_ELIGIBLE_CLOSE_TIME:
            continue
        eligible.append(row)
        source_captured = _num(row.get("source_captured_at"))
        kalshi_captured = _num(row.get("kalshi_microstructure_captured_at"))
        if source_captured is None or kalshi_captured is None:
            errors["market_prior_timestamp_missing"] += 1
        else:
            exact_offset = abs(source_captured - (close - 780.0))
            cutoff_delta = abs(kalshi_captured - source_captured)
            exact_offsets.append(exact_offset)
            cutoff_deltas.append(cutoff_delta)
            if exact_offset > MAX_EXACT_CAPTURE_OFFSET_SECONDS:
                errors["market_prior_exact_capture_offset_exceeded"] += 1
            if cutoff_delta > MAX_CUTOFF_DELTA_SECONDS:
                errors["market_prior_cutoff_mismatch"] += 1
        result = reconstruct_market_yes(row)
        if not result.get("available"):
            errors[str(result.get("error") or "unavailable")] += 1
            continue
        if result.get("stored_mid_available") is not True:
            errors["stored_mid_missing"] += 1
            continue
        delta = float(result["absolute_delta"])
        checked.append({
            "id": row.get("id"),
            "close_time": close,
            "asset": str(row.get("asset") or "").upper(),
            "absolute_delta": delta,
        })
        if delta > MAX_ABSOLUTE_DELTA:
            errors["market_prior_quote_mismatch"] += 1
    mismatches = [
        item for item in checked
        if float(item["absolute_delta"]) > MAX_ABSOLUTE_DELTA
    ]
    return {
        "audit_version": AUDIT_VERSION,
        "first_eligible_close_time": FIRST_ELIGIBLE_CLOSE_TIME,
        "maximum_absolute_delta_allowed": MAX_ABSOLUTE_DELTA,
        "maximum_exact_capture_offset_seconds_allowed": (
            MAX_EXACT_CAPTURE_OFFSET_SECONDS
        ),
        "maximum_cutoff_delta_seconds_allowed": MAX_CUTOFF_DELTA_SECONDS,
        "eligible_rows": len(eligible),
        "checked_rows": len(checked),
        "maximum_absolute_delta": max(
            (float(item["absolute_delta"]) for item in checked), default=None,
        ),
        "maximum_exact_capture_offset_seconds": max(
            exact_offsets, default=None,
        ),
        "maximum_kalshi_source_cutoff_delta_seconds": max(
            cutoff_deltas, default=None,
        ),
        "mismatch_rows": len(mismatches),
        "mismatch_examples": mismatches[:20],
        "errors": dict(sorted(errors.items())),
        "status": "PASS" if not errors else "FAIL",
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "automatic_scoring": False,
        "automatic_promotion": False,
        "real_trading_allowed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    args = parser.parse_args()
    report = audit_rows(load_feature_rows(Path(args.db)))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
