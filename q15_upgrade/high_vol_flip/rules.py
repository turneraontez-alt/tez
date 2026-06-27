"""Pure HVF quote reconstruction and rule evaluation."""
from __future__ import annotations

import math
from typing import Any, Mapping


REASON_OWN_NO_FLASH = "HVF_OWN_NO_FLASH"
REASON_OWN_STRONG_SELECTED = "HVF_OWN_STRONG_SELECTED"
REASON_HYPE_BULLISH_FLASH = "HVF_HYPE_BULLISH_FLASH"
REASON_OWN_EARLY_FLIP = "HVF_OWN_EARLY_FLIP"
REASON_HYPE_EARLY_BULLISH_FLIP = "HVF_HYPE_EARLY_BULLISH_FLIP"
REASON_BTC_EARLY_FOLLOW_LAG = "HVF_BTC_EARLY_FOLLOW_LAG"
REASON_BTC_FOLLOW_EXTREME = "HVF_BTC_FOLLOW_EXTREME"
REASON_BTC_DIVERGENCE_ACCEL_WATCH = "HVF_BTC_DIVERGENCE_ACCEL_WATCH"


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        v = float(value)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _side(value: Any) -> str | None:
    s = str(value or "").upper()
    return s if s in {"YES", "NO"} else None


def _clip_cents(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, min(100.0, float(value)))


def _mid(bid: float | None, ask: float | None) -> float | None:
    vals = [v for v in (bid, ask) if v is not None and 0.0 <= v <= 100.0]
    return sum(vals) / len(vals) if vals else None


def reconstruct_side_quotes(
    predicted_side: str | None,
    selected_bid_cents: Any,
    selected_ask_cents: Any,
) -> dict[str, float | None]:
    """Rebuild true YES/NO quotes from the champion's selected-side quote.

    The live analysis stores ``quote.bid_cents`` / ``quote.ask_cents`` for the
    predicted side, not always YES. For a NO prediction, the YES quote is the
    inverse side: YES bid = 100 - NO ask, YES ask = 100 - NO bid.
    """
    side = _side(predicted_side)
    bid = _clip_cents(_num(selected_bid_cents))
    ask = _clip_cents(_num(selected_ask_cents))
    out: dict[str, float | None] = {
        "yes_bid_cents": None,
        "yes_ask_cents": None,
        "no_bid_cents": None,
        "no_ask_cents": None,
    }
    if side == "YES":
        out["yes_bid_cents"] = bid
        out["yes_ask_cents"] = ask
        out["no_bid_cents"] = None if ask is None else _clip_cents(100.0 - ask)
        out["no_ask_cents"] = None if bid is None else _clip_cents(100.0 - bid)
    elif side == "NO":
        out["no_bid_cents"] = bid
        out["no_ask_cents"] = ask
        out["yes_bid_cents"] = None if ask is None else _clip_cents(100.0 - ask)
        out["yes_ask_cents"] = None if bid is None else _clip_cents(100.0 - bid)
    return out


def side_bid(cand: Mapping[str, Any], side: str | None) -> float | None:
    s = _side(side)
    if s == "YES":
        return _num(cand.get("yes_bid_cents"))
    if s == "NO":
        return _num(cand.get("no_bid_cents"))
    return None


def side_ask(cand: Mapping[str, Any], side: str | None) -> float | None:
    s = _side(side)
    if s == "YES":
        return _num(cand.get("yes_ask_cents"))
    if s == "NO":
        return _num(cand.get("no_ask_cents"))
    return None


def side_mid(cand: Mapping[str, Any], side: str | None) -> float | None:
    return _mid(side_bid(cand, side), side_ask(cand, side))


def opposite_side(side: str | None) -> str | None:
    s = _side(side)
    if s == "YES":
        return "NO"
    if s == "NO":
        return "YES"
    return None


