"""Fail-closed, point-in-time evidence for Drift 13M candidates.

The enricher is deliberately read-only.  Every query is bounded by the
candidate's ``created_at`` and every database is opened with SQLite
``mode=ro``.  Missing, partial, stale, or malformed sources remain explicit in
``drift_evidence_availability``; they are never converted to a neutral score.

Multi-minute spot flow is reconstructed only from trailing-60-second snapshots
whose end times are at least 60 seconds apart.  This avoids double-counting the
overlapping rolling windows stored by the spot-depth collector.  A horizon is
``None`` until enough non-overlapping buckets exist for it.
"""
from __future__ import annotations

from contextlib import closing
import json
import math
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence


EVIDENCE_VERSION = "drift-evidence-v1"
CORE_ASSETS = ("DOGE", "HYPE", "SOL", "XRP")
SOURCE_NAMES = (
    "interval_15m",
    "v91",
    "v95_15m",
    "btc_15m",
    "breadth_13m",
    "spot_flow",
)


_SCALAR_DEFAULTS: dict[str, Any] = {
    "drift_evidence_version": EVIDENCE_VERSION,
    "drift_evidence_decision_at": None,
    "drift_evidence_status": "missing",
    "drift_evidence_complete": False,
    "drift_evidence_missing_reasons": [],
    "drift_interval_15m_side": None,
    "drift_interval_15m_ticker": None,
    "drift_interval_15m_captured_at": None,
    "drift_interval_15m_age_seconds": None,
    "drift_v91_yes_fraction_all": None,
    "drift_v91_yes_fraction_directional": None,
    "drift_v91_observation_count": 0,
    "drift_v91_directional_count": 0,
    "drift_v91_yes_count": 0,
    "drift_v91_no_count": 0,
    "drift_v91_uncertain_count": 0,
    "drift_v91_expected_observation_count": None,
    "drift_v91_coverage": None,
    "drift_v91_first_observed_at": None,
    "drift_v91_last_observed_at": None,
    "drift_v91_first_side": None,
    "drift_v91_last_side": None,
    "drift_v91_p_yes_min": None,
    "drift_v91_p_yes_max": None,
    "drift_v91_model_versions": [],
    "drift_v95_15m_side": None,
    "drift_v95_15m_flow_score": None,
    "drift_v95_15m_age_seconds": None,
    "drift_v95_15m_created_at": None,
    "drift_v95_15m_prediction_id": None,
    "drift_v95_15m_model_version": None,
    "drift_v95_15m_feature_schema_version": None,
    "drift_v95_15m_selected_probability": None,
    "drift_btc_15m_side": None,
    "drift_btc_15m_ticker": None,
    "drift_btc_15m_captured_at": None,
    "drift_btc_15m_age_seconds": None,
    "drift_core_breadth": None,
    "drift_asymmetric_breadth": None,
    "drift_core_breadth_fraction": None,
    "drift_asymmetric_breadth_fraction": None,
    "drift_core_breadth_assets": [],
    "drift_asymmetric_breadth_assets": [],
    "drift_breadth_observed_assets": [],
    "drift_breadth_missing_assets": list(CORE_ASSETS),
    "drift_flow_1m": None,
    "drift_flow_3m": None,
    "drift_flow_5m": None,
    "drift_flow_13m": None,
    "drift_flow_positive_bucket_fraction": None,
    "drift_flow_sign_flips": None,
    "drift_flow_persistence": None,
    "drift_flow_coverage": 0.0,
    "drift_flow_bucket_count": 0,
    "drift_flow_bucket_timestamps": [],
    "drift_flow_bucket_values": [],
    "drift_flow_latest_snapshot_at": None,
    "drift_flow_oldest_snapshot_at": None,
    "drift_flow_provider": None,
    "drift_flow_trade_side_semantics": None,
}


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


def _side(value: Any) -> str | None:
    text = str(value or "").upper()
    return text if text in {"YES", "NO"} else None


def _asset(value: Any) -> str:
    return str(value or "").upper()


def _age(decision_at: float, source_at: Any) -> float | None:
    timestamp = _num(source_at)
    if timestamp is None or timestamp > decision_at:
        return None
    return decision_at - timestamp


def _availability(
    *,
    available: bool = False,
    status: str = "missing",
    missing_reason: str | None = "not_evaluated",
    source_at: float | None = None,
    age_seconds: float | None = None,
    **details: Any,
) -> dict[str, Any]:
    out = {
        "available": bool(available),
        "status": str(status),
        "missing_reason": missing_reason,
        "source_at": source_at,
        "age_seconds": age_seconds,
    }
    out.update(details)
    return out


