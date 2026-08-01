"""Point-in-time cross-asset crypto regime context for exact RTI research.

The capture is reconstructed from locally archived Coinbase L2 and Kraken L3
rows at or before one immutable exact-13M cutoff.  It summarizes whether the
target move is broad, isolated, leading, or lagging without reading outcomes,
fitting a model, notifying, or trading.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import sqlite3
from statistics import median
from typing import Any, Mapping


SCHEMA_VERSION = "rti-cross-asset-regime-v1"
TIME_BASIS = "local_created_at"
ASSETS = ("BNB", "BTC", "DOGE", "ETH", "HYPE", "SOL", "XRP")
HORIZONS = (15, 60)
_SYMBOLS = {
    "BTC": ("BTC-USD", "BTC/USD"),
    "ETH": ("ETH-USD", "ETH/USD"),
    "SOL": ("SOL-USD", "SOL/USD"),
    "XRP": ("XRP-USD", "XRP/USD"),
    "DOGE": ("DOGE-USD", "DOGE/USD"),
    "BNB": ("BNB-USD", "BNB/USD"),
    "HYPE": ("HYPE-USD", "HYPE/USD"),
}


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _mid(row: Mapping[str, Any]) -> float | None:
    direct = _num(row.get("mid"))
    if direct is not None and direct > 0.0:
        return direct
    bid = _num(row.get("best_bid"))
    ask = _num(row.get("best_ask"))
    if bid is None or ask is None or bid <= 0.0 or ask <= bid:
        return None
    return (bid + ask) / 2.0


def _snapshot(
    conn: sqlite3.Connection,
    *,
    table: str,
    symbol_column: str,
    symbol: str,
    cutoff: float,
    max_lag: float,
) -> tuple[dict[str, float] | None, str | None]:
    row = conn.execute(
        f"SELECT created_at,last_message_age_seconds,best_bid,best_ask,mid "
        f"FROM {table} WHERE {symbol_column}=? AND created_at<=? "
        "ORDER BY created_at DESC LIMIT 1",
        (symbol, cutoff),
    ).fetchone()
    if row is None:
        return None, "snapshot_missing"
    raw = dict(row)
    created_at = _num(raw.get("created_at"))
    message_age = _num(raw.get("last_message_age_seconds"))
    price = _mid(raw)
    if created_at is None or message_age is None or price is None:
        return None, "snapshot_invalid"
    lag = cutoff - created_at
    effective_age = lag + message_age
    if lag < -1e-6:
        return None, "snapshot_after_cutoff"
    if lag > max_lag:
        return None, "snapshot_stale"
    if message_age < 0.0 or effective_age > max_lag:
        return None, "transport_stale"
    bid = _num(raw.get("best_bid"))
    ask = _num(raw.get("best_ask"))
    if bid is not None and ask is not None and bid >= ask:
        return None, "crossed_book"
    return {
        "created_at": created_at,
        "lag_seconds": max(0.0, lag),
        "effective_message_age_seconds": effective_age,
        "mid": price,
    }, None


def _venue_paths(
    *,
    db_path: str,
    table: str,
    symbol_column: str,
    symbol_index: int,
    captured_at: float,
    max_lag: float,
) -> tuple[dict[str, dict[int | str, float]] | None, str | None]:
    if not Path(db_path).exists():
        return None, "database_missing"
    output: dict[str, dict[int | str, float]] = {}
    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        conn.row_factory = sqlite3.Row
        try:
            for asset in ASSETS:
                symbol = _SYMBOLS[asset][symbol_index]
                current, reason = _snapshot(
                    conn,
                    table=table,
                    symbol_column=symbol_column,
                    symbol=symbol,
                    cutoff=captured_at,
                    max_lag=max_lag,
                )
                if reason is not None or current is None:
                    return None, f"{asset}:current:{reason}"
                values: dict[int | str, float] = {
                    "current_created_at": current["created_at"],
                    "current_lag": current["lag_seconds"],
                    "current_message_age": current[
                        "effective_message_age_seconds"
                    ],
                    "current_mid": current["mid"],
                }
                for horizon in HORIZONS:
                    start, reason = _snapshot(
                        conn,
                        table=table,
                        symbol_column=symbol_column,
                        symbol=symbol,
                        cutoff=captured_at - horizon,
                        max_lag=max_lag,
                    )
                    if reason is not None or start is None:
                        return None, f"{asset}:start_{horizon}s:{reason}"
                    values[horizon] = (
                        (current["mid"] - start["mid"])
                        / start["mid"] * 10_000.0
                    )
                    values[f"start_created_at_{horizon}s"] = start["created_at"]
                    values[f"start_lag_{horizon}s"] = start["lag_seconds"]
                    values[f"start_message_age_{horizon}s"] = start[
                        "effective_message_age_seconds"
                    ]
                output[asset] = values
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return None, f"query_error:{type(exc).__name__}"
    return output, None


def _signed_breadth(values: list[float]) -> float:
    epsilon = 1e-12
    return sum(
        1.0 if value > epsilon else -1.0 if value < -epsilon else 0.0
        for value in values
    ) / len(values)


def _direction_agreement(left: float, right: float) -> float:
    epsilon = 1e-12
    if abs(left) <= epsilon or abs(right) <= epsilon:
        return 0.5
    return 1.0 if (left > 0.0) == (right > 0.0) else 0.0


def _centered_rank(target: float, values: list[float]) -> float:
    if len(values) <= 1:
        return 0.0
    less = sum(value < target for value in values)
    greater = sum(value > target for value in values)
    return (less - greater) / (len(values) - 1.0)


def capture_rti_cross_asset_context(
    asset: str,
    *,
    captured_at: float,
    coinbase_db: str | None = None,
    kraken_db: str | None = None,
    max_lag_seconds: float = 10.0,
) -> dict[str, Any]:
    """Capture robust all-asset breadth/relative-strength evidence."""
    target = str(asset or "").upper()
    captured = _num(captured_at)
    max_lag = max(1.0, float(max_lag_seconds))
    prefix = "rti_cross_asset"
    out: dict[str, Any] = {
        f"{prefix}_schema_version": SCHEMA_VERSION,
        f"{prefix}_time_basis": TIME_BASIS,
        f"{prefix}_status": "missing",
        f"{prefix}_missing_reason": None,
        f"{prefix}_evidence_cutoff_at": captured,
        f"{prefix}_max_lag_seconds": max_lag,
        f"{prefix}_required_asset_count": len(ASSETS),
        f"{prefix}_available_asset_count": 0,
        f"{prefix}_latest_snapshot_created_at": None,
        f"{prefix}_max_snapshot_age_seconds": None,
        f"{prefix}_max_message_age_seconds": None,
    }
    for horizon in HORIZONS:
        out.update({
            f"{prefix}_latest_start_created_at_{horizon}s": None,
            f"{prefix}_max_start_age_seconds_{horizon}s": None,
            f"{prefix}_max_start_message_age_seconds_{horizon}s": None,
            f"{prefix}_median_momentum_bps_{horizon}s": None,
            f"{prefix}_breadth_signed_{horizon}s": None,
            f"{prefix}_dispersion_mad_bps_{horizon}s": None,
            f"{prefix}_btc_minus_non_btc_median_bps_{horizon}s": None,
            f"{prefix}_asset_centered_rank_{horizon}s": None,
            f"{prefix}_asset_btc_direction_agreement_{horizon}s": None,
        })
        for candidate in ASSETS:
            out[f"{prefix}_{candidate.lower()}_consensus_change_bps_{horizon}s"] = None
            for venue in ("coinbase", "kraken"):
                out[
                    f"{prefix}_{venue}_{candidate.lower()}_change_bps_{horizon}s"
                ] = None
    if target not in ASSETS or captured is None:
        out[f"{prefix}_missing_reason"] = "invalid_asset_or_timestamp"
        return out

    cb_path = coinbase_db or os.environ.get(
        "Q15_COINBASE_ADV_L2_DB", "data/q15_coinbase_adv_l2_v1.sqlite3"
    )
    kr_path = kraken_db or os.environ.get(
        "Q15_KRAKEN_L3_DB", "data/q15_kraken_l3_v1.sqlite3"
    )
    coinbase, cb_reason = _venue_paths(
        db_path=cb_path,
        table="coinbase_adv_l2_snapshots",
        symbol_column="product_id",
        symbol_index=0,
        captured_at=captured,
        max_lag=max_lag,
    )
    kraken, kr_reason = _venue_paths(
        db_path=kr_path,
        table="kraken_l3_summaries",
        symbol_column="symbol",
        symbol_index=1,
        captured_at=captured,
        max_lag=max_lag,
    )
    if coinbase is None or kraken is None:
        out[f"{prefix}_missing_reason"] = ";".join(
            value for value in (
                None if coinbase is not None else f"coinbase:{cb_reason}",
                None if kraken is not None else f"kraken:{kr_reason}",
            ) if value is not None
        )
        return out

    current_rows = [*coinbase.values(), *kraken.values()]
    out.update({
        f"{prefix}_available_asset_count": len(ASSETS),
        f"{prefix}_latest_snapshot_created_at": max(
            float(row["current_created_at"]) for row in current_rows
        ),
        f"{prefix}_max_snapshot_age_seconds": max(
            float(row["current_lag"]) for row in current_rows
        ),
        f"{prefix}_max_message_age_seconds": max(
            float(row["current_message_age"]) for row in current_rows
        ),
    })
    for horizon in HORIZONS:
        moves = {
            candidate: (
                float(coinbase[candidate][horizon])
                + float(kraken[candidate][horizon])
            ) / 2.0
            for candidate in ASSETS
        }
        values = list(moves.values())
        center = float(median(values))
        mad = float(median([abs(value - center) for value in values]))
        non_btc_center = float(median([
            moves[candidate] for candidate in ASSETS if candidate != "BTC"
        ]))
        out.update({
            f"{prefix}_latest_start_created_at_{horizon}s": max(
                float(row[f"start_created_at_{horizon}s"])
                for row in current_rows
            ),
            f"{prefix}_max_start_age_seconds_{horizon}s": max(
                float(row[f"start_lag_{horizon}s"])
                for row in current_rows
            ),
            f"{prefix}_max_start_message_age_seconds_{horizon}s": max(
                float(row[f"start_message_age_{horizon}s"])
                for row in current_rows
            ),
            f"{prefix}_median_momentum_bps_{horizon}s": center,
            f"{prefix}_breadth_signed_{horizon}s": _signed_breadth(values),
            f"{prefix}_dispersion_mad_bps_{horizon}s": mad,
            f"{prefix}_btc_minus_non_btc_median_bps_{horizon}s": (
                moves["BTC"] - non_btc_center
            ),
            f"{prefix}_asset_centered_rank_{horizon}s": _centered_rank(
                moves[target], values
            ),
            f"{prefix}_asset_btc_direction_agreement_{horizon}s": (
                _direction_agreement(moves[target], moves["BTC"])
            ),
            **{
                f"{prefix}_{candidate.lower()}_consensus_change_bps_{horizon}s": move
                for candidate, move in moves.items()
            },
            **{
                f"{prefix}_coinbase_{candidate.lower()}_change_bps_{horizon}s": (
                    float(coinbase[candidate][horizon])
                )
                for candidate in ASSETS
            },
            **{
                f"{prefix}_kraken_{candidate.lower()}_change_bps_{horizon}s": (
                    float(kraken[candidate][horizon])
                )
                for candidate in ASSETS
            },
        })
    out[f"{prefix}_status"] = "ok"
    return out


PERSISTED_KEYS = (
    "rti_cross_asset_schema_version",
    "rti_cross_asset_time_basis",
    "rti_cross_asset_status",
    "rti_cross_asset_missing_reason",
    "rti_cross_asset_evidence_cutoff_at",
    "rti_cross_asset_max_lag_seconds",
    "rti_cross_asset_required_asset_count",
    "rti_cross_asset_available_asset_count",
    "rti_cross_asset_latest_snapshot_created_at",
    "rti_cross_asset_max_snapshot_age_seconds",
    "rti_cross_asset_max_message_age_seconds",
    *(
        f"rti_cross_asset_{key}_{horizon}s"
        for horizon in HORIZONS
        for key in (
            "latest_start_created_at",
            "max_start_age_seconds",
            "max_start_message_age_seconds",
            "median_momentum_bps",
            "breadth_signed",
            "dispersion_mad_bps",
            "btc_minus_non_btc_median_bps",
            "asset_centered_rank",
            "asset_btc_direction_agreement",
        )
    ),
    *(
        f"rti_cross_asset_{asset.lower()}_consensus_change_bps_{horizon}s"
        for horizon in HORIZONS
        for asset in ASSETS
    ),
    *(
        f"rti_cross_asset_{venue}_{asset.lower()}_change_bps_{horizon}s"
        for horizon in HORIZONS
        for venue in ("coinbase", "kraken")
        for asset in ASSETS
    ),
)
