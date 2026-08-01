"""SQLite persistence for asset-specific strategy-bot decisions."""
from __future__ import annotations

from collections import Counter, defaultdict
import copy
import json
import math
import random
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .costs import (
    KALSHI_Q15_FEE_SCHEDULE_VERSION,
    RTI_EXECUTION_COST_MODEL_VERSION,
    kalshi_order_fee_cents,
    rti_simulated_execution,
    rti_simulated_net_pnl_cents,
)
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
    BOT_RTI_PATH_13M,
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
    RTI_PATH_13M_CHALLENGER_POLICY_VERSION,
    RTI_PATH_13M_COUNTERTREND_CHALLENGER_ID,
    RTI_PATH_13M_COUNTERTREND_POLICY_VERSION,
    RTI_PATH_13M_DELAYED_CONFIRM_60S_CHALLENGER_ID,
    RTI_PATH_13M_DELAYED_CONFIRM_60S_POLICY_VERSION,
    RTI_PATH_13M_DELAYED_CONFIRM_90S_CHALLENGER_ID,
    RTI_PATH_13M_DELAYED_CONFIRM_90S_POLICY_VERSION,
    RTI_PATH_13M_DELAYED_CONFIRM_CHALLENGER_ID,
    RTI_PATH_13M_DELAYED_CONFIRM_POLICY_VERSION,
    RTI_PATH_13M_DELAYED_FLIP_60S_CHALLENGER_ID,
    RTI_PATH_13M_DELAYED_FLIP_60S_POLICY_VERSION,
    RTI_PATH_13M_IMPULSE_CHALLENGER_ID,
    RTI_PATH_13M_IMPULSE_POLICY_VERSION,
    RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID,
    RTI_PATH_13M_MICROSTRUCTURE_V11_POLICY_VERSION,
    RTI_PATH_13M_PROBABILITY_V2_CHALLENGER_ID,
    RTI_PATH_13M_PROBABILITY_V2_POLICY_VERSION,
    RTI_PATH_13M_PROBABILITY_V3_CHALLENGER_ID,
    RTI_PATH_13M_PROBABILITY_V3_POLICY_VERSION,
    RTI_PATH_13M_SPOT_CONFIRM_CHALLENGER_ID,
    RTI_PATH_13M_SPOT_CONFIRM_POLICY_VERSION,
    RTI_EXACT_MICROSTRUCTURE_SCHEMA_V1,
    RTI_EXACT_MICROSTRUCTURE_SCHEMA_VERSION,
    SPOT_DEPTH_KEYS,
    STRATEGY_VERSION,
    TIER_A,
    TIER_B,
    TIER_C,
    BotDecision,
    source_rule,
    source_side,
)
from .rti_microstructure import (
    model_feature_window_coverage as v1_model_feature_window_coverage,
)
from .rti_microstructure_v2 import (
    model_feature_window_coverage as v2_model_feature_window_coverage,
)
from .rti_microstructure_v3 import (
    model_feature_window_coverage as v3_model_feature_window_coverage,
)
from .rti_microstructure_v4 import (
    DESIGN_ID as RTI_MICROSTRUCTURE_DESIGN_ID,
    DESIGN_SHA256 as RTI_MICROSTRUCTURE_DESIGN_SHA256,
    FEATURE_NAMES as RTI_MICROSTRUCTURE_FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION as RTI_MICROSTRUCTURE_FEATURE_SCHEMA_VERSION,
    model_feature_window_coverage as v4_model_feature_window_coverage,
)
from .rti_microstructure_v5 import (
    DESIGN_ID as RTI_DYNAMICS_DESIGN_ID,
    DESIGN_SHA256 as RTI_DYNAMICS_DESIGN_SHA256,
    FEATURE_NAMES as RTI_DYNAMICS_FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION as RTI_DYNAMICS_FEATURE_SCHEMA_VERSION,
    FIRST_ELIGIBLE_CLOSE_TIME as RTI_DYNAMICS_FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME as RTI_DYNAMICS_PROSPECTIVE_AFTER_CLOSE_TIME,
    model_feature_window_coverage as v5_model_feature_window_coverage,
)
from .rti_microstructure_v6 import (
    DESIGN_ID as RTI_LEAD_LAG_DESIGN_ID,
    DESIGN_SHA256 as RTI_LEAD_LAG_DESIGN_SHA256,
    FEATURE_NAMES as RTI_LEAD_LAG_FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION as RTI_LEAD_LAG_FEATURE_SCHEMA_VERSION,
    FIRST_ELIGIBLE_CLOSE_TIME as RTI_LEAD_LAG_FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME as RTI_LEAD_LAG_PROSPECTIVE_AFTER_CLOSE_TIME,
    model_feature_window_coverage as v6_model_feature_window_coverage,
)
from .rti_microstructure_v7 import (
    DESIGN_ID as RTI_CROSS_VENUE_DESIGN_ID,
    DESIGN_SHA256 as RTI_CROSS_VENUE_DESIGN_SHA256,
    FEATURE_NAMES as RTI_CROSS_VENUE_FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION as RTI_CROSS_VENUE_FEATURE_SCHEMA_VERSION,
    FIRST_ELIGIBLE_CLOSE_TIME as RTI_CROSS_VENUE_FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME as RTI_CROSS_VENUE_PROSPECTIVE_AFTER_CLOSE_TIME,
    model_feature_window_coverage as v7_model_feature_window_coverage,
)
from .rti_microstructure_v8 import (
    DESIGN_ID as RTI_INDEPENDENT_VENUE_DESIGN_ID,
    DESIGN_SHA256 as RTI_INDEPENDENT_VENUE_DESIGN_SHA256,
    FEATURE_NAMES as RTI_INDEPENDENT_VENUE_FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION as RTI_INDEPENDENT_VENUE_FEATURE_SCHEMA_VERSION,
    FIRST_ELIGIBLE_CLOSE_TIME as RTI_INDEPENDENT_VENUE_FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME as RTI_INDEPENDENT_VENUE_PROSPECTIVE_AFTER_CLOSE_TIME,
    model_feature_window_coverage as v8_model_feature_window_coverage,
)
from .rti_microstructure_v9 import (
    DESIGN_ID as RTI_INDEPENDENT_MICROSTRUCTURE_DESIGN_ID,
    DESIGN_SHA256 as RTI_INDEPENDENT_MICROSTRUCTURE_DESIGN_SHA256,
    FEATURE_NAMES as RTI_INDEPENDENT_MICROSTRUCTURE_FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION as RTI_INDEPENDENT_MICROSTRUCTURE_FEATURE_SCHEMA_VERSION,
    FIRST_ELIGIBLE_CLOSE_TIME as RTI_INDEPENDENT_MICROSTRUCTURE_FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME as RTI_INDEPENDENT_MICROSTRUCTURE_PROSPECTIVE_AFTER_CLOSE_TIME,
    model_feature_window_coverage as v9_model_feature_window_coverage,
)
from .rti_microstructure_v10 import (
    DESIGN_ID as RTI_COMPACT_MICROSTRUCTURE_DESIGN_ID,
    DESIGN_SHA256 as RTI_COMPACT_MICROSTRUCTURE_DESIGN_SHA256,
    FEATURE_NAMES as RTI_COMPACT_MICROSTRUCTURE_FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION as RTI_COMPACT_MICROSTRUCTURE_FEATURE_SCHEMA_VERSION,
    FIRST_ELIGIBLE_CLOSE_TIME as RTI_COMPACT_MICROSTRUCTURE_FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME as RTI_COMPACT_MICROSTRUCTURE_PROSPECTIVE_AFTER_CLOSE_TIME,
    model_feature_window_coverage as v10_model_feature_window_coverage,
)
from .rti_microstructure_v11_identity import (
    DESIGN_ID as RTI_MICROSTRUCTURE_V11_DESIGN_ID,
    DESIGN_SHA256 as RTI_MICROSTRUCTURE_V11_DESIGN_SHA256,
    EVALUATION_PROTOCOL_ID as RTI_MICROSTRUCTURE_V11_PROTOCOL_ID,
    EVALUATION_PROTOCOL_SHA256 as RTI_MICROSTRUCTURE_V11_PROTOCOL_SHA256,
    PROSPECTIVE_BOOTSTRAP_CLUSTER_KEY as RTI_V11_BOOTSTRAP_CLUSTER_KEY,
    PROSPECTIVE_BOOTSTRAP_CONFIDENCE_LEVEL as RTI_V11_BOOTSTRAP_CONFIDENCE_LEVEL,
    PROSPECTIVE_BOOTSTRAP_RANDOM_SEED as RTI_V11_BOOTSTRAP_RANDOM_SEED,
    PROSPECTIVE_BOOTSTRAP_RESAMPLES as RTI_V11_BOOTSTRAP_RESAMPLES,
    PROSPECTIVE_BOOTSTRAP_VERSION as RTI_V11_BOOTSTRAP_VERSION,
    PROSPECTIVE_MIN_MEAN_BRIER_IMPROVEMENT as RTI_V11_MIN_BRIER_IMPROVEMENT,
    PROSPECTIVE_MIN_MEAN_LOG_LOSS_IMPROVEMENT as RTI_V11_MIN_LOG_LOSS_IMPROVEMENT,
)
from .rti_microstructure_v11 import (
    DESIGN_ID as RTI_CROSS_ASSET_REGIME_DESIGN_ID,
    DESIGN_SHA256 as RTI_CROSS_ASSET_REGIME_DESIGN_SHA256,
    FEATURE_NAMES as RTI_CROSS_ASSET_REGIME_FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION as RTI_CROSS_ASSET_REGIME_FEATURE_SCHEMA_VERSION,
    FIRST_ELIGIBLE_CLOSE_TIME as RTI_CROSS_ASSET_REGIME_FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME as RTI_CROSS_ASSET_REGIME_PROSPECTIVE_AFTER_CLOSE_TIME,
    model_feature_window_coverage as v11_model_feature_window_coverage,
)
from .rti_microstructure_v12 import (
    DESIGN_ID as RTI_ORTHOGONAL_COMPACT_DESIGN_ID,
    DESIGN_SHA256 as RTI_ORTHOGONAL_COMPACT_DESIGN_SHA256,
    FEATURE_NAMES as RTI_ORTHOGONAL_COMPACT_FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION as RTI_ORTHOGONAL_COMPACT_FEATURE_SCHEMA_VERSION,
    FIRST_ELIGIBLE_CLOSE_TIME as RTI_ORTHOGONAL_COMPACT_FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME as RTI_ORTHOGONAL_COMPACT_PROSPECTIVE_AFTER_CLOSE_TIME,
    model_feature_window_coverage as v12_model_feature_window_coverage,
)
from .rti_microstructure_v13 import (
    DESIGN_ID as RTI_COHORT_CONDITIONED_COMPACT_DESIGN_ID,
    DESIGN_SHA256 as RTI_COHORT_CONDITIONED_COMPACT_DESIGN_SHA256,
    FEATURE_NAMES as RTI_COHORT_CONDITIONED_COMPACT_FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION as RTI_COHORT_CONDITIONED_COMPACT_FEATURE_SCHEMA_VERSION,
    FIRST_ELIGIBLE_CLOSE_TIME as RTI_COHORT_CONDITIONED_COMPACT_FIRST_ELIGIBLE_CLOSE_TIME,
    PROSPECTIVE_AFTER_CLOSE_TIME as RTI_COHORT_CONDITIONED_COMPACT_PROSPECTIVE_AFTER_CLOSE_TIME,
    model_feature_window_coverage as v13_model_feature_window_coverage,
)
from .rti_microstructure_extension import (
    EXTENSION_SCHEMA_VERSION as RTI_MICROSTRUCTURE_EXTENSION_SCHEMA_VERSION,
    extension_window_coverage,
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


def _prospective_net_pnl_cents(
    entry_ask_cents: float | int | None,
    correct: bool,
    threshold_json: Any,
) -> float | None:
    """Apply cohort-declared simulation costs without changing legacy books."""
    ask = _num(entry_ask_cents)
    if ask is None:
        return None
    if isinstance(threshold_json, Mapping):
        profile = threshold_json
    else:
        try:
            profile = json.loads(str(threshold_json or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            profile = {}
    if not isinstance(profile, Mapping):
        profile = {}
    contracts = _num(profile.get("sim_contracts"))
    slippage = _num(profile.get("slippage_cents_per_contract"))
    if contracts is None and slippage is None:
        return net_pnl_cents(ask, correct)
    contract_count = max(1, int(contracts or 1))
    slip = max(0.0, float(slippage or 0.0))
    return rti_simulated_net_pnl_cents(
        ask,
        correct,
        contract_count,
        slip,
    )


def _resolved_row_pnl_cents(row: Mapping[str, Any]) -> float | None:
    """Reconstruct an RTI row's P/L from immutable side/result/entry evidence."""
    official = str(row.get("official_result") or "").upper()
    side = str(row.get("side") or "").upper()
    if official not in {"YES", "NO"} or side not in {"YES", "NO"}:
        return None
    return _prospective_net_pnl_cents(
        row.get("entry_ask_cents"),
        side == official,
        row.get("threshold_json"),
    )


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
            sources,
            "evidence_as_of",
            "evidence_timestamp",
            "feature_captured_at",
            "rti_evaluated_at",
            "rti_confirm_evaluated_at",
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
        "kalshi_microstructure_schema_version",
        "kalshi_microstructure_extension_schema_version",
        "kalshi_microstructure_time_basis",
        "spot_mid_path_schema_version",
        "spot_mid_path_time_basis",
        "spot_mid_path_missing_reason_15s",
        "spot_mid_path_missing_reason_60s",
        "rti_spot_lead_lag_schema_version",
        "rti_spot_lead_lag_status",
        "rti_spot_lead_lag_missing_reason",
        "rti_cross_venue_schema_version",
        "rti_cross_venue_time_basis",
        "rti_cross_venue_status",
        "rti_cross_venue_missing_reason",
        "rti_cross_venue_primary_source",
        "rti_cross_venue_coinbase_status",
        "rti_cross_venue_coinbase_missing_reason",
        "rti_cross_venue_coinbase_symbol",
        "rti_cross_venue_kraken_status",
        "rti_cross_venue_kraken_missing_reason",
        "rti_cross_venue_kraken_symbol",
        "rti_independent_venue_schema_version",
        "rti_independent_venue_time_basis",
        "rti_independent_venue_status",
        "rti_independent_venue_missing_reason",
        "rti_independent_venue_coinbase_status",
        "rti_independent_venue_coinbase_missing_reason",
        "rti_independent_venue_coinbase_symbol",
        "rti_independent_venue_kraken_status",
        "rti_independent_venue_kraken_missing_reason",
        "rti_independent_venue_kraken_symbol",
        "rti_independent_microstructure_schema_version",
        "rti_independent_microstructure_time_basis",
        "rti_independent_microstructure_status",
        "rti_independent_microstructure_missing_reason",
        "rti_independent_microstructure_coinbase_status",
        "rti_independent_microstructure_coinbase_missing_reason",
        "rti_independent_microstructure_coinbase_symbol",
        "rti_independent_microstructure_kraken_status",
        "rti_independent_microstructure_kraken_missing_reason",
        "rti_independent_microstructure_kraken_symbol",
        "rti_independent_microstructure_kraken_partial_fill_flow_schema_version",
        "rti_independent_path_schema_version",
        "rti_independent_path_time_basis",
        "rti_independent_path_design_id",
        "rti_independent_path_design_sha256",
        "rti_independent_path_status",
        "rti_independent_path_missing_reason",
        "rti_independent_path_evidence_json",
        "rti_independent_path_evidence_sha256",
        "rti_independent_path_coinbase_status",
        "rti_independent_path_coinbase_missing_reason",
        "rti_independent_path_coinbase_symbol",
        "rti_independent_path_kraken_status",
        "rti_independent_path_kraken_missing_reason",
        "rti_independent_path_kraken_symbol",
        "rti_cross_asset_schema_version",
        "rti_cross_asset_time_basis",
        "rti_cross_asset_status",
        "rti_cross_asset_missing_reason",
    }
    if name in text_names:
        return "TEXT"
    if (
        name.endswith("_visible")
        or name == "kalshi_history_count_capped"
        or "_window_complete_" in name
    ):
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
        self._rti_scoreboard_generation = 0
        self._rti_scoreboard_cache: dict[
            tuple[str, int], tuple[int, int, dict[str, Any]]
        ] = {}
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
        for key in KALSHI_FLOW_KEYS:
            added.setdefault(key, _feature_column_type(key))
        for key in KRAKEN_L3_KEYS:
            added.setdefault(key, _feature_column_type(key))
        for key in SPOT_DEPTH_KEYS:
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
                if row.get("bot_name") == BOT_RTI_PATH_13M:
                    self._rti_scoreboard_generation += 1
                    self._rti_scoreboard_cache.clear()
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
            BOT_RTI_PATH_13M,
        )
        placeholders = ",".join("?" for _ in drift_bots)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, strategy_version, bot_name, window_key, ticker, "
                "notification_status FROM strategy_bot_decisions "
                "WHERE (source_system='drift_shadow' OR bot_name=?) AND paper_only=1 "
                "AND notification_status='QUEUED_RETRY' "
                f"AND bot_name IN ({placeholders}) "
                "ORDER BY COALESCE(notified_at, created_at) ASC, id ASC LIMIT ?",
                (BOT_RTI_PATH_13M, *drift_bots, bounded),
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

    def rti_delayed_recovery_rows(
        self,
        *,
        ticker: str,
        close_time: float,
    ) -> list[dict[str, Any]]:
        """Return one RTI ticker/close lineage for restart recovery.

        This is a bounded read of the durable strategy ledger.  The exact 13M
        parent and any already-recorded delayed intervals are returned together
        so the scheduler can recreate only missing future work.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM strategy_bot_decisions "
                "WHERE bot_name=? AND source_system='rti_path_13m' "
                "AND ticker=? AND ABS(close_time - ?) <= 0.001 "
                "AND interval IN ('13M', '12M30S', '12M', '11M30S') "
                "ORDER BY id ASC",
                (BOT_RTI_PATH_13M, str(ticker), float(close_time)),
            ).fetchall()
        return [dict(row) for row in rows]

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
                "SELECT id, side, entry_ask_cents, threshold_json, close_time "
                "FROM strategy_bot_decisions "
                "WHERE source_system=? AND source_model_version=? AND ticker=? "
                "AND official_result IS NULL",
                (source_system, source_model_version, ticker),
            ).fetchall()
            graded = 0
            for r in rows:
                row_resolved_at = max(
                    ts, float(r["close_time"] or ts),
                )
                side = str(r["side"] or "").upper()
                correct = side == official
                pnl = _prospective_net_pnl_cents(
                    r["entry_ask_cents"], correct, r["threshold_json"]
                )
                self._conn.execute(
                    "UPDATE strategy_bot_decisions SET official_result=?, resolved_at=?, "
                    "correct=?, hypothetical_pnl_cents=? WHERE id=?",
                    (
                        official,
                        row_resolved_at,
                        1 if correct else 0,
                        pnl,
                        r["id"],
                    ),
                )
                graded += 1
            self._conn.commit()
            if graded:
                self._rti_scoreboard_generation += 1
                self._rti_scoreboard_cache.clear()
        return graded

    def resolve_ticker(
        self,
        *,
        ticker: str,
        official_result: str,
        now: float | None = None,
    ) -> int:
        """Resolve every pending PAPER side-ledger row for one contract.

        A Kalshi contract has one official result regardless of which research
        source or frozen rule recorded it.  The older source-version-scoped
        resolver remains available for compatibility, while this contract-
        scoped path prevents a transient side-lane exception from leaving only
        some models or assets ungraded forever.
        """
        official = str(official_result or "").upper()
        contract = str(ticker or "")
        if official not in {"YES", "NO"} or not contract:
            return 0
        ts = time.time() if now is None else float(now)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, side, entry_ask_cents, threshold_json, close_time "
                "FROM strategy_bot_decisions WHERE ticker=? "
                "AND official_result IS NULL",
                (contract,),
            ).fetchall()
            graded = 0
            for row in rows:
                row_resolved_at = max(
                    ts, float(row["close_time"] or ts),
                )
                side = str(row["side"] or "").upper()
                correct = side == official
                pnl = _prospective_net_pnl_cents(
                    row["entry_ask_cents"], correct, row["threshold_json"]
                )
                self._conn.execute(
                    "UPDATE strategy_bot_decisions SET official_result=?, "
                    "resolved_at=?, correct=?, hypothetical_pnl_cents=? "
                    "WHERE id=? AND official_result IS NULL",
                    (
                        official,
                        row_resolved_at,
                        1 if correct else 0,
                        pnl,
                        row["id"],
                    ),
                )
                graded += 1
            self._conn.commit()
            if graded:
                self._rti_scoreboard_generation += 1
                self._rti_scoreboard_cache.clear()
        return graded

    def unresolved_rti_tickers(
        self,
        *,
        now: float | None = None,
        limit: int = 500,
    ) -> list[str]:
        """Bounded contract list for settlement-only startup reconciliation."""
        current = time.time() if now is None else float(now)
        bounded = max(1, min(int(limit), 5000))
        with self._lock:
            rows = self._conn.execute(
                "SELECT ticker, MIN(close_time) AS first_close FROM "
                "strategy_bot_decisions WHERE bot_name=? "
                "AND official_result IS NULL AND ticker IS NOT NULL "
                "AND close_time IS NOT NULL AND close_time<=? "
                "GROUP BY ticker ORDER BY first_close ASC LIMIT ?",
                (BOT_RTI_PATH_13M, current, bounded),
            ).fetchall()
        return [str(row["ticker"]) for row in rows if row["ticker"]]

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

    def rti_path_challenger_scoreboard(
        self,
        strategy_version: str = STRATEGY_VERSION,
        *,
        min_n: int = 30,
    ) -> dict[str, Any]:
        """Return the prospective RTI book with exact mutation-aware caching.

        Building every frozen feature lineage is intentionally expensive.  A
        cache is safe because every in-process RTI insert/grade increments the
        generation, while SQLite ``data_version`` detects external commits.
        Deep copies prevent health renderers from mutating the cached truth.
        """
        cache_key = (str(strategy_version), int(min_n))
        with self._lock:
            generation = self._rti_scoreboard_generation
            data_version = int(
                self._conn.execute("PRAGMA data_version").fetchone()[0]
            )
            cached = self._rti_scoreboard_cache.get(cache_key)
            if (
                cached is not None
                and cached[0] == generation
                and cached[1] == data_version
            ):
                return copy.deepcopy(cached[2])
            # The decision books need only a small subset of the frozen JSON
            # profile.  Reading every full feature blob here made health
            # rebuilds spend tens of seconds materializing evidence that only
            # the feature-coverage audit consumes.
            compact_profile_keys = (
                "challenger_policy_version",
                "sim_contracts",
                "slippage_cents_per_contract",
                "rti_risk_policy_version",
                "rti_reversal_risk_class",
                "rti_settlement_average_risk_class",
                "rti_path_regime_class",
                "rti_market_agreement_class",
                "rti_confirm_original_strict_accepted",
                "rti_confirm_original_row_id",
                "rule_version",
            )
            book_columns = (
                "id",
                "created_at",
                "bot_name",
                "decision_status",
                "asset",
                "side",
                "interval",
                "ticker",
                "close_time",
                "entry_ask_cents",
                "official_result",
                "correct",
                "hypothetical_pnl_cents",
                "threshold_json",
            )
            book_select = ", ".join(f'"{column}"' for column in book_columns)
            extension_columns = tuple(
                f"kalshi_{metric}_{horizon}s"
                for horizon in (5, 15, 30, 60)
                for metric in (
                    "book_add_volume_yes",
                    "book_remove_volume_yes",
                    "book_add_volume_no",
                    "book_remove_volume_no",
                    "microprice_change_cents",
                    "microprice_range_cents",
                    "microprice_variation_cents",
                    "microprice_trend_efficiency",
                    "trade_yes_price_change_cents",
                    "trade_yes_price_range_cents",
                    "trade_yes_price_variation_cents",
                    "trade_yes_price_trend_efficiency",
                )
            )
            feature_columns = tuple(dict.fromkeys((
                "id",
                "bot_name",
                "interval",
                "record_kind",
                "asset",
                "close_time",
                "entry_ask_cents",
                "spread_cents",
                "official_result",
                "source_captured_at",
                "evidence_as_of",
                "kalshi_microstructure_schema_version",
                "kalshi_microstructure_extension_schema_version",
                "kalshi_microstructure_time_basis",
                "kalshi_history_count_capped",
                *(f"kalshi_microstructure_window_complete_{horizon}s"
                  for horizon in (5, 15, 30, 60)),
                *extension_columns,
                *(f"kalshi_trade_count_{horizon}s"
                  for horizon in (5, 15, 30, 60)),
                *(f"kalshi_trade_yes_vwap_cents_{horizon}s"
                  for horizon in (5, 15, 30, 60)),
                "spot_mid_path_schema_version",
                "rti_spot_lead_lag_schema_version",
                "rti_cross_venue_schema_version",
                "rti_independent_venue_schema_version",
                "rti_independent_microstructure_schema_version",
                "rti_cross_asset_schema_version",
                *KALSHI_DEPTH_KEYS,
                *KALSHI_FLOW_KEYS,
                *SPOT_DEPTH_KEYS,
            )))
            feature_select = ", ".join(
                f'"{column}"' for column in feature_columns
            )
            rows = self._conn.execute(
                f"SELECT {book_select} "
                "FROM strategy_bot_decisions "
                "WHERE strategy_version=? AND bot_name=? "
                # The forward scoreboard ignores pre-challenger profiles in
                # Python.  Filtering them before materializing their large JSON
                # evidence avoids parsing tens of thousands of legacy rows on
                # every health refresh while preserving every row any current
                # book, counterfactual, feature lineage, or risk diagnostic can
                # consume (all are recorded in the challengers-era profile).
                "AND threshold_json IS NOT NULL "
                "AND instr(threshold_json, '\"challengers\"') > 0 "
                "ORDER BY id",
                (strategy_version, BOT_RTI_PATH_13M),
            ).fetchall()
            feature_rows = self._conn.execute(
                f"SELECT {feature_select}, "
                "json_remove(threshold_json, '$.challengers', "
                "'$.challenger_review') AS threshold_json "
                "FROM strategy_bot_decisions "
                "WHERE strategy_version=? AND bot_name=? "
                "AND interval='13M' "
                "AND record_kind='RTI_PATH_13M_PROSPECTIVE_EXACT' "
                "AND threshold_json IS NOT NULL "
                "AND instr(threshold_json, '\"challengers\"') > 0 "
                "ORDER BY id",
                (strategy_version, BOT_RTI_PATH_13M),
            ).fetchall()
        materialized_rows: list[dict[str, Any]] = []
        for raw_row in rows:
            row = dict(raw_row)
            raw_profile = row.get("threshold_json")
            try:
                decoded = json.loads(str(raw_profile or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                decoded = {}
            source_profile = decoded if isinstance(decoded, Mapping) else {}
            profile: dict[str, Any] = {}
            for key in ("challengers", *compact_profile_keys):
                if key in source_profile:
                    profile[key] = source_profile[key]
            row["threshold_json"] = profile
            materialized_rows.append(row)
        materialized_feature_rows: list[dict[str, Any]] = []
        for raw_row in feature_rows:
            row = dict(raw_row)
            raw_profile = row.get("threshold_json")
            try:
                profile = json.loads(str(raw_profile or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                profile = {}
            row["threshold_json"] = (
                profile if isinstance(profile, Mapping) else {}
            )
            materialized_feature_rows.append(row)
        result = self._rti_path_challenger_system(
            materialized_rows,
            min_n,
            feature_rows=materialized_feature_rows,
        )
        with self._lock:
            current_data_version = int(
                self._conn.execute("PRAGMA data_version").fetchone()[0]
            )
            if (
                self._rti_scoreboard_generation == generation
                and current_data_version == data_version
            ):
                self._rti_scoreboard_cache[cache_key] = (
                    generation, data_version, copy.deepcopy(result),
                )
        return result

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
            "rti_path_challengers": self._rti_path_challenger_system(rows, min_n),
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

    @classmethod
    def _agg(
        cls,
        rows: Sequence[Mapping[str, Any]],
        min_n: int,
    ) -> dict[str, Any]:
        all_rows = list(rows)
        settled = [r for r in all_rows if r.get("official_result") is not None]

        def _audited_correct(row: Mapping[str, Any]) -> bool | None:
            official = str(row.get("official_result") or "").upper()
            side = str(row.get("side") or "").upper()
            if official in {"YES", "NO"} and side in {"YES", "NO"}:
                return side == official
            stored = _num(row.get("correct"))
            if stored in {0.0, 1.0}:
                return bool(stored)
            return None

        def _audited_pnl(row: Mapping[str, Any]) -> float | None:
            correct = _audited_correct(row)
            if correct is None:
                return None
            contracts = _num(cls._threshold_value(row, "sim_contracts"))
            slippage = _num(
                cls._threshold_value(row, "slippage_cents_per_contract")
            )
            if contracts is None and slippage is None:
                return _num(row.get("hypothetical_pnl_cents"))
            return _prospective_net_pnl_cents(
                row.get("entry_ask_cents"),
                correct,
                row.get("threshold_json"),
            )

        audited_correct = [_audited_correct(row) for row in settled]
        label_scoreable = [value for value in audited_correct if value is not None]
        right = sum(bool(value) for value in label_scoreable)
        audited_pnls = [_audited_pnl(row) for row in settled]
        pnls = [float(value) for value in audited_pnls if value is not None]
        stored_pnls = [
            float(value)
            for row in settled
            for value in [_num(row.get("hypothetical_pnl_cents"))]
            if value is not None
        ]
        label_integrity_failures = sum(
            1
            for row, audited in zip(settled, audited_correct)
            if audited is not None
            and _num(row.get("correct")) in {0.0, 1.0}
            and bool(_num(row.get("correct"))) != audited
        )
        n = len(settled)
        accuracy_n = len(label_scoreable)
        wilson_low: float | None = None
        wilson_high: float | None = None
        if accuracy_n > 0:
            z = 1.959963984540054
            p = right / accuracy_n
            denominator = 1.0 + (z * z / accuracy_n)
            center = (p + (z * z / (2.0 * accuracy_n))) / denominator
            margin = (
                z
                * math.sqrt(
                    (p * (1.0 - p) / accuracy_n)
                    + (z * z / (4.0 * accuracy_n * accuracy_n))
                )
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
            pnl = _audited_pnl(row)
            if pnl is not None:
                ordered_pnls.append(pnl)
        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for pnl in ordered_pnls:
            equity += pnl
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)

        fee_breakevens: list[float] = []
        fee_slippage_breakevens: list[float] = []
        for row in settled:
            ask = _num(row.get("entry_ask_cents"))
            if ask is None:
                continue
            contracts = _num(cls._threshold_value(row, "sim_contracts"))
            slippage = _num(
                cls._threshold_value(row, "slippage_cents_per_contract")
            )
            if contracts is None and slippage is None:
                fee = kalshi_fee_cents(ask)
                fee_per_contract = None if fee is None else float(fee)
                fee_slippage_per_contract = fee_per_contract
                fill = ask
            else:
                contract_count = max(1, int(contracts or 1))
                quote_execution = rti_simulated_execution(
                    ask,
                    contract_count,
                    0.0,
                )
                full_execution = rti_simulated_execution(
                    ask,
                    contract_count,
                    max(0.0, float(slippage or 0.0)),
                )
                fee_per_contract = (
                    None
                    if quote_execution is None
                    else float(quote_execution["fee_cents_per_contract"])
                )
                fee_slippage_per_contract = (
                    None
                    if full_execution is None
                    else float(full_execution["fee_cents_per_contract"])
                )
                fill = (
                    None
                    if full_execution is None
                    else float(full_execution["simulated_fill_cents"])
                )
            if fee_per_contract is not None:
                fee_breakevens.append((ask + fee_per_contract) / 100.0)
            if fee_slippage_per_contract is not None and fill is not None:
                fee_slippage_breakevens.append(
                    (fill + fee_slippage_per_contract) / 100.0
                )
        avg_pnl = None if not pnls else sum(pnls) / len(pnls)
        net_pnl = None if not pnls else sum(pnls)
        stored_net_pnl = None if not stored_pnls else sum(stored_pnls)
        complete_cost_evidence = bool(
            n > 0
            and len(pnls) == n
            and len(fee_slippage_breakevens) == n
            and accuracy_n == n
            and label_integrity_failures == 0
        )
        return {
            "rows": len(all_rows),
            "resolved": n,
            "label_scoreable_resolved": accuracy_n,
            "pnl_scoreable_resolved": len(pnls),
            "unscoreable_resolved": n - len(pnls),
            "cost_evidence_complete": complete_cost_evidence,
            "label_integrity_failures": label_integrity_failures,
            "correct": right,
            "accuracy": None if accuracy_n <= 0 else right / accuracy_n,
            "accuracy_wilson_95_low": wilson_low,
            "accuracy_wilson_95_high": wilson_high,
            "wilson_95_low": wilson_low,
            "wilson_95_high": wilson_high,
            "avg_pnl_cents": avg_pnl,
            "net_pnl_cents": net_pnl,
            "stored_net_pnl_cents": stored_net_pnl,
            "cost_audit_delta_cents": (
                None
                if net_pnl is None or stored_net_pnl is None
                else net_pnl - stored_net_pnl
            ),
            "fee_schedule_version": KALSHI_Q15_FEE_SCHEDULE_VERSION,
            "execution_cost_model_version": RTI_EXECUTION_COST_MODEL_VERSION,
            "fee_adjusted_ev_cents": avg_pnl,
            "fee_adjusted_net_pnl_cents": net_pnl,
            "max_cumulative_drawdown_cents": max_drawdown if ordered_pnls else None,
            "avg_fee_adjusted_breakeven_rate": (
                None
                if not fee_breakevens
                else sum(fee_breakevens) / len(fee_breakevens)
            ),
            "avg_fee_slippage_adjusted_breakeven_rate": (
                None
                if not fee_slippage_breakevens
                else sum(fee_slippage_breakevens)
                / len(fee_slippage_breakevens)
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
        if isinstance(raw, Mapping):
            return raw.get(key)
        try:
            data = json.loads(str(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return data.get(key) if isinstance(data, Mapping) else None

    @classmethod
    def _threshold_flag(cls, row: Mapping[str, Any], key: str) -> bool:
        return bool(cls._threshold_value(row, key))

    @staticmethod
    def _probability_score_metrics(
        rows: Sequence[Mapping[str, Any]],
        min_n: int,
    ) -> dict[str, Any]:
        """Score immutable point-in-time YES probabilities against outcomes.

        These metrics deliberately ignore whether the value book accepted the
        trade.  Selection P/L answers a different question from whether the
        probability model was calibrated and directionally useful.
        """
        materialized = list(rows)
        n = len(materialized)

        def _wilson(correct: int, count: int) -> tuple[float | None, float | None]:
            if count <= 0:
                return None, None
            z = 1.959963984540054
            p = correct / count
            denominator = 1.0 + (z * z / count)
            center = (p + (z * z / (2.0 * count))) / denominator
            margin = (
                z
                * math.sqrt(
                    (p * (1.0 - p) / count)
                    + (z * z / (4.0 * count * count))
                )
                / denominator
            )
            return max(0.0, center - margin), min(1.0, center + margin)

        expected_assets = {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"}
        assets_by_close: dict[float, set[str]] = defaultdict(set)
        for row in materialized:
            close_time = _num(row.get("close_time"))
            if close_time is not None:
                assets_by_close[close_time].add(str(row.get("asset") or "").upper())
        asset_counts = [len(assets) for assets in assets_by_close.values()]
        complete_windows = sum(
            1 for assets in assets_by_close.values() if expected_assets.issubset(assets)
        )

        if n <= 0:
            return {
                "n": 0,
                "correct": 0,
                "accuracy": None,
                "wilson_95_low": None,
                "wilson_95_high": None,
                "brier_score": None,
                "log_loss": None,
                "mean_yes_probability": None,
                "observed_yes_rate": None,
                "calibration_bias": None,
                "market_n": 0,
                "market_accuracy": None,
                "market_brier_score": None,
                "market_log_loss": None,
                "paired_model_brier_score": None,
                "paired_model_log_loss": None,
                "brier_skill_vs_market": None,
                "log_loss_delta_vs_market": None,
                "accuracy_delta_vs_market": None,
                "close_windows": 0,
                "complete_seven_asset_close_windows": 0,
                "partial_close_windows": 0,
                "assets_per_close_window_min": None,
                "assets_per_close_window_max": None,
                "unique_probabilities_6dp": 0,
                "saturated_probability_rows": 0,
                "out_of_distribution_rows": 0,
                "calibration_deciles": {},
                "provisional": True,
            }

        epsilon = 1.0e-15

        def _log_loss(probability: float, label: float) -> float:
            clipped = min(1.0 - epsilon, max(epsilon, probability))
            return -(
                label * math.log(clipped)
                + (1.0 - label) * math.log(1.0 - clipped)
            )

        correct = sum(
            1
            for row in materialized
            if (float(row["yes_probability"]) >= 0.5)
            == bool(int(row["label_yes"]))
        )
        wilson_low, wilson_high = _wilson(correct, n)
        probabilities = [float(row["yes_probability"]) for row in materialized]
        labels = [float(row["label_yes"]) for row in materialized]
        brier = sum((p - y) ** 2 for p, y in zip(probabilities, labels)) / n
        log_loss = sum(_log_loss(p, y) for p, y in zip(probabilities, labels)) / n
        mean_probability = sum(probabilities) / n
        observed_yes = sum(labels) / n

        paired = [
            row for row in materialized if _num(row.get("market_yes_probability")) is not None
        ]
        market_n = len(paired)
        market_accuracy: float | None = None
        market_brier: float | None = None
        market_log_loss: float | None = None
        paired_model_brier: float | None = None
        paired_model_log_loss: float | None = None
        brier_skill: float | None = None
        log_loss_delta: float | None = None
        accuracy_delta: float | None = None
        if paired:
            market_correct = sum(
                1
                for row in paired
                if (float(row["market_yes_probability"]) >= 0.5)
                == bool(int(row["label_yes"]))
            )
            model_correct_paired = sum(
                1
                for row in paired
                if (float(row["yes_probability"]) >= 0.5)
                == bool(int(row["label_yes"]))
            )
            market_accuracy = market_correct / market_n
            accuracy_delta = (model_correct_paired / market_n) - market_accuracy
            market_brier = sum(
                (float(row["market_yes_probability"]) - float(row["label_yes"])) ** 2
                for row in paired
            ) / market_n
            paired_model_brier = sum(
                (float(row["yes_probability"]) - float(row["label_yes"])) ** 2
                for row in paired
            ) / market_n
            market_log_loss = sum(
                _log_loss(
                    float(row["market_yes_probability"]),
                    float(row["label_yes"]),
                )
                for row in paired
            ) / market_n
            paired_model_log_loss = sum(
                _log_loss(
                    float(row["yes_probability"]),
                    float(row["label_yes"]),
                )
                for row in paired
            ) / market_n
            if market_brier > 0.0:
                brier_skill = 1.0 - (paired_model_brier / market_brier)
            log_loss_delta = paired_model_log_loss - market_log_loss

        calibration_bins: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
        for row in materialized:
            probability = float(row["yes_probability"])
            calibration_bins[min(9, int(probability * 10.0))].append(row)
        calibration_deciles = {}
        for bucket, bucket_rows in sorted(calibration_bins.items()):
            bucket_n = len(bucket_rows)
            mean_predicted = sum(
                float(row["yes_probability"]) for row in bucket_rows
            ) / bucket_n
            bucket_observed = sum(
                float(row["label_yes"]) for row in bucket_rows
            ) / bucket_n
            calibration_deciles[f"{bucket / 10.0:.1f}-{(bucket + 1) / 10.0:.1f}"] = {
                "n": bucket_n,
                "mean_yes_probability": mean_predicted,
                "observed_yes_rate": bucket_observed,
                "calibration_bias": mean_predicted - bucket_observed,
                "brier_score": sum(
                    (
                        float(row["yes_probability"])
                        - float(row["label_yes"])
                    )
                    ** 2
                    for row in bucket_rows
                )
                / bucket_n,
            }

        return {
            "n": n,
            "correct": correct,
            "accuracy": correct / n,
            "wilson_95_low": wilson_low,
            "wilson_95_high": wilson_high,
            "brier_score": brier,
            "log_loss": log_loss,
            "mean_yes_probability": mean_probability,
            "observed_yes_rate": observed_yes,
            "calibration_bias": mean_probability - observed_yes,
            "market_n": market_n,
            "market_accuracy": market_accuracy,
            "market_brier_score": market_brier,
            "market_log_loss": market_log_loss,
            "paired_model_brier_score": paired_model_brier,
            "paired_model_log_loss": paired_model_log_loss,
            "brier_skill_vs_market": brier_skill,
            "log_loss_delta_vs_market": log_loss_delta,
            "accuracy_delta_vs_market": accuracy_delta,
            "close_windows": len(assets_by_close),
            "complete_seven_asset_close_windows": complete_windows,
            "partial_close_windows": len(assets_by_close) - complete_windows,
            "assets_per_close_window_min": min(asset_counts) if asset_counts else None,
            "assets_per_close_window_max": max(asset_counts) if asset_counts else None,
            "unique_probabilities_6dp": len({round(p, 6) for p in probabilities}),
            "saturated_probability_rows": sum(
                1 for p in probabilities if p <= 0.01 or p >= 0.99
            ),
            "out_of_distribution_rows": sum(
                1 for row in materialized if bool(row.get("out_of_distribution"))
            ),
            "calibration_deciles": calibration_deciles,
            "provisional": n < int(min_n),
        }

    @staticmethod
    def _paired_probability_close_window_bootstrap(
        rows: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Bootstrap model-minus-market loss deltas by whole close window.

        Assets sharing a Q15 close are dependent observations.  Averaging
        inside each close and resampling the resulting close-window pairs
        prevents six same-window transfer assets from masquerading as six
        independent trials.  The fixed seed/config make the prospective
        promotion evidence exactly reproducible.
        """

        epsilon = 1.0e-15

        def _log_loss(probability: float, label: float) -> float:
            clipped = min(1.0 - epsilon, max(epsilon, probability))
            return -(
                label * math.log(clipped)
                + (1.0 - label) * math.log(1.0 - clipped)
            )

        paired_by_close: dict[float, list[tuple[float, float]]] = defaultdict(
            list
        )
        paired_rows = 0
        for row in rows:
            close_time = _num(row.get("close_time"))
            label = _num(row.get("label_yes"))
            model_probability = _num(row.get("yes_probability"))
            market_probability = _num(row.get("market_yes_probability"))
            if (
                close_time is None
                or label not in {0.0, 1.0}
                or model_probability is None
                or market_probability is None
                or not 0.0 <= model_probability <= 1.0
                or not 0.0 <= market_probability <= 1.0
            ):
                continue
            model_brier = (model_probability - label) ** 2
            market_brier = (market_probability - label) ** 2
            paired_by_close[close_time].append((
                model_brier - market_brier,
                _log_loss(model_probability, label)
                - _log_loss(market_probability, label),
            ))
            paired_rows += 1

        window_deltas = []
        for close_time in sorted(paired_by_close):
            values = paired_by_close[close_time]
            if not values:
                continue
            window_deltas.append((
                math.fsum(value[0] for value in values) / len(values),
                math.fsum(value[1] for value in values) / len(values),
            ))

        base: dict[str, Any] = {
            "version": RTI_V11_BOOTSTRAP_VERSION,
            "cluster_key": RTI_V11_BOOTSTRAP_CLUSTER_KEY,
            "resamples": RTI_V11_BOOTSTRAP_RESAMPLES,
            "confidence_level": RTI_V11_BOOTSTRAP_CONFIDENCE_LEVEL,
            "random_seed": RTI_V11_BOOTSTRAP_RANDOM_SEED,
            "same_close_assets_resampled_together": True,
            "within_close_assets_equal_weighted": True,
            "close_windows_equal_weighted": True,
            "loss_delta_direction": "MODEL_MINUS_MARKET",
            "minimum_mean_brier_improvement": (
                RTI_V11_MIN_BRIER_IMPROVEMENT
            ),
            "minimum_mean_log_loss_improvement": (
                RTI_V11_MIN_LOG_LOSS_IMPROVEMENT
            ),
            "rows": paired_rows,
            "close_windows": len(window_deltas),
        }
        if not window_deltas:
            return {
                **base,
                "available": False,
                "reason": "no_complete_paired_close_windows",
                "brier_delta": None,
                "log_loss_delta": None,
                "gate_met": False,
            }

        observed_brier = math.fsum(value[0] for value in window_deltas) / len(
            window_deltas
        )
        observed_log_loss = math.fsum(value[1] for value in window_deltas) / len(
            window_deltas
        )
        rng = random.Random(RTI_V11_BOOTSTRAP_RANDOM_SEED)
        bootstrap_brier: list[float] = []
        bootstrap_log_loss: list[float] = []
        window_count = len(window_deltas)
        for _ in range(RTI_V11_BOOTSTRAP_RESAMPLES):
            sampled_brier: list[float] = []
            sampled_log_loss: list[float] = []
            for _ in range(window_count):
                brier_delta, log_loss_delta = window_deltas[
                    rng.randrange(window_count)
                ]
                sampled_brier.append(brier_delta)
                sampled_log_loss.append(log_loss_delta)
            bootstrap_brier.append(math.fsum(sampled_brier) / window_count)
            bootstrap_log_loss.append(
                math.fsum(sampled_log_loss) / window_count
            )

        def _quantile(values: Sequence[float], probability: float) -> float:
            ordered = sorted(values)
            position = (len(ordered) - 1) * probability
            lower = int(math.floor(position))
            upper = int(math.ceil(position))
            if lower == upper:
                return ordered[lower]
            fraction = position - lower
            return (
                ordered[lower] * (1.0 - fraction)
                + ordered[upper] * fraction
            )

        two_sided_tail = (
            1.0 - RTI_V11_BOOTSTRAP_CONFIDENCE_LEVEL
        ) / 2.0
        one_sided_probability = RTI_V11_BOOTSTRAP_CONFIDENCE_LEVEL

        def _summary(observed: float, values: Sequence[float]) -> dict[str, Any]:
            return {
                "observed_mean_delta": observed,
                "two_sided_lower": _quantile(values, two_sided_tail),
                "two_sided_upper": _quantile(
                    values, 1.0 - two_sided_tail
                ),
                "one_sided_upper": _quantile(
                    values, one_sided_probability
                ),
                "bootstrap_probability_delta_below_zero": (
                    sum(1 for value in values if value < 0.0) / len(values)
                ),
            }

        brier_summary = _summary(observed_brier, bootstrap_brier)
        log_loss_summary = _summary(observed_log_loss, bootstrap_log_loss)
        return {
            **base,
            "available": True,
            "reason": None,
            "brier_delta": brier_summary,
            "log_loss_delta": log_loss_summary,
            "gate_met": bool(
                brier_summary["one_sided_upper"]
                <= -RTI_V11_MIN_BRIER_IMPROVEMENT
                and log_loss_summary["one_sided_upper"]
                <= -RTI_V11_MIN_LOG_LOSS_IMPROVEMENT
            ),
        }

    @classmethod
    def _probability_scorecard(
        cls,
        records: Sequence[Mapping[str, Any]],
        min_n: int,
        *,
        challenger_id: str,
    ) -> dict[str, Any]:
        """Build a leakage-resistant scorecard from stored challenger evidence."""
        exclusions: Counter[str] = Counter()
        evidence_integrity: Counter[str] = Counter()
        normalized: list[dict[str, Any]] = []
        prospective_lineage: list[dict[str, Any]] = []
        model_versions: set[str] = set()
        artifact_hashes: set[str] = set()
        test_state_hashes: set[str] = set()
        is_v11 = (
            challenger_id
            == RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID
        )
        probability_evidence_key = (
            "yes_probability" if is_v11 else "calibrated_yes_probability"
        )

        for record in records:
            row = record.get("row")
            challenger = record.get("challenger")
            if not isinstance(row, Mapping) or not isinstance(challenger, Mapping):
                exclusions["malformed_record"] += 1
                continue
            evidence = challenger.get("evidence")
            if not isinstance(evidence, Mapping):
                exclusions["missing_evidence"] += 1
                continue

            model_version = str(evidence.get("model_version") or "")
            artifact_sha = str(evidence.get("artifact_sha256") or "")
            if model_version:
                model_versions.add(model_version)
            else:
                evidence_integrity["missing_model_version"] += 1
            if artifact_sha:
                artifact_hashes.add(artifact_sha)
            else:
                evidence_integrity["missing_artifact_sha256"] += 1
            if is_v11:
                test_state_sha = str(evidence.get("test_state_sha256") or "")
                if test_state_sha:
                    test_state_hashes.add(test_state_sha)
                else:
                    evidence_integrity["missing_test_state_sha256"] += 1

            close_time = _num(row.get("close_time"))
            cutoff = _num(evidence.get("prospective_after_close_time"))
            if close_time is None or cutoff is None:
                exclusions["missing_close_or_cutoff"] += 1
                continue
            if close_time <= cutoff:
                exclusions["pre_or_at_freeze_cutoff"] += 1
                continue

            asset = str(row.get("asset") or "").upper()
            transfer_cohort = (
                "BTC" if asset == "BTC" else "NON_BTC_TRANSFER"
            )
            prospective_lineage.append({
                "asset": asset,
                "transfer_cohort": transfer_cohort,
                "evidence_cohort": str(evidence.get("cohort") or ""),
                "model_version": model_version,
                "artifact_sha256": artifact_sha,
                "test_state_version": str(
                    evidence.get("test_state_version") or ""
                ),
                "test_state_sha256": str(
                    evidence.get("test_state_sha256") or ""
                ),
                "test_metrics_sha256": str(
                    evidence.get("test_metrics_sha256") or ""
                ),
                "untouched_test_status": str(
                    evidence.get("untouched_test_status") or ""
                ),
                "design_id": str(evidence.get("design_id") or ""),
                "design_sha256": str(
                    evidence.get("design_sha256") or ""
                ),
                "walk_forward_protocol_id": str(
                    evidence.get("walk_forward_protocol_id") or ""
                ),
                "walk_forward_protocol_sha256": str(
                    evidence.get("walk_forward_protocol_sha256") or ""
                ),
            })

            official = str(row.get("official_result") or "").upper()
            if official not in {"YES", "NO"}:
                exclusions["unresolved"] += 1
                continue
            probability = _num(evidence.get(probability_evidence_key))
            if probability is None or not 0.0 <= probability <= 1.0:
                exclusions["invalid_yes_probability"] += 1
                continue
            market_probability = _num(evidence.get("market_yes_probability"))
            if market_probability is not None and not 0.0 <= market_probability <= 1.0:
                market_probability = None
                evidence_integrity["invalid_market_yes_probability"] += 1
            elif market_probability is None:
                evidence_integrity["missing_market_yes_probability"] += 1

            normalized.append({
                "asset": asset,
                "close_time": close_time,
                "label_yes": 1 if official == "YES" else 0,
                "yes_probability": probability,
                "market_yes_probability": market_probability,
                "out_of_distribution": bool(evidence.get("out_of_distribution")),
                "accepted": bool(challenger.get("accepted")),
            })

        by_asset: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in normalized:
            by_asset[str(row.get("asset") or "unknown")].append(row)
        btc_rows = [row for row in normalized if row.get("asset") == "BTC"]
        transfer_rows = [row for row in normalized if row.get("asset") != "BTC"]
        in_distribution = [
            row for row in normalized if not bool(row.get("out_of_distribution"))
        ]
        accepted_scored = sum(1 for row in normalized if row.get("accepted"))
        promotion_prohibited = (
            challenger_id == RTI_PATH_13M_PROBABILITY_V2_CHALLENGER_ID
        )

        def _valid_sha256(value: str) -> bool:
            return len(value) == 64 and all(
                character in "0123456789abcdef" for character in value
            )

        def _lineage_summary(
            source: Sequence[Mapping[str, Any]],
        ) -> dict[str, Any]:
            models = sorted({
                str(item.get("model_version") or "") for item in source
                if str(item.get("model_version") or "")
            })
            artifacts = sorted({
                str(item.get("artifact_sha256") or "") for item in source
                if str(item.get("artifact_sha256") or "")
            })
            state_hashes = sorted({
                str(item.get("test_state_sha256") or "") for item in source
                if str(item.get("test_state_sha256") or "")
            })
            metrics_hashes = sorted({
                str(item.get("test_metrics_sha256") or "") for item in source
                if str(item.get("test_metrics_sha256") or "")
            })
            cohort_matches = all(
                item.get("evidence_cohort") == item.get("transfer_cohort")
                for item in source
            )
            base_met = bool(
                source
                and len(models) == 1
                and len(artifacts) == 1
                and _valid_sha256(artifacts[0])
                and cohort_matches
            )
            v11_identity_met = None
            if is_v11:
                v11_identity_met = bool(
                    len(state_hashes) == 1
                    and len(metrics_hashes) == 1
                    and _valid_sha256(state_hashes[0])
                    and _valid_sha256(metrics_hashes[0])
                    and all(
                        item.get("test_state_version")
                        == "q15-rti-untouched-test-state-v2"
                        and item.get("untouched_test_status")
                        == "PASSED_UNTOUCHED_TEST_PAPER_ARTIFACT_ONLY"
                        and item.get("design_id")
                        == RTI_MICROSTRUCTURE_V11_DESIGN_ID
                        and item.get("design_sha256")
                        == RTI_MICROSTRUCTURE_V11_DESIGN_SHA256
                        and item.get("walk_forward_protocol_id")
                        == RTI_MICROSTRUCTURE_V11_PROTOCOL_ID
                        and item.get("walk_forward_protocol_sha256")
                        == RTI_MICROSTRUCTURE_V11_PROTOCOL_SHA256
                        for item in source
                    )
                )
            return {
                "prospective_evidence_rows": len(source),
                "observed_model_versions": models,
                "observed_artifact_sha256": artifacts,
                "observed_test_state_sha256": state_hashes,
                "observed_test_metrics_sha256": metrics_hashes,
                "single_model_version": len(models) == 1,
                "single_artifact_sha256": len(artifacts) == 1,
                "single_test_state_sha256": (
                    len(state_hashes) == 1 if is_v11 else None
                ),
                "single_test_metrics_sha256": (
                    len(metrics_hashes) == 1 if is_v11 else None
                ),
                "artifact_sha256_valid": bool(
                    len(artifacts) == 1 and _valid_sha256(artifacts[0])
                ),
                "test_state_sha256_valid": (
                    bool(
                        len(state_hashes) == 1
                        and _valid_sha256(state_hashes[0])
                    )
                    if is_v11 else None
                ),
                "test_metrics_sha256_valid": (
                    bool(
                        len(metrics_hashes) == 1
                        and _valid_sha256(metrics_hashes[0])
                    )
                    if is_v11 else None
                ),
                "evidence_cohort_matches_row_cohort": cohort_matches,
                "v11_exact_test_design_protocol_lineage": v11_identity_met,
                "met": bool(
                    base_met and (v11_identity_met if is_v11 else True)
                ),
            }

        lineage_by_cohort = {
            cohort: _lineage_summary([
                item for item in prospective_lineage
                if item.get("transfer_cohort") == cohort
            ])
            for cohort in ("BTC", "NON_BTC_TRANSFER")
        }

        def _metrics(
            source: Sequence[Mapping[str, Any]],
        ) -> dict[str, Any]:
            result = cls._probability_score_metrics(source, min_n)
            if is_v11:
                result["paired_close_window_bootstrap"] = (
                    cls._paired_probability_close_window_bootstrap(source)
                )
            return result

        return {
            "challenger_id": challenger_id,
            "available": True,
            "paper_only": True,
            "point_in_time_stored_evidence_only": True,
            "accepted_trade_filter_applied": False,
            "historical_recomputation_allowed": False,
            "cutoff_rule": "close_time > stored prospective_after_close_time",
            "evaluated_evidence_rows": len(records),
            "scoreable_resolved_rows": len(normalized),
            "accepted_rows_scored": accepted_scored,
            "rejected_rows_scored": len(normalized) - accepted_scored,
            "excluded": dict(exclusions.most_common()),
            "evidence_integrity": {
                **dict(evidence_integrity.most_common()),
                "observed_model_versions": sorted(model_versions),
                "observed_artifact_sha256": sorted(artifact_hashes),
                "single_model_version": len(model_versions) == 1,
                "single_artifact_sha256": len(artifact_hashes) == 1,
                "observed_test_state_sha256": sorted(test_state_hashes),
                "single_test_state_sha256": (
                    len(test_state_hashes) == 1 if is_v11 else None
                ),
            },
            "stored_probability_field": probability_evidence_key,
            "prospective_lineage": _lineage_summary(prospective_lineage),
            "prospective_lineage_by_transfer_cohort": lineage_by_cohort,
            "promotion_prohibited": promotion_prohibited,
            "manual_promotion_only": True,
            "overall": _metrics(normalized),
            "in_distribution": _metrics(in_distribution),
            "by_transfer_cohort": {
                "BTC": _metrics(btc_rows),
                "NON_BTC_TRANSFER": _metrics(transfer_rows),
            },
            "by_asset": {
                asset: _metrics(asset_rows)
                for asset, asset_rows in sorted(by_asset.items())
            },
        }

    @classmethod
    def _rti_path_challenger_system(
        cls,
        rows: Sequence[Mapping[str, Any]],
        min_n: int,
        *,
        feature_rows: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        books: dict[str, list[Mapping[str, Any]]] = {
            "strong_path_wide_v1": [],
            "value_price_wide_v1": [],
            RTI_PATH_13M_SPOT_CONFIRM_CHALLENGER_ID: [],
            RTI_PATH_13M_IMPULSE_CHALLENGER_ID: [],
            RTI_PATH_13M_COUNTERTREND_CHALLENGER_ID: [],
            RTI_PATH_13M_DELAYED_CONFIRM_CHALLENGER_ID: [],
            RTI_PATH_13M_DELAYED_CONFIRM_60S_CHALLENGER_ID: [],
            RTI_PATH_13M_DELAYED_CONFIRM_90S_CHALLENGER_ID: [],
            RTI_PATH_13M_DELAYED_FLIP_60S_CHALLENGER_ID: [],
            RTI_PATH_13M_PROBABILITY_V2_CHALLENGER_ID: [],
            RTI_PATH_13M_PROBABILITY_V3_CHALLENGER_ID: [],
            RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID: [],
        }
        rejected_books: dict[str, list[Mapping[str, Any]]] = {
            challenger_id: [] for challenger_id in books
        }
        policy_versions: set[str] = {RTI_PATH_13M_CHALLENGER_POLICY_VERSION}
        notification_eligibility: dict[str, bool] = {}
        probability_evidence: dict[str, list[dict[str, Any]]] = {
            RTI_PATH_13M_PROBABILITY_V2_CHALLENGER_ID: [],
            RTI_PATH_13M_PROBABILITY_V3_CHALLENGER_ID: [],
            RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID: [],
        }
        evaluated: Counter[str] = Counter()
        failure_counts: dict[str, Counter[str]] = defaultdict(Counter)
        last_evaluated_close: dict[str, float] = {}
        for row in rows:
            if row.get("bot_name") != BOT_RTI_PATH_13M:
                continue
            policy = cls._threshold_value(row, "challenger_policy_version")
            challengers = cls._threshold_value(row, "challengers")
            if not isinstance(challengers, Mapping):
                continue
            if policy:
                policy_versions.add(str(policy))
            for challenger_id, raw in challengers.items():
                if not isinstance(raw, Mapping):
                    continue
                challenger_key = str(challenger_id)
                evaluated[challenger_key] += 1
                if challenger_key in probability_evidence:
                    probability_evidence[challenger_key].append({
                        "row": row,
                        "challenger": raw,
                    })
                failure_counts[challenger_key].update(
                    str(failure) for failure in (raw.get("failures") or ())
                )
                close_time = _num(row.get("close_time"))
                if close_time is not None:
                    last_evaluated_close[challenger_key] = max(
                        close_time,
                        last_evaluated_close.get(challenger_key, close_time),
                    )
                notification_eligibility[str(challenger_id)] = bool(
                    raw.get("notification_eligible")
                )
                criteria = raw.get("criteria")
                if isinstance(criteria, Mapping) and criteria.get("policy_version"):
                    policy_versions.add(str(criteria["policy_version"]))
                book_row = dict(row)
                side_override = str(raw.get("side_override") or "").upper()
                entry_override = _num(raw.get("entry_ask_cents"))
                has_side_override = side_override in {"YES", "NO"}
                if has_side_override:
                    book_row["side"] = side_override
                if entry_override is not None:
                    book_row["entry_ask_cents"] = entry_override
                official = str(row.get("official_result") or "").upper()
                effective_side = str(book_row.get("side") or "").upper()
                if (
                    official in {"YES", "NO"}
                    and effective_side in {"YES", "NO"}
                    and (has_side_override or entry_override is not None)
                ):
                    correct = effective_side == official
                    book_row["correct"] = 1 if correct else 0
                    book_row["hypothetical_pnl_cents"] = (
                        _prospective_net_pnl_cents(
                            book_row.get("entry_ask_cents"),
                            correct,
                            row.get("threshold_json"),
                        )
                    )
                if not bool(raw.get("accepted")):
                    rejected_books.setdefault(challenger_key, []).append(book_row)
                    continue
                books.setdefault(str(challenger_id), []).append(book_row)

        labeled_exact = [
            row
            for row in rows
            if str(row.get("interval") or "").upper() == "13M"
            and cls._threshold_value(row, "rti_risk_policy_version")
        ]
        labeled_strict = [
            row
            for row in labeled_exact
            if row.get("decision_status") == ACCEPTED
        ]

        def _risk_groups(
            source_rows: Sequence[Mapping[str, Any]],
            key: str,
        ) -> dict[str, Any]:
            grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
            for source_row in source_rows:
                label = cls._threshold_value(source_row, key)
                grouped[str(label or "unknown")].append(source_row)
            return {
                label: cls._agg(group_rows, min_n)
                for label, group_rows in sorted(grouped.items())
            }

        risk_dimensions = {
            "by_reversal_risk": "rti_reversal_risk_class",
            "by_settlement_average_risk": (
                "rti_settlement_average_risk_class"
            ),
            "by_path_regime": "rti_path_regime_class",
            "by_market_agreement": "rti_market_agreement_class",
        }

        def _risk_report(
            source_rows: Sequence[Mapping[str, Any]],
        ) -> dict[str, Any]:
            return {
                "overall": cls._agg(source_rows, min_n),
                **{
                    name: _risk_groups(source_rows, key)
                    for name, key in risk_dimensions.items()
                },
            }

        row_by_id = {
            int(row["id"]): row
            for row in rows
            if _num(row.get("id")) is not None
        }
        def _delayed_pair_set(
            interval: str,
            challenger_id: str,
            *,
            pre_policy_missing_is_not_evaluated: bool = False,
        ) -> tuple[list[dict[str, Any]], int, int]:
            pairs: list[dict[str, Any]] = []
            invalid_links = 0
            pre_policy_rows = 0
            for delayed_row in rows:
                if str(delayed_row.get("interval") or "").upper() != interval:
                    continue
                original_accepted = cls._threshold_flag(
                    delayed_row, "rti_confirm_original_strict_accepted"
                )
                if not original_accepted:
                    continue
                parent_id = int(
                    _num(
                        cls._threshold_value(
                            delayed_row, "rti_confirm_original_row_id"
                        )
                    )
                    or 0
                )
                parent = row_by_id.get(parent_id)
                if (
                    parent is None
                    or str(parent.get("interval") or "").upper() != "13M"
                    or parent.get("decision_status") != ACCEPTED
                    or parent.get("ticker") != delayed_row.get("ticker")
                    or parent.get("asset") != delayed_row.get("asset")
                    or _num(parent.get("close_time"))
                    != _num(delayed_row.get("close_time"))
                ):
                    invalid_links += 1
                    continue
                challengers = cls._threshold_value(
                    delayed_row, "challengers"
                )
                raw = (
                    challengers.get(challenger_id)
                    if isinstance(challengers, Mapping)
                    else None
                )
                if not isinstance(raw, Mapping):
                    if pre_policy_missing_is_not_evaluated:
                        pre_policy_rows += 1
                        continue
                    invalid_links += 1
                    continue
                pairs.append({
                    "parent": parent,
                    "delayed": delayed_row,
                    "accepted": bool(raw.get("accepted")),
                })
            return pairs, invalid_links, pre_policy_rows

        delayed_pairs, invalid_delayed_links, _ = _delayed_pair_set(
            "12M30S", RTI_PATH_13M_DELAYED_CONFIRM_CHALLENGER_ID
        )
        delayed_60s_pairs, invalid_delayed_60s_links, _ = _delayed_pair_set(
            "12M", RTI_PATH_13M_DELAYED_CONFIRM_60S_CHALLENGER_ID
        )
        (
            delayed_90s_pairs,
            invalid_delayed_90s_links,
            pre_policy_90s_rows,
        ) = _delayed_pair_set(
            "11M30S",
            RTI_PATH_13M_DELAYED_CONFIRM_90S_CHALLENGER_ID,
            pre_policy_missing_is_not_evaluated=True,
        )
        (
            delayed_flip_60s_pairs,
            invalid_delayed_flip_60s_links,
            pre_policy_flip_60s_rows,
        ) = (
            _delayed_pair_set(
                "12M",
                RTI_PATH_13M_DELAYED_FLIP_60S_CHALLENGER_ID,
                pre_policy_missing_is_not_evaluated=True,
            )
        )

        def _matched_summary(pairs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
            parent_rows = [pair["parent"] for pair in pairs]
            taken_rows = [
                pair["delayed"] for pair in pairs if pair.get("accepted")
            ]
            rejected_parent_rows = [
                pair["parent"] for pair in pairs if not pair.get("accepted")
            ]
            resolved_pairs = [
                pair
                for pair in pairs
                if str(pair["parent"].get("official_result") or "").upper()
                in {"YES", "NO"}
                and str(pair["delayed"].get("official_result") or "").upper()
                in {"YES", "NO"}
            ]
            policy_pnl = sum(
                float(_resolved_row_pnl_cents(pair["delayed"]) or 0.0)
                for pair in resolved_pairs
                if pair.get("accepted")
            )
            control_pnl = sum(
                float(_resolved_row_pnl_cents(pair["parent"]) or 0.0)
                for pair in resolved_pairs
            )
            saved_losses = sum(
                1
                for pair in resolved_pairs
                if not pair.get("accepted")
                and int(pair["parent"].get("correct") or 0) == 0
            )
            skipped_winners = sum(
                1
                for pair in resolved_pairs
                if not pair.get("accepted")
                and int(pair["parent"].get("correct") or 0) == 1
            )
            ask_deltas = [
                float(delayed_ask) - float(parent_ask)
                for pair in pairs
                for parent_ask in [_num(pair["parent"].get("entry_ask_cents"))]
                for delayed_ask in [_num(pair["delayed"].get("entry_ask_cents"))]
                if parent_ask is not None and delayed_ask is not None
            ]
            taken_ask_deltas = [
                float(delayed_ask) - float(parent_ask)
                for pair in pairs
                if pair.get("accepted")
                for parent_ask in [_num(pair["parent"].get("entry_ask_cents"))]
                for delayed_ask in [_num(pair["delayed"].get("entry_ask_cents"))]
                if parent_ask is not None and delayed_ask is not None
            ]
            incremental = policy_pnl - control_pnl
            return {
                "pairs": len(pairs),
                "resolved_pairs": len(resolved_pairs),
                "unresolved_pairs": len(pairs) - len(resolved_pairs),
                "delayed_taken": len(taken_rows),
                "delayed_rejected": len(rejected_parent_rows),
                "saved_losses": saved_losses,
                "skipped_winners": skipped_winners,
                "control": cls._agg(parent_rows, min_n),
                "delayed_taken_book": cls._agg(taken_rows, min_n),
                "rejected_parent_counterfactual": cls._agg(
                    rejected_parent_rows, min_n
                ),
                "control_net_pnl_cents": control_pnl,
                "delayed_policy_net_pnl_cents": policy_pnl,
                "incremental_net_pnl_cents": incremental,
                "ten_contract_control_pnl_dollars": control_pnl * 10.0 / 100.0,
                "ten_contract_delayed_policy_pnl_dollars": (
                    policy_pnl * 10.0 / 100.0
                ),
                "ten_contract_incremental_pnl_dollars": (
                    incremental * 10.0 / 100.0
                ),
                "avg_ask_change_cents": (
                    None if not ask_deltas else sum(ask_deltas) / len(ask_deltas)
                ),
                "avg_taken_ask_change_cents": (
                    None
                    if not taken_ask_deltas
                    else sum(taken_ask_deltas) / len(taken_ask_deltas)
                ),
            }

        def _pair_groups(
            pairs: Sequence[Mapping[str, Any]],
            key: str,
        ) -> dict[str, Any]:
            grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
            for pair in pairs:
                label = cls._threshold_value(pair["parent"], key)
                grouped[str(label or "unknown")].append(pair)
            return {
                label: _matched_summary(group_pairs)
                for label, group_pairs in sorted(grouped.items())
            }

        def _delayed_matched_report(
            pairs: Sequence[Mapping[str, Any]],
            invalid_links: int,
            challenger_id: str,
            pre_policy_rows: int = 0,
        ) -> dict[str, Any]:
            return {
                "challenger_id": challenger_id,
                "paper_only": True,
                "forward_only": True,
                "notification_eligible": False,
                "historical_credit_allowed": False,
                "parent_control_unchanged": True,
                "invalid_parent_links": invalid_links,
                "pre_policy_parent_rows_excluded": pre_policy_rows,
                "overall": _matched_summary(pairs),
                "by_transfer_cohort": {
                    "BTC": _matched_summary([
                        pair
                        for pair in pairs
                        if pair["parent"].get("asset") == "BTC"
                    ]),
                    "NON_BTC_TRANSFER": _matched_summary([
                        pair
                        for pair in pairs
                        if pair["parent"].get("asset") != "BTC"
                    ]),
                },
                "by_reversal_risk": _pair_groups(
                    pairs, "rti_reversal_risk_class"
                ),
                "by_settlement_average_risk": _pair_groups(
                    pairs, "rti_settlement_average_risk_class"
                ),
            }

        delayed_matched = _delayed_matched_report(
            delayed_pairs,
            invalid_delayed_links,
            RTI_PATH_13M_DELAYED_CONFIRM_CHALLENGER_ID,
        )
        delayed_60s_matched = _delayed_matched_report(
            delayed_60s_pairs,
            invalid_delayed_60s_links,
            RTI_PATH_13M_DELAYED_CONFIRM_60S_CHALLENGER_ID,
        )
        delayed_90s_matched = _delayed_matched_report(
            delayed_90s_pairs,
            invalid_delayed_90s_links,
            RTI_PATH_13M_DELAYED_CONFIRM_90S_CHALLENGER_ID,
            pre_policy_90s_rows,
        )
        delayed_flip_60s_matched = _delayed_matched_report(
            delayed_flip_60s_pairs,
            invalid_delayed_flip_60s_links,
            RTI_PATH_13M_DELAYED_FLIP_60S_CHALLENGER_ID,
            pre_policy_flip_60s_rows,
        )

        def _pair_parent_id(pair: Mapping[str, Any]) -> int:
            return int(pair["parent"]["id"])

        pairs_30_by_parent = {
            _pair_parent_id(pair): pair for pair in delayed_pairs
        }
        pairs_60_by_parent = {
            _pair_parent_id(pair): pair for pair in delayed_60s_pairs
        }
        pairs_90_by_parent = {
            _pair_parent_id(pair): pair for pair in delayed_90s_pairs
        }
        flip_pairs_by_parent = {
            _pair_parent_id(pair): pair for pair in delayed_flip_60s_pairs
        }
        ladder_entries = [
            {
                "parent": pair_60["parent"],
                "confirm_30": pairs_30_by_parent.get(parent_id),
                "confirm_60": pair_60,
                "confirm_90": pairs_90_by_parent.get(parent_id),
                "flip_60": flip_pairs_by_parent.get(parent_id),
            }
            for parent_id, pair_60 in sorted(pairs_60_by_parent.items())
        ]

        def _entry_state(
            pair: Mapping[str, Any] | None,
        ) -> str:
            if pair is None:
                return "NOT_EVALUATED"
            return "TAKEN" if pair.get("accepted") else "REJECTED"

        def _challenger_pnl(
            pair: Mapping[str, Any] | None,
            challenger_id: str,
        ) -> float:
            if pair is None or not pair.get("accepted"):
                return 0.0
            delayed = pair["delayed"]
            challengers = cls._threshold_value(delayed, "challengers")
            raw = (
                challengers.get(challenger_id)
                if isinstance(challengers, Mapping)
                else None
            )
            if not isinstance(raw, Mapping) or not raw.get("accepted"):
                return 0.0
            official = str(delayed.get("official_result") or "").upper()
            side = str(raw.get("side_override") or delayed.get("side") or "").upper()
            entry = _num(raw.get("entry_ask_cents"))
            if official not in {"YES", "NO"} or side not in {"YES", "NO"}:
                return 0.0
            return float(
                _prospective_net_pnl_cents(
                    entry,
                    side == official,
                    delayed.get("threshold_json"),
                )
                or 0.0
            )

        def _ladder_summary(
            entries: Sequence[Mapping[str, Any]],
        ) -> dict[str, Any]:
            resolved = [
                entry for entry in entries
                if str(entry["parent"].get("official_result") or "").upper()
                in {"YES", "NO"}
            ]
            control_pnl = sum(
                float(_resolved_row_pnl_cents(entry["parent"]) or 0.0)
                for entry in resolved
            )
            stage_specs = {
                "confirm_30": RTI_PATH_13M_DELAYED_CONFIRM_CHALLENGER_ID,
                "confirm_60": RTI_PATH_13M_DELAYED_CONFIRM_60S_CHALLENGER_ID,
                "confirm_90": RTI_PATH_13M_DELAYED_CONFIRM_90S_CHALLENGER_ID,
                "flip_60": RTI_PATH_13M_DELAYED_FLIP_60S_CHALLENGER_ID,
            }
            stages: dict[str, Any] = {}
            for stage, challenger_id in stage_specs.items():
                evaluated_entries = [
                    entry for entry in entries if entry.get(stage) is not None
                ]
                resolved_evaluated = [
                    entry for entry in resolved if entry.get(stage) is not None
                ]
                taken = [
                    entry for entry in evaluated_entries
                    if entry[stage].get("accepted")
                ]
                resolved_taken = [
                    entry for entry in resolved_evaluated
                    if entry[stage].get("accepted")
                ]
                pnl = sum(
                    _challenger_pnl(entry.get(stage), challenger_id)
                    for entry in resolved_evaluated
                )
                stage_control_pnl = sum(
                    float(_resolved_row_pnl_cents(entry["parent"]) or 0.0)
                    for entry in resolved_evaluated
                )
                stages[stage] = {
                    "evaluated": len(evaluated_entries),
                    "resolved_evaluated": len(resolved_evaluated),
                    "taken": len(taken),
                    "resolved_taken": len(resolved_taken),
                    "correct_taken": sum(
                        int(
                            str(
                                entry[stage]["delayed"].get(
                                    "official_result"
                                )
                                or ""
                            ).upper()
                            == str(
                                cls._threshold_value(
                                    entry[stage]["delayed"], "challengers"
                                )[challenger_id].get("side_override")
                                or entry[stage]["delayed"].get("side")
                                or ""
                            ).upper()
                        )
                        for entry in resolved_taken
                    ),
                    "policy_net_pnl_cents": pnl,
                    "ten_contract_policy_pnl_dollars": pnl * 10.0 / 100.0,
                    "matched_control_net_pnl_cents": stage_control_pnl,
                    "incremental_vs_control_net_pnl_cents": (
                        pnl - stage_control_pnl
                    ),
                    "ten_contract_incremental_vs_control_dollars": (
                        (pnl - stage_control_pnl) * 10.0 / 100.0
                    ),
                }
            return {
                "parents": len(entries),
                "resolved_parents": len(resolved),
                "control_net_pnl_cents": control_pnl,
                "ten_contract_control_pnl_dollars": control_pnl * 10.0 / 100.0,
                "stages": stages,
            }

        common_30_60_entries = [
            entry for entry in ladder_entries
            if entry.get("confirm_30") is not None
        ]
        post_flip_freeze_entries = [
            entry for entry in ladder_entries
            if entry.get("flip_60") is not None
        ]
        transition_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for entry in common_30_60_entries:
            transition = (
                f"30_{_entry_state(entry.get('confirm_30'))}__"
                f"60_{_entry_state(entry.get('confirm_60'))}__"
                f"90_{_entry_state(entry.get('confirm_90'))}__"
                f"FLIP_{_entry_state(entry.get('flip_60'))}"
            )
            transition_groups[transition].append(entry)
        delayed_confirmation_ladder = {
            "paper_only": True,
            "notification_eligible": False,
            "historical_credit_allowed": False,
            "parent_control_unchanged": True,
            "anchor": "valid +60s continuation evaluations",
            "overall_60s_anchor": _ladder_summary(ladder_entries),
            "common_30s_60s": _ladder_summary(common_30_60_entries),
            "post_flip_policy_freeze": _ladder_summary(
                post_flip_freeze_entries
            ),
            "post_90s_policy_freeze": _ladder_summary([
                entry for entry in ladder_entries
                if entry.get("confirm_90") is not None
            ]),
            "by_transition": {
                transition: _ladder_summary(entries)
                for transition, entries in sorted(transition_groups.items())
            },
            "lineage": {
                "invalid_30s_parent_links": invalid_delayed_links,
                "invalid_60s_parent_links": invalid_delayed_60s_links,
                "invalid_90s_parent_links": invalid_delayed_90s_links,
                "invalid_flip_parent_links": invalid_delayed_flip_60s_links,
                "pre_90s_policy_parent_rows_excluded": pre_policy_90s_rows,
                "pre_flip_policy_parent_rows_excluded": (
                    pre_policy_flip_60s_rows
                ),
            },
        }
        feature_source_rows = rows if feature_rows is None else feature_rows
        exact_feature_rows = [
            row
            for row in feature_source_rows
            if row.get("bot_name") == BOT_RTI_PATH_13M
            and str(row.get("interval") or "").upper() == "13M"
            and str(row.get("record_kind") or "").upper()
            == "RTI_PATH_13M_PROSPECTIVE_EXACT"
        ]
        microstructure_v1_rows = [
            row
            for row in exact_feature_rows
            if row.get("kalshi_microstructure_schema_version")
            == RTI_EXACT_MICROSTRUCTURE_SCHEMA_V1
        ]
        microstructure_v2_rows = [
            row
            for row in exact_feature_rows
            if row.get("kalshi_microstructure_schema_version")
            == RTI_EXACT_MICROSTRUCTURE_SCHEMA_VERSION
        ]
        microstructure_extension_rows = [
            row
            for row in exact_feature_rows
            if row.get("kalshi_microstructure_extension_schema_version")
            == RTI_MICROSTRUCTURE_EXTENSION_SCHEMA_VERSION
        ]
        dynamics_extension_coverage = extension_window_coverage(
            exact_feature_rows
        )
        v1_model_feature_coverage = v1_model_feature_window_coverage(
            exact_feature_rows
        )
        v2_model_feature_coverage_result = v2_model_feature_window_coverage(
            exact_feature_rows
        )
        v3_model_feature_coverage_result = v3_model_feature_window_coverage(
            exact_feature_rows
        )
        model_feature_coverage = v4_model_feature_window_coverage(
            exact_feature_rows
        )
        v5_model_feature_coverage_result = v5_model_feature_window_coverage(
            exact_feature_rows
        )
        v6_model_feature_coverage_result = v6_model_feature_window_coverage(
            exact_feature_rows
        )
        v7_model_feature_coverage_result = v7_model_feature_window_coverage(
            exact_feature_rows
        )
        v8_model_feature_coverage_result = v8_model_feature_window_coverage(
            exact_feature_rows
        )
        v9_model_feature_coverage_result = v9_model_feature_window_coverage(
            exact_feature_rows
        )
        v10_model_feature_coverage_result = v10_model_feature_window_coverage(
            exact_feature_rows
        )
        v11_model_feature_coverage_result = v11_model_feature_window_coverage(
            exact_feature_rows
        )
        v12_model_feature_coverage_result = v12_model_feature_window_coverage(
            exact_feature_rows
        )
        v13_model_feature_coverage_result = v13_model_feature_window_coverage(
            exact_feature_rows
        )
        executable_windows = int(
            model_feature_coverage["complete_model_feature_close_windows"]
        )
        schema_complete_windows = int(
            model_feature_coverage[
                "schema_complete_model_candidate_close_windows"
            ]
        )
        primary_timestamp_failures = len(
            model_feature_coverage["model_feature_timestamp_failures"]
        )
        primary_timestamp_integrity_clean = primary_timestamp_failures == 0
        model_readiness = {
            "design_id": RTI_MICROSTRUCTURE_DESIGN_ID,
            "design_sha256": RTI_MICROSTRUCTURE_DESIGN_SHA256,
            "feature_schema_version": (
                RTI_MICROSTRUCTURE_FEATURE_SCHEMA_VERSION
            ),
            "feature_count": len(RTI_MICROSTRUCTURE_FEATURE_NAMES),
            "paper_only": True,
            "notification_eligible": False,
            "real_trading_allowed": False,
            "automatic_refit": False,
            "automatic_promotion": False,
            "readiness_uses_outcome_labels": False,
            "model_fit_performed": False,
            "artifact_emitted": False,
            "schema_complete_close_windows": schema_complete_windows,
            "complete_executable_close_windows": executable_windows,
            "unusable_close_windows": len(
                model_feature_coverage[
                    "unusable_model_feature_close_windows"
                ]
            ),
            "feature_unavailable_rows": len(
                model_feature_coverage["model_feature_unavailable_rows"]
            ),
            "timestamp_alignment_failures": primary_timestamp_failures,
            "timestamp_integrity_clean": primary_timestamp_integrity_clean,
            "cohorts": {
                "NON_BTC_TRANSFER": {
                    "minimum_complete_close_windows": 60,
                    "windows_remaining": max(0, 60 - executable_windows),
                    "ready_for_locked_freeze": bool(
                        primary_timestamp_integrity_clean
                        and executable_windows >= 60
                    ),
                },
                "BTC": {
                    "minimum_complete_close_windows": 150,
                    "windows_remaining": max(0, 150 - executable_windows),
                    "ready_for_locked_freeze": bool(
                        primary_timestamp_integrity_clean
                        and executable_windows >= 150
                    ),
                },
            },
        }
        model_readiness["ready_for_any_locked_freeze"] = any(
            bool(raw["ready_for_locked_freeze"])
            for raw in model_readiness["cohorts"].values()
        )
        model_readiness["status"] = (
            "TIMESTAMP_INTEGRITY_FAILURE_REVIEW_REQUIRED"
            if not primary_timestamp_integrity_clean
            else "READY_FOR_SEPARATE_LOCKED_FREEZE"
            if model_readiness["ready_for_any_locked_freeze"]
            else "WAITING_FOR_COMPLETE_EXECUTABLE_WINDOWS"
        )
        v5_executable_windows = int(
            v5_model_feature_coverage_result[
                "complete_model_feature_close_windows"
            ]
        )
        v5_schema_complete_windows = int(
            v5_model_feature_coverage_result[
                "schema_complete_model_candidate_close_windows"
            ]
        )
        v5_timestamp_failures = len(
            v5_model_feature_coverage_result[
                "model_feature_timestamp_failures"
            ]
        )
        v5_timestamp_clean = v5_timestamp_failures == 0
        v5_model_readiness = {
            "design_id": RTI_DYNAMICS_DESIGN_ID,
            "design_sha256": RTI_DYNAMICS_DESIGN_SHA256,
            "feature_schema_version": RTI_DYNAMICS_FEATURE_SCHEMA_VERSION,
            "feature_count": len(RTI_DYNAMICS_FEATURE_NAMES),
            "prospective_after_close_time": (
                RTI_DYNAMICS_PROSPECTIVE_AFTER_CLOSE_TIME
            ),
            "first_eligible_close_time": (
                RTI_DYNAMICS_FIRST_ELIGIBLE_CLOSE_TIME
            ),
            "paper_only": True,
            "notification_eligible": False,
            "real_trading_allowed": False,
            "automatic_refit": False,
            "automatic_promotion": False,
            "readiness_uses_outcome_labels": False,
            "model_fit_performed": False,
            "artifact_emitted": False,
            "schema_complete_close_windows": v5_schema_complete_windows,
            "complete_executable_close_windows": v5_executable_windows,
            "unusable_close_windows": len(
                v5_model_feature_coverage_result[
                    "unusable_model_feature_close_windows"
                ]
            ),
            "feature_unavailable_rows": len(
                v5_model_feature_coverage_result[
                    "model_feature_unavailable_rows"
                ]
            ),
            "timestamp_alignment_failures": v5_timestamp_failures,
            "timestamp_integrity_clean": v5_timestamp_clean,
            "cohorts": {
                "NON_BTC_TRANSFER": {
                    "minimum_complete_close_windows": 60,
                    "windows_remaining": max(0, 60 - v5_executable_windows),
                    "ready_for_locked_freeze": bool(
                        v5_timestamp_clean and v5_executable_windows >= 60
                    ),
                },
                "BTC": {
                    "minimum_complete_close_windows": 150,
                    "windows_remaining": max(0, 150 - v5_executable_windows),
                    "ready_for_locked_freeze": bool(
                        v5_timestamp_clean and v5_executable_windows >= 150
                    ),
                },
            },
        }
        v5_model_readiness["ready_for_any_locked_freeze"] = any(
            bool(raw["ready_for_locked_freeze"])
            for raw in v5_model_readiness["cohorts"].values()
        )
        v5_model_readiness["status"] = (
            "TIMESTAMP_INTEGRITY_FAILURE_REVIEW_REQUIRED"
            if not v5_timestamp_clean
            else "READY_FOR_SEPARATE_LOCKED_FREEZE"
            if v5_model_readiness["ready_for_any_locked_freeze"]
            else "WAITING_FOR_COMPLETE_EXECUTABLE_WINDOWS"
        )
        v6_executable_windows = int(
            v6_model_feature_coverage_result[
                "complete_model_feature_close_windows"
            ]
        )
        v6_schema_complete_windows = int(
            v6_model_feature_coverage_result[
                "schema_complete_model_candidate_close_windows"
            ]
        )
        v6_timestamp_failures = len(
            v6_model_feature_coverage_result[
                "model_feature_timestamp_failures"
            ]
        )
        v6_timestamp_clean = v6_timestamp_failures == 0
        v6_model_readiness = {
            "design_id": RTI_LEAD_LAG_DESIGN_ID,
            "design_sha256": RTI_LEAD_LAG_DESIGN_SHA256,
            "feature_schema_version": RTI_LEAD_LAG_FEATURE_SCHEMA_VERSION,
            "feature_count": len(RTI_LEAD_LAG_FEATURE_NAMES),
            "prospective_after_close_time": (
                RTI_LEAD_LAG_PROSPECTIVE_AFTER_CLOSE_TIME
            ),
            "first_eligible_close_time": (
                RTI_LEAD_LAG_FIRST_ELIGIBLE_CLOSE_TIME
            ),
            "paper_only": True,
            "notification_eligible": False,
            "real_trading_allowed": False,
            "automatic_refit": False,
            "automatic_promotion": False,
            "readiness_uses_outcome_labels": False,
            "model_fit_performed": False,
            "artifact_emitted": False,
            "schema_complete_close_windows": v6_schema_complete_windows,
            "complete_executable_close_windows": v6_executable_windows,
            "unusable_close_windows": len(
                v6_model_feature_coverage_result[
                    "unusable_model_feature_close_windows"
                ]
            ),
            "feature_unavailable_rows": len(
                v6_model_feature_coverage_result[
                    "model_feature_unavailable_rows"
                ]
            ),
            "timestamp_alignment_failures": v6_timestamp_failures,
            "timestamp_integrity_clean": v6_timestamp_clean,
            "cohorts": {
                "NON_BTC_TRANSFER": {
                    "minimum_complete_close_windows": 60,
                    "windows_remaining": max(0, 60 - v6_executable_windows),
                    "ready_for_locked_freeze": bool(
                        v6_timestamp_clean and v6_executable_windows >= 60
                    ),
                },
                "BTC": {
                    "minimum_complete_close_windows": 150,
                    "windows_remaining": max(0, 150 - v6_executable_windows),
                    "ready_for_locked_freeze": bool(
                        v6_timestamp_clean and v6_executable_windows >= 150
                    ),
                },
            },
        }
        v6_model_readiness["ready_for_any_locked_freeze"] = any(
            bool(raw["ready_for_locked_freeze"])
            for raw in v6_model_readiness["cohorts"].values()
        )
        v6_model_readiness["status"] = (
            "TIMESTAMP_INTEGRITY_FAILURE_REVIEW_REQUIRED"
            if not v6_timestamp_clean
            else "READY_FOR_SEPARATE_LOCKED_FREEZE"
            if v6_model_readiness["ready_for_any_locked_freeze"]
            else "WAITING_FOR_COMPLETE_EXECUTABLE_WINDOWS"
        )
        v7_executable_windows = int(
            v7_model_feature_coverage_result[
                "complete_model_feature_close_windows"
            ]
        )
        v7_schema_complete_windows = int(
            v7_model_feature_coverage_result[
                "schema_complete_model_candidate_close_windows"
            ]
        )
        v7_timestamp_failures = len(
            v7_model_feature_coverage_result[
                "model_feature_timestamp_failures"
            ]
        )
        v7_timestamp_clean = v7_timestamp_failures == 0
        v7_model_readiness = {
            "design_id": RTI_CROSS_VENUE_DESIGN_ID,
            "design_sha256": RTI_CROSS_VENUE_DESIGN_SHA256,
            "feature_schema_version": RTI_CROSS_VENUE_FEATURE_SCHEMA_VERSION,
            "feature_count": len(RTI_CROSS_VENUE_FEATURE_NAMES),
            "prospective_after_close_time": (
                RTI_CROSS_VENUE_PROSPECTIVE_AFTER_CLOSE_TIME
            ),
            "first_eligible_close_time": (
                RTI_CROSS_VENUE_FIRST_ELIGIBLE_CLOSE_TIME
            ),
            "paper_only": True,
            "notification_eligible": False,
            "real_trading_allowed": False,
            "automatic_refit": False,
            "automatic_promotion": False,
            "readiness_uses_outcome_labels": False,
            "model_fit_performed": False,
            "artifact_emitted": False,
            "schema_complete_close_windows": v7_schema_complete_windows,
            "complete_executable_close_windows": v7_executable_windows,
            "unusable_close_windows": len(
                v7_model_feature_coverage_result[
                    "unusable_model_feature_close_windows"
                ]
            ),
            "feature_unavailable_rows": len(
                v7_model_feature_coverage_result[
                    "model_feature_unavailable_rows"
                ]
            ),
            "timestamp_alignment_failures": v7_timestamp_failures,
            "timestamp_integrity_clean": v7_timestamp_clean,
            "cohorts": {
                "NON_BTC_TRANSFER": {
                    "minimum_complete_close_windows": 60,
                    "windows_remaining": max(0, 60 - v7_executable_windows),
                    "ready_for_locked_freeze": bool(
                        v7_timestamp_clean and v7_executable_windows >= 60
                    ),
                },
                "BTC": {
                    "minimum_complete_close_windows": 150,
                    "windows_remaining": max(0, 150 - v7_executable_windows),
                    "ready_for_locked_freeze": bool(
                        v7_timestamp_clean and v7_executable_windows >= 150
                    ),
                },
            },
        }
        v7_model_readiness["ready_for_any_locked_freeze"] = any(
            bool(raw["ready_for_locked_freeze"])
            for raw in v7_model_readiness["cohorts"].values()
        )
        v7_model_readiness["status"] = (
            "TIMESTAMP_INTEGRITY_FAILURE_REVIEW_REQUIRED"
            if not v7_timestamp_clean
            else "READY_FOR_SEPARATE_LOCKED_FREEZE"
            if v7_model_readiness["ready_for_any_locked_freeze"]
            else "WAITING_FOR_COMPLETE_EXECUTABLE_WINDOWS"
        )
        v8_executable_windows = int(
            v8_model_feature_coverage_result[
                "complete_model_feature_close_windows"
            ]
        )
        v8_schema_complete_windows = int(
            v8_model_feature_coverage_result[
                "schema_complete_model_candidate_close_windows"
            ]
        )
        v8_timestamp_failures = len(
            v8_model_feature_coverage_result[
                "model_feature_timestamp_failures"
            ]
        )
        v8_timestamp_clean = v8_timestamp_failures == 0
        v8_model_readiness = {
            "design_id": RTI_INDEPENDENT_VENUE_DESIGN_ID,
            "design_sha256": RTI_INDEPENDENT_VENUE_DESIGN_SHA256,
            "feature_schema_version": RTI_INDEPENDENT_VENUE_FEATURE_SCHEMA_VERSION,
            "feature_count": len(RTI_INDEPENDENT_VENUE_FEATURE_NAMES),
            "prospective_after_close_time": RTI_INDEPENDENT_VENUE_PROSPECTIVE_AFTER_CLOSE_TIME,
            "first_eligible_close_time": RTI_INDEPENDENT_VENUE_FIRST_ELIGIBLE_CLOSE_TIME,
            "paper_only": True,
            "notification_eligible": False,
            "real_trading_allowed": False,
            "automatic_refit": False,
            "automatic_promotion": False,
            "readiness_uses_outcome_labels": False,
            "model_fit_performed": False,
            "artifact_emitted": False,
            "schema_complete_close_windows": v8_schema_complete_windows,
            "complete_executable_close_windows": v8_executable_windows,
            "unusable_close_windows": len(
                v8_model_feature_coverage_result[
                    "unusable_model_feature_close_windows"
                ]
            ),
            "feature_unavailable_rows": len(
                v8_model_feature_coverage_result[
                    "model_feature_unavailable_rows"
                ]
            ),
            "timestamp_alignment_failures": v8_timestamp_failures,
            "timestamp_integrity_clean": v8_timestamp_clean,
            "cohorts": {
                "NON_BTC_TRANSFER": {
                    "minimum_complete_close_windows": 60,
                    "windows_remaining": max(0, 60 - v8_executable_windows),
                    "ready_for_locked_freeze": bool(
                        v8_timestamp_clean and v8_executable_windows >= 60
                    ),
                },
                "BTC": {
                    "minimum_complete_close_windows": 150,
                    "windows_remaining": max(0, 150 - v8_executable_windows),
                    "ready_for_locked_freeze": bool(
                        v8_timestamp_clean and v8_executable_windows >= 150
                    ),
                },
            },
        }
        v8_model_readiness["ready_for_any_locked_freeze"] = any(
            bool(raw["ready_for_locked_freeze"])
            for raw in v8_model_readiness["cohorts"].values()
        )
        v8_model_readiness["status"] = (
            "TIMESTAMP_INTEGRITY_FAILURE_REVIEW_REQUIRED"
            if not v8_timestamp_clean
            else "READY_FOR_SEPARATE_LOCKED_FREEZE"
            if v8_model_readiness["ready_for_any_locked_freeze"]
            else "WAITING_FOR_COMPLETE_EXECUTABLE_WINDOWS"
        )
        v9_executable_windows = int(
            v9_model_feature_coverage_result[
                "complete_model_feature_close_windows"
            ]
        )
        v9_schema_complete_windows = int(
            v9_model_feature_coverage_result[
                "schema_complete_model_candidate_close_windows"
            ]
        )
        v9_timestamp_failures = len(
            v9_model_feature_coverage_result[
                "model_feature_timestamp_failures"
            ]
        )
        v9_timestamp_clean = v9_timestamp_failures == 0
        v9_model_readiness = {
            "design_id": RTI_INDEPENDENT_MICROSTRUCTURE_DESIGN_ID,
            "design_sha256": RTI_INDEPENDENT_MICROSTRUCTURE_DESIGN_SHA256,
            "feature_schema_version": (
                RTI_INDEPENDENT_MICROSTRUCTURE_FEATURE_SCHEMA_VERSION
            ),
            "feature_count": len(RTI_INDEPENDENT_MICROSTRUCTURE_FEATURE_NAMES),
            "prospective_after_close_time": (
                RTI_INDEPENDENT_MICROSTRUCTURE_PROSPECTIVE_AFTER_CLOSE_TIME
            ),
            "first_eligible_close_time": (
                RTI_INDEPENDENT_MICROSTRUCTURE_FIRST_ELIGIBLE_CLOSE_TIME
            ),
            "paper_only": True,
            "notification_eligible": False,
            "real_trading_allowed": False,
            "automatic_refit": False,
            "automatic_promotion": False,
            "readiness_uses_outcome_labels": False,
            "model_fit_performed": False,
            "artifact_emitted": False,
            "schema_complete_close_windows": v9_schema_complete_windows,
            "complete_executable_close_windows": v9_executable_windows,
            "unusable_close_windows": len(
                v9_model_feature_coverage_result[
                    "unusable_model_feature_close_windows"
                ]
            ),
            "feature_unavailable_rows": len(
                v9_model_feature_coverage_result[
                    "model_feature_unavailable_rows"
                ]
            ),
            "timestamp_alignment_failures": v9_timestamp_failures,
            "timestamp_integrity_clean": v9_timestamp_clean,
            "cohorts": {
                "NON_BTC_TRANSFER": {
                    "minimum_complete_close_windows": 60,
                    "windows_remaining": max(0, 60 - v9_executable_windows),
                    "ready_for_locked_freeze": bool(
                        v9_timestamp_clean and v9_executable_windows >= 60
                    ),
                },
                "BTC": {
                    "minimum_complete_close_windows": 150,
                    "windows_remaining": max(0, 150 - v9_executable_windows),
                    "ready_for_locked_freeze": bool(
                        v9_timestamp_clean and v9_executable_windows >= 150
                    ),
                },
            },
        }
        v9_model_readiness["ready_for_any_locked_freeze"] = any(
            bool(raw["ready_for_locked_freeze"])
            for raw in v9_model_readiness["cohorts"].values()
        )
        v9_model_readiness["status"] = (
            "TIMESTAMP_INTEGRITY_FAILURE_REVIEW_REQUIRED"
            if not v9_timestamp_clean
            else "READY_FOR_SEPARATE_LOCKED_FREEZE"
            if v9_model_readiness["ready_for_any_locked_freeze"]
            else "WAITING_FOR_COMPLETE_EXECUTABLE_WINDOWS"
        )
        v10_executable_windows = int(
            v10_model_feature_coverage_result[
                "complete_model_feature_close_windows"
            ]
        )
        v10_schema_complete_windows = int(
            v10_model_feature_coverage_result[
                "schema_complete_model_candidate_close_windows"
            ]
        )
        v10_timestamp_failures = len(
            v10_model_feature_coverage_result[
                "model_feature_timestamp_failures"
            ]
        )
        v10_timestamp_clean = v10_timestamp_failures == 0
        v10_model_readiness = {
            **v9_model_readiness,
            "design_id": RTI_COMPACT_MICROSTRUCTURE_DESIGN_ID,
            "design_sha256": RTI_COMPACT_MICROSTRUCTURE_DESIGN_SHA256,
            "feature_schema_version": RTI_COMPACT_MICROSTRUCTURE_FEATURE_SCHEMA_VERSION,
            "feature_count": len(RTI_COMPACT_MICROSTRUCTURE_FEATURE_NAMES),
            "prospective_after_close_time": RTI_COMPACT_MICROSTRUCTURE_PROSPECTIVE_AFTER_CLOSE_TIME,
            "first_eligible_close_time": RTI_COMPACT_MICROSTRUCTURE_FIRST_ELIGIBLE_CLOSE_TIME,
            "schema_complete_close_windows": v10_schema_complete_windows,
            "complete_executable_close_windows": v10_executable_windows,
            "unusable_close_windows": len(v10_model_feature_coverage_result[
                "unusable_model_feature_close_windows"
            ]),
            "feature_unavailable_rows": len(v10_model_feature_coverage_result[
                "model_feature_unavailable_rows"
            ]),
            "timestamp_alignment_failures": v10_timestamp_failures,
            "timestamp_integrity_clean": v10_timestamp_clean,
            "cohorts": {
                "NON_BTC_TRANSFER": {
                    "minimum_complete_close_windows": 60,
                    "windows_remaining": max(0, 60 - v10_executable_windows),
                    "ready_for_locked_freeze": bool(
                        v10_timestamp_clean and v10_executable_windows >= 60
                    ),
                },
                "BTC": {
                    "minimum_complete_close_windows": 150,
                    "windows_remaining": max(0, 150 - v10_executable_windows),
                    "ready_for_locked_freeze": bool(
                        v10_timestamp_clean and v10_executable_windows >= 150
                    ),
                },
            },
        }
        v10_model_readiness["ready_for_any_locked_freeze"] = any(
            bool(raw["ready_for_locked_freeze"])
            for raw in v10_model_readiness["cohorts"].values()
        )
        v10_model_readiness["status"] = (
            "TIMESTAMP_INTEGRITY_FAILURE_REVIEW_REQUIRED"
            if not v10_timestamp_clean
            else "READY_FOR_SEPARATE_LOCKED_FREEZE"
            if v10_model_readiness["ready_for_any_locked_freeze"]
            else "WAITING_FOR_COMPLETE_EXECUTABLE_WINDOWS"
        )
        v11_executable_windows = int(v11_model_feature_coverage_result[
            "complete_model_feature_close_windows"
        ])
        v11_schema_complete_windows = int(v11_model_feature_coverage_result[
            "schema_complete_model_candidate_close_windows"
        ])
        v11_timestamp_failures = len(v11_model_feature_coverage_result[
            "model_feature_timestamp_failures"
        ])
        v11_timestamp_clean = v11_timestamp_failures == 0
        v11_model_readiness = {
            **v10_model_readiness,
            "design_id": RTI_CROSS_ASSET_REGIME_DESIGN_ID,
            "design_sha256": RTI_CROSS_ASSET_REGIME_DESIGN_SHA256,
            "feature_schema_version": RTI_CROSS_ASSET_REGIME_FEATURE_SCHEMA_VERSION,
            "feature_count": len(RTI_CROSS_ASSET_REGIME_FEATURE_NAMES),
            "prospective_after_close_time": RTI_CROSS_ASSET_REGIME_PROSPECTIVE_AFTER_CLOSE_TIME,
            "first_eligible_close_time": RTI_CROSS_ASSET_REGIME_FIRST_ELIGIBLE_CLOSE_TIME,
            "schema_complete_close_windows": v11_schema_complete_windows,
            "complete_executable_close_windows": v11_executable_windows,
            "unusable_close_windows": len(v11_model_feature_coverage_result[
                "unusable_model_feature_close_windows"
            ]),
            "feature_unavailable_rows": len(v11_model_feature_coverage_result[
                "model_feature_unavailable_rows"
            ]),
            "timestamp_alignment_failures": v11_timestamp_failures,
            "timestamp_integrity_clean": v11_timestamp_clean,
            "cohorts": {
                "NON_BTC_TRANSFER": {
                    "minimum_complete_close_windows": 60,
                    "windows_remaining": max(0, 60 - v11_executable_windows),
                    "ready_for_locked_freeze": bool(
                        v11_timestamp_clean and v11_executable_windows >= 60
                    ),
                },
                "BTC": {
                    "minimum_complete_close_windows": 150,
                    "windows_remaining": max(0, 150 - v11_executable_windows),
                    "ready_for_locked_freeze": bool(
                        v11_timestamp_clean and v11_executable_windows >= 150
                    ),
                },
            },
        }
        v11_model_readiness["ready_for_any_locked_freeze"] = any(
            bool(raw["ready_for_locked_freeze"])
            for raw in v11_model_readiness["cohorts"].values()
        )
        v11_model_readiness["status"] = (
            "TIMESTAMP_INTEGRITY_FAILURE_REVIEW_REQUIRED"
            if not v11_timestamp_clean
            else "READY_FOR_SEPARATE_LOCKED_FREEZE"
            if v11_model_readiness["ready_for_any_locked_freeze"]
            else "WAITING_FOR_COMPLETE_EXECUTABLE_WINDOWS"
        )
        v12_executable_windows = int(v12_model_feature_coverage_result[
            "complete_model_feature_close_windows"
        ])
        v12_schema_complete_windows = int(v12_model_feature_coverage_result[
            "schema_complete_model_candidate_close_windows"
        ])
        v12_timestamp_failures = len(v12_model_feature_coverage_result[
            "model_feature_timestamp_failures"
        ])
        v12_timestamp_clean = v12_timestamp_failures == 0
        v12_model_readiness = {
            **v11_model_readiness,
            "design_id": RTI_ORTHOGONAL_COMPACT_DESIGN_ID,
            "design_sha256": RTI_ORTHOGONAL_COMPACT_DESIGN_SHA256,
            "feature_schema_version": (
                RTI_ORTHOGONAL_COMPACT_FEATURE_SCHEMA_VERSION
            ),
            "feature_count": len(RTI_ORTHOGONAL_COMPACT_FEATURE_NAMES),
            "prospective_after_close_time": (
                RTI_ORTHOGONAL_COMPACT_PROSPECTIVE_AFTER_CLOSE_TIME
            ),
            "first_eligible_close_time": (
                RTI_ORTHOGONAL_COMPACT_FIRST_ELIGIBLE_CLOSE_TIME
            ),
            "schema_complete_close_windows": v12_schema_complete_windows,
            "complete_executable_close_windows": v12_executable_windows,
            "unusable_close_windows": len(v12_model_feature_coverage_result[
                "unusable_model_feature_close_windows"
            ]),
            "feature_unavailable_rows": len(v12_model_feature_coverage_result[
                "model_feature_unavailable_rows"
            ]),
            "timestamp_alignment_failures": v12_timestamp_failures,
            "timestamp_integrity_clean": v12_timestamp_clean,
            "cohorts": {
                "NON_BTC_TRANSFER": {
                    "minimum_complete_close_windows": 60,
                    "windows_remaining": max(0, 60 - v12_executable_windows),
                    "ready_for_locked_freeze": bool(
                        v12_timestamp_clean and v12_executable_windows >= 60
                    ),
                },
                "BTC": {
                    "minimum_complete_close_windows": 150,
                    "windows_remaining": max(0, 150 - v12_executable_windows),
                    "ready_for_locked_freeze": bool(
                        v12_timestamp_clean and v12_executable_windows >= 150
                    ),
                },
            },
        }
        v12_model_readiness["ready_for_any_locked_freeze"] = any(
            bool(raw["ready_for_locked_freeze"])
            for raw in v12_model_readiness["cohorts"].values()
        )
        v12_model_readiness["status"] = (
            "TIMESTAMP_INTEGRITY_FAILURE_REVIEW_REQUIRED"
            if not v12_timestamp_clean
            else "READY_FOR_SEPARATE_LOCKED_FREEZE"
            if v12_model_readiness["ready_for_any_locked_freeze"]
            else "WAITING_FOR_COMPLETE_EXECUTABLE_WINDOWS"
        )
        v13_executable_windows = int(v13_model_feature_coverage_result[
            "complete_model_feature_close_windows"
        ])
        v13_schema_complete_windows = int(v13_model_feature_coverage_result[
            "schema_complete_model_candidate_close_windows"
        ])
        v13_timestamp_failures = len(v13_model_feature_coverage_result[
            "model_feature_timestamp_failures"
        ])
        v13_timestamp_clean = v13_timestamp_failures == 0
        v13_model_readiness = {
            **v12_model_readiness,
            "design_id": RTI_COHORT_CONDITIONED_COMPACT_DESIGN_ID,
            "design_sha256": RTI_COHORT_CONDITIONED_COMPACT_DESIGN_SHA256,
            "feature_schema_version": (
                RTI_COHORT_CONDITIONED_COMPACT_FEATURE_SCHEMA_VERSION
            ),
            "feature_count": len(RTI_COHORT_CONDITIONED_COMPACT_FEATURE_NAMES),
            "prospective_after_close_time": (
                RTI_COHORT_CONDITIONED_COMPACT_PROSPECTIVE_AFTER_CLOSE_TIME
            ),
            "first_eligible_close_time": (
                RTI_COHORT_CONDITIONED_COMPACT_FIRST_ELIGIBLE_CLOSE_TIME
            ),
            "schema_complete_close_windows": v13_schema_complete_windows,
            "complete_executable_close_windows": v13_executable_windows,
            "unusable_close_windows": len(v13_model_feature_coverage_result[
                "unusable_model_feature_close_windows"
            ]),
            "feature_unavailable_rows": len(v13_model_feature_coverage_result[
                "model_feature_unavailable_rows"
            ]),
            "timestamp_alignment_failures": v13_timestamp_failures,
            "timestamp_integrity_clean": v13_timestamp_clean,
            "cohorts": {
                "NON_BTC_TRANSFER": {
                    "minimum_complete_close_windows": 60,
                    "windows_remaining": max(0, 60 - v13_executable_windows),
                    "ready_for_locked_freeze": bool(
                        v13_timestamp_clean and v13_executable_windows >= 60
                    ),
                },
                "BTC": {
                    "minimum_complete_close_windows": 150,
                    "windows_remaining": max(0, 150 - v13_executable_windows),
                    "ready_for_locked_freeze": bool(
                        v13_timestamp_clean and v13_executable_windows >= 150
                    ),
                },
            },
            "v11_and_v12_remain_frozen_parallel_controls": True,
        }
        v13_model_readiness["ready_for_any_locked_freeze"] = any(
            bool(raw["ready_for_locked_freeze"])
            for raw in v13_model_readiness["cohorts"].values()
        )
        v13_model_readiness["status"] = (
            "TIMESTAMP_INTEGRITY_FAILURE_REVIEW_REQUIRED"
            if not v13_timestamp_clean
            else "READY_FOR_SEPARATE_LOCKED_FREEZE"
            if v13_model_readiness["ready_for_any_locked_freeze"]
            else "WAITING_FOR_COMPLETE_EXECUTABLE_WINDOWS"
        )
        exact_feature_coverage = {
            "schema_version": RTI_EXACT_MICROSTRUCTURE_SCHEMA_VERSION,
            "paper_only": True,
            "historical_backfill_allowed": False,
            "all_exact": cls._coverage_summary(exact_feature_rows),
            "microstructure_v1": cls._coverage_summary(
                microstructure_v1_rows
            ),
            "microstructure_v1_by_asset": cls._coverage_group(
                microstructure_v1_rows, ("asset",)
            ),
            "microstructure_v2": cls._coverage_summary(
                microstructure_v2_rows
            ),
            "microstructure_v2_by_asset": cls._coverage_group(
                microstructure_v2_rows, ("asset",)
            ),
            "microstructure_extension_v1": cls._coverage_summary(
                microstructure_extension_rows
            ),
            "microstructure_extension_v1_by_asset": cls._coverage_group(
                microstructure_extension_rows, ("asset",)
            ),
            "dynamics_extension_v1": {
                "extension_schema_version": (
                    RTI_MICROSTRUCTURE_EXTENSION_SCHEMA_VERSION
                ),
                "schema_complete_close_windows": int(
                    dynamics_extension_coverage[
                        "schema_complete_extension_close_windows"
                    ]
                ),
                "complete_executable_close_windows": int(
                    dynamics_extension_coverage[
                        "complete_extension_close_windows"
                    ]
                ),
                "unusable_close_windows": len(
                    dynamics_extension_coverage[
                        "unusable_extension_close_windows"
                    ]
                ),
                "feature_unavailable_rows": len(
                    dynamics_extension_coverage[
                        "extension_unavailable_rows"
                    ]
                ),
                "outcome_labels_read": False,
                "model_fit_performed": False,
                "notification_eligible": False,
                "paper_only": True,
            },
            "model_feature_v1": {
                "schema_complete_close_windows": int(
                    v1_model_feature_coverage[
                        "schema_complete_model_candidate_close_windows"
                    ]
                ),
                "complete_executable_close_windows": int(
                    v1_model_feature_coverage[
                        "complete_model_feature_close_windows"
                    ]
                ),
                "unusable_close_windows": len(
                    v1_model_feature_coverage[
                        "unusable_model_feature_close_windows"
                    ]
                ),
                "feature_unavailable_rows": len(
                    v1_model_feature_coverage[
                        "model_feature_unavailable_rows"
                    ]
                ),
                "timestamp_alignment_failures": len(
                    v1_model_feature_coverage[
                        "model_feature_timestamp_failures"
                    ]
                ),
            },
            "model_feature_v2": {
                "schema_complete_close_windows": int(
                    v2_model_feature_coverage_result[
                        "schema_complete_model_candidate_close_windows"
                    ]
                ),
                "complete_executable_close_windows": int(
                    v2_model_feature_coverage_result[
                        "complete_model_feature_close_windows"
                    ]
                ),
                "unusable_close_windows": len(
                    v2_model_feature_coverage_result[
                        "unusable_model_feature_close_windows"
                    ]
                ),
                "feature_unavailable_rows": len(
                    v2_model_feature_coverage_result[
                        "model_feature_unavailable_rows"
                    ]
                ),
                "timestamp_alignment_failures": len(
                    v2_model_feature_coverage_result[
                        "model_feature_timestamp_failures"
                    ]
                ),
            },
            "model_feature_v3": {
                "schema_complete_close_windows": int(
                    v3_model_feature_coverage_result[
                        "schema_complete_model_candidate_close_windows"
                    ]
                ),
                "complete_executable_close_windows": int(
                    v3_model_feature_coverage_result[
                        "complete_model_feature_close_windows"
                    ]
                ),
                "unusable_close_windows": len(
                    v3_model_feature_coverage_result[
                        "unusable_model_feature_close_windows"
                    ]
                ),
                "feature_unavailable_rows": len(
                    v3_model_feature_coverage_result[
                        "model_feature_unavailable_rows"
                    ]
                ),
                "timestamp_alignment_failures": len(
                    v3_model_feature_coverage_result[
                        "model_feature_timestamp_failures"
                    ]
                ),
                "primary_preregistered_design": False,
            },
            "model_feature_v4": {
                "schema_complete_close_windows": schema_complete_windows,
                "complete_executable_close_windows": executable_windows,
                "unusable_close_windows": len(
                    model_feature_coverage[
                        "unusable_model_feature_close_windows"
                    ]
                ),
                "feature_unavailable_rows": len(
                    model_feature_coverage[
                        "model_feature_unavailable_rows"
                    ]
                ),
                "timestamp_alignment_failures": len(
                    model_feature_coverage[
                        "model_feature_timestamp_failures"
                    ]
                ),
                "primary_preregistered_design": True,
            },
            "model_feature_v5": {
                "schema_complete_close_windows": v5_schema_complete_windows,
                "complete_executable_close_windows": v5_executable_windows,
                "unusable_close_windows": len(
                    v5_model_feature_coverage_result[
                        "unusable_model_feature_close_windows"
                    ]
                ),
                "feature_unavailable_rows": len(
                    v5_model_feature_coverage_result[
                        "model_feature_unavailable_rows"
                    ]
                ),
                "timestamp_alignment_failures": v5_timestamp_failures,
                "primary_preregistered_design": False,
                "next_preregistered_design": False,
            },
            "model_feature_v6": {
                "schema_complete_close_windows": v6_schema_complete_windows,
                "complete_executable_close_windows": v6_executable_windows,
                "unusable_close_windows": len(
                    v6_model_feature_coverage_result[
                        "unusable_model_feature_close_windows"
                    ]
                ),
                "feature_unavailable_rows": len(
                    v6_model_feature_coverage_result[
                        "model_feature_unavailable_rows"
                    ]
                ),
                "timestamp_alignment_failures": v6_timestamp_failures,
                "primary_preregistered_design": False,
                "next_preregistered_design": False,
            },
            "model_feature_v7": {
                "schema_complete_close_windows": v7_schema_complete_windows,
                "complete_executable_close_windows": v7_executable_windows,
                "unusable_close_windows": len(
                    v7_model_feature_coverage_result[
                        "unusable_model_feature_close_windows"
                    ]
                ),
                "feature_unavailable_rows": len(
                    v7_model_feature_coverage_result[
                        "model_feature_unavailable_rows"
                    ]
                ),
                "timestamp_alignment_failures": v7_timestamp_failures,
                "primary_preregistered_design": False,
                "next_preregistered_design": False,
            },
            "model_feature_v8": {
                "schema_complete_close_windows": v8_schema_complete_windows,
                "complete_executable_close_windows": v8_executable_windows,
                "unusable_close_windows": len(
                    v8_model_feature_coverage_result[
                        "unusable_model_feature_close_windows"
                    ]
                ),
                "feature_unavailable_rows": len(
                    v8_model_feature_coverage_result[
                        "model_feature_unavailable_rows"
                    ]
                ),
                "timestamp_alignment_failures": v8_timestamp_failures,
                "primary_preregistered_design": False,
                "next_preregistered_design": False,
            },
            "model_feature_v9": {
                "schema_complete_close_windows": v9_schema_complete_windows,
                "complete_executable_close_windows": v9_executable_windows,
                "unusable_close_windows": len(
                    v9_model_feature_coverage_result[
                        "unusable_model_feature_close_windows"
                    ]
                ),
                "feature_unavailable_rows": len(
                    v9_model_feature_coverage_result[
                        "model_feature_unavailable_rows"
                    ]
                ),
                "timestamp_alignment_failures": v9_timestamp_failures,
                "primary_preregistered_design": False,
                "next_preregistered_design": False,
            },
            "model_feature_v10": {
                "schema_complete_close_windows": v10_schema_complete_windows,
                "complete_executable_close_windows": v10_executable_windows,
                "unusable_close_windows": len(v10_model_feature_coverage_result[
                    "unusable_model_feature_close_windows"
                ]),
                "feature_unavailable_rows": len(v10_model_feature_coverage_result[
                    "model_feature_unavailable_rows"
                ]),
                "timestamp_alignment_failures": v10_timestamp_failures,
                "primary_preregistered_design": False,
                "next_preregistered_design": False,
            },
            "model_feature_v11": {
                "schema_complete_close_windows": v11_schema_complete_windows,
                "complete_executable_close_windows": v11_executable_windows,
                "unusable_close_windows": len(v11_model_feature_coverage_result[
                    "unusable_model_feature_close_windows"
                ]),
                "feature_unavailable_rows": len(v11_model_feature_coverage_result[
                    "model_feature_unavailable_rows"
                ]),
                "timestamp_alignment_failures": v11_timestamp_failures,
                "primary_preregistered_design": False,
                "next_preregistered_design": False,
            },
            "model_feature_v12": {
                "schema_complete_close_windows": v12_schema_complete_windows,
                "complete_executable_close_windows": v12_executable_windows,
                "unusable_close_windows": len(v12_model_feature_coverage_result[
                    "unusable_model_feature_close_windows"
                ]),
                "feature_unavailable_rows": len(v12_model_feature_coverage_result[
                    "model_feature_unavailable_rows"
                ]),
                "timestamp_alignment_failures": v12_timestamp_failures,
                "primary_preregistered_design": False,
                "next_preregistered_design": False,
            },
            "model_feature_v13": {
                "schema_complete_close_windows": v13_schema_complete_windows,
                "complete_executable_close_windows": v13_executable_windows,
                "unusable_close_windows": len(v13_model_feature_coverage_result[
                    "unusable_model_feature_close_windows"
                ]),
                "feature_unavailable_rows": len(v13_model_feature_coverage_result[
                    "model_feature_unavailable_rows"
                ]),
                "timestamp_alignment_failures": v13_timestamp_failures,
                "primary_preregistered_design": False,
                "next_preregistered_design": True,
            },
            "preregistered_model_readiness": model_readiness,
            "dynamics_v5_model_readiness": v5_model_readiness,
            "lead_lag_v6_model_readiness": v6_model_readiness,
            "cross_venue_v7_model_readiness": v7_model_readiness,
            "independent_venue_v8_model_readiness": v8_model_readiness,
            "independent_microstructure_v9_model_readiness": (
                v9_model_readiness
            ),
            "independent_microstructure_compact_v10_model_readiness": (
                v10_model_readiness
            ),
            "cross_asset_regime_v11_model_readiness": v11_model_readiness,
            "orthogonal_compact_v12_model_readiness": v12_model_readiness,
            "cohort_conditioned_compact_v13_model_readiness": (
                v13_model_readiness
            ),
        }
        return {
            "paper_only": True,
            "counterfactual": True,
            "notification_eligible": True,
            "policy_versions": sorted(policy_versions),
            "exact_feature_coverage": exact_feature_coverage,
            "probability_scorecards": {
                challenger_id: cls._probability_scorecard(
                    evidence_rows,
                    min_n,
                    challenger_id=challenger_id,
                )
                for challenger_id, evidence_rows in sorted(
                    probability_evidence.items()
                )
            },
            "books": {
                challenger_id: {
                    "overall": cls._agg(book_rows, min_n),
                    "by_asset": cls._group(book_rows, ("asset",), min_n),
                    "by_transfer_cohort": {
                        "BTC": cls._agg(
                            [row for row in book_rows if row.get("asset") == "BTC"],
                            min_n,
                        ),
                        "NON_BTC_TRANSFER": cls._agg(
                            [row for row in book_rows if row.get("asset") != "BTC"],
                            min_n,
                        ),
                    },
                    "notification_eligible": notification_eligibility.get(
                        challenger_id, False
                    ),
                    "evaluated": int(evaluated.get(challenger_id, 0)),
                    "qualified": len(book_rows),
                    "rejected": len(rejected_books.get(challenger_id, ())),
                    "qualification_rate": (
                        0.0
                        if evaluated.get(challenger_id, 0) <= 0
                        else len(book_rows) / evaluated[challenger_id]
                    ),
                    "failure_counts": dict(
                        failure_counts.get(challenger_id, Counter()).most_common()
                    ),
                    "last_evaluated_close_time": last_evaluated_close.get(
                        challenger_id
                    ),
                    "rejected_counterfactual": cls._agg(
                        rejected_books.get(challenger_id, ()), min_n
                    ),
                    "policy_version": (
                        RTI_PATH_13M_SPOT_CONFIRM_POLICY_VERSION
                        if challenger_id == RTI_PATH_13M_SPOT_CONFIRM_CHALLENGER_ID
                        else RTI_PATH_13M_IMPULSE_POLICY_VERSION
                        if challenger_id == RTI_PATH_13M_IMPULSE_CHALLENGER_ID
                        else RTI_PATH_13M_COUNTERTREND_POLICY_VERSION
                        if challenger_id == RTI_PATH_13M_COUNTERTREND_CHALLENGER_ID
                        else RTI_PATH_13M_DELAYED_CONFIRM_POLICY_VERSION
                        if challenger_id
                        == RTI_PATH_13M_DELAYED_CONFIRM_CHALLENGER_ID
                        else RTI_PATH_13M_DELAYED_CONFIRM_60S_POLICY_VERSION
                        if challenger_id
                        == RTI_PATH_13M_DELAYED_CONFIRM_60S_CHALLENGER_ID
                        else RTI_PATH_13M_DELAYED_CONFIRM_90S_POLICY_VERSION
                        if challenger_id
                        == RTI_PATH_13M_DELAYED_CONFIRM_90S_CHALLENGER_ID
                        else RTI_PATH_13M_DELAYED_FLIP_60S_POLICY_VERSION
                        if challenger_id
                        == RTI_PATH_13M_DELAYED_FLIP_60S_CHALLENGER_ID
                        else RTI_PATH_13M_PROBABILITY_V2_POLICY_VERSION
                        if challenger_id
                        == RTI_PATH_13M_PROBABILITY_V2_CHALLENGER_ID
                        else RTI_PATH_13M_PROBABILITY_V3_POLICY_VERSION
                        if challenger_id
                        == RTI_PATH_13M_PROBABILITY_V3_CHALLENGER_ID
                        else RTI_PATH_13M_MICROSTRUCTURE_V11_POLICY_VERSION
                        if challenger_id
                        == RTI_PATH_13M_MICROSTRUCTURE_V11_CHALLENGER_ID
                        else RTI_PATH_13M_CHALLENGER_POLICY_VERSION
                    ),
                }
                for challenger_id, book_rows in sorted(books.items())
            },
            "review": {
                "resolved_bars": [30, 60, 150],
                "manual_promotion_only": True,
                "strict_book_unchanged": True,
            },
            "point_in_time_risk_diagnostics": {
                "paper_only": True,
                "notification_eligible": False,
                "historical_credit_allowed": False,
                "policy_versions": sorted({
                    str(cls._threshold_value(row, "rti_risk_policy_version"))
                    for row in labeled_exact
                }),
                "labeled_exact_rows": len(labeled_exact),
                "strict_accepted_labeled_rows": len(labeled_strict),
                "all_exact_rows": _risk_report(labeled_exact),
                "strict_control_rows": _risk_report(labeled_strict),
            },
            "delayed_confirmation_matched": delayed_matched,
            "delayed_confirmation_60s_matched": delayed_60s_matched,
            "delayed_confirmation_90s_matched": delayed_90s_matched,
            "delayed_flip_60s_matched": delayed_flip_60s_matched,
            "delayed_confirmation_ladder": delayed_confirmation_ladder,
        }

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
            "kalshi_microprice": present("kalshi_yes_microprice_cents"),
            "kalshi_dynamics_extension": present(
                "kalshi_microstructure_extension_schema_version"
            ),
            "kalshi_receive_time_basis": sum(
                1
                for row in all_rows
                if row.get("kalshi_microstructure_time_basis")
                == "local_received_at"
            ),
            "kalshi_count_cap_disabled": sum(
                1
                for row in all_rows
                if row.get("kalshi_history_count_capped") in {False, 0, 0.0}
            ),
            **{
                f"kalshi_complete_window_{horizon}s": sum(
                    1
                    for row in all_rows
                    if row.get(
                        f"kalshi_microstructure_window_complete_{horizon}s"
                    ) in {True, 1, 1.0}
                )
                for horizon in (5, 15, 30, 60)
            },
            **{
                f"kalshi_microprice_path_{horizon}s": present(
                    f"kalshi_microprice_change_cents_{horizon}s"
                )
                for horizon in (5, 15, 30, 60)
            },
            **{
                f"kalshi_queue_flow_{horizon}s": any_present((
                    f"kalshi_book_add_volume_yes_{horizon}s",
                    f"kalshi_book_remove_volume_yes_{horizon}s",
                    f"kalshi_book_add_volume_no_{horizon}s",
                    f"kalshi_book_remove_volume_no_{horizon}s",
                ))
                for horizon in (5, 15, 30, 60)
            },
            **{
                f"kalshi_trade_price_path_{horizon}s": present(
                    f"kalshi_trade_yes_price_change_cents_{horizon}s"
                )
                for horizon in (5, 15, 30, 60)
            },
            **{
                f"kalshi_event_window_{horizon}s": present(
                    f"kalshi_event_count_{horizon}s"
                )
                for horizon in (5, 15, 30, 60)
            },
            **{
                f"kalshi_pressure_{horizon}s": present(
                    f"kalshi_book_delta_pressure_yes_{horizon}s"
                )
                for horizon in (5, 15, 30, 60)
            },
            **{
                f"kalshi_trade_window_{horizon}s": present(
                    f"kalshi_trade_count_{horizon}s"
                )
                for horizon in (5, 15, 30, 60)
            },
            **{
                f"kalshi_trade_imbalance_{horizon}s": present(
                    f"kalshi_trade_imbalance_yes_{horizon}s"
                )
                for horizon in (5, 15, 30, 60)
            },
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