def _base_output(row: Mapping[str, Any] | None) -> dict[str, Any]:
    try:
        out = dict(row or {})
    except Exception:
        out = {}
    for key, value in _SCALAR_DEFAULTS.items():
        # Lists must not be shared across calls.
        out[key] = list(value) if isinstance(value, list) else value
    out["drift_evidence_availability"] = {
        name: _availability() for name in SOURCE_NAMES
    }
    out["drift_evidence_ages"] = {name: None for name in SOURCE_NAMES}
    return out


def _mark(
    out: dict[str, Any],
    source: str,
    *,
    available: bool,
    status: str,
    missing_reason: str | None,
    source_at: float | None = None,
    age_seconds: float | None = None,
    **details: Any,
) -> None:
    out["drift_evidence_availability"][source] = _availability(
        available=available,
        status=status,
        missing_reason=missing_reason,
        source_at=source_at,
        age_seconds=age_seconds,
        **details,
    )
    out["drift_evidence_ages"][source] = age_seconds


def _path(env_name: str, default: str) -> Path:
    return Path(os.environ.get(env_name) or default).expanduser()


def _connect_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _error_reason(prefix: str, exc: Exception) -> str:
    return f"{prefix}_query_error:{type(exc).__name__}"


def _model_version(row: Mapping[str, Any]) -> str | None:
    value = row.get("model_version") or row.get("source_model_version")
    return str(value) if value else None


def _interval_capture(
    connection: sqlite3.Connection,
    *,
    asset: str,
    window_key: int,
    decision_at: float,
    ticker: str | None,
    model_version: str | None,
    interval: str,
) -> dict[str, Any] | None:
    clauses = [
        "asset=?",
        "interval=?",
        "window_key=?",
        "captured_at IS NOT NULL",
        "captured_at<=?",
    ]
    params: list[Any] = [asset, interval, int(window_key), decision_at]
    if ticker:
        clauses.append("ticker=?")
        params.append(ticker)
    if model_version:
        clauses.append("model_version=?")
        params.append(model_version)
    found = connection.execute(
        "SELECT * FROM interval_captures WHERE "
        + " AND ".join(clauses)
        + " ORDER BY captured_at DESC, id DESC LIMIT 1",
        tuple(params),
    ).fetchone()
    return dict(found) if found is not None else None


def _load_interval_15m(
    out: dict[str, Any], row: Mapping[str, Any], decision_at: float
) -> dict[str, Any] | None:
    asset = _asset(row.get("asset"))
    ticker = str(row.get("ticker") or "") or None
    window_raw = row.get("window_key")
    try:
        window_key = int(window_raw)
    except (TypeError, ValueError):
        _mark(
            out,
            "interval_15m",
            available=False,
            status="invalid",
            missing_reason="missing_window_key",
        )
        return None
    path = _path("Q15_INTERVAL_RESEARCH_DB", "data/q15_interval_research_v1.sqlite3")
    try:
        with closing(_connect_read_only(path)) as connection:
            capture = _interval_capture(
                connection,
                asset=asset,
                window_key=window_key,
                decision_at=decision_at,
                ticker=ticker,
                model_version=_model_version(row),
                interval="15M",
            )
    except FileNotFoundError:
        _mark(
            out,
            "interval_15m",
            available=False,
            status="missing",
            missing_reason="interval_db_missing",
        )
        return None
    except Exception as exc:
        _mark(
            out,
            "interval_15m",
            available=False,
            status="error",
            missing_reason=_error_reason("interval_15m", exc),
        )
        return None
    if capture is None:
        _mark(
            out,
            "interval_15m",
            available=False,
            status="missing",
            missing_reason="same_asset_window_15m_capture_missing",
        )
        return None

    captured_at = _num(capture.get("captured_at"))
    age = _age(decision_at, captured_at)
    side = _side(capture.get("predicted_side"))
    out.update(
        {
            "drift_interval_15m_side": side,
            "drift_interval_15m_ticker": capture.get("ticker"),
            "drift_interval_15m_captured_at": captured_at,
            "drift_interval_15m_age_seconds": age,
        }
    )
    if captured_at is None or age is None:
        status, reason, available = "invalid", "invalid_15m_capture_timestamp", False
    elif age > _float_env("Q15_DRIFT_EVIDENCE_INTERVAL_MAX_AGE_SECONDS", 180.0, 1.0):
        status, reason, available = "stale", "interval_15m_capture_stale", False
    elif side is None:
        status, reason, available = "partial", "15m_prediction_side_missing", False
    else:
        status, reason, available = "ok", None, True
    _mark(
        out,
        "interval_15m",
        available=available,
        status=status,
        missing_reason=reason,
        source_at=captured_at,
        age_seconds=age,
        ticker=capture.get("ticker"),
        asset=asset,
        window_key=window_key,
        side=side,
        model_version=capture.get("model_version"),
    )
    return capture


