"""Audit exact-13M RTI feature capture without inspecting trade outcomes."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from q15_upgrade.strategy_bots.rules import (
    BOT_RTI_PATH_13M,
    KALSHI_FLOW_KEYS,
    RTI_CROSS_VENUE_KEYS,
    RTI_CROSS_ASSET_KEYS,
    RTI_INDEPENDENT_VENUE_KEYS,
    RTI_INDEPENDENT_MICROSTRUCTURE_KEYS,
    RTI_INDEPENDENT_PATH_KEYS,
    RTI_SPOT_LEAD_LAG_KEYS,
    RTI_EXACT_MICROSTRUCTURE_SCHEMA_V1,
    RTI_EXACT_MICROSTRUCTURE_SCHEMA_V2,
    RTI_EXACT_MICROSTRUCTURE_SCHEMA_VERSION,
    SPOT_MID_PATH_KEYS,
)
from q15_upgrade.strategy_bots.rti_microstructure_extension import (
    extension_window_coverage,
)


SPOT_FLOW_KEYS = (
    "spot_depth_imbalance",
    "spot_depth_trade_net_qty_5s",
    "spot_depth_trade_net_notional_5s",
    "spot_depth_trade_net_qty_15s",
    "spot_depth_trade_net_notional_15s",
    "spot_depth_trade_net_qty_60s",
    "spot_depth_trade_net_notional_60s",
)
FEATURE_KEYS = (
    tuple(KALSHI_FLOW_KEYS)
    + SPOT_FLOW_KEYS
    + tuple(SPOT_MID_PATH_KEYS)
    + tuple(RTI_SPOT_LEAD_LAG_KEYS)
    + tuple(RTI_CROSS_VENUE_KEYS)
    + tuple(RTI_INDEPENDENT_VENUE_KEYS)
    + tuple(RTI_INDEPENDENT_MICROSTRUCTURE_KEYS)
    + tuple(RTI_INDEPENDENT_PATH_KEYS)
    + tuple(RTI_CROSS_ASSET_KEYS)
)
# The strategy ledger's threshold blob also contains display-only historical
# scoreboard values.  Feature-only tools must never receive that whole object,
# even though today's builders ignore those values.  This explicit allow-list
# contains only immutable decision-time evidence needed by the frozen feature
# lineages and preregistered outcome-blind source extensions.
SAFE_FEATURE_PROFILE_KEYS = frozenset(FEATURE_KEYS) | frozenset({
    "asset_cohort",
    "rti_side",
    "rti_market_mid_probability",
    "rti_opposite_ask_cents",
    "rti_opposite_depth_contracts",
    "rti_signed_distance_bps",
    "rti_side_move_bps",
    "rti_path_first_half_side_move_bps",
    "rti_path_second_half_side_move_bps",
    "rti_path_acceleration_bps",
    "rti_path_start_px",
    "rti_path_end_px",
    "rti_path_range_bps",
    "rti_path_realized_volatility_bps",
    "rti_path_trend_efficiency",
    "rti_path_persistence",
    "rti_path_strike_crossings",
    "rti_path_seconds_since_last_crossing",
    "rti_expected_remaining_volatility_bps",
    "rti_distance_to_remaining_volatility",
    "rti_evaluated_at",
    "kalshi_microstructure_captured_at",
})


def sanitize_feature_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Return only explicitly approved decision-time feature evidence."""
    return {
        key: profile[key]
        for key in SAFE_FEATURE_PROFILE_KEYS
        if key in profile
    }