def extract_candidate(asset: Any, analysis: Mapping[str, Any], canonical: Any) -> dict[str, Any] | None:
    if canonical is None or not isinstance(analysis, Mapping):
        return None
    if not analysis.get("prediction_available"):
        return None
    ticker = getattr(canonical, "ticker", None)
    seconds = _num(getattr(canonical, "seconds_remaining", None))
    if not ticker or seconds is None:
        return None
    predicted = _side(analysis.get("prediction_side"))
    if predicted is None:
        return None
    dq = _num(analysis.get("data_quality"))
    quote = analysis.get("quote") if isinstance(analysis.get("quote"), Mapping) else {}
    selected_bid = _num(quote.get("bid_cents"))
    selected_ask = _num(quote.get("ask_cents"))
    quotes = reconstruct_side_quotes(predicted, selected_bid, selected_ask)
    spread = _num(quote.get("spread_cents"))
    if spread is None and selected_bid is not None and selected_ask is not None:
        spread = max(0.0, selected_ask - selected_bid)
    depth = _num(quote.get("ask_depth"))
    if depth is None:
        depth = _num(quote.get("depth_contracts"))
    yes_mid = _mid(quotes["yes_bid_cents"], quotes["yes_ask_cents"])
    no_mid = _mid(quotes["no_bid_cents"], quotes["no_ask_cents"])
    dominant_side = None
    dominant_mid = None
    if yes_mid is not None or no_mid is not None:
        if no_mid is None or (yes_mid is not None and yes_mid >= no_mid):
            dominant_side, dominant_mid = "YES", yes_mid
        else:
            dominant_side, dominant_mid = "NO", no_mid
    close_time = getattr(canonical, "settlement_time", None)
    return {
        "asset": str(asset).upper(),
        "ticker": str(ticker),
        "seconds_remaining": seconds,
        "close_time": _num(close_time),
        "predicted_side": predicted,
        "model_yes_probability": _num(
            analysis.get("model_yes_probability")
            if analysis.get("model_yes_probability") is not None
            else analysis.get("yes_probability")
        ),
        "raw_yes_probability": _num(analysis.get("raw_yes_probability")),
        "calibrated_yes_probability": _num(analysis.get("yes_probability")),
        "conservative_probability": _num(analysis.get("conservative_probability")),
        "data_quality": dq,
        "selected_bid_cents": selected_bid,
        "selected_ask_cents": selected_ask,
        "spread_cents": spread,
        "depth_contracts": depth,
        "yes_mid_cents": yes_mid,
        "no_mid_cents": no_mid,
        "dominant_side": dominant_side,
        "dominant_mid_cents": dominant_mid,
        **quotes,
    }


def _spread_ok(cand: Mapping[str, Any], cfg: Any, limit: float | None = None) -> bool:
    spread = _num(cand.get("spread_cents"))
    max_spread = float(limit if limit is not None else cfg.max_spread_cents)
    return spread is not None and spread <= max_spread


def _depth_ok(cand: Mapping[str, Any], cfg: Any) -> bool:
    minimum = float(getattr(cfg, "min_depth_contracts", 0.0) or 0.0)
    if minimum <= 0.0:
        return True
    depth = _num(cand.get("depth_contracts"))
    return depth is not None and depth >= minimum


def _tradable(cand: Mapping[str, Any], side: str, cfg: Any, *, spread_limit: float | None = None) -> bool:
    return (
        side_bid(cand, side) is not None
        and side_ask(cand, side) is not None
        and _spread_ok(cand, cfg, spread_limit)
        and _depth_ok(cand, cfg)
    )


def _early_price_ok(cand: Mapping[str, Any], side: str | None, cfg: Any) -> bool:
    if not getattr(cfg, "early_enabled", True):
        return False
    ask = side_ask(cand, side)
    mid = side_mid(cand, side)
    return (
        ask is not None
        and mid is not None
        and cfg.early_ask_lo <= ask <= cfg.early_ask_hi
        and cfg.early_mid_lo <= mid <= cfg.early_mid_hi
        and _tradable(cand, str(side), cfg, spread_limit=cfg.early_spread_max)
    )


def btc_jump_cents(current_btc: Mapping[str, Any] | None,
                   previous_btc: Mapping[str, Any] | None) -> float | None:
    if not current_btc or not previous_btc:
        return None
    side = _side(current_btc.get("dominant_side"))
    cur = side_mid(current_btc, side)
    prev = side_mid(previous_btc, side)
    if cur is None or prev is None:
        return None
    return cur - prev


def _decision(cand: Mapping[str, Any], rule: str, reason: str,
              predicted: str, *, btc: Mapping[str, Any] | None = None,
              btc_jump: float | None = None) -> dict[str, Any]:
    return {
        "asset": cand["asset"],
        "ticker": cand["ticker"],
        "rule": rule,
        "reason_code": reason,
        "predicted_outcome": predicted,
        "entry_ask_cents": side_ask(cand, predicted),
        "selected_side": predicted,
        "selected_bid_cents": side_bid(cand, predicted),
        "selected_ask_cents": side_ask(cand, predicted),
        "btc_ticker": None if btc is None else btc.get("ticker"),
        "btc_dominant_side": None if btc is None else btc.get("dominant_side"),
        "btc_dominant_mid_cents": None if btc is None else btc.get("dominant_mid_cents"),
        "btc_selected_bid_cents": None if btc is None else side_bid(btc, btc.get("dominant_side")),
        "btc_selected_ask_cents": None if btc is None else side_ask(btc, btc.get("dominant_side")),
        "btc_yes_mid_cents": None if btc is None else btc.get("yes_mid_cents"),
        "btc_no_mid_cents": None if btc is None else btc.get("no_mid_cents"),
        "btc_jump_cents": btc_jump,
    }