def _load_v91(
    out: dict[str, Any],
    row: Mapping[str, Any],
    decision_at: float,
    capture_15m: Mapping[str, Any] | None,
) -> None:
    start_at = _num((capture_15m or {}).get("captured_at"))
    ticker = str((capture_15m or {}).get("ticker") or row.get("ticker") or "")
    asset = _asset(row.get("asset"))
    if start_at is None or not ticker:
        _mark(
            out,
            "v91",
            available=False,
            status="missing",
            missing_reason="interval_15m_capture_unavailable",
        )
        return
    path = _path("Q15_V91_SQLITE_PATH", "q15_v91_state.sqlite3")
    try:
        with closing(_connect_read_only(path)) as connection:
            rows = connection.execute(
                "SELECT observed_at,side,p_yes,bucket,model_version "
                "FROM q15_v91_observations WHERE contract_key=? AND asset=? "
                "AND observed_at>=? AND observed_at<=? ORDER BY observed_at ASC",
                (f"{ticker}|15M", asset, start_at, decision_at),
            ).fetchall()
    except FileNotFoundError:
        _mark(
            out,
            "v91",
            available=False,
            status="missing",
            missing_reason="v91_db_missing",
        )
        return
    except Exception as exc:
        _mark(
            out,
            "v91",
            available=False,
            status="error",
            missing_reason=_error_reason("v91", exc),
        )
        return

    bucket_seconds = _float_env(
        "Q15_DRIFT_EVIDENCE_V91_BUCKET_SECONDS",
        _float_env("Q15_V91_OBSERVATION_BUCKET_SECONDS", 12.0, 1.0),
        1.0,
    )
    # Only one observation per bucket is evidence.  If an old process wrote a
    # duplicate, retain the latest value that was already present by cutoff.
    by_bucket: dict[int, sqlite3.Row] = {}
    for observed in rows:
        observed_at = _num(observed["observed_at"])
        if observed_at is None or observed_at < start_at or observed_at > decision_at:
            continue
        bucket_value = observed["bucket"]
        try:
            bucket = int(bucket_value)
        except (TypeError, ValueError):
            bucket = int(math.floor(observed_at / bucket_seconds))
        by_bucket[bucket] = observed
    observations = sorted(by_bucket.values(), key=lambda item: float(item["observed_at"]))
    if not observations:
        _mark(
            out,
            "v91",
            available=False,
            status="missing",
            missing_reason="v91_observations_missing_in_15m_to_13m_window",
        )
        return

    sides = [str(item["side"] or "").upper() for item in observations]
    yes = sides.count("YES")
    no = sides.count("NO")
    total = len(sides)
    directional = yes + no
    uncertain = total - directional
    start_bucket = int(math.floor(start_at / bucket_seconds))
    end_bucket = int(math.floor(decision_at / bucket_seconds))
    expected = max(1, end_bucket - start_bucket + 1)
    coverage = min(1.0, total / expected)
    first_at = _num(observations[0]["observed_at"])
    last_at = _num(observations[-1]["observed_at"])
    p_values = [
        value
        for value in (_num(item["p_yes"]) for item in observations)
        if value is not None
    ]
    model_versions = sorted(
        {str(item["model_version"]) for item in observations if item["model_version"]}
    )
    out.update(
        {
            "drift_v91_yes_fraction_all": yes / total,
            "drift_v91_yes_fraction_directional": (
                yes / directional if directional else None
            ),
            "drift_v91_observation_count": total,
            "drift_v91_directional_count": directional,
            "drift_v91_yes_count": yes,
            "drift_v91_no_count": no,
            "drift_v91_uncertain_count": uncertain,
            "drift_v91_expected_observation_count": expected,
            "drift_v91_coverage": coverage,
            "drift_v91_first_observed_at": first_at,
            "drift_v91_last_observed_at": last_at,
            "drift_v91_first_side": sides[0],
            "drift_v91_last_side": sides[-1],
            "drift_v91_p_yes_min": min(p_values) if p_values else None,
            "drift_v91_p_yes_max": max(p_values) if p_values else None,
            "drift_v91_model_versions": model_versions,
        }
    )
    age = _age(decision_at, last_at)
    minimum = _int_env("Q15_DRIFT_EVIDENCE_V91_MIN_OBSERVATIONS", 3)
    if age is None or age > _float_env(
        "Q15_DRIFT_EVIDENCE_V91_MAX_AGE_SECONDS", 60.0, 1.0
    ):
        available, status, reason = False, "stale", "v91_observations_stale"
    elif directional <= 0:
        available, status, reason = False, "partial", "v91_directional_observations_missing"
    elif total < minimum:
        available, status, reason = (
            False,
            "partial",
            f"v91_observations_below_minimum:{total}/{minimum}",
        )
    else:
        available, status, reason = True, "ok", None
    _mark(
        out,
        "v91",
        available=available,
        status=status,
        missing_reason=reason,
        source_at=last_at,
        age_seconds=age,
        window_start_at=start_at,
        observation_count=total,
        directional_count=directional,
        expected_observation_count=expected,
        coverage=coverage,
        model_versions=model_versions,
    )


