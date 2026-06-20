from __future__ import annotations

import os
import tempfile
import unittest
import threading
from pathlib import Path
from unittest.mock import patch

from q15_upgrade.checkpoint_v94 import CheckpointPolicyV94
from q15_upgrade.checkpoint_v94_adaptive15 import Adaptive15LearningStore
from q15_upgrade.checkpoint_v94_unified import (
    CheckpointPolicyV94Unified,
    TelegramNotificationGate,
    READ_ONLY,
    VERSION,
    analyse_snapshot,
    apply_unified_policy,
    build_unified_message,
    format_telegram_message,
    rank_analyses,
)

NOW = 1_800_000_000.0


def make_candles(count=180, start=100.0, step=0.00008, cadence=5.0, outlier=False):
    rows = []
    price = start
    for index in range(count):
        change = step * (1.0 if index % 7 != 0 else -0.25)
        if outlier and index == count // 2:
            change = 0.08
        elif outlier and index == count // 2 + 1:
            change = (1.0 / 1.08) - 1.0
        close = price * (1.0 + change)
        rows.append({
            "start_time": NOW - (count - index) * cadence,
            "end_time": NOW - (count - index - 1) * cadence,
            "open": price,
            "high": max(price, close) * 1.00015,
            "low": min(price, close) * 0.99985,
            "close": close,
            "volume": 10 + index,
        })
        price = close
    return rows


def context(score=0.30, direction="BULLISH"):
    return {
        "status": "PASS",
        "relation": "CONTINUATION_WITH_SIDE",
        "combined_side_support": score,
        "previous_15m": {"direction": direction, "candle_count": 132, "coverage": 0.73},
        "current_15m": {"direction": direction, "candle_count": 41, "coverage": 0.69},
    }


def snapshot(asset="BNB", checkpoint="10M", ask=52.0, target=100.0, spot=None):
    rows = make_candles()
    current = spot if spot is not None else rows[-1]["close"]
    return {
        "asset": asset,
        "ticker": f"KX{asset}-{checkpoint}-{int(NOW)}",
        "checkpoint": checkpoint,
        "underlying_current": current,
        "target_value": target,
        "seconds_remaining": 590 if checkpoint == "10M" else 840,
        "snapshot_time": NOW,
        "underlying_candles_5s": rows[-12:],  # intentionally shorter than canonical cache
        "yes_bid": ask - 1,
        "yes_ask": ask,
        "no_bid": 99 - ask,
        "no_ask": 100 - (ask - 1),
    }


class FakeNotifier:
    def __init__(self):
        self.enabled = True
        self.messages = []

    def send(self, text, *args, **kwargs):
        self.messages.append(str(text))
        return True


class FakeStore:
    def __init__(self, rows=None):
        self.rows = rows or []

    def query(self, sql, params=None):
        return list(self.rows)


