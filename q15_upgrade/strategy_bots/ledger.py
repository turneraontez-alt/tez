"""SQLite persistence for asset-specific strategy-bot decisions."""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .rules import (
    ACCEPTED,
    BOT_BNB_NO,
    BOT_BNB_YES_REVERSAL,
    BOT_CONFIDENCE_TIER,
    BOT_BASELINE,
    BOT_DRIFT_13M,
    BOT_DRIFT_ACCURACY_V91,
    BOT_DRIFT_ASYMMETRIC_VOLUME,
    BOT_DRIFT_BALANCED_V95,
    BOT_DRIFT_CONSENSUS_FALLBACK,
    BOT_DRIFT_FLOW_SPREAD,
    BOT_DRIFT_FLOW_SPREAD_SHADOW_FLOW15,
    BOT_DRIFT_FLOW_SPREAD_SHADOW_SPREAD4,
    BOT_DRIFT_ADDON,
    BOT_DRIFT_LATEQUAL,
    BOT_DRIFT_NO_EXPANSION,
    BOT_DRIFT_NO_MIRROR,
    BOT_THIRTEEN_M_SNIPER,
    DRIFT_REVIEW_BARS,
    DRIFT_CORE_RULE_VERSION,
    BTC_REGIME_KEYS,
    COINBASE_L2_KEYS,
    KALSHI_DEPTH_KEYS,
    KALSHI_FLOW_KEYS,
    KRAKEN_L3_KEYS,
    REJECTED,
    RESEARCH_ONLY,
    SPOT_DEPTH_KEYS,
    STRATEGY_VERSION,
    TIER_A,
    TIER_B,
    TIER_C,
    BotDecision,
    source_rule,
    source_side,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_bot_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    bot_name TEXT NOT NULL,
    tier TEXT,
    strategy_version TEXT NOT NULL,
    decision_status TEXT NOT NULL,
    decision_mode TEXT NOT NULL DEFAULT 'PAPER_RESEARCH',
    paper_only INTEGER NOT NULL DEFAULT 1,
    reason_codes TEXT,
    reason_json TEXT,
    threshold_json TEXT,
    source_system TEXT NOT NULL,
    source_model_version TEXT,
    source_rule TEXT,
    source_rule_name TEXT,
    source_reason_codes TEXT,
    build_sha TEXT,
    config_hash TEXT,
    source_build_sha TEXT,
    source_config_hash TEXT,
    feature_schema_version TEXT,
    source_features_version TEXT,
    feature_cohort TEXT,
    feature_availability_json TEXT,
    feature_age_json TEXT,
    evidence_grade TEXT,
    evidence_reason_codes TEXT,
    drift_candidate_lane TEXT,
    data_complete INTEGER,
    full_feature_complete INTEGER,
    source_created_at REAL,
    source_captured_at REAL,
    evidence_as_of REAL,
    drift_evidence_json TEXT,
    drift_v91_yes_fraction_all REAL,
    drift_v91_yes_fraction_directional REAL,
    drift_v91_observation_count INTEGER,
    drift_v95_15m_side TEXT,
    drift_v95_15m_flow_score REAL,
    drift_btc_15m_side TEXT,
    drift_core_breadth INTEGER,
    drift_asymmetric_breadth INTEGER,
    drift_flow_1m REAL,
    drift_flow_3m REAL,
    drift_flow_5m REAL,
    drift_flow_13m REAL,
    drift_flow_positive_bucket_fraction REAL,
    drift_flow_sign_flips INTEGER,
    drift_flow_coverage REAL,
    record_kind TEXT,
    delivery_status TEXT,
    asset TEXT,
    side TEXT,
    original_source_side TEXT,
    interval TEXT,
    window_key INTEGER,
    ticker TEXT,
    close_time REAL,
    entry_ask_cents REAL,
    spread_cents REAL,
    depth_contracts REAL,
    yes_bid_depth_contracts REAL,
    yes_ask_depth_contracts REAL,
    no_bid_depth_contracts REAL,
    no_ask_depth_contracts REAL,
    kalshi_depth_status TEXT,
    kalshi_depth_missing_reason TEXT,
    kalshi_depth_retry_used INTEGER,
    kalshi_taker_yes_volume_15s REAL,
    kalshi_taker_no_volume_15s REAL,
    kalshi_taker_net_yes_volume_15s REAL,
    spot_depth_status TEXT,
    spot_depth_missing_reason TEXT,
    spot_depth_source TEXT,
    spot_depth_age_seconds REAL,
    spot_depth_trade_age_seconds REAL,
    spot_depth_best_bid REAL,
    spot_depth_best_ask REAL,
    spot_depth_mid REAL,
    spot_depth_spread_bps REAL,
    spot_depth_bid_depth_top REAL,
    spot_depth_ask_depth_top REAL,
    spot_depth_bid_depth_levels REAL,
    spot_depth_ask_depth_levels REAL,
    spot_depth_bid_notional_levels REAL,
    spot_depth_ask_notional_levels REAL,
    spot_depth_imbalance REAL,
    spot_depth_trade_buy_qty_5s REAL,
    spot_depth_trade_sell_qty_5s REAL,
    spot_depth_trade_net_qty_5s REAL,
    spot_depth_trade_buy_notional_5s REAL,
    spot_depth_trade_sell_notional_5s REAL,
    spot_depth_trade_net_notional_5s REAL,
    spot_depth_trade_buy_qty_15s REAL,
    spot_depth_trade_sell_qty_15s REAL,
    spot_depth_trade_net_qty_15s REAL,
    spot_depth_trade_buy_notional_15s REAL,
    spot_depth_trade_sell_notional_15s REAL,
    spot_depth_trade_net_notional_15s REAL,
    spot_depth_trade_buy_qty_60s REAL,
    spot_depth_trade_sell_qty_60s REAL,
    spot_depth_trade_net_qty_60s REAL,
    spot_depth_trade_buy_notional_60s REAL,
    spot_depth_trade_sell_notional_60s REAL,
    spot_depth_trade_net_notional_60s REAL,
    spot_depth_last_trade_price REAL,
    spot_depth_last_trade_side TEXT,
    spot_depth_last_trade_size REAL,
    btc_context_json TEXT,
    btc_ticker TEXT,
    btc_depth_contracts REAL,
    btc_book_pressure_cents REAL,
    btc_dominant_side TEXT,
    btc_model_predicted_side TEXT,
    btc_model_yes_probability REAL,
    btc_calibrated_yes_probability REAL,
    btc_market_implied_yes_probability REAL,
    btc_regime TEXT,
    btc_regime_agreement TEXT,
    btc_regime_vote_yes REAL,
    btc_regime_vote_no REAL,
    btc_regime_vote_detail TEXT,
    btc_regime_status TEXT,
    btc_regime_missing_reason TEXT,
    btc_spot_age_seconds REAL,
    btc_spot_depth_imbalance REAL,
    btc_spot_trade_net_notional_15s REAL,
    btc_spot_trade_net_notional_60s REAL,
    btc_coinbase_l2_age_seconds REAL,
    btc_coinbase_l2_last_message_age_seconds REAL,
    btc_coinbase_l2_top_12_bid_notional REAL,
    btc_coinbase_l2_top_12_ask_notional REAL,
    btc_coinbase_l2_top_12_imbalance_notional REAL,
    btc_coinbase_l2_top_60_bid_notional REAL,
    btc_coinbase_l2_top_60_ask_notional REAL,
    btc_coinbase_l2_top_60_imbalance_notional REAL,
    btc_coinbase_l2_top_250_bid_notional REAL,
    btc_coinbase_l2_top_250_ask_notional REAL,
    btc_coinbase_l2_top_250_imbalance_notional REAL,
    btc_kalshi_book_imbalance REAL,
    btc_kalshi_taker_net_yes_volume_15s REAL,
    btc_v95_age_seconds REAL,
    btc_v95_checkpoint TEXT,
    btc_v95_grade TEXT,
    btc_v95_predicted_side TEXT,
    btc_v95_selected_probability REAL,
    btc_kraken_l3_age_seconds REAL,
    btc_kraken_l3_depth_imbalance REAL,
    btc_kraken_l3_cancel_to_add_60s REAL,
    btc_kraken_l3_net_matched_buy_notional_60s REAL,
    notification_status TEXT,
    notification_message_id INTEGER,
    notification_error TEXT,
    notified_at REAL,
    official_result TEXT,
    resolved_at REAL,
    correct INTEGER,
    hypothetical_pnl_cents REAL,
    UNIQUE(
        strategy_version, bot_name, source_system, source_model_version,
        ticker, interval, window_key, source_rule
    )
);
CREATE INDEX IF NOT EXISTS idx_strategy_bot_resolve
    ON strategy_bot_decisions(source_system, source_model_version, ticker, official_result);