def _load_v95(out: dict[str, Any], row: Mapping[str, Any], decision_at: float) -> None:
    ticker = str(row.get("ticker") or "")
    asset = _asset(row.get("asset"))
    if not ticker or not asset:
        _mark(
            out,
            "v95_15m",
            available=False,
            status="invalid",
            missing_reason="missing_ticker_or_asset",
        )
        return
    path = _path("Q15_V95_LEDGER_DB", "data/q15_v95_ledger_v1.sqlite3")
    try:
        with closing(_connect_read_only(path)) as connection:
            found = connection.execute(
                "SELECT * FROM predictions WHERE ticker=? AND asset=? "
                "AND checkpoint='15M' AND created_at<=? "
                "ORDER BY created_at DESC, prediction_id DESC LIMIT 1",
                (ticker, asset, decision_at),
            ).fetchone()
    except FileNotFoundError:
        _mark(
            out,
            "v95_15m",
            available=False,
            status="missing",
            missing_reason="v95_db_missing",
        )
        return
    except Exception as exc:
        _mark(
            out,
            "v95_15m",
            available=False,
            status="error",
            missing_reason=_error_reason("v95_15m", exc),
        )
        return
    if found is None:
        _mark(
            out,
            "v95_15m",
            available=False,
            status="missing",
            missing_reason="exact_ticker_v95_15m_prediction_missing",
        )
        return

    prediction = dict(found)
    created_at = _num(prediction.get("created_at"))
    age = _age(decision_at, created_at)
    side = _side(prediction.get("predicted_side"))
    features: Mapping[str, Any] = {}
    feature_error = False
    try:
        loaded = json.loads(str(prediction.get("feature_json") or "{}"))
        if isinstance(loaded, Mapping):
            features = loaded
        else:
            feature_error = True
    except (TypeError, ValueError, json.JSONDecodeError):
        feature_error = True
    flow = _num(features.get("flow"))
    out.update(
        {
            "drift_v95_15m_side": side,
            "drift_v95_15m_flow_score": flow,
            "drift_v95_15m_age_seconds": age,
            "drift_v95_15m_created_at": created_at,
            "drift_v95_15m_prediction_id": prediction.get("prediction_id"),
            "drift_v95_15m_model_version": prediction.get("model_version"),
            "drift_v95_15m_feature_schema_version": prediction.get(
                "feature_schema_version"
            ),
            "drift_v95_15m_selected_probability": _num(
                prediction.get("selected_probability")
            ),
        }
    )
    if created_at is None or age is None:
        available, status, reason = False, "invalid", "invalid_v95_created_at"
    elif age > _float_env("Q15_DRIFT_EVIDENCE_V95_MAX_AGE_SECONDS", 180.0, 1.0):
        available, status, reason = False, "stale", "v95_15m_prediction_stale"
    elif side is None:
        available, status, reason = False, "partial", "v95_15m_side_missing"
    elif feature_error:
        available, status, reason = False, "partial", "v95_feature_json_invalid"
    elif flow is None:
        available, status, reason = False, "partial", "v95_flow_feature_missing"
    else:
        available, status, reason = True, "ok", None
    _mark(
        out,
        "v95_15m",
        available=available,
        status=status,
        missing_reason=reason,
        source_at=created_at,
        age_seconds=age,
        prediction_id=prediction.get("prediction_id"),
        ticker=ticker,
        side=side,
        flow_available=flow is not None,
        model_version=prediction.get("model_version"),
        feature_schema_version=prediction.get("feature_schema_version"),
    )


