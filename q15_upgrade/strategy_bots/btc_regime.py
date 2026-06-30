"""Point-in-time BTC regime enrichment for V3 strategy-bot rows.

This is a record-only context layer. It stamps BTC market state onto alt rows so
we can test whether BTC should become a hard gate later, without changing live
alert behavior today.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


def _path(name: str, default: str) -> str:
    return os.environ.get(name) or default


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


def _asset(row: Mapping[str, Any]) -> str:
    return str(row.get("asset") or "").upper()


def _side_value(value: Any) -> str | None:
    side = str(value or "").upper()
    return side if side in {"YES", "NO"} else None


def _side(row: Mapping[str, Any]) -> str | None:
    return _side_value(
        row.get("predicted_side")
        or row.get("predicted_outcome")
        or row.get("selected_side")
        or row.get("side")
    )


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def _nearest(
    path: str,
    sql: str,
    params: tuple[Any, ...],
    timestamp: float,
    max_age: float,
) -> tuple[dict[str, Any] | None, float | None, str | None]:
    if not Path(path).exists():
        return None, None, "db_missing"
    try:
        conn = _connect(path)
        try:
            row = conn.execute(sql, params).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return None, None, f"query_error:{type(exc).__name__}"
    if row is None:
        return None, None, "snapshot_missing"
    data = dict(row)
    age = max(0.0, timestamp - float(data.get("created_at") or timestamp))
    if age > max_age:
        return data, age, "snapshot_gt_max_age"
    return data, age, None


def _parse_levels(raw: Any) -> list[tuple[float, float]]:
    if not raw:
        return []
    try:
        arr = json.loads(str(raw))
    except Exception:
        return []
    out: list[tuple[float, float]] = []
    for item in arr:
        try:
            price = float(item[0])
            qty = float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if price > 0.0 and qty >= 0.0 and math.isfinite(price) and math.isfinite(qty):
            out.append((price, qty))
    return out


def _imbalance(left: float, right: float) -> float | None:
    denom = left + right
    return (left - right) / denom if denom > 0.0 else None


def _band_imbalance(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    levels: int,
) -> tuple[float | None, float | None, float | None, float | None]:
    bid_qty = sum(qty for _, qty in bids[:levels])
    ask_qty = sum(qty for _, qty in asks[:levels])
    bid_notional = sum(price * qty for price, qty in bids[:levels])
    ask_notional = sum(price * qty for price, qty in asks[:levels])
    return (
        bid_notional,
        ask_notional,
        _imbalance(bid_qty, ask_qty),
        _imbalance(bid_notional, ask_notional),
    )


def _vote_from_number(value: Any, *, deadband: float = 0.0) -> str | None:
    num = _num(value)
    if num is None:
        return None
    if num > deadband:
        return "YES"
    if num < -deadband:
        return "NO"
    return None


def _add_vote(votes: list[str], detail: list[str], label: str, side: str | None) -> None:
    if side in {"YES", "NO"}:
        votes.append(side)
        detail.append(f"{label}:{side}")


def _score_regime(votes: list[str]) -> str:
    yes = votes.count("YES")
    no = votes.count("NO")
    if yes >= 4 and yes - no >= 2:
        return "BULLISH"
    if no >= 4 and no - yes >= 2:
        return "BEARISH"
    return "CHOP"


def _agreement(regime: str | None, side: str | None) -> str | None:
    if regime is None or side is None:
        return None
    if regime == "CHOP":
        return "CHOP"
    btc_side = "YES" if regime == "BULLISH" else "NO"
    return "AGREES" if btc_side == side else "CONTRADICTS"


def _book_imbalance(yes_depth: Any, no_depth: Any) -> float | None:
    yes = _num(yes_depth)
    no = _num(no_depth)
    if yes is None or no is None or yes + no <= 0:
        return None
    return (yes - no) / (yes + no)


def enrich_btc_regime(
    row: Mapping[str, Any],
    btc_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    out = dict(row)
    if not _bool("Q15_V3_BTC_REGIME_ENABLED", True):
        out.setdefault("btc_regime_status", "disabled")
        out.setdefault("btc_regime_missing_reason", "btc_regime_disabled")
        return out
    if _asset(out) == "BTC":
        out.setdefault("btc_regime_status", "self")
        out.setdefault("btc_regime_missing_reason", "asset_is_btc")
        return out
    timestamp = _num(out.get("created_at"))
    if timestamp is None:
        out["btc_regime_status"] = "missing"
        out["btc_regime_missing_reason"] = "missing_timestamp"
        return out

    max_age = _float_env("Q15_V3_BTC_REGIME_MAX_AGE_SECONDS", 180.0, minimum=1.0)
    votes: list[str] = []
    detail: list[str] = []
    missing: list[str] = []

    spot_db = _path("Q15_SPOT_DEPTH_DB", "data/q15_spot_depth_v1.sqlite3")
    spot, spot_age, spot_missing = _nearest(
        spot_db,
        "SELECT * FROM spot_depth_snapshots WHERE asset='BTC' AND created_at<=? "
        "ORDER BY created_at DESC LIMIT 1",
        (timestamp,),
        timestamp,
        max_age,
    )
    if spot_missing:
        missing.append(f"spot:{spot_missing}")
    if spot and not spot_missing:
        buy15 = _num(spot.get("trade_buy_notional_15s")) or 0.0
        sell15 = _num(spot.get("trade_sell_notional_15s")) or 0.0
        buy60 = _num(spot.get("trade_buy_notional_60s")) or 0.0
        sell60 = _num(spot.get("trade_sell_notional_60s")) or 0.0
        out["btc_spot_age_seconds"] = spot_age
        out["btc_spot_depth_imbalance"] = _num(spot.get("depth_imbalance"))
        out["btc_spot_trade_net_notional_15s"] = buy15 - sell15
        out["btc_spot_trade_net_notional_60s"] = buy60 - sell60
        _add_vote(votes, detail, "spot15", _vote_from_number(out["btc_spot_trade_net_notional_15s"]))
        _add_vote(votes, detail, "spot60", _vote_from_number(out["btc_spot_trade_net_notional_60s"]))
        _add_vote(votes, detail, "spot_imb", _vote_from_number(out["btc_spot_depth_imbalance"], deadband=0.01))

    l2_db = _path("Q15_COINBASE_ADV_L2_DB", "data/q15_coinbase_adv_l2_v1.sqlite3")
    l2, l2_age, l2_missing = _nearest(
        l2_db,
        "SELECT * FROM coinbase_adv_l2_snapshots WHERE product_id='BTC-USD' AND created_at<=? "
        "ORDER BY created_at DESC LIMIT 1",
        (timestamp,),
        timestamp,
        max_age,
    )
    if l2_missing:
        missing.append(f"coinbase_l2:{l2_missing}")
    if l2 and not l2_missing:
        bids = _parse_levels(l2.get("bid_levels_json"))
        asks = _parse_levels(l2.get("ask_levels_json"))
        out["btc_coinbase_l2_age_seconds"] = l2_age
        out["btc_coinbase_l2_last_message_age_seconds"] = l2.get("last_message_age_seconds")
        for band in (12, 60, 250):
            bid_notional, ask_notional, _, imb_notional = _band_imbalance(bids, asks, band)
            out[f"btc_coinbase_l2_top_{band}_bid_notional"] = bid_notional
            out[f"btc_coinbase_l2_top_{band}_ask_notional"] = ask_notional
            out[f"btc_coinbase_l2_top_{band}_imbalance_notional"] = imb_notional
            _add_vote(votes, detail, f"top{band}", _vote_from_number(imb_notional, deadband=0.01))

    l3_db = _path("Q15_KRAKEN_L3_DB", "data/q15_kraken_l3_v1.sqlite3")
    l3, l3_age, l3_missing = _nearest(
        l3_db,
        "SELECT * FROM kraken_l3_summaries WHERE symbol='BTC/USD' AND created_at<=? "
        "ORDER BY created_at DESC LIMIT 1",
        (timestamp,),
        timestamp,
        max_age,
    )
    if l3_missing:
        missing.append(f"kraken_l3:{l3_missing}")
    if l3 and not l3_missing:
        out["btc_kraken_l3_age_seconds"] = l3_age
        out["btc_kraken_l3_depth_imbalance"] = _num(l3.get("depth_imbalance"))
        out["btc_kraken_l3_cancel_to_add_60s"] = _num(l3.get("cancel_to_add_60s"))
        out["btc_kraken_l3_net_matched_buy_notional_60s"] = _num(
            l3.get("net_matched_buy_notional_60s")
        )
        _add_vote(votes, detail, "l3", _vote_from_number(out["btc_kraken_l3_depth_imbalance"], deadband=0.10))
        _add_vote(votes, detail, "l3_matched", _vote_from_number(out["btc_kraken_l3_net_matched_buy_notional_60s"]))

    v95_db = _path("Q15_V95_LEDGER_DB", "data/q15_v95_ledger_v1.sqlite3")
    v95, v95_age, v95_missing = _nearest(
        v95_db,
        "SELECT * FROM predictions WHERE asset='BTC' AND created_at<=? "
        "ORDER BY created_at DESC LIMIT 1",
        (timestamp,),
        timestamp,
        max_age,
    )
    if v95_missing:
        missing.append(f"v95:{v95_missing}")
    if v95 and not v95_missing:
        out["btc_v95_age_seconds"] = v95_age
        out["btc_v95_checkpoint"] = v95.get("checkpoint")
        out["btc_v95_grade"] = v95.get("confidence_grade")
        out["btc_v95_predicted_side"] = _side_value(v95.get("predicted_side"))
        out["btc_v95_selected_probability"] = _num(v95.get("selected_probability"))
        if out.get("btc_v95_grade") in {"A", "B"}:
            _add_vote(votes, detail, f"v95{out.get('btc_v95_grade')}", out.get("btc_v95_predicted_side"))
        try:
            quote = json.loads(str(v95.get("quote_json") or "{}"))
        except Exception:
            quote = {}
        out["btc_kalshi_taker_net_yes_volume_15s"] = _num(
            quote.get("kalshi_taker_net_yes_volume_15s")
        )
        out["btc_kalshi_book_imbalance"] = _book_imbalance(
            quote.get("yes_bid_depth_contracts"),
            quote.get("no_bid_depth_contracts"),
        )
        _add_vote(votes, detail, "btc_taker", _vote_from_number(out["btc_kalshi_taker_net_yes_volume_15s"]))
        _add_vote(votes, detail, "btc_kalshi_book", _vote_from_number(out["btc_kalshi_book_imbalance"], deadband=0.05))

    source = btc_context or {}
    if source:
        out["btc_ticker"] = source.get("ticker") or out.get("btc_ticker")
        out["btc_depth_contracts"] = _num(source.get("depth_contracts") or out.get("btc_depth_contracts"))
        out["btc_dominant_side"] = _side_value(source.get("dominant_side") or out.get("btc_dominant_side"))
        out["btc_model_predicted_side"] = _side_value(
            source.get("predicted_side") or out.get("btc_model_predicted_side")
        )
        out["btc_model_yes_probability"] = _num(
            source.get("model_yes_probability") or out.get("btc_model_yes_probability")
        )
        out["btc_calibrated_yes_probability"] = _num(
            source.get("calibrated_yes_probability") or out.get("btc_calibrated_yes_probability")
        )
        out["btc_market_implied_yes_probability"] = _num(
            source.get("market_implied_yes_probability")
            or out.get("btc_market_implied_yes_probability")
        )
        _add_vote(votes, detail, "btc_dominant", _side_value(out.get("btc_dominant_side")))
        _add_vote(votes, detail, "btc_model", _side_value(out.get("btc_model_predicted_side")))

    regime = _score_regime(votes)
    out["btc_regime"] = regime
    out["btc_regime_agreement"] = _agreement(regime, _side(out))
    out["btc_regime_vote_yes"] = votes.count("YES")
    out["btc_regime_vote_no"] = votes.count("NO")
    out["btc_regime_vote_detail"] = ";".join(detail)
    out["btc_regime_status"] = "ok" if detail else "missing"
    out["btc_regime_missing_reason"] = ",".join(missing) if missing else None
    return out