def _own_no_flash(cand: Mapping[str, Any], cfg: Any) -> dict[str, Any] | None:
    model_yes = _num(cand.get("model_yes_probability"))
    if (
        cand.get("predicted_side") == "NO"
        and model_yes is not None
        and model_yes <= cfg.own_no_yes_max
        and (side_bid(cand, "NO") or -1.0) >= cfg.own_no_bid_min
        and _tradable(cand, "NO", cfg)
    ):
        return _decision(cand, "OWN_NO_FLASH", REASON_OWN_NO_FLASH, "NO")
    return None


def _own_strong_selected(cand: Mapping[str, Any], cfg: Any) -> dict[str, Any] | None:
    side = _side(cand.get("predicted_side"))
    if (
        side is not None
        and (side_bid(cand, side) or -1.0) >= cfg.own_strong_bid_min
        and _tradable(cand, side, cfg, spread_limit=cfg.own_strong_spread_max)
    ):
        return _decision(cand, "OWN_STRONG_SELECTED", REASON_OWN_STRONG_SELECTED, side)
    return None


def _own_early_flip(cand: Mapping[str, Any], cfg: Any) -> dict[str, Any] | None:
    if not getattr(cfg, "early_entry_enabled", False):
        return None
    asset = str(cand.get("asset") or "").upper()
    if asset not in cfg.own_early_assets:
        return None
    side = _side(cand.get("predicted_side"))
    model_yes = _num(cand.get("model_yes_probability"))
    if side is None or model_yes is None or not _early_price_ok(cand, side, cfg):
        return None
    yes_ok = side == "YES" and model_yes >= cfg.own_early_yes_min
    no_ok = side == "NO" and model_yes <= cfg.own_early_no_max
    if yes_ok or no_ok:
        return _decision(cand, "OWN_EARLY_FLIP", REASON_OWN_EARLY_FLIP, side)
    return None


def _hype_early_bullish_flip(cand: Mapping[str, Any], cfg: Any) -> dict[str, Any] | None:
    model_yes = _num(cand.get("model_yes_probability"))
    if (
        getattr(cfg, "early_entry_enabled", False)
        and
        cfg.hype_early_enabled
        and
        cand.get("asset") == "HYPE"
        and cand.get("predicted_side") == "YES"
        and model_yes is not None
        and model_yes >= cfg.hype_early_yes_min
        and _early_price_ok(cand, "YES", cfg)
    ):
        return _decision(cand, "HYPE_EARLY_BULLISH_FLIP",
                         REASON_HYPE_EARLY_BULLISH_FLIP, "YES")
    return None


def _hype_bullish_flash(cand: Mapping[str, Any], cfg: Any) -> dict[str, Any] | None:
    model_yes = _num(cand.get("model_yes_probability"))
    if (
        cand.get("asset") == "HYPE"
        and cand.get("predicted_side") == "YES"
        and model_yes is not None
        and model_yes >= cfg.hype_bull_yes_min
        and (side_bid(cand, "YES") or -1.0) >= cfg.hype_bull_yes_bid_min
        and _tradable(cand, "YES", cfg, spread_limit=cfg.hype_bull_spread_max)
    ):
        return _decision(cand, "HYPE_BULLISH_FLASH", REASON_HYPE_BULLISH_FLASH, "YES")
    return None


def _btc_early_follow_lag(cand: Mapping[str, Any], btc: Mapping[str, Any] | None,
                          cfg: Any) -> dict[str, Any] | None:
    asset = str(cand.get("asset") or "").upper()
    if (
        not getattr(cfg, "early_entry_enabled", False)
        or not cfg.btc_early_follow_enabled
        or asset not in cfg.btc_early_follow_assets
        or not btc
    ):
        return None
    btc_side = _side(btc.get("dominant_side"))
    btc_mid = _num(btc.get("dominant_mid_cents"))
    if (
        btc_side is not None
        and btc_mid is not None
        and btc_mid >= cfg.btc_early_follow_mid_min
        and _early_price_ok(cand, btc_side, cfg)
    ):
        return _decision(cand, "BTC_EARLY_FOLLOW_LAG",
                         REASON_BTC_EARLY_FOLLOW_LAG, btc_side, btc=btc)
    return None