def _load_btc_15m(
    out: dict[str, Any], row: Mapping[str, Any], decision_at: float
) -> None:
    try:
        window_key = int(row.get("window_key"))
    except (TypeError, ValueError):
        _mark(
            out,
            "btc_15m",
            available=False,
            status="invalid",
            missing_reason="missing_window_key",
        )
        return
    path = _path("Q15_INTERVAL_RESEARCH_DB", "data/q15_interval_research_v1.sqlite3")
    try:
        with closing(_connect_read_only(path)) as connection:
            capture = _interval_capture(
                connection,
                asset="BTC",
                window_key=window_key,
                decision_at=decision_at,
                ticker=None,
                model_version=_model_version(row),
                interval="15M",
            )
    except FileNotFoundError:
        _mark(
            out,
            "btc_15m",
            available=False,
            status="missing",
            missing_reason="interval_db_missing",
        )
        return
    except Exception as exc:
        _mark(
            out,
            "btc_15m",
            available=False,
            status="error",
            missing_reason=_error_reason("btc_15m", exc),
        )
        return
    if capture is None:
        _mark(
            out,
            "btc_15m",
            available=False,
            status="missing",
            missing_reason="same_window_btc_15m_capture_missing",
        )
        return
    captured_at = _num(capture.get("captured_at"))
    age = _age(decision_at, captured_at)
    side = _side(capture.get("predicted_side"))
    out.update(
        {
            "drift_btc_15m_side": side,
            "drift_btc_15m_ticker": capture.get("ticker"),
            "drift_btc_15m_captured_at": captured_at,
            "drift_btc_15m_age_seconds": age,
        }
    )
    stale = age is None or age > _float_env(
        "Q15_DRIFT_EVIDENCE_BTC_MAX_AGE_SECONDS", 180.0, 1.0
    )
    available = side is not None and not stale
    _mark(
        out,
        "btc_15m",
        available=available,
        status="ok" if available else ("stale" if stale else "partial"),
        missing_reason=(
            None
            if available
            else ("btc_15m_capture_stale" if stale else "btc_15m_side_or_timestamp_missing")
        ),
        source_at=captured_at,
        age_seconds=age,
        ticker=capture.get("ticker"),
        window_key=window_key,
        side=side,
        model_version=capture.get("model_version"),
    )


def _qualifies_core(capture: Mapping[str, Any]) -> bool:
    ask = _num(capture.get("yes_ask_cents"))
    if ask is None:
        ask = _num(capture.get("entry_ask_cents"))
    distance = _num(capture.get("distance_from_strike"))
    flip = _num(capture.get("flip_probability"))
    return bool(
        _side(capture.get("predicted_side")) == "YES"
        and ask is not None
        and 60.0 <= ask <= 73.0
        and distance is not None
        and distance <= 3e-5
        and flip is not None
        and flip <= 30.0
    )


def _qualifies_asymmetric(capture: Mapping[str, Any]) -> bool:
    ask = _num(capture.get("yes_ask_cents"))
    if ask is None:
        ask = _num(capture.get("entry_ask_cents"))
    distance = _num(capture.get("distance_from_strike"))
    flip = _num(capture.get("flip_probability"))
    if (
        _side(capture.get("predicted_side")) != "YES"
        or ask is None
        or distance is None
        or flip is None
        or flip > 30.0
    ):
        return False
    return bool(
        (60.0 <= ask <= 64.0 and distance <= 3e-5)
        or (65.0 <= ask <= 73.0 and distance <= 1e-4)
    )