def sanitize_feature_rows(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Enforce the profile allow-list for programmatic callers as well."""
    output: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        profile_raw = row.get("threshold_json")
        if isinstance(profile_raw, Mapping):
            profile = dict(profile_raw)
        else:
            try:
                decoded = json.loads(str(profile_raw or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                decoded = {}
            profile = dict(decoded) if isinstance(decoded, Mapping) else {}
        row["threshold_json"] = json.dumps(
            sanitize_feature_profile(profile),
            sort_keys=True,
            separators=(",", ":"),
        )
        output.append(row)
    return output


def feature_only_sql_projection(
    columns: set[str],
) -> tuple[list[str], dict[str, str]]:
    """Build a SQL allow-list that never returns the raw profile blob."""
    if "threshold_json" not in columns:
        raise ValueError("feature_database_threshold_json_missing")
    expressions: list[str] = []
    profile_aliases: dict[str, str] = {}
    for index, key in enumerate(sorted(SAFE_FEATURE_PROFILE_KEYS)):
        if re.fullmatch(r"[A-Za-z0-9_]+", key) is None:
            raise AssertionError("unsafe_feature_profile_key")
        extracted = (
            "CASE WHEN json_valid(threshold_json) "
            f"THEN json_extract(threshold_json, '$.{key}') ELSE NULL END"
        )
        if key in columns:
            expressions.append(
                f'COALESCE("{key}", {extracted}) AS "{key}"'
            )
        else:
            alias = f"__safe_feature_profile_{index:04d}"
            expressions.append(f'{extracted} AS "{alias}"')
            profile_aliases[key] = alias
    return expressions, profile_aliases


def materialize_feature_only_row(
    raw: Mapping[str, Any], profile_aliases: Mapping[str, str],
) -> dict[str, Any]:
    """Rebuild the minimal safe profile from SQL-projected values only."""
    row = dict(raw)
    profile: dict[str, Any] = {}
    for key, alias in profile_aliases.items():
        value = row.pop(alias, None)
        if value is not None:
            profile[key] = value
    row["threshold_json"] = json.dumps(
        profile, sort_keys=True, separators=(",", ":"),
    )
    return row
EXPECTED_ASSETS = frozenset({"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"})
FIRST_FEATURE_REVIEW_WINDOWS = 30
MIN_MODELING_WINDOWS = 60


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _coverage(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    counts = {
        key: sum(row.get(key) is not None for row in rows)
        for key in FEATURE_KEYS
    }
    return {
        "rows": total,
        "counts": counts,
        "rates": {
            key: None if total == 0 else count / total
            for key, count in counts.items()
        },
    }


def build_report(
    rows: Sequence[Mapping[str, Any]],
    *,
    source_schema: str = RTI_EXACT_MICROSTRUCTURE_SCHEMA_VERSION,
) -> dict[str, Any]:
    safe_rows = sanitize_feature_rows(rows)
    exact = [
        dict(row)
        for row in safe_rows
        if row.get("bot_name") == BOT_RTI_PATH_13M
        and str(row.get("interval") or "").upper() == "13M"
        and str(row.get("record_kind") or "").upper()
        == "RTI_PATH_13M_PROSPECTIVE_EXACT"
    ]
    rich = [
        row
        for row in exact
        if row.get("kalshi_microstructure_schema_version")
        == source_schema
    ]
    version_rows = {
        RTI_EXACT_MICROSTRUCTURE_SCHEMA_V1: [
            row for row in exact
            if row.get("kalshi_microstructure_schema_version")
            == RTI_EXACT_MICROSTRUCTURE_SCHEMA_V1
        ],
        RTI_EXACT_MICROSTRUCTURE_SCHEMA_V2: [
            row for row in exact
            if row.get("kalshi_microstructure_schema_version")
            == RTI_EXACT_MICROSTRUCTURE_SCHEMA_V2
        ],
    }
    dynamics_extension = extension_window_coverage(exact)
    failures: list[dict[str, Any]] = []
    for row in rich:
        close = _num(row.get("close_time"))
        source = _num(row.get("source_captured_at"))
        captured = _num(row.get("kalshi_microstructure_captured_at"))
        evidence = _num(row.get("evidence_as_of"))
        reasons = []
        if close is None or source is None or captured is None or evidence is None:
            reasons.append("TIMESTAMP_MISSING")
        else:
            if not 0.0 <= captured - (close - 780.0) <= 2.0:
                reasons.append("NOT_EXACT_13M")
            if abs(captured - source) > 1e-6:
                reasons.append("QUOTE_SOURCE_TIMESTAMP_MISMATCH")
            if captured > evidence + 1e-6:
                reasons.append("EVIDENCE_PRECEDES_CAPTURE")
        if reasons:
            failures.append({
                "id": row.get("id"),
                "ticker": row.get("ticker"),
                "asset": row.get("asset"),
                "close_time": close,
                "reasons": reasons,
            })

    by_asset: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    by_close_all: dict[float, list[Mapping[str, Any]]] = defaultdict(list)
    by_close_rich: dict[float, list[Mapping[str, Any]]] = defaultdict(list)
    for row in exact:
        by_asset[str(row.get("asset") or "UNKNOWN")].append(row)
        close = _num(row.get("close_time"))
        if close is not None:
            by_close_all[close].append(row)
    for row in rich:
        close = _num(row.get("close_time"))
        if close is not None:
            by_close_rich[close].append(row)
    partial_windows = [
        {
            "close_time": close,
            "all_exact_rows": len(window_rows),
            "source_schema_rows": len(by_close_rich.get(close, ())),
        }
        for close, window_rows in sorted(by_close_all.items())
        if by_close_rich.get(close)
        and len(by_close_rich[close]) != len(window_rows)
    ]
    complete_rich_windows = []
    incomplete_rich_windows = []
    for close, window_rows in sorted(by_close_rich.items()):
        assets = {
            str(row.get("asset") or "UNKNOWN").upper()
            for row in window_rows
        }
        complete = len(window_rows) == len(EXPECTED_ASSETS) and assets == EXPECTED_ASSETS
        details = {
            "close_time": close,
            "rows": len(window_rows),
            "assets": sorted(assets),
            "missing_assets": sorted(EXPECTED_ASSETS - assets),
            "unexpected_assets": sorted(assets - EXPECTED_ASSETS),
        }
        if complete:
            complete_rich_windows.append(details)
        else:
            incomplete_rich_windows.append(details)
    complete_window_count = len(complete_rich_windows)
    first_review_ready = bool(
        complete_window_count >= FIRST_FEATURE_REVIEW_WINDOWS
        and not failures
        and not partial_windows
        and not incomplete_rich_windows
    )
    modeling_ready = bool(
        complete_window_count >= MIN_MODELING_WINDOWS
        and not failures
        and not partial_windows
        and not incomplete_rich_windows
    )
    rich_assets: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rich:
        rich_assets[str(row.get("asset") or "UNKNOWN")].append(row)
    version_assets: dict[str, dict[str, list[Mapping[str, Any]]]] = {}
    for version, version_group in version_rows.items():
        grouped_assets: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in version_group:
            grouped_assets[str(row.get("asset") or "UNKNOWN")].append(row)
        version_assets[version] = grouped_assets
    return {
        "audit_version": "rti-exact-feature-coverage-v2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "outcome_labels_read": False,
        "historical_backfill_allowed": False,
        "schema_version": source_schema,
        "all_exact": _coverage(exact),
        "microstructure_source": _coverage(rich),
        "microstructure_source_by_asset": {
            asset: _coverage(asset_rows)
            for asset, asset_rows in sorted(rich_assets.items())
        },
        "microstructure_v1": _coverage(
            version_rows[RTI_EXACT_MICROSTRUCTURE_SCHEMA_V1]
        ),
        "microstructure_v1_by_asset": {
            asset: _coverage(asset_rows)
            for asset, asset_rows in sorted(
                version_assets[RTI_EXACT_MICROSTRUCTURE_SCHEMA_V1].items()
            )
        },
        "microstructure_v2": _coverage(
            version_rows[RTI_EXACT_MICROSTRUCTURE_SCHEMA_V2]
        ),
        "microstructure_v2_by_asset": {
            asset: _coverage(asset_rows)
            for asset, asset_rows in sorted(
                version_assets[RTI_EXACT_MICROSTRUCTURE_SCHEMA_V2].items()
            )
        },
        "dynamics_extension_v1": dynamics_extension,
        "timestamp_alignment_failures": failures,
        "cross_asset_partial_schema_windows": partial_windows,
        "microstructure_source_close_windows": len(by_close_rich),
        "complete_microstructure_close_windows": complete_window_count,
        "incomplete_microstructure_close_windows": incomplete_rich_windows,
        # Compatibility aliases used by the frozen v1-v3 tooling.  They refer
        # to the explicitly requested source schema, never silently mix v1/v2.
        "microstructure_v1_close_windows": len(by_close_rich),
        "complete_microstructure_v1_close_windows": complete_window_count,
        "incomplete_microstructure_v1_close_windows": incomplete_rich_windows,
        "first_feature_review_windows": FIRST_FEATURE_REVIEW_WINDOWS,
        "minimum_modeling_windows": MIN_MODELING_WINDOWS,
        "ready_for_first_feature_review": first_review_ready,
        "ready_for_modeling": modeling_ready,
        "complete_windows_required_before_first_feature_review": max(
            0, FIRST_FEATURE_REVIEW_WINDOWS - complete_window_count
        ),
        "complete_windows_required_before_modeling": max(
            0, MIN_MODELING_WINDOWS - complete_window_count
        ),
        # Compatibility alias.  This now counts independent complete close
        # windows, never correlated per-asset rows.
        "rows_required_before_first_feature_review": max(
            0, FIRST_FEATURE_REVIEW_WINDOWS - complete_window_count
        ),
    }


def _load_rows(db_path: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(
        f"file:{db_path.resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=30.0,
    )
    connection.row_factory = sqlite3.Row
    try:
        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(strategy_bot_decisions)"
            ).fetchall()
        }
        selected = [
            key
            for key in (
                "id", "bot_name", "source_system", "interval", "record_kind",
                "ticker", "asset", "side", "entry_ask_cents", "spread_cents",
                "depth_contracts",
                "close_time", "source_captured_at", "evidence_as_of",
            )
            if key in columns
        ]
        if not selected:
            return []
        feature_projection, profile_aliases = feature_only_sql_projection(
            columns
        )
        query = (
            f"SELECT {','.join([*selected, *feature_projection])} "
            "FROM strategy_bot_decisions "
            "WHERE bot_name=? AND interval='13M' ORDER BY close_time,id"
        )
        output = []
        for raw in connection.execute(query, (BOT_RTI_PATH_13M,)).fetchall():
            row = materialize_feature_only_row(raw, profile_aliases)
            profile = json.loads(row["threshold_json"])
            if row.get("evidence_as_of") is None:
                row["evidence_as_of"] = profile.get("rti_evaluated_at")
            if row.get("kalshi_microstructure_captured_at") is None:
                row["kalshi_microstructure_captured_at"] = profile.get(
                    "kalshi_microstructure_captured_at"
                )
            output.append(row)
        return output
    finally:
        connection.close()


def _markdown(report: Mapping[str, Any]) -> str:
    summary = report["microstructure_source"]
    return "\n".join((
        "# RTI exact feature coverage audit",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Schema: `{report['schema_version']}`",
        f"- Exact rows with schema: {summary['rows']}",
        f"- Complete seven-asset close windows: {report['complete_microstructure_close_windows']}",
        f"- Complete dynamics-extension windows: {report['dynamics_extension_v1']['complete_extension_close_windows']}",
        f"- Timestamp failures: {len(report['timestamp_alignment_failures'])}",
        f"- Partial same-close windows: {len(report['cross_asset_partial_schema_windows'])}",
        f"- Ready for first feature review: {report['ready_for_first_feature_review']}",
        f"- Ready for modeling: {report['ready_for_modeling']}",
        f"- Complete windows remaining to 30: {report['complete_windows_required_before_first_feature_review']}",
        f"- Complete windows remaining to 60: {report['complete_windows_required_before_modeling']}",
        "",
        "This audit does not read outcomes and cannot promote a trading rule.",
        "",
    ))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--strategy-db",
        default=os.environ.get("Q15_STRATEGY_BOTS_DB")
        or "data/q15_strategy_bots_v3.sqlite3",
    )
    parser.add_argument(
        "--output-dir", default="work/rti-feature-coverage"
    )
    args = parser.parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    report = build_report(_load_rows(Path(args.strategy_db)))
    json_path = output / "audit.json"
    markdown_path = output / "audit.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    print(json.dumps({
        "json": str(json_path),
        "markdown": str(markdown_path),
        "microstructure_v1_rows": report["microstructure_v1"]["rows"],
        "complete_microstructure_v1_close_windows": report[
            "complete_microstructure_v1_close_windows"
        ],
        "ready_for_first_feature_review": report[
            "ready_for_first_feature_review"
        ],
        "ready_for_modeling": report["ready_for_modeling"],
        "timestamp_alignment_failures": len(
            report["timestamp_alignment_failures"]
        ),
        "partial_windows": len(report["cross_asset_partial_schema_windows"]),
    }, indent=2))


if __name__ == "__main__":
    main()