CREATE INDEX IF NOT EXISTS idx_strategy_bot_score
    ON strategy_bot_decisions(strategy_version, bot_name, decision_status, asset, side);

CREATE TABLE IF NOT EXISTS strategy_bot_meta (
    meta_key TEXT PRIMARY KEY,
    claimed_at REAL NOT NULL
);
"""

_COLS = (
    "created_at",
    "bot_name",
    "tier",
    "strategy_version",
    "decision_status",
    "decision_mode",
    "paper_only",
    "reason_codes",
    "reason_json",
    "threshold_json",
    "source_system",
    "source_model_version",
    "source_rule",
    "source_rule_name",
    "source_reason_codes",
    "build_sha",
    "config_hash",
    "source_build_sha",
    "source_config_hash",
    "feature_schema_version",
    "source_features_version",
    "feature_cohort",
    "feature_availability_json",
    "feature_age_json",
    "evidence_grade",
    "evidence_reason_codes",
    "drift_candidate_lane",
    "data_complete",
    "full_feature_complete",
    "source_created_at",
    "source_captured_at",
    "evidence_as_of",
    "drift_evidence_json",
    "drift_v91_yes_fraction_all",
    "drift_v91_yes_fraction_directional",
    "drift_v91_observation_count",
    "drift_v95_15m_side",
    "drift_v95_15m_flow_score",
    "drift_btc_15m_side",
    "drift_core_breadth",
    "drift_asymmetric_breadth",
    "drift_flow_1m",
    "drift_flow_3m",
    "drift_flow_5m",
    "drift_flow_13m",
    "drift_flow_positive_bucket_fraction",
    "drift_flow_sign_flips",
    "drift_flow_coverage",
    "record_kind",
    "delivery_status",
    "asset",
    "side",
    "original_source_side",
    "interval",
    "window_key",
    "ticker",
    "close_time",
    "entry_ask_cents",
    "spread_cents",
    *KALSHI_DEPTH_KEYS,
    *KALSHI_FLOW_KEYS,
    *SPOT_DEPTH_KEYS,
    *COINBASE_L2_KEYS,
    *KRAKEN_L3_KEYS,
    "btc_context_json",
    "btc_ticker",
    "btc_depth_contracts",
    "btc_book_pressure_cents",
    "btc_dominant_side",
    "btc_model_predicted_side",
    "btc_model_yes_probability",
    "btc_calibrated_yes_probability",
    "btc_market_implied_yes_probability",
    *BTC_REGIME_KEYS,
    "notification_status",
    "notification_message_id",
    "notification_error",
    "notified_at",
    "official_result",
    "resolved_at",
    "correct",
    "hypothetical_pnl_cents",
)


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def kalshi_fee_cents(entry_ask_cents: float | int | None) -> int | None:
    if entry_ask_cents is None:
        return None
    ask = max(0.0, min(100.0, float(entry_ask_cents)))
    if ask <= 0.0 or ask >= 100.0:
        return 0
    p = ask / 100.0
    return int(math.ceil(0.07 * p * (1.0 - p) * 100.0))


def net_pnl_cents(entry_ask_cents: float | int | None, correct: bool) -> float | None:
    ask = _num(entry_ask_cents)
    if ask is None:
        return None
    fee = kalshi_fee_cents(ask)
    if fee is None:
        return None
    gross = 100.0 - ask if correct else -ask
    return gross - float(fee)


def _json(data: Any) -> str | None:
    if data is None:
        return None

    def clean(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(k): clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [clean(v) for v in value]
        if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        try:
            out = float(value)
            return out if math.isfinite(out) else str(value)
        except (TypeError, ValueError):
            return str(value)

    return json.dumps(clean(data), sort_keys=True, separators=(",", ":"))


def _csv(values: Sequence[str]) -> str:
    return ",".join(str(v) for v in values if str(v))


def _entry_ask(row: Mapping[str, Any]) -> Any:
    return row.get("entry_ask_cents") if row.get("entry_ask_cents") is not None else row.get("selected_ask_cents")


def _source_model(row: Mapping[str, Any]) -> str | None:
    return str(row.get("model_version")) if row.get("model_version") is not None else None


_LINEAGE_COLUMN_TYPES: dict[str, str] = {
    "build_sha": "TEXT",
    "config_hash": "TEXT",
    "source_build_sha": "TEXT",
    "source_config_hash": "TEXT",
    "feature_schema_version": "TEXT",
    "source_features_version": "TEXT",
    "feature_cohort": "TEXT",
    "feature_availability_json": "TEXT",
    "feature_age_json": "TEXT",
    "evidence_grade": "TEXT",
    "evidence_reason_codes": "TEXT",
    "drift_candidate_lane": "TEXT",
    "data_complete": "INTEGER",
    "full_feature_complete": "INTEGER",
    "source_created_at": "REAL",
    "source_captured_at": "REAL",
    "evidence_as_of": "REAL",
    "drift_evidence_json": "TEXT",
    "drift_v91_yes_fraction_all": "REAL",
    "drift_v91_yes_fraction_directional": "REAL",
    "drift_v91_observation_count": "INTEGER",
    "drift_v95_15m_side": "TEXT",
    "drift_v95_15m_flow_score": "REAL",
    "drift_btc_15m_side": "TEXT",
    "drift_core_breadth": "INTEGER",
    "drift_asymmetric_breadth": "INTEGER",
    "drift_flow_1m": "REAL",
    "drift_flow_3m": "REAL",
    "drift_flow_5m": "REAL",
    "drift_flow_13m": "REAL",
    "drift_flow_positive_bucket_fraction": "REAL",
    "drift_flow_sign_flips": "INTEGER",
    "drift_flow_coverage": "REAL",
}


def _mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, Mapping) else None
    return None


def _evidence_sources(
    row: Mapping[str, Any], threshold: Mapping[str, Any]
) -> tuple[Mapping[str, Any], ...]:
    """Return only explicitly supplied lineage/evidence mappings.

    The recorder must not manufacture completeness, grades, or lineage.  Nested
    JSON/mappings are accepted so upstream enrichers can keep one canonical
    evidence bundle while the policy inputs below remain first-class columns.
    """
    sources: list[Mapping[str, Any]] = [row, threshold]
    for parent in (row, threshold):
        for key in (
            "drift_evidence", "drift_evidence_json", "evidence",
            "lineage", "lineage_json", "feature_identity",
        ):
            nested = _mapping(parent.get(key))
            if nested is not None:
                sources.append(nested)
    return tuple(sources)


def _explicit(sources: Sequence[Mapping[str, Any]], *keys: str) -> Any:
    for source in sources:
        for key in keys:
            if key in source and source.get(key) is not None:
                return source.get(key)
    return None


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _json_payload(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return _json(value)


def _explicit_flag(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = _num(value)
        return None if number is None else (1 if number != 0.0 else 0)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return 1
    if text in {"0", "false", "no", "off"}:
        return 0
    return None


def _explicit_int(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None and number.is_integer() else None


def _evidence_reason_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return _csv([str(item) for item in value])
    return str(value)


def _lineage_record(
    row: Mapping[str, Any], threshold: Mapping[str, Any]
) -> dict[str, Any]:
    sources = _evidence_sources(row, threshold)
    decision_sources = (threshold, row, *sources[2:])
    source_created_at = _num(row.get("source_created_at"))
    if source_created_at is None:
        source_created_at = _num(row.get("created_at"))
    return {
        # Decision lineage and source lineage intentionally remain separate.
        # A missing source hash must not inherit the process/decision hash.
        "build_sha": _text(_explicit(decision_sources, "build_sha", "decision_build_sha")),
        "config_hash": _text(_explicit(decision_sources, "config_hash", "decision_config_hash")),
        "source_build_sha": _text(_explicit(sources, "source_build_sha")),
        "source_config_hash": _text(_explicit(sources, "source_config_hash")),
        "feature_schema_version": _text(_explicit(sources, "feature_schema_version")),
        "source_features_version": _text(_explicit(
            sources, "source_features_version", "features_version"
        )),
        "feature_cohort": _text(_explicit(
            sources, "feature_cohort", "cohort", "cohort_name"
        )),
        "feature_availability_json": _json_payload(_explicit(
            sources, "feature_availability_json", "feature_availability"
        )),
        "feature_age_json": _json_payload(_explicit(
            sources, "feature_age_json", "feature_ages"
        )),
        "evidence_grade": _text(_explicit(sources, "evidence_grade")),
        "evidence_reason_codes": _evidence_reason_text(_explicit(
            sources, "evidence_reason_codes"
        )),
        "drift_candidate_lane": _text(_explicit(sources, "drift_candidate_lane")),
        "data_complete": _explicit_flag(_explicit(sources, "data_complete")),
        "full_feature_complete": _explicit_flag(_explicit(
            sources, "full_feature_complete"
        )),
        "source_created_at": source_created_at,
        "source_captured_at": _num(_explicit(
            sources, "source_captured_at", "captured_at", "capture_timestamp"
        )),
        "evidence_as_of": _num(_explicit(
            sources, "evidence_as_of", "evidence_timestamp", "feature_captured_at"
        )),
        "drift_evidence_json": _json_payload(_explicit(
            (row, threshold), "drift_evidence_json", "drift_evidence", "evidence_json"
        )),
        "drift_v91_yes_fraction_all": _num(_explicit(
            sources, "drift_v91_yes_fraction_all", "v91_full_path_yes_fraction"
        )),
        "drift_v91_yes_fraction_directional": _num(_explicit(
            sources,
            "drift_v91_yes_fraction_directional",
            "v91_full_path_yes_fraction_directional",
        )),
        "drift_v91_observation_count": _explicit_int(_explicit(
            sources,
            "drift_v91_observation_count",
            "v91_full_path_all_count",
        )),
        "drift_v95_15m_side": _text(_explicit(
            sources, "drift_v95_15m_side", "v95_15m_side"
        )),
        "drift_v95_15m_flow_score": _num(_explicit(
            sources, "drift_v95_15m_flow_score", "v95_15m_flow_score"
        )),
        "drift_btc_15m_side": _text(_explicit(
            sources, "drift_btc_15m_side", "btc_15m_side"
        )),
        "drift_core_breadth": _explicit_int(_explicit(
            sources, "drift_core_breadth", "core_breadth"
        )),
        "drift_asymmetric_breadth": _explicit_int(_explicit(
            sources, "drift_asymmetric_breadth", "asymmetric_breadth"
        )),
        "drift_flow_1m": _num(_explicit(sources, "drift_flow_1m", "drift_flow_60s")),
        "drift_flow_3m": _num(_explicit(sources, "drift_flow_3m")),
        "drift_flow_5m": _num(_explicit(sources, "drift_flow_5m")),
        "drift_flow_13m": _num(_explicit(sources, "drift_flow_13m")),
        "drift_flow_positive_bucket_fraction": _num(_explicit(
            sources, "drift_flow_positive_bucket_fraction"
        )),
        "drift_flow_sign_flips": _explicit_int(_explicit(
            sources, "drift_flow_sign_flips"
        )),
        "drift_flow_coverage": _num(_explicit(sources, "drift_flow_coverage")),
    }


def _feature_column_type(name: str) -> str:
    text_names = {
        "coinbase_l2_status",
        "coinbase_l2_missing_reason",
        "coinbase_l2_product_id",
        "coinbase_l2_target_source",
        "coinbase_l2_easier_side",
        "kraken_l3_status",
        "kraken_l3_missing_reason",
        "kraken_l3_symbol",
        "kraken_l3_checksum",
        "btc_regime",
        "btc_regime_agreement",
        "btc_regime_vote_detail",
        "btc_regime_status",
        "btc_regime_missing_reason",
        "btc_v95_checkpoint",
        "btc_v95_grade",
        "btc_v95_predicted_side",
    }
    if name in text_names:
        return "TEXT"
    if name.endswith("_visible"):
        return "INTEGER"
    return "REAL"


def _build_record(
    decision: BotDecision,
    row: Mapping[str, Any],
    source_system: str,
) -> dict[str, Any]:
    threshold = dict(decision.threshold_profile or {})
    lineage = _lineage_record(row, threshold)
    btc = dict(decision.btc_context or {})
    if not btc:
        btc_keys = (
            "btc_ticker",
            "btc_depth_contracts",
            "btc_book_pressure_cents",
            "btc_dominant_side",
            "btc_model_predicted_side",
            "btc_model_yes_probability",
            "btc_calibrated_yes_probability",
            "btc_market_implied_yes_probability",
            *BTC_REGIME_KEYS,
        )
        btc = {key: row.get(key) for key in btc_keys if row.get(key) is not None}
    created = _num(row.get("created_at")) or time.time()
    out: dict[str, Any] = {
        "created_at": created,
        "bot_name": decision.bot_name,
        "tier": decision.tier,
        "strategy_version": decision.strategy_version,
        "decision_status": decision.decision_status,
        "decision_mode": "PAPER_RESEARCH",
        "paper_only": 1,
        "reason_codes": _csv(decision.reason_codes),
        "reason_json": _json(list(decision.reason_codes)),
        "threshold_json": _json(threshold),
        "source_system": source_system,
        "source_model_version": _source_model(row),
        "source_rule": source_rule(row),
        "source_rule_name": row.get("rule_name"),
        "source_reason_codes": row.get("reason_codes"),
        **lineage,
        "record_kind": row.get("record_kind"),
        "delivery_status": row.get("delivery_status"),
        "asset": row.get("asset"),
        "side": decision.side_override or source_side(row),
        "original_source_side": decision.original_source_side,
        "interval": row.get("interval"),
        "window_key": row.get("window_key"),
        "ticker": row.get("ticker"),
        "close_time": row.get("close_time"),
        "entry_ask_cents": (
            decision.entry_ask_cents if decision.use_entry_ask_override else _entry_ask(row)
        ),
        "spread_cents": row.get("spread_cents"),
        "btc_context_json": _json(btc) if btc else None,
        "btc_ticker": btc.get("btc_ticker") if "btc_ticker" in btc else row.get("btc_ticker"),
        "btc_depth_contracts": (
            btc.get("btc_depth_contracts") if "btc_depth_contracts" in btc else row.get("btc_depth_contracts")
        ),
        "btc_book_pressure_cents": (
            btc.get("btc_book_pressure_cents")
            if "btc_book_pressure_cents" in btc
            else row.get("btc_book_pressure_cents")
        ),
        "btc_dominant_side": (
            btc.get("btc_dominant_side") if "btc_dominant_side" in btc else row.get("btc_dominant_side")
        ),
        "btc_model_predicted_side": (
            btc.get("btc_model_predicted_side")
            if "btc_model_predicted_side" in btc
            else row.get("btc_model_predicted_side")
        ),
        "btc_model_yes_probability": (
            btc.get("btc_model_yes_probability")
            if "btc_model_yes_probability" in btc
            else row.get("btc_model_yes_probability")
        ),
        "btc_calibrated_yes_probability": (
            btc.get("btc_calibrated_yes_probability")
            if "btc_calibrated_yes_probability" in btc
            else row.get("btc_calibrated_yes_probability")
        ),
        "btc_market_implied_yes_probability": (
            btc.get("btc_market_implied_yes_probability")
            if "btc_market_implied_yes_probability" in btc
            else row.get("btc_market_implied_yes_probability")
        ),
        "notification_status": None,
        "notification_message_id": None,
        "notification_error": None,
        "notified_at": None,
        "official_result": None,
        "resolved_at": None,
        "correct": None,
        "hypothetical_pnl_cents": None,
    }
    for key in KALSHI_DEPTH_KEYS:
        out[key] = row.get(key)
    for key in KALSHI_FLOW_KEYS:
        out[key] = row.get(key)
    for key in SPOT_DEPTH_KEYS:
        out[key] = row.get(key)
    for key in COINBASE_L2_KEYS:
        out[key] = row.get(key)
    for key in KRAKEN_L3_KEYS:
        out[key] = row.get(key)
    for key in BTC_REGIME_KEYS:
        out[key] = btc.get(key) if key in btc else row.get(key)
    return out


class StrategyBotLedger:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._ensure_columns_locked()
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def claim_meta_once(self, meta_key: str) -> bool:
        """Durable once-only claim (e.g. the 13M sniper auto-mute notice).

        Returns True exactly once per key across restarts; subsequent claims (or a
        concurrent second caller) get False. INSERT OR IGNORE on the PRIMARY KEY is
        the atomicity — no read-modify-write race.
        """
        with self._lock:
            now = time.time()
            cols = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(strategy_bot_meta)").fetchall()
            }
            if {"meta_key", "claimed_at"}.issubset(cols):
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO strategy_bot_meta (meta_key, claimed_at) VALUES (?, ?)",
                    (str(meta_key), now),
                )
            elif {"key", "value", "updated_at"}.issubset(cols):
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO strategy_bot_meta (key, value, updated_at) VALUES (?, ?, ?)",
                    (str(meta_key), "claimed", now),
                )
            else:
                raise sqlite3.OperationalError("strategy_bot_meta has an unsupported schema")
            self._conn.commit()
            return cur.rowcount > 0

    def _ensure_columns_locked(self) -> None:
        existing = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(strategy_bot_decisions)").fetchall()
        }
        added = {
            "tier": "TEXT",
            "original_source_side": "TEXT",
            "notification_status": "TEXT",
            "notification_message_id": "INTEGER",
            "notification_error": "TEXT",
            "notified_at": "REAL",
            **_LINEAGE_COLUMN_TYPES,
        }
        for key in COINBASE_L2_KEYS:
            added.setdefault(key, _feature_column_type(key))
        for key in KRAKEN_L3_KEYS:
            added.setdefault(key, _feature_column_type(key))
        for key in BTC_REGIME_KEYS:
            added.setdefault(key, _feature_column_type(key))
        for name, column_type in added.items():
            if name not in existing:
                try:
                    self._conn.execute(
                        f"ALTER TABLE strategy_bot_decisions ADD COLUMN {name} {column_type}"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise

    def record_decision(
        self,
        decision: BotDecision,
        source_row: Mapping[str, Any],
        *,
        source_system: str,
    ) -> int | None:
        row = _build_record(decision, source_row, source_system)
        placeholders = ",".join("?" for _ in _COLS)
        values = [row.get(c) for c in _COLS]
        with self._lock:
            try:
                cur = self._conn.execute(
                    f"INSERT INTO strategy_bot_decisions({','.join(_COLS)}) "
                    f"VALUES({placeholders})",
                    values,
                )
                self._conn.commit()
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                return None

    def row_for_decision(
        self,
        decision: BotDecision,
        source_row: Mapping[str, Any],
        *,
        source_system: str,
    ) -> dict[str, Any] | None:
        """Return the persisted row matching ``record_decision``'s identity.

        Recorders use this after an idempotent insert reports a duplicate.  It
        lets a replay finish notification delivery when the process previously
        crashed after committing the strategy decision but before enqueueing its
        card.  ``IS`` deliberately gives NULL-safe equality for legacy rows.
        """
        identity = _build_record(decision, source_row, source_system)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM strategy_bot_decisions WHERE "
                "strategy_version IS ? AND bot_name IS ? AND source_system IS ? "
                "AND source_model_version IS ? AND ticker IS ? AND interval IS ? "
                "AND window_key IS ? AND source_rule IS ? ORDER BY id LIMIT 1",
                (
                    identity.get("strategy_version"),
                    identity.get("bot_name"),
                    identity.get("source_system"),
                    identity.get("source_model_version"),
                    identity.get("ticker"),
                    identity.get("interval"),
                    identity.get("window_key"),
                    identity.get("source_rule"),
                ),
            ).fetchone()
        return None if row is None else dict(row)

    def mark_notification(
        self,
        row_id: int,
        *,
        status: str,
        message_id: int | None,
        error: str | None = None,
        now: float | None = None,
    ) -> None:
        ts = time.time() if now is None else float(now)
        with self._lock:
            self._conn.execute(
                "UPDATE strategy_bot_decisions SET notification_status=?, "
                "notification_message_id=?, notification_error=?, notified_at=? WHERE id=?",
                (status, message_id, error, ts, row_id),
            )
            self._conn.commit()

    def drift_notifications_to_reconcile(
        self,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return a bounded batch of paper Drift rows awaiting outbox truth.

        Delivery reconciliation deliberately starts from the strategy ledger,
        rather than walking the whole outbox.  This keeps each live-loop pass
        bounded and also finds an old pending decision even when the outbox has
        accumulated many newer messages.
        """
        bounded = max(1, min(int(limit), 500))
        drift_bots = (
            BOT_DRIFT_13M,
            BOT_DRIFT_FLOW_SPREAD,
            BOT_DRIFT_ADDON,
            BOT_DRIFT_LATEQUAL,
            BOT_DRIFT_NO_EXPANSION,
            BOT_DRIFT_NO_MIRROR,
        )
        placeholders = ",".join("?" for _ in drift_bots)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, strategy_version, bot_name, window_key, ticker, "
                "notification_status FROM strategy_bot_decisions "
                "WHERE source_system='drift_shadow' AND paper_only=1 "
                "AND notification_status='QUEUED_RETRY' "
                f"AND bot_name IN ({placeholders}) "
                "ORDER BY COALESCE(notified_at, created_at) ASC, id ASC LIMIT ?",
                (*drift_bots, bounded),
            ).fetchall()
        return [dict(row) for row in rows]

    def reconcile_drift_notification_terminals(
        self,
        updates: Sequence[tuple[int, str, str | None]],
        *,
        now: float | None = None,
    ) -> int:
        """Atomically apply terminal outbox states to queued Drift decisions.

        The ``QUEUED_RETRY`` predicate prevents a stale reconciliation read from
        overwriting a newer decision state.  All updates share one transaction,
        so a grouped card does not require one commit per constituent row.
        """
        allowed = {"SENT", "EXPIRED", "DEAD_LETTER"}
        clean: list[tuple[str, str | None, float, int]] = []
        ts = time.time() if now is None else float(now)
        for row_id, status, error in updates:
            terminal = str(status).upper()
            if terminal not in allowed:
                continue
            clean.append((terminal, error, ts, int(row_id)))
        if not clean:
            return 0
        changed = 0
        with self._lock:
            for terminal, error, reconciled_at, row_id in clean:
                cur = self._conn.execute(
                    "UPDATE strategy_bot_decisions SET notification_status=?, "
                    "notification_message_id=NULL, notification_error=?, notified_at=? "
                    "WHERE id=? AND notification_status='QUEUED_RETRY'",
                    (terminal, error, reconciled_at, row_id),
                )
                changed += max(0, int(cur.rowcount))
            self._conn.commit()
        return changed

    def row_by_id(self, row_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM strategy_bot_decisions WHERE id=?",
                (int(row_id),),
            ).fetchone()
        return None if row is None else dict(row)

    def has_accepted_window(
        self,
        *,
        bot_name: str,
        strategy_version: str,
        asset: str,
        side: str,
        window_key: int,
        ticker: str | None = None,
    ) -> bool:
        query = (
            "SELECT 1 FROM strategy_bot_decisions "
            "WHERE bot_name=? AND strategy_version=? AND asset=? AND side=? "
            "AND window_key=? AND decision_status=?"
        )
        params: list[Any] = [bot_name, strategy_version, asset, side, window_key, ACCEPTED]
        if ticker:
            query += " AND ticker<>?"
            params.append(ticker)
        query += " LIMIT 1"
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return row is not None

    @staticmethod
    def _wilson_lower(correct: int, n: int, z: float = 1.96) -> float | None:
        if n <= 0:
            return None
        p = correct / n
        denom = 1.0 + z * z / n
        centre = (p + z * z / (2 * n)) / denom
        half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4 * n * n)) / denom
        return centre - half

    def bot_accepted_resolved_stats(
        self,
        bot_name: str = BOT_THIRTEEN_M_SNIPER,
        strategy_version: str = STRATEGY_VERSION,
        *,
        threshold_rule_version: str | None = None,
    ) -> dict[str, Any]:
        return self.bot_resolved_stats(
            bot_name=bot_name,
            strategy_version=strategy_version,
            decision_status=ACCEPTED,
            threshold_rule_version=threshold_rule_version,
        )

    def bot_resolved_stats(
        self,
        bot_name: str,
        strategy_version: str = STRATEGY_VERSION,
        *,
        decision_status: str,
        threshold_rule_version: str | None = None,
    ) -> dict[str, Any]:
        """Resolved economics for one explicit decision-status cohort."""
        if threshold_rule_version is None:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n, COALESCE(SUM(correct), 0) AS correct, "
                    "COALESCE(SUM(hypothetical_pnl_cents), 0) AS net_pnl_cents "
                    "FROM strategy_bot_decisions "
                    "WHERE strategy_version=? AND bot_name=? AND decision_status=? "
                    "AND official_result IS NOT NULL",
                    (strategy_version, bot_name, decision_status),
                ).fetchone()
            n = int(row["n"] or 0) if row is not None else 0
            correct = int(row["correct"] or 0) if row is not None else 0
            net_pnl = float(row["net_pnl_cents"] or 0.0) if row is not None else 0.0
        else:
            with self._lock:
                candidates = self._conn.execute(
                    "SELECT correct, hypothetical_pnl_cents, threshold_json "
                    "FROM strategy_bot_decisions "
                    "WHERE strategy_version=? AND bot_name=? AND decision_status=? "
                    "AND official_result IS NOT NULL",
                    (strategy_version, bot_name, decision_status),
                ).fetchall()
            matched = [
                row for row in candidates
                if self._threshold_value(dict(row), "rule_version")
                == threshold_rule_version
            ]
            n = len(matched)
            correct = sum(int(row["correct"] or 0) for row in matched)
            net_pnl = sum(
                float(row["hypothetical_pnl_cents"] or 0.0) for row in matched
            )
        accuracy = None if n <= 0 else correct / n
        return {
            "n": n,
            "correct": correct,
            "accuracy": accuracy,
            "wilson_lb": self._wilson_lower(correct, n),
            "net_pnl_cents": net_pnl,
        }

    def trailing_abs_flow_percentile(
        self,
        pctl: float | None = None,
        window_n: int | None = None,
        *,
        asset: str | None = None,
        created_before: float | None = None,
        percentile: float | None = None,
        limit: int | None = None,
        strategy_version: str = STRATEGY_VERSION,
    ) -> float | None:
        if percentile is None:
            percentile = 0.70 if pctl is None else pctl
        if limit is None:
            limit = 200 if window_n is None else window_n
        params: list[Any] = [strategy_version, BOT_BASELINE]
        query = (
            "SELECT ABS(spot_depth_trade_net_notional_60s) AS value "
            "FROM strategy_bot_decisions "
            "WHERE strategy_version=? AND bot_name=? "
            "AND spot_depth_trade_net_notional_60s IS NOT NULL"
        )
        if asset:
            query += " AND asset=?"
            params.append(str(asset).upper())
        if created_before is not None:
            query += " AND created_at<?"
            params.append(float(created_before))
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        values = sorted(float(row["value"]) for row in rows if row["value"] is not None)
        if not values:
            return None
        p = max(0.0, min(1.0, float(percentile)))
        index = int(math.ceil((len(values) - 1) * p))
        return values[index]

    def resolve(
        self,
        *,
        source_system: str,
        source_model_version: str,
        ticker: str,
        official_result: str,
        now: float | None = None,
    ) -> int:
        official = str(official_result or "").upper()
        if official not in {"YES", "NO"}:
            return 0
        ts = time.time() if now is None else float(now)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, side, entry_ask_cents FROM strategy_bot_decisions "
                "WHERE source_system=? AND source_model_version=? AND ticker=? "
                "AND official_result IS NULL",
                (source_system, source_model_version, ticker),
            ).fetchall()
            graded = 0
            for r in rows:
                side = str(r["side"] or "").upper()
                correct = side == official
                pnl = net_pnl_cents(r["entry_ask_cents"], correct)
                self._conn.execute(
                    "UPDATE strategy_bot_decisions SET official_result=?, resolved_at=?, "
                    "correct=?, hypothetical_pnl_cents=? WHERE id=?",
                    (official, ts, 1 if correct else 0, pnl, r["id"]),
                )
                graded += 1
            self._conn.commit()
        return graded

    def rows(self, strategy_version: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM strategy_bot_decisions"
        params: tuple[Any, ...] = ()
        if strategy_version:
            query += " WHERE strategy_version=?"
            params = (strategy_version,)
        query += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def scoreboard(
        self,
        strategy_version: str = STRATEGY_VERSION,
        *,
        min_n: int = 30,
    ) -> dict[str, Any]:
        rows = self.rows(strategy_version)
        counterfactual_bots = {
            BOT_DRIFT_FLOW_SPREAD_SHADOW_SPREAD4,
            BOT_DRIFT_FLOW_SPREAD_SHADOW_FLOW15,
            BOT_DRIFT_ASYMMETRIC_VOLUME,
            BOT_DRIFT_BALANCED_V95,
            BOT_DRIFT_ACCURACY_V91,
            BOT_DRIFT_CONSENSUS_FALLBACK,
        }
        exposure_rows = [
            r for r in rows
            if r.get("bot_name") not in counterfactual_bots
            and not (
                r.get("bot_name") == BOT_DRIFT_NO_EXPANSION
                and self._threshold_value(r, "rule_version")
                == "drift-no-expansion-13m-shadow-v2"
            )
        ]
        independent_rows = [
            r for r in exposure_rows if r.get("bot_name") != BOT_DRIFT_ADDON
        ]
        return {
            "available": True,
            "strategy_version": strategy_version,
            "paper_only": True,
            "min_n": int(min_n),
            "total_rows": len(rows),
            "independent_rows": len(independent_rows),
            "correlated_exposure_rows": len(exposure_rows) - len(independent_rows),
            "counterfactual_research_rows": len(rows) - len(exposure_rows),
            "resolved": sum(
                1 for r in independent_rows if r.get("official_result") is not None
            ),
            "accepted": self._agg(
                [r for r in independent_rows if r.get("decision_status") == ACCEPTED], min_n
            ),
            "research_only": self._agg(
                [r for r in independent_rows if r.get("decision_status") == RESEARCH_ONLY], min_n
            ),
            "all": self._agg(independent_rows, min_n),
            "all_exposure": self._agg(exposure_rows, min_n),
            "by_bot": self._group(rows, ("bot_name",), min_n),
            "by_tier": self._group(
                [r for r in rows if r.get("bot_name") == BOT_CONFIDENCE_TIER],
                ("tier",),
                min_n,
            ),
            "by_tier_status": self._group(
                [r for r in rows if r.get("bot_name") == BOT_CONFIDENCE_TIER],
                ("tier", "decision_status"),
                min_n,
            ),
            "by_bot_status": self._group(rows, ("bot_name", "decision_status"), min_n),
            "by_bot_asset": self._group(rows, ("bot_name", "asset"), min_n),
            "by_bot_asset_side": self._group(rows, ("bot_name", "asset", "side"), min_n),
            "by_bot_rule": self._group(rows, ("bot_name", "source_rule"), min_n),
            "by_bot_interval": self._group(rows, ("bot_name", "interval"), min_n),
            "by_bot_delivery_status": self._group(rows, ("bot_name", "delivery_status"), min_n),
            "by_drift_candidate_lane": self._group(
                [r for r in rows if r.get("drift_candidate_lane") is not None],
                ("drift_candidate_lane",),
                min_n,
            ),
            "by_feature_cohort": self._group(
                [r for r in rows if r.get("feature_cohort") is not None],
                ("feature_cohort",),
                min_n,
            ),
            "by_evidence_grade": self._group(
                [r for r in rows if r.get("evidence_grade") is not None],
                ("evidence_grade",),
                min_n,
            ),
            "by_full_feature_complete": self._group(
                [r for r in rows if r.get("full_feature_complete") is not None],
                ("full_feature_complete",),
                min_n,
            ),
            "by_tier_source_asset_side_rule": self._group(
                [r for r in rows if r.get("bot_name") == BOT_CONFIDENCE_TIER],
                ("tier", "source_system", "asset", "side", "source_rule"),
                min_n,
            ),
            "by_bot_rule_interval_delivery": self._group(
                rows, ("bot_name", "source_rule", "interval", "delivery_status"), min_n
            ),
            "accepted_by_bot_asset_side_rule_interval_delivery": self._group(
                [r for r in independent_rows if r.get("decision_status") == ACCEPTED],
                ("bot_name", "asset", "side", "source_rule", "interval", "delivery_status"),
                min_n,
            ),
            "positive_ev_gate": self._positive_ev_gate(rows, min_n),
            "bnb_system": self._bnb_system(rows, min_n),
            "drift_system": self._drift_system(rows, min_n),
            "tier_confirmation_system": self._tier_confirmation_system(rows, min_n),
            # Counterfactuals reuse the same point-in-time enrichment, so
            # counting them here would triple-weight identical feed coverage.
            "data_coverage": self._data_coverage(exposure_rows),
        }

    @classmethod
    def _group(
        cls,
        rows: Sequence[Mapping[str, Any]],
        keys: Sequence[str],
        min_n: int,
    ) -> dict[str, Any]:
        groups: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            label = "|".join(str(row.get(k) if row.get(k) is not None else "") for k in keys)
            groups.setdefault(label, []).append(row)
        return {label: cls._agg(group, min_n) for label, group in sorted(groups.items())}

    @staticmethod
    def _agg(rows: Sequence[Mapping[str, Any]], min_n: int) -> dict[str, Any]:
        all_rows = list(rows)
        settled = [r for r in all_rows if r.get("official_result") is not None]
        right = sum(1 for r in settled if int(r.get("correct") or 0) == 1)
        pnls = [
            float(r["hypothetical_pnl_cents"])
            for r in settled
            if r.get("hypothetical_pnl_cents") is not None
        ]
        n = len(settled)
        wilson_low: float | None = None
        wilson_high: float | None = None
        if n > 0:
            z = 1.959963984540054
            p = right / n
            denominator = 1.0 + (z * z / n)
            center = (p + (z * z / (2.0 * n))) / denominator
            margin = (
                z
                * math.sqrt((p * (1.0 - p) / n) + (z * z / (4.0 * n * n)))
                / denominator
            )
            wilson_low = max(0.0, center - margin)
            wilson_high = min(1.0, center + margin)

        ordered_pnls: list[float] = []
        for row in sorted(
            settled,
            key=lambda r: (
                float(
                    r.get("close_time")
                    if r.get("close_time") is not None
                    else r.get("created_at") or 0.0
                ),
                int(r.get("id") or 0),
            ),
        ):
            pnl = _num(row.get("hypothetical_pnl_cents"))
            if pnl is not None:
                ordered_pnls.append(pnl)
        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for pnl in ordered_pnls:
            equity += pnl
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)

        breakevens: list[float] = []
        for row in settled:
            ask = _num(row.get("entry_ask_cents"))
            fee = kalshi_fee_cents(ask)
            if ask is not None and fee is not None:
                breakevens.append((ask + float(fee)) / 100.0)
        avg_pnl = None if not pnls else sum(pnls) / len(pnls)
        net_pnl = None if not pnls else sum(pnls)
        return {
            "rows": len(all_rows),
            "resolved": n,
            "correct": right,
            "accuracy": None if n <= 0 else right / n,
            "accuracy_wilson_95_low": wilson_low,
            "accuracy_wilson_95_high": wilson_high,
            "wilson_95_low": wilson_low,
            "wilson_95_high": wilson_high,
            "avg_pnl_cents": avg_pnl,
            "net_pnl_cents": net_pnl,
            "fee_adjusted_ev_cents": avg_pnl,
            "fee_adjusted_net_pnl_cents": net_pnl,
            "max_cumulative_drawdown_cents": max_drawdown if ordered_pnls else None,
            "avg_fee_adjusted_breakeven_rate": (
                None if not breakevens else sum(breakevens) / len(breakevens)
            ),
            "provisional": n < int(min_n),
        }

    @classmethod
    def _cohort_view(
        cls,
        rows: Sequence[Mapping[str, Any]],
        min_n: int,
    ) -> dict[str, Any]:
        materialized = list(rows)
        overall = cls._agg(materialized, min_n)
        resolved = int(overall["resolved"])
        if resolved < DRIFT_REVIEW_BARS[0]:
            stage = "ACCRUING_TO_30"
        elif resolved < DRIFT_REVIEW_BARS[1]:
            stage = "DIAGNOSTIC_REVIEW_30"
        elif resolved < DRIFT_REVIEW_BARS[2]:
            stage = "KEEP_KILL_REVIEW_60"
        else:
            stage = "PROMOTION_REVIEW_150"
        return {
            "overall": overall,
            "by_asset": cls._group(materialized, ("asset",), min_n),
            "by_status": cls._group(materialized, ("decision_status",), min_n),
            "review": {
                "stage": stage,
                "bars": list(DRIFT_REVIEW_BARS),
                "manual_only": True,
                "automatic_threshold_changes": False,
                "automatic_promotion": False,
            },
        }

    @staticmethod
    def _threshold_value(row: Mapping[str, Any], key: str) -> Any:
        raw = row.get("threshold_json")
        if not raw:
            return None
        try:
            data = json.loads(str(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return data.get(key) if isinstance(data, Mapping) else None

    @classmethod
    def _threshold_flag(cls, row: Mapping[str, Any], key: str) -> bool:
        return bool(cls._threshold_value(row, key))

    @classmethod
    def _positive_ev_gate(
        cls,
        rows: Sequence[Mapping[str, Any]],
        min_n: int,
    ) -> dict[str, Any]:
        gate_rows = [
            r for r in rows
            if "V3_POSITIVE_EV_GATE_" in str(r.get("reason_codes") or "")
        ]
        allowed = [
            r for r in gate_rows
            if r.get("decision_status") == ACCEPTED
            and "_ALLOWED" in str(r.get("reason_codes") or "")
        ]
        research_blocks = [
            r for r in gate_rows
            if r.get("decision_status") == RESEARCH_ONLY
            and "_RESEARCH_ONLY" in str(r.get("reason_codes") or "")
        ]
        return {
            "all": cls._agg(gate_rows, min_n),
            "allowed_candidates": cls._agg(allowed, min_n),
            "research_blocks": cls._agg(research_blocks, min_n),
            "by_bot_rule_status": cls._group(
                gate_rows,
                ("bot_name", "source_rule", "decision_status"),
                min_n,
            ),
            "by_asset_side_rule_status": cls._group(
                gate_rows,
                ("asset", "side", "source_rule", "decision_status"),
                min_n,
            ),
        }

    @classmethod
    def _data_coverage(cls, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        all_rows = list(rows)
        tier_rows = [r for r in all_rows if r.get("bot_name") == BOT_CONFIDENCE_TIER]
        return {
            "all": cls._coverage_summary(all_rows),
            "confidence_tier_rows": cls._coverage_summary(tier_rows),
            "by_source_asset": cls._coverage_group(all_rows, ("source_system", "asset")),
            "by_source_asset_tier": cls._coverage_group(
                tier_rows,
                ("source_system", "asset", "tier"),
            ),
        }

    @classmethod
    def _drift_system(
        cls,
        rows: Sequence[Mapping[str, Any]],
        min_n: int,
    ) -> dict[str, Any]:
        raw_shadow = [r for r in rows if r.get("bot_name") == BOT_DRIFT_13M]
        core_candidates = [
            r for r in rows if r.get("bot_name") == BOT_DRIFT_FLOW_SPREAD
        ]
        addon_candidates = [r for r in rows if r.get("bot_name") == BOT_DRIFT_ADDON]
        latequal_candidates = [
            r for r in rows if r.get("bot_name") == BOT_DRIFT_LATEQUAL
        ]
        no_mirror = [r for r in rows if r.get("bot_name") == BOT_DRIFT_NO_MIRROR]
        no_expansion = [r for r in rows if r.get("bot_name") == BOT_DRIFT_NO_EXPANSION]
        spread4 = [
            r for r in rows
            if r.get("bot_name") == BOT_DRIFT_FLOW_SPREAD_SHADOW_SPREAD4
        ]
        flow15 = [
            r for r in rows
            if r.get("bot_name") == BOT_DRIFT_FLOW_SPREAD_SHADOW_FLOW15
        ]
        asymmetric_volume = [
            r for r in rows if r.get("bot_name") == BOT_DRIFT_ASYMMETRIC_VOLUME
        ]
        balanced_v95 = [
            r for r in rows if r.get("bot_name") == BOT_DRIFT_BALANCED_V95
        ]
        accuracy_v91 = [
            r for r in rows if r.get("bot_name") == BOT_DRIFT_ACCURACY_V91
        ]
        consensus_fallback = [
            r for r in rows if r.get("bot_name") == BOT_DRIFT_CONSENSUS_FALLBACK
        ]
        spread4_qualifiers = [
            r for r in spread4 if cls._threshold_flag(r, "would_accept_variant")
        ]
        flow15_qualifiers = [
            r for r in flow15 if cls._threshold_flag(r, "would_accept_variant")
        ]
        spread4_incremental = [
            r for r in spread4_qualifiers
            if cls._threshold_flag(r, "incremental_to_core")
        ]
        flow15_incremental = [
            r for r in flow15_qualifiers
            if cls._threshold_flag(r, "incremental_to_core")
        ]
        frozen_core_candidates = [
            r for r in core_candidates
            if cls._threshold_value(r, "rule_version")
            == DRIFT_CORE_RULE_VERSION
        ]
        frozen_core = [
            r for r in frozen_core_candidates
            if r.get("decision_status") == ACCEPTED
        ]
        core = [
            r for r in core_candidates if r.get("decision_status") == ACCEPTED
        ]
        addons = [
            r for r in addon_candidates if r.get("decision_status") == ACCEPTED
        ]
        latequal = [
            r for r in latequal_candidates if r.get("decision_status") == ACCEPTED
        ]
        bnb_quarantine_funnel = [
            r for r in frozen_core_candidates
            if str(r.get("asset") or "").upper() == "BNB"
        ]
        bnb_quarantine = [
            r for r in bnb_quarantine_funnel
            if cls._threshold_flag(r, "would_accept_core")
        ]
        addon_11m_quarantine = [
            r for r in addon_candidates
            if str(r.get("interval") or "").upper() == "11M"
            and cls._threshold_value(r, "quarantined_interval") == "11M"
        ]
        no_expansion_accepted = [
            r for r in no_expansion if r.get("decision_status") == ACCEPTED
        ]
        no_expansion_qualifiers = [
            r for r in no_expansion
            if cls._threshold_flag(r, "would_accept_variant")
        ]
        independent = core + latequal
        independent_candidates = core_candidates + latequal_candidates
        return {
            # Deployable performance views are ACCEPTED-only. Candidate/funnel
            # views below retain rejected and inconclusive rows for diagnosis.
            "independent_picks": cls._agg(independent, min_n),
            "base_13m": cls._agg(core, min_n),
            "flow_spread_13m": cls._agg(core, min_n),
            "frozen_core_accepted": cls._cohort_view(frozen_core, min_n),
            "core_funnel": cls._cohort_view(frozen_core_candidates, min_n),
            "bnb_quarantine": cls._cohort_view(bnb_quarantine, min_n),
            "bnb_quarantine_funnel": cls._cohort_view(
                bnb_quarantine_funnel, min_n
            ),
            "addon_11m_quarantine": cls._cohort_view(addon_11m_quarantine, min_n),
            "latequal_research": cls._cohort_view(latequal_candidates, min_n),
            "counterfactual_research": {
                "spread4": {
                    "funnel": cls._cohort_view(spread4, min_n),
                    "full": cls._cohort_view(spread4_qualifiers, min_n),
                    "incremental": cls._cohort_view(spread4_incremental, min_n),
                },
                "flow15": {
                    "funnel": cls._cohort_view(flow15, min_n),
                    "full": cls._cohort_view(flow15_qualifiers, min_n),
                    "incremental": cls._cohort_view(flow15_incremental, min_n),
                },
                "asymmetric_volume": {
                    "full": cls._cohort_view(asymmetric_volume, min_n),
                    "incremental": cls._cohort_view(
                        [
                            r for r in asymmetric_volume
                            if cls._threshold_flag(r, "incremental_to_core")
                        ],
                        min_n,
                    ),
                },
                "balanced_v95": {
                    "full": cls._cohort_view(balanced_v95, min_n),
                    "incremental": cls._cohort_view(
                        [
                            r for r in balanced_v95
                            if cls._threshold_flag(r, "incremental_to_core")
                        ],
                        min_n,
                    ),
                },
                "accuracy_v91": {
                    "full": cls._cohort_view(accuracy_v91, min_n),
                    "incremental": cls._cohort_view(
                        [
                            r for r in accuracy_v91
                            if cls._threshold_flag(r, "incremental_to_core")
                        ],
                        min_n,
                    ),
                },
                "consensus_fallback": {
                    "full": cls._cohort_view(consensus_fallback, min_n),
                },
                "no_expansion": {
                    "funnel": cls._cohort_view(no_expansion, min_n),
                    "qualifiers": cls._cohort_view(no_expansion_qualifiers, min_n),
                },
            },
            "independent_candidates": cls._agg(independent_candidates, min_n),
            "base_13m_all_candidates": cls._agg(core_candidates, min_n),
            "flow_spread_13m_all_candidates": cls._agg(core_candidates, min_n),
            "flow_spread_13m_by_status": cls._group(
                core_candidates, ("decision_status",), min_n
            ),
            "raw_13m_legacy_shadow": cls._agg(raw_shadow, min_n),
            "latequal_12m_11m": cls._agg(latequal, min_n),
            "latequal_12m_11m_all_candidates": cls._agg(
                latequal_candidates, min_n
            ),
            "correlated_addon_exposure": cls._agg(addons, min_n),
            "correlated_addon_candidates": cls._agg(addon_candidates, min_n),
            "total_exposure": cls._agg(independent + addons, min_n),
            "total_candidate_exposure": cls._agg(
                independent_candidates + addon_candidates, min_n
            ),
            "no_mirror_research": cls._agg(no_mirror, min_n),
            "no_mirror_by_asset": cls._group(no_mirror, ("asset",), min_n),
            "no_expansion": cls._agg(no_expansion, min_n),
            "no_expansion_accepted": cls._agg(no_expansion_accepted, min_n),
            "no_expansion_by_status": cls._group(
                no_expansion, ("decision_status",), min_n
            ),
            "no_expansion_by_asset_status": cls._group(
                no_expansion, ("asset", "decision_status"), min_n
            ),
            "all_drift_research_exposure": cls._agg(
                independent_candidates + addon_candidates + no_mirror + no_expansion,
                min_n,
            ),
            "accounting": (
                "raw drift_13m is the legacy shadow/control and is excluded from "
                "current independent performance; deployable base, independent, "
                "and total-exposure views count ACCEPTED decisions only. Explicit "
                "candidate/status views retain rejected and inconclusive rows. "
                "drift_flow_spread_13m owns base Telegram delivery; "
                "drift_addon_requal is correlated exposure; drift_no_mirror is the "
                "legacy NO shadow; drift_no_expansion is quarantined silent research. "
                "spread4, flow15, asymmetric-volume, balanced-V95, accuracy-V91, and "
                "consensus-fallback are silent counterfactual research and never count "
                "as independent or total exposure"
            ),
        }

    @classmethod
    def _coverage_group(
        cls,
        rows: Sequence[Mapping[str, Any]],
        keys: Sequence[str],
    ) -> dict[str, Any]:
        groups: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            label = "|".join(str(row.get(k) if row.get(k) is not None else "") for k in keys)
            groups.setdefault(label, []).append(row)
        return {
            label: cls._coverage_summary(group)
            for label, group in sorted(groups.items())
        }

    @staticmethod
    def _coverage_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        all_rows = list(rows)
        total = len(all_rows)

        def present(key: str) -> int:
            return sum(1 for row in all_rows if row.get(key) is not None)

        def any_present(keys: Sequence[str]) -> int:
            return sum(1 for row in all_rows if any(row.get(k) is not None for k in keys))

        def status_ok(key: str) -> int:
            return sum(1 for row in all_rows if str(row.get(key) or "").lower() == "ok")

        counts = {
            "entry_ask": present("entry_ask_cents"),
            "spread": present("spread_cents"),
            "kalshi_depth": any_present((
                "yes_bid_depth_contracts",
                "yes_ask_depth_contracts",
                "no_bid_depth_contracts",
                "no_ask_depth_contracts",
            )),
            "kalshi_taker_flow": present("kalshi_taker_net_yes_volume_15s"),
            "spot_depth": any_present((
                "spot_depth_imbalance",
                "spot_depth_bid_depth_levels",
                "spot_depth_ask_depth_levels",
            )),
            "spot_trade_flow_5s": present("spot_depth_trade_net_qty_5s"),
            "spot_trade_flow_15s": any_present((
                "spot_depth_trade_net_qty_15s",
                "spot_depth_trade_net_notional_15s",
            )),
            "spot_trade_flow_60s": any_present((
                "spot_depth_trade_net_qty_60s",
                "spot_depth_trade_net_notional_60s",
            )),
            "coinbase_l2_status": present("coinbase_l2_status"),
            "coinbase_l2_ok": status_ok("coinbase_l2_status"),
            "coinbase_l2_top12": present("coinbase_l2_top_12_imbalance_notional"),
            "coinbase_l2_top60": present("coinbase_l2_top_60_imbalance_notional"),
            "coinbase_l2_top250": present("coinbase_l2_top_250_imbalance_notional"),
            "coinbase_l2_depth_to_target": any_present((
                "coinbase_l2_distance_to_target_bps",
                "coinbase_l2_up_to_target_notional",
                "coinbase_l2_down_to_target_notional",
            )),
            "kraken_l3_status": present("kraken_l3_status"),
            "kraken_l3_ok": status_ok("kraken_l3_status"),
            "kraken_l3_depth": present("kraken_l3_depth_imbalance"),
            "kraken_l3_book_churn": any_present((
                "kraken_l3_cancel_to_add_15s",
                "kraken_l3_cancel_to_add_60s",
            )),
            "btc_context": any_present((
                "btc_ticker",
                "btc_depth_contracts",
                "btc_book_pressure_cents",
                "btc_dominant_side",
            )),
            "settlement": present("official_result"),
        }

        def rate(count: int) -> float | None:
            return None if total <= 0 else count / total

        return {
            "rows": total,
            "counts": counts,
            "rates": {key: rate(value) for key, value in counts.items()},
        }

    @classmethod
    def _tier_confirmation_system(
        cls,
        rows: Sequence[Mapping[str, Any]],
        min_n: int,
    ) -> dict[str, Any]:
        tier_rows = [r for r in rows if r.get("bot_name") == BOT_CONFIDENCE_TIER]

        def has(row: Mapping[str, Any], text: str) -> bool:
            return text in str(row.get("reason_codes") or "")

        def tier(value: str) -> list[Mapping[str, Any]]:
            return [r for r in tier_rows if str(r.get("tier") or "").upper() == value]

        def rejected_by(rows_in: Sequence[Mapping[str, Any]], needle: str) -> list[Mapping[str, Any]]:
            return [
                r for r in rows_in
                if r.get("decision_status") == REJECTED and has(r, needle)
            ]

        def saved_losses(rows_in: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
            return [
                r for r in rows_in
                if r.get("decision_status") == REJECTED
                and r.get("official_result") is not None
                and int(r.get("correct") or 0) == 0
            ]

        def skipped_winners(rows_in: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
            return [
                r for r in rows_in
                if r.get("decision_status") == REJECTED
                and r.get("official_result") is not None
                and int(r.get("correct") or 0) == 1
            ]

        def block(rows_in: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
            rows_list = list(rows_in)
            return {
                "all": cls._agg(rows_list, min_n),
                "accepted_confirmed": cls._agg(
                    [r for r in rows_list if r.get("decision_status") == ACCEPTED],
                    min_n,
                ),
                "research_inconclusive": cls._agg(
                    [r for r in rows_list if r.get("decision_status") == RESEARCH_ONLY],
                    min_n,
                ),
                "rejected_vetoed": cls._agg(
                    [r for r in rows_list if r.get("decision_status") == REJECTED],
                    min_n,
                ),
                "rejected_by_top12": cls._agg(
                    rejected_by(rows_list, "REJECTED_BY_COINBASE_TOP12_CONTRA"),
                    min_n,
                ),
                "rejected_by_combined_contra": cls._agg(
                    rejected_by(rows_list, "REJECTED_BY_COMBINED_CONTRA"),
                    min_n,
                ),
                "veto_saved_losses": cls._agg(saved_losses(rows_list), min_n),
                "veto_skipped_winners": cls._agg(skipped_winners(rows_list), min_n),
                "by_asset_side_status": cls._group(
                    rows_list,
                    ("asset", "side", "decision_status"),
                    min_n,
                ),
            }

        return {
            "all_tiers": block(tier_rows),
            "tier_a": block(tier(TIER_A)),
            "tier_b": block(tier(TIER_B)),
            "tier_c": block(tier(TIER_C)),
        }

    @classmethod
    def _bnb_system(
        cls,
        rows: Sequence[Mapping[str, Any]],
        min_n: int,
    ) -> dict[str, Any]:
        bnb_rows = [
            r for r in rows
            if str(r.get("asset") or "").upper() == "BNB"
            and r.get("bot_name") in {BOT_BNB_NO, BOT_BNB_YES_REVERSAL}
        ]
        no_rows = [r for r in bnb_rows if r.get("bot_name") == BOT_BNB_NO]
        reversal_rows = [
            r for r in bnb_rows
            if r.get("bot_name") == BOT_BNB_YES_REVERSAL
        ]
        vetoed = [
            r for r in no_rows
            if r.get("decision_status") == REJECTED
            and "BNB_NO_VETO_" in str(r.get("reason_codes") or "")
        ]
        no_veto_yes_would_have_won = [
            r for r in vetoed if str(r.get("official_result") or "").upper() == "YES"
        ]
        no_veto_no_would_have_won = [
            r for r in vetoed if str(r.get("official_result") or "").upper() == "NO"
        ]
        return {
            "bnb_rows": cls._agg(bnb_rows, min_n),
            "bnb_no_accepted": cls._agg(
                [r for r in no_rows if r.get("decision_status") == ACCEPTED],
                min_n,
            ),
            "bnb_no_vetoed": cls._agg(vetoed, min_n),
            "bnb_yes_reversal_candidates": cls._agg(reversal_rows, min_n),
            "no_veto_yes_would_have_won": cls._agg(no_veto_yes_would_have_won, min_n),
            "no_veto_no_would_have_won": cls._agg(no_veto_no_would_have_won, min_n),
            "yes_reversal_won": cls._agg(
                [r for r in reversal_rows if int(r.get("correct") or 0) == 1],
                min_n,
            ),
            "yes_reversal_lost": cls._agg(
                [
                    r for r in reversal_rows
                    if r.get("official_result") is not None
                    and int(r.get("correct") or 0) == 0
                ],
                min_n,
            ),
            "by_ticker_rule_side_status": cls._group(
                bnb_rows,
                ("ticker", "source_rule", "bot_name", "side", "decision_status"),
                min_n,
            ),
        }