def _load_breadth(
    out: dict[str, Any], row: Mapping[str, Any], decision_at: float
) -> None:
    try:
        window_key = int(row.get("window_key"))
    except (TypeError, ValueError):
        _mark(
            out,
            "breadth_13m",
            available=False,
            status="invalid",
            missing_reason="missing_window_key",
        )
        return
    path = _path("Q15_INTERVAL_RESEARCH_DB", "data/q15_interval_research_v1.sqlite3")
    clauses = [
        "interval='13M'",
        "window_key=?",
        "asset IN ('DOGE','HYPE','SOL','XRP')",
        "captured_at IS NOT NULL",
        "captured_at<=?",
    ]
    params: list[Any] = [window_key, decision_at]
    model_version = _model_version(row)
    if model_version:
        clauses.append("model_version=?")
        params.append(model_version)
    try:
        with closing(_connect_read_only(path)) as connection:
            rows = connection.execute(
                "SELECT * FROM interval_captures WHERE "
                + " AND ".join(clauses)
                + " ORDER BY asset ASC, captured_at DESC, id DESC",
                tuple(params),
            ).fetchall()
    except FileNotFoundError:
        _mark(
            out,
            "breadth_13m",
            available=False,
            status="missing",
            missing_reason="interval_db_missing",
        )
        return
    except Exception as exc:
        _mark(
            out,
            "breadth_13m",
            available=False,
            status="error",
            missing_reason=_error_reason("breadth_13m", exc),
        )
        return

    by_asset: dict[str, dict[str, Any]] = {}
    for found in rows:
        capture = dict(found)
        asset = _asset(capture.get("asset"))
        if asset in CORE_ASSETS and asset not in by_asset:
            by_asset[asset] = capture
    if not by_asset:
        _mark(
            out,
            "breadth_13m",
            available=False,
            status="missing",
            missing_reason="same_window_core_13m_captures_missing",
        )
        return

    observed = sorted(by_asset)
    missing = sorted(set(CORE_ASSETS) - set(observed))
    core_assets = sorted(asset for asset, capture in by_asset.items() if _qualifies_core(capture))
    asymmetric_assets = sorted(
        asset for asset, capture in by_asset.items() if _qualifies_asymmetric(capture)
    )
    source_times = [
        value
        for value in (_num(capture.get("captured_at")) for capture in by_asset.values())
        if value is not None
    ]
    oldest_at = min(source_times) if source_times else None
    latest_at = max(source_times) if source_times else None
    age = _age(decision_at, oldest_at)
    out.update(
        {
            "drift_core_breadth": len(core_assets),
            "drift_asymmetric_breadth": len(asymmetric_assets),
            "drift_core_breadth_fraction": (
                len(core_assets) / len(CORE_ASSETS) if not missing else None
            ),
            "drift_asymmetric_breadth_fraction": (
                len(asymmetric_assets) / len(CORE_ASSETS) if not missing else None
            ),
            "drift_core_breadth_assets": core_assets,
            "drift_asymmetric_breadth_assets": asymmetric_assets,
            "drift_breadth_observed_assets": observed,
            "drift_breadth_missing_assets": missing,
        }
    )
    complete = not missing and len(observed) == len(CORE_ASSETS)
    stale = age is None or age > _float_env(
        "Q15_DRIFT_EVIDENCE_BREADTH_MAX_AGE_SECONDS", 15.0, 1.0
    )
    available = complete and not stale
    _mark(
        out,
        "breadth_13m",
        available=available,
        status="ok" if available else ("stale" if complete and stale else "partial"),
        missing_reason=(
            None
            if available
            else (
                "breadth_13m_captures_stale"
                if complete and stale
                else "breadth_assets_missing:" + ",".join(missing)
            )
        ),
        source_at=oldest_at,
        age_seconds=age,
        latest_source_at=latest_at,
        window_key=window_key,
        observed_assets=observed,
        missing_assets=missing,
        expected_asset_count=len(CORE_ASSETS),
        observed_asset_count=len(observed),
        core_qualifying_assets=core_assets,
        asymmetric_qualifying_assets=asymmetric_assets,
    )


def _flow_value(snapshot: Mapping[str, Any]) -> tuple[float | None, str | None, str | None]:
    provider = str(snapshot.get("provider") or "").lower() or None
    semantics = str(snapshot.get("trade_side_semantics") or "").lower() or None
    buy = _num(snapshot.get("trade_buy_notional_60s"))
    sell = _num(snapshot.get("trade_sell_notional_60s"))
    if buy is not None and sell is not None:
        value = buy - sell
    else:
        value = _num(snapshot.get("trade_net_notional_60s"))
    if value is None:
        return None, provider, semantics
    # Old Coinbase rows used maker-side labels.  Match the established spot
    # enricher by converting them to aggressor-side direction.
    if provider == "coinbase" and semantics != "aggressor":
        value = -value
        semantics = "aggressor"
    return value, provider, semantics


def _sign(value: float) -> int:
    if value > 0.0:
        return 1
    if value < 0.0:
        return -1
    return 0