class UnifiedTests(unittest.TestCase):
    def make_policy(self):
        gate_path = str(Path(self.temp.name) / "telegram_gate.sqlite3")
        with patch.dict(os.environ, {"Q15_V94_TELEGRAM_GATE_DB": gate_path}, clear=False):
            with patch.object(CheckpointPolicyV94, "__init__", lambda self, *args, **kwargs: None):
                policy = CheckpointPolicyV94Unified(FakeStore())
        policy._candle_cache = {}
        policy._latest_context = {}
        policy._context_lock = threading.RLock()
        return policy

    @staticmethod
    def parent_run_cycle(self, snapshots, now, ws_health, focus_manager, calibrated_edge, notifier):
        notifier.send("👀 10M CHECK — WATCH, NO ENTRY YET")
        return snapshots

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.learner = Adaptive15LearningStore(Path(self.temp.name) / "learning.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_core_produces_pick_without_flow_or_book(self):
        result = analyse_snapshot(snapshot(), make_candles(), context(), NOW, "10M", None)
        self.assertTrue(result["prediction_available"])
        self.assertIn(result["prediction_side"], {"YES", "NO"})
        self.assertEqual(result["flow_status"], "UNAVAILABLE_NOT_OPPOSING")
        self.assertEqual(result["book_status"], "UNAVAILABLE_NOT_OPPOSING")

    def test_missing_core_fails_closed(self):
        row = snapshot()
        row.pop("target_value")
        result = analyse_snapshot(row, make_candles(), context(), NOW, "10M", None)
        self.assertFalse(result["prediction_available"])
        self.assertEqual(result["trade_decision"], "AVOID_INVALID_DATA")

    def test_stale_core_fails_closed(self):
        row = snapshot()
        row["snapshot_time"] = NOW - 120
        result = analyse_snapshot(row, make_candles(), context(), NOW, "10M", None)
        self.assertFalse(result["prediction_available"])

    def test_canonical_cache_supersedes_short_snapshot_list(self):
        result = analyse_snapshot(snapshot(), make_candles(180), context(), NOW, "10M", None)
        self.assertGreaterEqual(result["candle_count"], 180)
        self.assertTrue(result["feature_available"]["wick_structure"])
        self.assertTrue(result["feature_available"]["momentum_medium"])

    def test_probability_is_not_based_on_kalshi_ask(self):
        low_ask = snapshot(ask=20)
        high_ask = snapshot(ask=85)
        a = analyse_snapshot(low_ask, make_candles(), context(), NOW, "10M", None)
        b = analyse_snapshot(high_ask, make_candles(), context(), NOW, "10M", None)
        self.assertAlmostEqual(a["yes_probability"], b["yes_probability"], places=12)
        self.assertFalse(a["market_price_used_in_probability"])
        self.assertNotEqual(a["net_edge_cents"], b["net_edge_cents"])

    def test_sparse_evidence_shrinks_extreme_parent_probability(self):
        row = snapshot()
        row["fair_yes_probability"] = 0.97
        row.pop("underlying_candles_5s")
        result = analyse_snapshot(row, [], {}, NOW, "10M", None)
        self.assertTrue(result["prediction_available"])
        self.assertLess(result["selected_probability"], 0.95)
        self.assertLess(result["data_quality"], 0.50)

    def test_robust_volatility_limits_single_outlier_effect(self):
        normal = analyse_snapshot(snapshot(), make_candles(outlier=False), context(), NOW, "10M", None)
        outlier = analyse_snapshot(snapshot(), make_candles(outlier=True), context(), NOW, "10M", None)
        self.assertLess(abs(normal["yes_probability"] - outlier["yes_probability"]), 0.25)

    def test_unavailable_flow_is_not_labeled_opposing(self):
        result = analyse_snapshot(snapshot(), make_candles(), context(), NOW, "10M", None)
        self.assertNotIn("FAIL", result["flow_status"])
        self.assertIn("UNAVAILABLE", result["flow_status"])

    def test_ranking_is_economics_first(self):
        analyses = {
            "SOL": {"prediction_available": True, "prediction_side": "YES", "selected_probability": .59, "conservative_probability": .53, "data_quality": .7, "trade_decision": "WATCH_PRICE", "entry_allowed": False, "net_edge_cents": -.2, "ideal_entry_cents": 45, "ranking_score": 0},
            "BNB": {"prediction_available": True, "prediction_side": "YES", "selected_probability": .80, "conservative_probability": .74, "data_quality": .7, "trade_decision": "ENTRY_RECOMMENDED", "entry_allowed": True, "net_edge_cents": 20.3, "ideal_entry_cents": 64, "ranking_score": 0},
            "HYPE": {"prediction_available": True, "prediction_side": "YES", "selected_probability": .95, "conservative_probability": .89, "data_quality": .5, "trade_decision": "WATCH_PRICE", "entry_allowed": False, "net_edge_cents": 1.2, "ideal_entry_cents": 82, "ranking_score": 0},
        }
        ranking = rank_analyses(analyses)
        self.assertEqual([row["asset"] for row in ranking], ["BNB", "HYPE", "SOL"])

    def test_policy_overrides_old_hard_gate_state(self):
        row = snapshot()
        result = analyse_snapshot(row, make_candles(), context(), NOW, "10M", None, minimum_probability=.50, required_edge_cents=-20, minimum_quality=.10)
        output = apply_unified_policy(dict(row), result)
        self.assertEqual(output["selected_side"], result["prediction_side"])
        self.assertEqual(output["entry_allowed"], result["entry_allowed"])
        self.assertEqual(output["decision_state"], result["trade_decision"])

    def test_context_and_wicks_share_same_candle_count(self):
        result = analyse_snapshot(snapshot(), make_candles(173), context(), NOW, "10M", None)
        self.assertEqual(result["candle_count"], 173)
        self.assertEqual(result["model_details"]["wick"]["candle_count"], 173)
        self.assertEqual(result["context"]["previous_15m"]["candle_count"], 132)

    def test_message_is_single_unified_report(self):
        result = analyse_snapshot(snapshot(), make_candles(), context(), NOW, "10M", None)
        analyses = {"BNB": result}
        ranking = rank_analyses(analyses)
        message = build_unified_message("10M", analyses, ranking, self.learner.status())
        self.assertIn("10M UNIFIED CHECK", message)
        self.assertEqual(message.count("30M CHART CONTEXT"), 0)
        self.assertIn("Shared candle cache", message)
        self.assertNotIn("Three requirements", message)

    def test_10m_does_not_record_learning(self):
        notifier = FakeNotifier()
        policy = self.make_policy()
        policy.learning = self.learner
        row = snapshot(checkpoint="10M")
        policy._candle_cache = {"BNB": {c["start_time"]: c for c in make_candles()}}
        policy._latest_context = {"BNB": context()}
        with patch.object(CheckpointPolicyV94, "run_cycle", self.parent_run_cycle):
            policy.run_cycle({"BNB": row}, NOW, {}, None, None, notifier)
        self.assertEqual(self.learner.status()["unique_predictions"], 0)
        self.assertTrue(any("10M UNIFIED CHECK" in message for message in notifier.messages))

    def test_15m_records_one_unique_prediction(self):
        notifier = FakeNotifier()
        policy = self.make_policy()
        policy.learning = self.learner
        row = snapshot(checkpoint="15M")
        policy._candle_cache = {"BNB": {c["start_time"]: c for c in make_candles()}}
        policy._latest_context = {"BNB": context()}
        with patch.object(CheckpointPolicyV94, "run_cycle", self.parent_run_cycle):
            policy.run_cycle({"BNB": row}, NOW, {}, None, None, notifier)
            policy.run_cycle({"BNB": row}, NOW + 1, {}, None, None, notifier)
        self.assertEqual(self.learner.status()["unique_predictions"], 1)

    def test_message_ranking_uses_final_order(self):
        rows = {}
        for asset, ask in (("SOL", 70), ("BNB", 40), ("HYPE", 85)):
            rows[asset] = analyse_snapshot(snapshot(asset=asset, ask=ask), make_candles(), context(), NOW, "10M", None)
        ranking = rank_analyses(rows)
        message = build_unified_message("10M", rows, ranking, self.learner.status())
        first_asset = ranking[0]["asset"]
        self.assertIn(f"🥇 {first_asset}", message)

    def test_formatter_is_idempotent(self):
        text = "👀 10M UNIFIED CHECK — BEST PICKS, NO ENTRY YET"
        self.assertEqual(format_telegram_message(text), text)

    def test_read_only_markers(self):
        result = analyse_snapshot(snapshot(), make_candles(), context(), NOW, "10M", None)
        self.assertTrue(READ_ONLY)
        self.assertTrue(result["read_only"])
        self.assertEqual(result["version"], VERSION)

    def test_telegram_gate_persists_and_allows_only_entry_transition(self):
        path = Path(self.temp.name) / "gate.sqlite3"
        gate = TelegramNotificationGate(path)
        permit = gate.acquire(
            event_key="10M:1800000600",
            checkpoint="10M",
            close_bucket=1800000600,
            fingerprint="watch",
            has_entry=False,
            now=NOW,
        )
        self.assertIsNotNone(permit)
        gate.complete(permit, success=True, now=NOW)
        self.assertIsNone(gate.acquire(
            event_key="10M:1800000600",
            checkpoint="10M",
            close_bucket=1800000600,
            fingerprint="watch-again",
            has_entry=False,
            now=NOW + 1,
        ))
        restarted = TelegramNotificationGate(path)
        entry = restarted.acquire(
            event_key="10M:1800000600",
            checkpoint="10M",
            close_bucket=1800000600,
            fingerprint="entry",
            has_entry=True,
            now=NOW + 2,
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry.kind, "ENTRY_TRANSITION")
        restarted.complete(entry, success=True, now=NOW + 2)
        self.assertIsNone(restarted.acquire(
            event_key="10M:1800000600",
            checkpoint="10M",
            close_bucket=1800000600,
            fingerprint="entry-repeat",
            has_entry=True,
            now=NOW + 3,
        ))

    def test_telegram_gate_failed_send_remains_retryable(self):
        gate = TelegramNotificationGate(Path(self.temp.name) / "retry.sqlite3")
        first = gate.acquire(
            event_key="15M:1800000900",
            checkpoint="15M",
            close_bucket=1800000900,
            fingerprint="first",
            has_entry=False,
            now=NOW,
        )
        self.assertIsNotNone(first)
        gate.complete(first, success=False, now=NOW)
        retry = gate.acquire(
            event_key="15M:1800000900",
            checkpoint="15M",
            close_bucket=1800000900,
            fingerprint="retry",
            has_entry=False,
            now=NOW + 1,
        )
        self.assertIsNotNone(retry)

    def test_policy_suppresses_repeated_one_second_cycles(self):
        notifier = FakeNotifier()
        policy = self.make_policy()
        policy.learning = self.learner
        row = snapshot(checkpoint="10M")
        policy._candle_cache = {"BNB": {c["start_time"]: c for c in make_candles()}}
        policy._latest_context = {"BNB": context()}
        with patch.object(CheckpointPolicyV94, "run_cycle", self.parent_run_cycle):
            policy.run_cycle({"BNB": dict(row)}, NOW, {}, None, None, notifier)
            row2 = dict(row)
            row2["seconds_remaining"] = row["seconds_remaining"] - 1
            row2["snapshot_time"] = NOW + 1
            policy.run_cycle({"BNB": row2}, NOW + 1, {}, None, None, notifier)
        unified = [message for message in notifier.messages if "10M UNIFIED CHECK" in message]
        self.assertEqual(len(unified), 1)
        self.assertGreaterEqual(policy.health()["telegram_duplicates_suppressed"], 1)

    def test_no_order_placement_surface(self):
        forbidden = {"place_order", "submit_order", "create_order", "cancel_order", "modify_order"}
        self.assertTrue(forbidden.isdisjoint(set(dir(CheckpointPolicyV94Unified))))

    def test_selected_side_consistent_through_value(self):
        result = analyse_snapshot(snapshot(), make_candles(), context(), NOW, "10M", None)
        side = result["prediction_side"]
        expected_ask = snapshot()["yes_ask"] if side == "YES" else snapshot()["no_ask"]
        self.assertEqual(result["quote"]["ask_cents"], expected_ask)

    def test_low_quality_blocks_entry_but_not_prediction(self):
        row = snapshot()
        row.pop("underlying_candles_5s")
        result = analyse_snapshot(row, [], {}, NOW, "10M", None)
        self.assertTrue(result["prediction_available"])
        self.assertFalse(result["entry_allowed"])
        self.assertIn(result["trade_decision"], {"WATCH_DATA_QUALITY", "WATCH_CONFIDENCE", "WATCH_PRICE"})

    def test_parent_probability_is_only_a_bounded_adjustment(self):
        low = snapshot(); low["fair_yes_probability"] = 0.05
        high = snapshot(); high["fair_yes_probability"] = 0.95
        a = analyse_snapshot(low, make_candles(), context(), NOW, "10M", None)
        b = analyse_snapshot(high, make_candles(), context(), NOW, "10M", None)
        self.assertLess(abs(a["yes_probability"] - b["yes_probability"]), 0.50)

    def test_pattern_learning_is_15m_only(self):
        result = analyse_snapshot(snapshot(checkpoint="10M"), make_candles(), context(), NOW, "10M", self.learner)
        self.assertEqual(result["pattern_learning"]["reason"], "10m_learning_disabled")


    def test_no_selected_context_is_inverted_to_yes_orientation(self):
        no_context = context(score=0.40, direction="BEARISH")
        no_context["selected_side"] = "NO"
        result = analyse_snapshot(snapshot(), make_candles(), no_context, NOW, "10M", None)
        self.assertAlmostEqual(result["model_details"]["context_score"], -0.40, places=12)

    def test_resolved_15m_outcome_changes_future_probability_boundedly(self):
        row = snapshot(checkpoint="15M", spot=100.02)
        before = analyse_snapshot(row, make_candles(), context(), NOW, "15M", self.learner)
        self.learner.record_prediction(
            ticker=row["ticker"], asset="BNB", side=before["prediction_side"],
            yes_probability=before["yes_probability"], selected_probability=before["selected_probability"],
            data_quality=before["data_quality"], features=before["feature_values"],
            available=before["feature_available"], weights=self.learner.weights(),
            created_at=NOW, close_time=NOW + 840,
            training_yes_probability=before["training_yes_probability"],
        )
        opposite = "NO" if before["prediction_side"] == "YES" else "YES"
        self.learner.resolve_ticker(row["ticker"], opposite, NOW + 900)
        after = analyse_snapshot(row, make_candles(), context(), NOW + 1, "15M", self.learner)
        self.assertNotAlmostEqual(before["yes_probability"], after["yes_probability"], places=10)
        self.assertLess(abs(before["yes_probability"] - after["yes_probability"]), 0.10)

    def test_health_reports_unified_invariants(self):
        policy = self.make_policy()
        with patch.object(CheckpointPolicyV94, "health", lambda self: {"version": "parent"}):
            health = policy.health()
        self.assertTrue(health["single_canonical_candle_pipeline"])
        self.assertFalse(health["market_price_used_in_probability"])
        self.assertEqual(health["learning_scope"], "15M_ONLY")


if __name__ == "__main__":
    unittest.main()