def _btc_follow_extreme(cand: Mapping[str, Any], btc: Mapping[str, Any] | None,
                        cfg: Any) -> dict[str, Any] | None:
    asset = str(cand.get("asset") or "").upper()
    if asset not in cfg.btc_follow_assets or not btc:
        return None
    btc_side = _side(btc.get("dominant_side"))
    btc_mid = _num(btc.get("dominant_mid_cents"))
    asset_mid = side_mid(cand, btc_side)
    if (
        btc_side is not None
        and btc_mid is not None
        and btc_mid >= cfg.btc_follow_mid_min
        and asset_mid is not None
        and asset_mid >= cfg.btc_follow_asset_mid_min
        and _tradable(cand, btc_side, cfg)
    ):
        return _decision(cand, "BTC_FOLLOW_EXTREME", REASON_BTC_FOLLOW_EXTREME,
                         btc_side, btc=btc)
    return None


def _btc_divergence_watch(cand: Mapping[str, Any], btc: Mapping[str, Any] | None,
                          prev_btc: Mapping[str, Any] | None, cfg: Any) -> dict[str, Any] | None:
    asset = str(cand.get("asset") or "").upper()
    if not cfg.divergence_watch_enabled or asset not in cfg.divergence_watch_assets or not btc:
        return None
    btc_side = _side(btc.get("dominant_side"))
    btc_mid = _num(btc.get("dominant_mid_cents"))
    jump = btc_jump_cents(btc, prev_btc)
    same_mid = side_mid(cand, btc_side)
    pred = opposite_side(btc_side)
    if (
        btc_side is not None
        and pred is not None
        and btc_mid is not None
        and btc_mid >= cfg.divergence_btc_mid_min
        and jump is not None
        and jump >= cfg.divergence_btc_jump_min
        and same_mid is not None
        and cfg.divergence_asset_same_mid_lo <= same_mid <= cfg.divergence_asset_same_mid_hi
        and _tradable(cand, pred, cfg)
    ):
        return _decision(cand, "BTC_DIVERGENCE_ACCEL_WATCH",
                         REASON_BTC_DIVERGENCE_ACCEL_WATCH, pred, btc=btc, btc_jump=jump)
    return None


def evaluate_rules(cand: Mapping[str, Any], btc: Mapping[str, Any] | None,
                   prev_btc: Mapping[str, Any] | None, cfg: Any) -> dict[str, Any] | None:
    """Return the first confirmed rule for this asset/window, or None."""
    asset = str(cand.get("asset") or "").upper()
    if asset == "HYPE":
        checks = (
            lambda: _hype_early_bullish_flip(cand, cfg),
            lambda: _own_early_flip(cand, cfg),
            lambda: _hype_bullish_flash(cand, cfg),
            lambda: _own_no_flash(cand, cfg),
        )
    elif asset == "BNB":
        checks = (
            lambda: _own_early_flip(cand, cfg),
            lambda: _own_no_flash(cand, cfg),
            lambda: _own_strong_selected(cand, cfg),
        )
    elif asset == "DOGE":
        checks = (
            lambda: _own_early_flip(cand, cfg),
            lambda: _btc_early_follow_lag(cand, btc, cfg),
            lambda: _own_strong_selected(cand, cfg),
            lambda: _own_no_flash(cand, cfg),
            lambda: _btc_follow_extreme(cand, btc, cfg),
        )
    elif asset in {"SOL", "ETH"}:
        checks = (
            lambda: _own_early_flip(cand, cfg),
            lambda: _btc_early_follow_lag(cand, btc, cfg),
            lambda: _own_no_flash(cand, cfg),
            lambda: _own_strong_selected(cand, cfg),
            lambda: _btc_follow_extreme(cand, btc, cfg),
        )
    elif asset == "XRP":
        checks = (
            lambda: _own_early_flip(cand, cfg),
            lambda: _btc_early_follow_lag(cand, btc, cfg),
            lambda: _own_no_flash(cand, cfg),
            lambda: _btc_follow_extreme(cand, btc, cfg),
            lambda: _btc_divergence_watch(cand, btc, prev_btc, cfg),
        )
    else:
        checks = (lambda: None,)
    for check in checks:
        decision = check()
        if decision is not None:
            return decision
    return None