def _persistence(values_chronological: Sequence[float]) -> tuple[int, float]:
    signs = [_sign(value) for value in values_chronological if _sign(value) != 0]
    if not signs:
        return 0, 0.0
    flips = sum(1 for left, right in zip(signs, signs[1:]) if left != right)
    longest = 1
    run = 1
    for left, right in zip(signs, signs[1:]):
        if left == right:
            run += 1
            longest = max(longest, run)
        else:
            run = 1
    return flips, longest / len(signs)


def _load_spot_flow(
    out: dict[str, Any], row: Mapping[str, Any], decision_at: float
) -> None:
    asset = _asset(row.get("asset"))
    if not asset:
        _mark(
            out,
            "spot_flow",
            available=False,
            status="invalid",
            missing_reason="missing_asset",
        )
        return
    path = _path("Q15_SPOT_DEPTH_DB", "data/q15_spot_depth_v1.sqlite3")
    bucket_count = 13
    bucket_seconds = 60.0
    boundary_lag_max = _float_env(
        "Q15_DRIFT_EVIDENCE_FLOW_BUCKET_MAX_LAG_SECONDS", 15.0, 0.0
    )
    effective_trade_age_max = _float_env(
        "Q15_DRIFT_EVIDENCE_FLOW_TRADE_MAX_AGE_SECONDS", 15.0, 0.0
    )
    lower_bound = decision_at - bucket_count * bucket_seconds - boundary_lag_max
    try:
        with closing(_connect_read_only(path)) as connection:
            rows = connection.execute(
                "SELECT * FROM spot_depth_snapshots WHERE asset=? "
                "AND created_at>=? AND created_at<=? ORDER BY created_at DESC, id DESC",
                (asset, lower_bound, decision_at),
            ).fetchall()
    except FileNotFoundError:
        _mark(
            out,
            "spot_flow",
            available=False,
            status="missing",
            missing_reason="spot_depth_db_missing",
        )
        return
    except Exception as exc:
        _mark(
            out,
            "spot_flow",
            available=False,
            status="error",
            missing_reason=_error_reason("spot_flow", exc),
        )
        return

    snapshots = [dict(found) for found in rows]
    selected: list[dict[str, Any]] = []
    newer_at: float | None = None
    identity: tuple[str, str, str] | None = None
    for index in range(bucket_count):
        target_at = decision_at - index * bucket_seconds
        minimum_at = target_at - boundary_lag_max
        chosen: dict[str, Any] | None = None
        for snapshot in snapshots:
            created_at = _num(snapshot.get("created_at"))
            if created_at is None or created_at > target_at or created_at < minimum_at:
                continue
            if newer_at is not None and newer_at - created_at < bucket_seconds:
                continue
            flow, provider, semantics = _flow_value(snapshot)
            raw_trade_age = _num(snapshot.get("trade_age_seconds"))
            if flow is None or raw_trade_age is None:
                continue
            raw_trade_age = max(0.0, raw_trade_age)
            boundary_age = target_at - created_at
            if boundary_age + raw_trade_age > effective_trade_age_max:
                continue
            candidate_identity = (
                str(provider or ""),
                str(snapshot.get("symbol") or snapshot.get("source") or ""),
                str(semantics or ""),
            )
            if identity is not None and candidate_identity != identity:
                continue
            chosen = {
                "created_at": created_at,
                "flow": flow,
                "provider": provider,
                "semantics": semantics,
                "raw_trade_age": raw_trade_age,
                "target_at": target_at,
                "effective_age": boundary_age + raw_trade_age,
                "identity": candidate_identity,
            }
            break
        if chosen is None:
            break
        if identity is None:
            identity = chosen["identity"]
        selected.append(chosen)
        newer_at = float(chosen["created_at"])

    if not selected:
        _mark(
            out,
            "spot_flow",
            available=False,
            status="missing",
            missing_reason="nonoverlapping_fresh_60s_flow_buckets_missing",
        )
        return

    values = [float(item["flow"]) for item in selected]  # newest -> oldest
    timestamps = [float(item["created_at"]) for item in selected]
    count = len(values)
    chronological = list(reversed(values))
    flips, persistence = _persistence(chronological)
    out.update(
        {
            "drift_flow_1m": sum(values[:1]) if count >= 1 else None,
            "drift_flow_3m": sum(values[:3]) if count >= 3 else None,
            "drift_flow_5m": sum(values[:5]) if count >= 5 else None,
            "drift_flow_13m": sum(values[:13]) if count >= 13 else None,
            "drift_flow_positive_bucket_fraction": (
                sum(1 for value in values if value > 0.0) / count
            ),
            "drift_flow_sign_flips": flips,
            "drift_flow_persistence": persistence,
            "drift_flow_coverage": count / bucket_count,
            "drift_flow_bucket_count": count,
            "drift_flow_bucket_timestamps": timestamps,
            "drift_flow_bucket_values": values,
            "drift_flow_latest_snapshot_at": timestamps[0],
            "drift_flow_oldest_snapshot_at": timestamps[-1],
            "drift_flow_provider": selected[0]["provider"],
            "drift_flow_trade_side_semantics": selected[0]["semantics"],
        }
    )
    latest_effective_age = decision_at - timestamps[0] + float(
        selected[0]["raw_trade_age"]
    )
    complete = count == bucket_count
    _mark(
        out,
        "spot_flow",
        # ``available`` is deliberately the fail-closed, full-source flag.  A
        # partial chain may still expose its supported scalar horizons, but a
        # caller cannot mistake it for complete 13-minute evidence.
        available=complete,
        status="ok" if complete else "partial",
        missing_reason=(
            None
            if complete
            else f"insufficient_nonoverlapping_flow_buckets:{count}/{bucket_count}"
        ),
        source_at=timestamps[0],
        age_seconds=latest_effective_age,
        oldest_source_at=timestamps[-1],
        bucket_count=count,
        expected_bucket_count=bucket_count,
        coverage=count / bucket_count,
        provider=selected[0]["provider"],
        trade_side_semantics=selected[0]["semantics"],
        nonoverlapping=True,
        horizons_available={
            "1m": count >= 1,
            "3m": count >= 3,
            "5m": count >= 5,
            "13m": count >= 13,
        },
    )


