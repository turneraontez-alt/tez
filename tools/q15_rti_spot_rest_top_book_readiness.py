"""Outcome-blind integrity and geometry report for official spot REST books."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.rti_spot_rest_top_book import (  # noqa: E402
    ASSETS,
    DEFAULT_DB,
    DEPTH_SCOPE,
    EVIDENCE_COLUMNS,
    STAGE_DELAY_SECONDS,
    _canonical,
    _num,
    load_protocol,
)
from q15_upgrade.strategy_bots import (  # noqa: E402
    rti_spot_rest_top_book_reservoir_identity as identity,
)
SELECTED_COLUMNS = (*EVIDENCE_COLUMNS, "evidence_json", "evidence_sha256")
FORBIDDEN_SCHEMA_NAMES = frozenset({
    "outcome", "result", "resolved", "correct", "settlement", "pnl", "profit",
    "label", "prediction", "score", "probability",
})


def load_rows(db_path: Path = DEFAULT_DB) -> list[dict[str, Any]]:
    if not Path(db_path).exists():
        return []
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        columns = {
            str(row[1]).lower()
            for row in conn.execute("PRAGMA table_info(spot_rest_top_book)")
        }
        if columns & FORBIDDEN_SCHEMA_NAMES:
            raise ValueError("spot_rest_book_outcome_column_forbidden")
        unique_identities = set()
        for index_row in conn.execute("PRAGMA index_list(spot_rest_top_book)"):
            if not bool(index_row[2]):
                continue
            index_name = str(index_row[1]).replace("'", "''")
            identity_columns = tuple(
                str(info[2])
                for info in conn.execute(f"PRAGMA index_info('{index_name}')")
            )
            unique_identities.add(identity_columns)
        if ("ticker", "close_time", "stage") not in unique_identities:
            raise ValueError("spot_rest_book_unique_identity_constraint_missing")
        rows = conn.execute(
            f"SELECT {','.join(SELECTED_COLUMNS)} "
            "FROM spot_rest_top_book ORDER BY close_time,stage,asset,id"
        ).fetchall()
    return [dict(row) for row in rows]


def _same(left: Any, right: Any) -> bool:
    left_num = _num(left)
    right_num = _num(right)
    if left_num is not None or right_num is not None:
        return (
            left_num is not None
            and right_num is not None
            and abs(left_num - right_num) <= 1e-9
        )
    return left == right


def _quality_failures(row: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    raw_json = str(row.get("evidence_json") or "")
    try:
        evidence = json.loads(raw_json)
    except json.JSONDecodeError:
        return ["EVIDENCE_JSON_INVALID"]
    if not isinstance(evidence, Mapping):
        return ["EVIDENCE_JSON_NOT_OBJECT"]
    canonical = _canonical(evidence)
    expected_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if raw_json != canonical or row.get("evidence_sha256") != expected_sha:
        failures.append("EVIDENCE_HASH_OR_CANONICAL_MISMATCH")
    for key in SELECTED_COLUMNS[:-2]:
        if key == "created_at":
            continue
        if key not in evidence or not _same(row.get(key), evidence.get(key)):
            failures.append("ROW_EVIDENCE_IDENTITY_MISMATCH")
            break
    if (
        row.get("protocol_id") != identity.PROTOCOL_ID
        or row.get("protocol_sha256") != identity.PROTOCOL_SHA256
        or row.get("schema_version") != identity.SCHEMA_VERSION
        or row.get("depth_scope") != DEPTH_SCOPE
    ):
        failures.append("PROTOCOL_OR_SCHEMA_IDENTITY_MISMATCH")
    asset = str(row.get("asset") or "").upper()
    ticker = str(row.get("ticker") or "")
    stage = str(row.get("stage") or "").upper()
    close = _num(row.get("close_time"))
    target = _num(row.get("target_at"))
    submitted = _num(row.get("submitted_at"))
    started = _num(row.get("request_started_at"))
    received = _num(row.get("received_at"))
    start_offset = _num(row.get("request_start_offset_seconds"))
    latency = _num(row.get("response_latency_seconds"))
    receive_offset = _num(row.get("receive_offset_seconds"))
    source = identity.SOURCE_IDENTITIES.get(asset)
    if (
        asset not in ASSETS
        or not ticker
        or stage not in STAGE_DELAY_SECONDS
        or close is None
        or close <= identity.PROSPECTIVE_AFTER_CLOSE_TIME
        or target is None
        or abs(target - (close - 780.0 + STAGE_DELAY_SECONDS.get(stage, 0.0)))
        > 1e-6
    ):
        failures.append("CONTRACT_OR_STAGE_IDENTITY_INVALID")
    if source is None or (
        str(row.get("provider")), str(row.get("symbol")),
        str(row.get("quote_currency")),
    ) != tuple(source):
        failures.append("OFFICIAL_PROVIDER_IDENTITY_INVALID")
    if any(value is None for value in (
        submitted, started, received, target, start_offset, latency, receive_offset,
    )):
        failures.append("TIMESTAMP_EVIDENCE_MISSING")
    else:
        if (
            submitted < target
            or started < target
            or started < submitted
            or received < started
            or start_offset < 0.0
            or start_offset > identity.MAX_REQUEST_START_OFFSET_SECONDS
            or latency < 0.0
            or latency > identity.MAX_RESPONSE_LATENCY_SECONDS
            or receive_offset < 0.0
            or receive_offset > identity.MAX_RECEIVE_OFFSET_SECONDS
            or abs((started - target) - start_offset) > 1e-6
            or abs((received - started) - latency) > 1e-6
            or abs((received - target) - receive_offset) > 1e-6
        ):
            failures.append("TIMESTAMP_ALIGNMENT_INVALID")
    if row.get("status") != "OK" or row.get("failure_reason") is not None:
        failures.append("CAPTURE_NOT_OK")
    bid = _num(row.get("best_bid"))
    ask = _num(row.get("best_ask"))
    bid_size = _num(row.get("bid_size"))
    ask_size = _num(row.get("ask_size"))
    mid = _num(row.get("mid"))
    imbalance = _num(row.get("top_imbalance"))
    if (
        bid is None or ask is None or bid_size is None or ask_size is None
        or mid is None or imbalance is None
        or bid <= 0.0 or ask <= 0.0 or bid > ask
        or bid_size <= 0.0 or ask_size <= 0.0
        or abs(mid - (bid + ask) / 2.0) > 1e-9
        or abs(imbalance - (bid_size - ask_size) / (bid_size + ask_size)) > 1e-9
    ):
        failures.append("BOOK_GEOMETRY_INVALID")
    source_ts = _num(row.get("source_timestamp"))
    mutation_age = _num(row.get("source_mutation_age_seconds"))
    if source_ts is not None and (
        received is None or mutation_age is None
        or abs((received - source_ts) - mutation_age) > 1e-6
        or mutation_age < -identity.MAX_EXCHANGE_CLOCK_LEAD_SECONDS
    ):
        failures.append("SOURCE_TIMESTAMP_PROVENANCE_INVALID")
    return list(dict.fromkeys(failures))


def build_readiness(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    load_protocol()
    failures: Counter[str] = Counter()
    valid_by_stage_close: dict[tuple[float, str], set[str]] = defaultdict(set)
    geometry_by_stage_close: dict[tuple[float, str], set[str]] = defaultdict(set)
    rows_by_asset: Counter[str] = Counter()
    ok_rows_by_asset: Counter[str] = Counter()
    provider_rows: Counter[str] = Counter()
    eligible = []
    for row in rows:
        close = _num(row.get("close_time"))
        stage = str(row.get("stage") or "").upper()
        asset = str(row.get("asset") or "").upper()
        if (
            close is None or close <= identity.PROSPECTIVE_AFTER_CLOSE_TIME
            or stage not in STAGE_DELAY_SECONDS or asset not in ASSETS
        ):
            continue
        eligible.append(row)
    exact_identity_counts = Counter(
        (
            str(row.get("ticker") or ""),
            _num(row.get("close_time")),
            str(row.get("stage") or "").upper(),
        )
        for row in eligible
    )
    asset_stage_counts = Counter(
        (
            _num(row.get("close_time")),
            str(row.get("stage") or "").upper(),
            str(row.get("asset") or "").upper(),
        )
        for row in eligible
    )
    duplicate_exact = {
        key for key, count in exact_identity_counts.items() if count != 1
    }
    duplicate_asset_stage = {
        key for key, count in asset_stage_counts.items() if count != 1
    }
    for row in eligible:
        close = _num(row.get("close_time"))
        stage = str(row.get("stage") or "").upper()
        asset = str(row.get("asset") or "").upper()
        rows_by_asset[asset] += 1
        provider_rows[str(row.get("provider") or "")] += 1
        geometry_by_stage_close[(close, stage)].add(asset)
        row_failures = _quality_failures(row)
        if (str(row.get("ticker") or ""), close, stage) in duplicate_exact:
            row_failures.append("DUPLICATE_EXACT_IDENTITY")
        if (close, stage, asset) in duplicate_asset_stage:
            row_failures.append("DUPLICATE_ASSET_STAGE_IDENTITY")
        failures.update(row_failures)
        if not row_failures:
            valid_by_stage_close[(close, stage)].add(asset)
            ok_rows_by_asset[asset] += 1
    complete_stage_windows = {
        key for key, assets in valid_by_stage_close.items() if assets == ASSETS
    }
    geometry_stage_windows = {
        key for key, assets in geometry_by_stage_close.items() if assets == ASSETS
    }
    closes = sorted({close for close, _ in geometry_by_stage_close})
    complete_closes = [
        close for close in closes
        if all((close, stage) in complete_stage_windows for stage in STAGE_DELAY_SECONDS)
    ]
    geometry_closes = [
        close for close in closes
        if all((close, stage) in geometry_stage_windows for stage in STAGE_DELAY_SECONDS)
    ]
    return {
        "protocol_id": identity.PROTOCOL_ID,
        "protocol_sha256": identity.PROTOCOL_SHA256,
        "first_eligible_close_time": identity.FIRST_ELIGIBLE_CLOSE_TIME,
        "eligible_rows": len(eligible),
        "all_seven_geometry_stage_windows": len(geometry_stage_windows),
        "valid_all_seven_stage_windows": len(complete_stage_windows),
        "all_four_stage_geometry_close_windows": len(geometry_closes),
        "complete_all_four_stage_close_windows": len(complete_closes),
        "rows_by_asset": dict(sorted(rows_by_asset.items())),
        "valid_rows_by_asset": dict(sorted(ok_rows_by_asset.items())),
        "rows_by_provider": dict(sorted(provider_rows.items())),
        "quality_failure_counts": dict(sorted(failures.items())),
        "database_schema_outcome_free": True,
        "outcome_columns_selected": False,
        "outcome_labels_read": False,
        "model_fit_performed": False,
        "threshold_selection_performed": False,
        "notification_eligible": False,
        "real_trading_allowed": False,
        "used_by_v21": False,
        "status": "COLLECTING_OUTCOME_BLIND_OFFICIAL_REST_BOOKS",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    args = parser.parse_args()
    print(json.dumps(
        build_readiness(load_rows(Path(args.db))), indent=2, sort_keys=True
    ))


if __name__ == "__main__":
    main()
