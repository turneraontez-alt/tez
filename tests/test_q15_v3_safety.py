from datetime import datetime, timezone

from q15_upgrade.config import Q15Settings
from q15_upgrade.runtime import Q15Runtime


class Learner:
    def adjustment_for(self, *args):
        return {"edge_adjustment": 0.0, "suppressed": False, "reasons": []}


def make_snapshot(now, candle, direction="none", pattern="none"):
    return {
        "asset": "BTC",
        "ticker": "KXBTC15M-SAFETY",
        "market_state": "live",
        "seconds_remaining": 600,
        "close_time": datetime.fromtimestamp(now + 600, timezone.utc).isoformat(),
        "last_updated": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "underlying_current": candle["close"],
        "target_value": 100.0,
        "target_type": "greater_or_equal",
        "underlying_candles_5s": [candle],
        "pattern_direction": direction,
        "pattern_detected": pattern,
        "latest_wick_metrics": {
            "lower_wick_to_body_ratio": 2.5,
            "upper_wick_to_body_ratio": 0.1,
            "crossed_and_reclaimed_target": True,
        },
        "yes_bid": 45.0,
        "yes_ask": 46.0,
        "no_bid": 54.0,
        "no_ask": 55.0,
        "spread": 1.0,
        "yes_ask_qty": 100,
        "no_ask_qty": 100,
        "yes_ask_levels": [[46.0, 100]],
        "no_ask_levels": [[55.0, 100]],
        "taker_yes_volume_15s": 80,
        "taker_no_volume_15s": 20,
        "recent_trades": [{"ts": now - 1}, {"ts": now - 2}, {"ts": now - 3}],
        "candles_15s": [{"volume": 20}, {"volume": 40}],
        "orderbook_imbalance": 0.4,
        "bid_liquidity_added": 30,
        "ask_liquidity_added": 5,
        "opportunity_score": 0,
        "invalidation_level": 99.0,
    }


def build_runtime():
    return Q15Runtime(
        learner=Learner(),
        settings=Q15Settings(
            min_confirmation_groups=2,
            min_confirmation_score=35,
            min_model_confidence=0.0,
            min_win_probability=0.0,
            edge_6_12m=-100,
            candidate_persistence_seconds=0.0,
            entry_stability_samples=1,
            min_depth_contracts=1,
            minimum_hold_seconds=15,
            soft_exit_persistence_seconds=12,
        ),
    )


def history(base):
    rows = []
    for i in range(20):
        rows.append({
            "start_time": base + i * 5,
            "end_time": base + (i + 1) * 5,
            "open": 100 + i * 0.02,
            "high": 100.1 + i * 0.02,
            "low": 99.9 + i * 0.02,
            "close": 100 + i * 0.02,
        })
    return rows


def enter(runtime, base):
    rows = history(base)
    pattern = rows[-1]
    first = make_snapshot(base + 101, pattern, "bullish", "bullish_rejection")
    first["underlying_candles_5s"] = rows
    runtime.enrich_all({"BTC": first}, base + 101, {"connected": True})

    confirm = {
        "start_time": base + 100,
        "end_time": base + 105,
        "open": pattern["close"],
        "high": pattern["close"] + 0.5,
        "low": pattern["close"] - 0.05,
        "close": pattern["close"] + 0.4,
    }
    second = make_snapshot(base + 106, confirm, "none", "none")
    second["underlying_candles_5s"] = rows + [confirm]
    out = runtime.enrich_all({"BTC": second}, base + 106, {"connected": True})["BTC"]
    assert out["decision_state"] == "ENTRY CONFIRMED"
    return rows, confirm, out


def test_confirmation_flicker_does_not_immediately_invalidate_entry():
    runtime = build_runtime()
    base = 1000.0
    rows, confirm, _ = enter(runtime, base)

    next_candle = {
        "start_time": base + 105,
        "end_time": base + 110,
        "open": confirm["close"],
        "high": confirm["close"] + 0.05,
        "low": confirm["close"] - 0.05,
        "close": confirm["close"] + 0.01,
    }
    weak = make_snapshot(base + 111, next_candle, "none", "none")
    weak["underlying_candles_5s"] = rows + [confirm, next_candle]
    weak.update({
        "taker_yes_volume_15s": 0,
        "taker_no_volume_15s": 0,
        "recent_trades": [],
        "orderbook_imbalance": 0.0,
        "bid_liquidity_added": 0,
        "ask_liquidity_added": 0,
    })

    out = runtime.enrich_all({"BTC": weak}, base + 111, {"connected": True})["BTC"]
    assert out["decision_state"] == "ENTRY CONFIRMED"
    assert "confirmation_groups_weakened" in out["monitor_warnings"]
    assert out["exit_reason"] is None


def test_price_crossed_frozen_invalidation_exits_immediately():
    runtime = build_runtime()
    base = 2000.0
    rows, confirm, _ = enter(runtime, base)

    break_candle = {
        "start_time": base + 105,
        "end_time": base + 110,
        "open": confirm["close"],
        "high": confirm["close"],
        "low": 98.0,
        "close": 98.5,
    }
    broken = make_snapshot(base + 111, break_candle, "none", "none")
    broken["underlying_candles_5s"] = rows + [confirm, break_candle]
    broken["underlying_current"] = 98.5

    out = runtime.enrich_all({"BTC": broken}, base + 111, {"connected": True})["BTC"]
    assert out["decision_state"] == "ENTRY INVALIDATED"
    assert out["exit_reason"] == "price_crossed_frozen_invalidation_level"


def test_actionable_alerts_default_to_paper_mode(monkeypatch):
    monkeypatch.delenv("Q15_ACTIONABLE_ALERTS", raising=False)
    assert Q15Settings().actionable_alerts is False