def _fail_all(out: dict[str, Any], reason: str, status: str = "invalid") -> None:
    for source in SOURCE_NAMES:
        _mark(
            out,
            source,
            available=False,
            status=status,
            missing_reason=reason,
        )


def _finalize(out: dict[str, Any]) -> dict[str, Any]:
    sources = out["drift_evidence_availability"]
    complete = all(sources[name].get("status") == "ok" for name in SOURCE_NAMES)
    any_available = any(bool(sources[name].get("available")) for name in SOURCE_NAMES)
    reasons = [
        f"{name}:{sources[name].get('missing_reason') or sources[name].get('status')}"
        for name in SOURCE_NAMES
        if sources[name].get("status") != "ok"
    ]
    out["drift_evidence_complete"] = complete
    out["drift_evidence_status"] = "ok" if complete else ("partial" if any_available else "missing")
    out["drift_evidence_missing_reasons"] = reasons
    return out


def enrich_drift_evidence(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``row`` plus immutable point-in-time Drift evidence.

    No exception escapes this function.  A source failure leaves its scalar
    values at fail-closed defaults and records the error type in
    ``drift_evidence_availability``.
    """
    out = _base_output(row)
    decision_at = _num(out.get("created_at"))
    asset = _asset(out.get("asset"))
    if decision_at is None or not asset:
        _fail_all(out, "missing_decision_timestamp_or_asset")
        return _finalize(out)
    out["drift_evidence_decision_at"] = decision_at

    capture_15m: dict[str, Any] | None = None
    try:
        capture_15m = _load_interval_15m(out, out, decision_at)
    except Exception as exc:  # final guard: enrichment must never break delivery
        _mark(
            out,
            "interval_15m",
            available=False,
            status="error",
            missing_reason=_error_reason("interval_15m", exc),
        )
    for source, loader, args in (
        ("v91", _load_v91, (out, out, decision_at, capture_15m)),
        ("v95_15m", _load_v95, (out, out, decision_at)),
        ("btc_15m", _load_btc_15m, (out, out, decision_at)),
        ("breadth_13m", _load_breadth, (out, out, decision_at)),
        ("spot_flow", _load_spot_flow, (out, out, decision_at)),
    ):
        try:
            loader(*args)
        except Exception as exc:  # pragma: no cover - defense beyond source guards
            _mark(
                out,
                source,
                available=False,
                status="error",
                missing_reason=_error_reason(source, exc),
            )
    return _finalize(out)


__all__ = [
    "CORE_ASSETS",
    "EVIDENCE_VERSION",
    "SOURCE_NAMES",
    "enrich_drift_evidence",
]
