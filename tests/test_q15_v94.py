from __future__ import annotations

import copy
import os
import time

os.environ.setdefault("Q15_V94_CACHE_PERSIST", "false")

from q15_upgrade.checkpoint_v94 import (
    VERSION,
    CheckpointPolicyV94,
    analyse_dual_window_context,
    apply_context_gate,
    format_telegram_message,
    _BufferedNotifier,
    _rewrite_gated_message,
)


class _PassThroughFocus:
    def update(self, prepared, now, ws_health):
        return prepared


class _PassThroughEdge:
    def enrich_all(self, focused, now, ws_health):
        return focused


def candles(start: float, count: int, *, price: float = 100.0, step: float = 0.01, upper_wick: float = 0.005, lower_wick: float = 0.005):
    rows = []
    value = price
    for index in range(count):
        prior = value
        value += step
        rows.append({
            "start_time": start + index * 5.0,
            "end_time": start + (index + 1) * 5.0,
            "open": prior,
            "high": max(prior, value) + upper_wick,
            "low": min(prior, value) - lower_wick,
            "close": value,
            "volume": 1.0,
        })
    return rows


def snapshot(now: float, side: str = "YES"):
    close = (now // 900 + 1) * 900
    return {
        "asset": "BTC",
        "ticker": "TEST-BTC",
        "market_state": "live",
        "close_time": close,
        "seconds_remaining": close - now,
        "entry_side": side,
        "target_value": 100.0,
        "target_type": "greater_or_equal",
        "decision_state": "ENTRY RECOMMENDED",
        "entry_status": "ENTRY RECOMMENDED",
        "q15_new_entry_allowed": True,
        "q15_v93_decision": "ENTRY_RECOMMENDED",
        "entry_blockers": [],
        "monitor_warnings": [],
    }


def test_dual_window_bullish_yes_passes():
    now = 10_000.0
    snap = snapshot(now, "YES")
    current_start = snap["close_time"] - 900
    rows = candles(current_start - 900, 180, price=98.0, step=0.01)
    rows += candles(current_start, 20, price=99.8, step=0.015)
    context = analyse_dual_window_context(snap, rows, now)
    assert context["status"] == "PASS"
    assert context["selected_side"] == "YES"
    assert context["combined_side_support"] > 0
    assert context["previous_15m"]["candle_count"] == 180
    assert context["current_15m"]["candle_count"] == 20


def test_dual_window_bearish_yes_vetoes():
    now = 10_000.0
    snap = snapshot(now, "YES")
    current_start = snap["close_time"] - 900
    rows = candles(current_start - 900, 180, price=103.0, step=-0.012, upper_wick=0.02, lower_wick=0.001)
    rows += candles(current_start, 20, price=100.8, step=-0.025, upper_wick=0.03, lower_wick=0.001)
    context = analyse_dual_window_context(snap, rows, now)
    assert context["status"] == "VETO"
    assert context["combined_side_support"] < 0


def test_no_side_inverts_direction_without_changing_side():
    now = 10_000.0
    snap = snapshot(now, "NO")
    current_start = snap["close_time"] - 900
    rows = candles(current_start - 900, 180, price=103.0, step=-0.01)
    rows += candles(current_start, 20, price=101.2, step=-0.01)
    context = analyse_dual_window_context(snap, rows, now)
    assert context["status"] == "PASS"
    assert context["selected_side"] == "NO"
    assert context["changes_selected_side"] is False


def test_insufficient_data_fails_closed_for_new_entry():
    now = 10_000.0
    snap = snapshot(now, "YES")
    context = analyse_dual_window_context(snap, [], now)
    result = apply_context_gate(copy.deepcopy(snap), context)
    assert result["q15_v9_4_context_status"] == "INSUFFICIENT"
    assert result["q15_v9_4_entry_allowed"] is False
    assert result["q15_new_entry_allowed"] is False
    assert result["decision_state"] == "WATCH"
    assert any(item.startswith("q15_v94:") for item in result["entry_blockers"])


def test_context_does_not_invalidate_existing_confirmed_position():
    now = 10_000.0
    snap = snapshot(now, "YES")
    snap.update({
        "decision_state": "ENTRY CONFIRMED",
        "entry_status": "ENTRY CONFIRMED",
        "entry_confirmed_at": now - 30,
        "entry_age_seconds": 30,
        "position_state": "ACTIVE",
    })
    context = {
        "status": "VETO",
        "relation": "CURRENT_REVERSAL_AGAINST_SIDE",
        "reason": "strong_current_15m_reversal_against_selected_side",
        "combined_side_support": -0.9,
        "previous_15m": {},
        "current_15m": {},
    }
    result = apply_context_gate(copy.deepcopy(snap), context)
    assert result["decision_state"] == "ENTRY CONFIRMED"
    assert result["q15_v9_4_existing_position_unchanged"] is True


def test_value_failure_remains_authoritative():
    now = 10_000.0
    snap = snapshot(now, "YES")
    snap["q15_v93_decision"] = "PRICE_NOT_ATTRACTIVE"
    context = {
        "status": "PASS",
        "relation": "CONTINUATION_WITH_SIDE",
        "reason": "supported",
        "combined_side_support": 0.8,
        "previous_15m": {},
        "current_15m": {},
    }
    result = apply_context_gate(copy.deepcopy(snap), context)
    assert result["q15_v9_4_decision"] == "PRICE_NOT_ATTRACTIVE"
    assert result["q15_v9_4_entry_allowed"] is False


def test_future_and_malformed_candles_are_ignored():
    now = 10_000.0
    snap = snapshot(now, "YES")
    current_start = snap["close_time"] - 900
    rows = candles(current_start - 900, 180, price=98.0, step=0.01)
    rows += [
        {"start_time": now + 10, "end_time": now + 15, "open": 1, "high": 2, "low": 0.5, "close": 1.5},
        {"start_time": current_start, "open": None, "high": 2, "low": 1, "close": 1.5},
    ]
    # Direct helper receives normalized rows, so exercise policy ingestion.
    policy = CheckpointPolicyV94(None)
    raw = snapshot(now, "YES")
    raw["underlying_candles_5s"] = rows
    result = policy.run_cycle({"BTC": raw}, now, {}, _PassThroughFocus(), _PassThroughEdge(), object())["BTC"]
    assert result["q15_v9_4_previous_15m"]["candle_count"] == 180


def test_policy_preserves_side_and_exposes_health():
    now = 10_000.0
    snap = snapshot(now, "YES")
    current_start = snap["close_time"] - 900
    snap["underlying_candles_5s"] = candles(current_start - 900, 180, price=98.0, step=0.01) + candles(current_start, 20, price=99.8, step=0.015)
    policy = CheckpointPolicyV94(None)
    result = policy.run_cycle({"BTC": snap}, now, {}, _PassThroughFocus(), _PassThroughEdge(), object())["BTC"]
    assert result["entry_side"] == "YES"
    assert result["q15_v9_4_version"] == VERSION
    health = policy.health()
    assert health["read_only"] is True
    assert health["automatic_learning"] is False
    assert policy.context_status()["context_is_vote"] is False


def test_formatter_appends_context_once():
    now = 10_000.0
    snap = snapshot(now, "YES")
    current_start = snap["close_time"] - 900
    snap["underlying_candles_5s"] = candles(current_start - 900, 180, price=98.0, step=0.01) + candles(current_start, 20, price=99.8, step=0.015)
    policy = CheckpointPolicyV94(None)
    policy.run_cycle({"BTC": snap}, now, {}, _PassThroughFocus(), _PassThroughEdge(), object())
    once = format_telegram_message("15M EARLY CHECK — BTC YES")
    twice = format_telegram_message(once)
    assert "30M CHART CONTEXT — PRIOR 15M + CURRENT 15M" in once
    assert twice.count("30M CHART CONTEXT — PRIOR 15M + CURRENT 15M") == 1


def test_no_order_placement_surface():
    import inspect
    import q15_upgrade.checkpoint_v94 as module

    source = inspect.getsource(module)
    forbidden = ("place_order(", "submit_order(", "create_order(", "cancel_order(")
    assert not any(token in source for token in forbidden)


class CapturingNotifier:
    enabled = True
    def __init__(self):
        self.calls = []
    def send(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return True


def test_buffered_notifier_downgrades_entry_before_real_delivery():
    real = CapturingNotifier()
    buffered = _BufferedNotifier(real)
    assert buffered.send("15M EARLY CHECK — ENTRY RECOMMENDED BTC YES", idempotency_key="abc")
    sent, failed = buffered.flush({"BTC": {"status": "VETO"}})
    assert (sent, failed) == (1, 0)
    assert "NO TRADE" in real.calls[0][0][0]
    assert "ENTRY RECOMMENDED" not in real.calls[0][0][0]
    assert real.calls[0][1]["idempotency_key"] == "abc"


def test_pass_context_does_not_rewrite_entry_headline():
    original = "15M EARLY CHECK — ENTRY RECOMMENDED BTC YES"
    assert _rewrite_gated_message(original, {"BTC": {"status": "PASS"}}) == original


def test_persistent_cache_survives_policy_restart(tmp_path, monkeypatch):
    db_path = tmp_path / "context.sqlite3"
    monkeypatch.setenv("Q15_V94_CACHE_PERSIST", "true")
    monkeypatch.setenv("Q15_V94_CACHE_DB_PATH", str(db_path))
    now = 10_000.0
    snap = snapshot(now, "YES")
    current_start = snap["close_time"] - 900
    prior_rows = candles(current_start - 900, 180, price=98.0, step=0.01)
    first = copy.deepcopy(snap)
    first["underlying_candles_5s"] = prior_rows
    CheckpointPolicyV94(None).run_cycle({"BTC": first}, now, {}, _PassThroughFocus(), _PassThroughEdge(), CapturingNotifier())

    second = copy.deepcopy(snap)
    second["underlying_candles_5s"] = candles(current_start, 20, price=99.8, step=0.015)
    out = CheckpointPolicyV94(None).run_cycle(
        {"BTC": second}, now, {}, _PassThroughFocus(), _PassThroughEdge(), CapturingNotifier()
    )["BTC"]
    assert out["q15_v9_4_previous_15m"]["candle_count"] == 180
    assert out["q15_v9_4_current_15m"]["candle_count"] == 20
    assert out["q15_v9_4_context_status"] == "PASS"
