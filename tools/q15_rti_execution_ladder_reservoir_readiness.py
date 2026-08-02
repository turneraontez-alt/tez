"""Outcome-blind readiness for genuine displayed 10-contract RTI ladders."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots import rti_execution_ladder_reservoir_identity as identity
from tools.q15_rti_microstructure_preregister import design_fingerprint
from tools.q15_rti_v17_development_seal import DEFAULT_DB
from tools.q15_rti_v21_readiness import load_trajectory_feature_rows_after


ASSETS = frozenset({"BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP"})
FIELDS = (
    "rti_execution_ladder_schema_version",
    "rti_ladder_depth_within_2c_contracts",
    "rti_ladder_10_contract_filled_contracts",
    "rti_ladder_10_contract_full_fill_supported",
    "rti_ladder_10_contract_vwap_cents",
    "rti_ladder_10_contract_worst_price_cents",
    "rti_ladder_10_contract_slippage_cents",
)


def _profile(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("threshold_json")
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        value = json.loads(str(raw or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def load_protocol(path: Path | None = None) -> dict[str, Any]:
    target = ROOT / identity.PROTOCOL_RELATIVE_PATH if path is None else path
    try:
        protocol = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("execution_ladder_protocol_unreadable") from exc
    boundary = dict(protocol.get("prospective_boundary") or {})
    capture = dict(protocol.get("capture_contract") or {})
    usage = dict(protocol.get("usage") or {})
    if (
        design_fingerprint(protocol) != identity.PROTOCOL_SHA256
        or protocol.get("protocol_id") != identity.PROTOCOL_ID
        or protocol.get("protocol_status")
        != "FROZEN_BEFORE_FIRST_ELIGIBLE_LADDER_EVIDENCE"
        or float(boundary.get("strictly_after_close_time") or 0.0)
        != identity.PROSPECTIVE_AFTER_CLOSE_TIME
        or float(boundary.get("first_eligible_close_time") or 0.0)
        != identity.FIRST_ELIGIBLE_CLOSE_TIME
        or capture.get("schema_version") != identity.SCHEMA_VERSION
        or float(capture.get("contracts") or 0.0) != identity.CONTRACTS
        or float(capture.get("maximum_price_distance_from_best_ask_cents") or 0.0)
        != identity.MAX_SLIPPAGE_CENTS
        or capture.get("partial_fill_never_treated_as_full") is not True
        or capture.get("vwap_and_worst_price_null_unless_full_fill_supported")
        is not True
        or usage.get("record_only") is not True
        or usage.get("used_by_v21") is not False
        or usage.get("changes_existing_sim_full_fill_supported") is not False
        or any(usage.get(key) is not False for key in (
            "outcome_access_allowed", "label_access_allowed", "model_fit_allowed",
            "threshold_selection_allowed", "paper_artifact_allowed",
            "notifications_allowed", "automatic_promotion_allowed",
            "real_trading_allowed",
        ))
    ):
        raise ValueError("execution_ladder_protocol_identity_or_safety_invalid")
    return protocol


def _quality_failures(row: Mapping[str, Any], profile: Mapping[str, Any]) -> list[str]:
    failures = []
    if any(key not in profile for key in FIELDS):
        failures.append("LADDER_SCHEMA_INCOMPLETE")
        return failures
    if profile.get("rti_execution_ladder_schema_version") != identity.SCHEMA_VERSION:
        failures.append("LADDER_SCHEMA_IDENTITY")
    depth = _num(profile.get("rti_ladder_depth_within_2c_contracts"))
    filled = _num(profile.get("rti_ladder_10_contract_filled_contracts"))
    full = profile.get("rti_ladder_10_contract_full_fill_supported")
    ask = _num(row.get("entry_ask_cents"))
    vwap = _num(profile.get("rti_ladder_10_contract_vwap_cents"))
    worst = _num(profile.get("rti_ladder_10_contract_worst_price_cents"))
    slip = _num(profile.get("rti_ladder_10_contract_slippage_cents"))
    quote_age = _num(profile.get("quote_age_seconds"))
    if (
        depth is None or filled is None or ask is None
        or depth < 0.0 or not 0.0 <= filled <= identity.CONTRACTS
        or depth + 1e-9 < filled
        or full not in {True, False}
    ):
        failures.append("LADDER_CAPACITY_INVALID")
    if (
        quote_age is None or not 0.0 <= quote_age <= 3.0
        or profile.get("quote_evidence_source") not in {
            "kalshi_official_websocket_book", "kalshi_official_rest_orderbook",
        }
    ):
        failures.append("LADDER_OFFICIAL_QUOTE_NOT_FRESH")
    if full is True:
        if (
            filled != identity.CONTRACTS or depth is None or depth < identity.CONTRACTS
            or None in {vwap, worst, slip}
            or not ask <= vwap <= worst <= ask + identity.MAX_SLIPPAGE_CENTS + 1e-9
            or abs((vwap - ask) - slip) > 1e-6
        ):
            failures.append("LADDER_FULL_FILL_CONTRADICTION")
    elif any(value is not None for value in (vwap, worst, slip)):
        failures.append("LADDER_PARTIAL_FILL_HAS_FAKE_PRICE")
    return failures


def build_readiness(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    load_protocol()
    grouped: dict[float, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        try:
            close_time = float(row.get("close_time") or 0.0)
        except (TypeError, ValueError):
            continue
        if (
            close_time > identity.PROSPECTIVE_AFTER_CLOSE_TIME
            and str(row.get("interval") or "").upper() == "12M"
            and str(row.get("record_kind") or "").upper()
            == "RTI_PATH_12M_CONFIRM_PROSPECTIVE"
        ):
            grouped[close_time].append(row)
    geometry = {
        close: window for close, window in grouped.items()
        if len(window) == 7
        and {str(row.get("asset") or "").upper() for row in window} == ASSETS
    }
    complete = 0
    failures: Counter[str] = Counter()
    full_fill_rows = 0
    top_of_book_full_fill_rows = 0
    recovered_full_fill_rows = 0
    rows_by_asset: Counter[str] = Counter()
    ladder_full_fill_rows_by_asset: Counter[str] = Counter()
    recovered_full_fill_rows_by_asset: Counter[str] = Counter()
    for _, window in sorted(geometry.items()):
        valid = True
        for row in window:
            asset = str(row.get("asset") or "").upper()
            rows_by_asset[asset] += 1
            profile = _profile(row)
            row_failures = _quality_failures(row, profile)
            failures.update(row_failures)
            valid = valid and not row_failures
            ladder_full = (
                not row_failures
                and profile.get("rti_ladder_10_contract_full_fill_supported")
                is True
            )
            top_depth = _num(row.get("depth_contracts"))
            top_full = top_depth is not None and top_depth >= identity.CONTRACTS
            top_of_book_full_fill_rows += int(top_full)
            if ladder_full:
                full_fill_rows += 1
                ladder_full_fill_rows_by_asset[asset] += 1
            if ladder_full and not top_full:
                recovered_full_fill_rows += 1
                recovered_full_fill_rows_by_asset[asset] += 1
        complete += int(valid)
    return {
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "first_eligible_close_time": identity.FIRST_ELIGIBLE_CLOSE_TIME,
        "all_seven_geometry_close_windows": len(geometry),
        "usable_ladder_complete_close_windows": complete,
        "full_fill_supported_rows": full_fill_rows,
        "top_of_book_full_fill_supported_rows": top_of_book_full_fill_rows,
        "ladder_recovered_full_fill_rows": recovered_full_fill_rows,
        "rows_by_asset": dict(sorted(rows_by_asset.items())),
        "ladder_full_fill_rows_by_asset": dict(sorted(
            ladder_full_fill_rows_by_asset.items()
        )),
        "ladder_recovered_full_fill_rows_by_asset": dict(sorted(
            recovered_full_fill_rows_by_asset.items()
        )),
        "quality_failure_counts": dict(sorted(failures.items())),
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "threshold_selection_performed": False,
        "notification_eligible": False,
        "real_trading_allowed": False,
        "status": "COLLECTING_OUTCOME_BLIND_EXECUTION_LADDERS",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-db", default=str(DEFAULT_DB))
    args = parser.parse_args()
    rows = load_trajectory_feature_rows_after(
        Path(args.strategy_db), identity.PROSPECTIVE_AFTER_CLOSE_TIME,
    )
    print(json.dumps(build_readiness(rows), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
