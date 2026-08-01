"""Point-in-time cross-venue price consensus for exact RTI research.

The Coinbase and Kraken collectors persist independently of the RTI sampler.
This module reconstructs their state using only snapshots whose local
``created_at`` is at or before the exact decision timestamp.  It is feature
capture only: no outcome, model, notification, or order surface lives here.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from .rti_independent_microstructure import (
    capture_rti_independent_microstructure,
)
from .rti_independent_path import capture_rti_independent_path
from .rti_cross_asset_context import capture_rti_cross_asset_context


SCHEMA_VERSION = "rti-cross-venue-consensus-v1"
INDEPENDENT_SCHEMA_VERSION = "rti-independent-venue-consensus-v1"
TIME_BASIS = "local_created_at"
HORIZONS = (15, 60)

_ASSET_SYMBOLS = {
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


def _max_lag_seconds() -> float:
    try:
        return max(
            1.0,
            float(os.environ.get("Q15_RTI_CROSS_VENUE_MAX_LAG_SECONDS", "10")),
        )
    except (TypeError, ValueError):
        return 10.0


def _mid(row: Mapping[str, Any]) -> float | None:
    mid = _num(row.get("mid"))
    if mid is not None and mid > 0.0:
        return mid
    bid = _num(row.get("best_bid"))
    ask = _num(row.get("best_ask"))
    if bid is None or ask is None or bid <= 0.0 or ask <= bid:
        return None
    return (bid + ask) / 2.0


def _nearest(
    conn: sqlite3.Connection,
    *,
    table: str,
    symbol_column: str,
    symbol: str,
    cutoff: float,
) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT created_at, last_message_age_seconds, best_bid, best_ask, mid "
        f"FROM {table} WHERE {symbol_column}=? AND created_at<=? "
        "ORDER BY created_at DESC LIMIT 1",
        (symbol, cutoff),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def _endpoint(
    row: Mapping[str, Any] | None,
    *,
    cutoff: float,
    max_lag: float,
) -> tuple[dict[str, float] | None, str | None]:
    if not row:
        return None, "snapshot_missing"
    created_at = _num(row.get("created_at"))
    price = _mid(row)
    if created_at is None or price is None:
        return None, "snapshot_invalid"
    lag = cutoff - created_at
    if lag < -1e-6:
        return None, "snapshot_after_cutoff"
    message_age = _num(row.get("last_message_age_seconds"))
    if message_age is None or message_age < 0.0:
        return None, "message_age_invalid"
    effective_age = max(0.0, lag) + message_age
    if lag > max_lag:
        return None, "snapshot_stale"
    if effective_age > max_lag:
        return None, "transport_stale"
    bid = _num(row.get("best_bid"))
    ask = _num(row.get("best_ask"))
    if bid is not None and ask is not None and bid >= ask:
        return None, "crossed_book"
    return {
        "created_at": created_at,
        "lag_seconds": max(0.0, lag),
        "effective_message_age_seconds": effective_age,
        "mid": price,
    }, None


def _venue(
    *,
    db_path: str,
    table: str,
    symbol_column: str,
    symbol: str,
    captured_at: float,
    max_lag: float,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "missing",
        "missing_reason": None,
        "symbol": symbol,
        "snapshot_created_at": None,
        "snapshot_age_seconds": None,
        "message_age_seconds": None,
        "mid": None,
    }
    for horizon in HORIZONS:
        result.update({
            f"start_created_at_{horizon}s": None,
            f"start_age_seconds_{horizon}s": None,
            f"start_mid_{horizon}s": None,
            f"change_bps_{horizon}s": None,
        })
    if not Path(db_path).exists():
        result["missing_reason"] = "database_missing"
        return result
    try:
        conn = sqlite3.connect(db_path, timeout=2.0)
        conn.row_factory = sqlite3.Row
        try:
            end, reason = _endpoint(
                _nearest(
                    conn,
                    table=table,
                    symbol_column=symbol_column,
                    symbol=symbol,
                    cutoff=captured_at,
                ),
                cutoff=captured_at,
                max_lag=max_lag,
            )
            starts: dict[int, dict[str, float]] = {}
            if reason is None and end is not None:
                for horizon in HORIZONS:
                    start, start_reason = _endpoint(
                        _nearest(
                            conn,
                            table=table,
                            symbol_column=symbol_column,
                            symbol=symbol,
                            cutoff=captured_at - horizon,
                        ),
                        cutoff=captured_at - horizon,
                        max_lag=max_lag,
                    )
                    if start_reason is not None or start is None:
                        reason = f"start_{horizon}s_{start_reason}"
                        break
                    starts[horizon] = start
        finally:
            conn.close()
    except sqlite3.Error as exc:
        result["missing_reason"] = f"query_error:{type(exc).__name__}"
        return result
    if reason is not None or end is None:
        result["missing_reason"] = reason or "endpoint_missing"
        return result

    result.update({
        "status": "ok",
        "missing_reason": None,
        "snapshot_created_at": end["created_at"],
        "snapshot_age_seconds": end["lag_seconds"],
        "message_age_seconds": end["effective_message_age_seconds"],
        "mid": end["mid"],
    })
    for horizon, start in starts.items():
        result.update({
            f"start_created_at_{horizon}s": start["created_at"],
            f"start_age_seconds_{horizon}s": start["lag_seconds"],
            f"start_mid_{horizon}s": start["mid"],
            f"change_bps_{horizon}s": (
                (end["mid"] - start["mid"]) / start["mid"] * 10_000.0
            ),
        })
    return result


def _direction_agreement(left: float, right: float) -> float:
    epsilon = 1e-12
    if abs(left) <= epsilon or abs(right) <= epsilon:
        return 0.5
    return 1.0 if (left > 0.0) == (right > 0.0) else 0.0


def capture_rti_cross_venue(
    asset: str,
    *,
    captured_at: float,
    primary_mid: Any,
    primary_change_bps_15s: Any,
    primary_change_bps_60s: Any,
    primary_source: Any = None,
    coinbase_db: str | None = None,
    kraken_db: str | None = None,
    max_lag_seconds: float | None = None,
) -> dict[str, Any]:
    """Freeze two independent collector paths at the exact RTI cutoff."""
    asset_key = str(asset or "").upper()
    captured = _num(captured_at)
    max_lag = _max_lag_seconds() if max_lag_seconds is None else max(
        1.0, float(max_lag_seconds)
    )
    base: dict[str, Any] = {
        "rti_cross_venue_schema_version": SCHEMA_VERSION,
        "rti_cross_venue_time_basis": TIME_BASIS,
        "rti_cross_venue_status": "missing",
        "rti_cross_venue_missing_reason": None,
        "rti_cross_venue_evidence_cutoff_at": captured,
        "rti_cross_venue_max_lag_seconds": max_lag,
        "rti_cross_venue_primary_source": (
            str(primary_source) if primary_source is not None else None
        ),
        "rti_cross_venue_available_count": 0,
        "rti_cross_venue_consensus_mid": None,
        "rti_cross_venue_current_divergence_bps": None,
        "rti_cross_venue_primary_basis_bps": None,
        "rti_independent_venue_schema_version": INDEPENDENT_SCHEMA_VERSION,
        "rti_independent_venue_time_basis": TIME_BASIS,
        "rti_independent_venue_status": "missing",
        "rti_independent_venue_missing_reason": None,
        "rti_independent_venue_evidence_cutoff_at": captured,
        "rti_independent_venue_max_lag_seconds": max_lag,
        "rti_independent_venue_available_count": 0,
        "rti_independent_venue_consensus_mid": None,
        "rti_independent_venue_current_divergence_bps": None,
    }
    base.update(capture_rti_independent_microstructure(
        asset_key,
        captured_at=float(captured) if captured is not None else float("nan"),
        coinbase_db=coinbase_db,
        kraken_db=kraken_db,
        max_lag_seconds=max_lag,
    ))
    base.update(capture_rti_independent_path(
        asset_key,
        captured_at=float(captured) if captured is not None else float("nan"),
        coinbase_db=coinbase_db,
        kraken_db=kraken_db,
        max_gap_seconds=max_lag,
    ))
    for venue in ("coinbase", "kraken"):
        for key in (
            "status", "missing_reason", "symbol", "snapshot_created_at",
            "snapshot_age_seconds", "message_age_seconds", "mid",
        ):
            base[f"rti_cross_venue_{venue}_{key}"] = None
        for horizon in HORIZONS:
            for key in (
                "start_created_at", "start_age_seconds", "start_mid",
                "change_bps",
            ):
                base[f"rti_cross_venue_{venue}_{key}_{horizon}s"] = None
    for venue in ("coinbase", "kraken"):
        for key in (
            "status", "missing_reason", "symbol", "snapshot_created_at",
            "snapshot_age_seconds", "message_age_seconds", "mid",
        ):
            base[f"rti_independent_venue_{venue}_{key}"] = None
        for horizon in HORIZONS:
            for key in (
                "start_created_at", "start_age_seconds", "start_mid",
                "change_bps",
            ):
                base[f"rti_independent_venue_{venue}_{key}_{horizon}s"] = None
    for horizon in HORIZONS:
        base.update({
            f"rti_cross_venue_consensus_change_bps_{horizon}s": None,
            f"rti_cross_venue_momentum_spread_bps_{horizon}s": None,
            f"rti_cross_venue_direction_agreement_{horizon}s": None,
            f"rti_cross_venue_primary_minus_consensus_bps_{horizon}s": None,
            f"rti_cross_venue_primary_direction_agreement_{horizon}s": None,
            f"rti_independent_venue_consensus_start_mid_{horizon}s": None,
            f"rti_independent_venue_consensus_change_bps_{horizon}s": None,
            f"rti_independent_venue_momentum_spread_bps_{horizon}s": None,
            f"rti_independent_venue_direction_agreement_{horizon}s": None,
        })
    symbols = _ASSET_SYMBOLS.get(asset_key)
    primary = _num(primary_mid)
    primary_changes = {
        15: _num(primary_change_bps_15s),
        60: _num(primary_change_bps_60s),
    }
    if captured is None or symbols is None:
        base["rti_cross_venue_missing_reason"] = "invalid_asset_or_timestamp"
        return base
    base.update(capture_rti_cross_asset_context(
        asset_key,
        captured_at=captured,
        coinbase_db=coinbase_db,
        kraken_db=kraken_db,
        max_lag_seconds=max_lag,
    ))
    venue_rows = {
        "coinbase": _venue(
            db_path=coinbase_db or os.environ.get(
                "Q15_COINBASE_ADV_L2_DB", "data/q15_coinbase_adv_l2_v1.sqlite3"
            ),
            table="coinbase_adv_l2_snapshots",
            symbol_column="product_id",
            symbol=symbols[0],
            captured_at=captured,
            max_lag=max_lag,
        ),
        "kraken": _venue(
            db_path=kraken_db or os.environ.get(
                "Q15_KRAKEN_L3_DB", "data/q15_kraken_l3_v1.sqlite3"
            ),
            table="kraken_l3_summaries",
            symbol_column="symbol",
            symbol=symbols[1],
            captured_at=captured,
            max_lag=max_lag,
        ),
    }
    for venue, values in venue_rows.items():
        for key, value in values.items():
            base[f"rti_cross_venue_{venue}_{key}"] = value
            base[f"rti_independent_venue_{venue}_{key}"] = value
    available = sum(values.get("status") == "ok" for values in venue_rows.values())
    base["rti_cross_venue_available_count"] = available
    base["rti_independent_venue_available_count"] = available
    if available != 2:
        reasons = [
            f"{venue}:{values.get('missing_reason') or values.get('status')}"
            for venue, values in venue_rows.items()
            if values.get("status") != "ok"
        ]
        base["rti_cross_venue_missing_reason"] = ";".join(reasons)
        base["rti_independent_venue_missing_reason"] = ";".join(reasons)
        return base

    coinbase = venue_rows["coinbase"]
    kraken = venue_rows["kraken"]
    coinbase_mid = float(coinbase["mid"])
    kraken_mid = float(kraken["mid"])
    consensus_mid = (coinbase_mid + kraken_mid) / 2.0
    base.update({
        "rti_cross_venue_consensus_mid": consensus_mid,
        "rti_cross_venue_current_divergence_bps": (
            abs(coinbase_mid - kraken_mid) / consensus_mid * 10_000.0
        ),
        "rti_independent_venue_status": "ok",
        "rti_independent_venue_missing_reason": None,
        "rti_independent_venue_consensus_mid": consensus_mid,
        "rti_independent_venue_current_divergence_bps": (
            abs(coinbase_mid - kraken_mid) / consensus_mid * 10_000.0
        ),
    })
    for horizon in HORIZONS:
        cb_move = float(coinbase[f"change_bps_{horizon}s"])
        kr_move = float(kraken[f"change_bps_{horizon}s"])
        consensus_move = (cb_move + kr_move) / 2.0
        consensus_start = (
            float(coinbase[f"start_mid_{horizon}s"])
            + float(kraken[f"start_mid_{horizon}s"])
        ) / 2.0
        base.update({
            f"rti_cross_venue_consensus_change_bps_{horizon}s": consensus_move,
            f"rti_cross_venue_momentum_spread_bps_{horizon}s": abs(cb_move - kr_move),
            f"rti_cross_venue_direction_agreement_{horizon}s": (
                _direction_agreement(cb_move, kr_move)
            ),
            f"rti_independent_venue_consensus_start_mid_{horizon}s": (
                consensus_start
            ),
            f"rti_independent_venue_consensus_change_bps_{horizon}s": (
                consensus_move
            ),
            f"rti_independent_venue_momentum_spread_bps_{horizon}s": (
                abs(cb_move - kr_move)
            ),
            f"rti_independent_venue_direction_agreement_{horizon}s": (
                _direction_agreement(cb_move, kr_move)
            ),
        })
    if primary is None or primary <= 0.0 or any(
        value is None for value in primary_changes.values()
    ):
        base["rti_cross_venue_missing_reason"] = "primary_spot_context_missing"
        return base

    base.update({
        "rti_cross_venue_status": "ok",
        "rti_cross_venue_missing_reason": None,
        "rti_cross_venue_primary_basis_bps": (
            (primary - consensus_mid) / consensus_mid * 10_000.0
        ),
    })
    for horizon in HORIZONS:
        consensus_move = float(
            base[f"rti_cross_venue_consensus_change_bps_{horizon}s"]
        )
        primary_move = float(primary_changes[horizon])
        base.update({
            f"rti_cross_venue_primary_minus_consensus_bps_{horizon}s": (
                primary_move - consensus_move
            ),
            f"rti_cross_venue_primary_direction_agreement_{horizon}s": (
                _direction_agreement(primary_move, consensus_move)
            ),
        })
    return base
