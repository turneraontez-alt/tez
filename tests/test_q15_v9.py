from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from q15_upgrade.calibrated_edge import (
    CalibratedEdgeEngine,
    calculate_trade_economics,
    canonical_probability,
    directional_vote_metrics,
)
from q15_upgrade.oos_v9 import OutOfSampleEvaluator
from notifications.outbox_v9 import ReliableTelegramOutbox


class FakeNotifier:
    def __init__(self, outcomes):
        self.enabled = True
        self.outcomes = list(outcomes)
        self.sent_count = 0
        self.last_error = None
        self.last_sent_at = None
        self.token = "SECRET"

    def status(self):
        return "configured"

    def send(self, text):
        outcome = self.outcomes.pop(0) if self.outcomes else True
        if outcome:
            self.sent_count += 1
            self.last_sent_at = 1.0
            self.last_error = None
            return True
        self.last_error = "temporary timeout"
        return False


class DisabledStore:
    enabled = False


class V9Tests(unittest.TestCase):
    def test_market_baseline_is_added_once(self):
        snap = {
            "yes_bid": 49,
            "yes_ask": 51,
            "estimated_fair_probability": 0.80,
            "taker_yes_volume_15s": 0,
            "taker_no_volume_15s": 0,
        }
        with patch.dict(os.environ, {"Q15_V9_INDEPENDENT_RESIDUAL_WEIGHT": "0.35"}, clear=False):
            result = canonical_probability(snap)
        expected = 1 / (1 + math.exp(-(0.35 * math.log(0.8 / 0.2))))
        self.assertAlmostEqual(result["uncalibrated_p_yes"], expected, places=7)
        self.assertEqual(result["market_baseline_p_yes"], 0.5)
        self.assertEqual(result["independent_signal_p_yes"], 0.8)

    def test_canonical_economics_includes_all_costs_and_not_spread_twice(self):
        result = calculate_trade_economics(
            side="YES",
            calibrated_probability=0.75,
            conservative_probability=0.70,
            executable_entry_ask=60,
            estimated_fee=1,
            estimated_slippage=0.5,
            estimated_market_impact=0.5,
            required_edge_cents=4,
        )
        self.assertEqual(result["gross_edge_cents"], 15)
        self.assertEqual(result["net_edge_cents"], 13)
        self.assertEqual(result["conservative_net_edge_cents"], 8)
        self.assertEqual(result["total_estimated_cost_cents"], 2)
        self.assertFalse(result["spread_deducted_separately"])

    def test_no_side_is_preserved(self):
        result = calculate_trade_economics(
            side="NO",
            calibrated_probability=0.80,
            conservative_probability=0.74,
            executable_entry_ask=60,
            estimated_fee=1,
            estimated_slippage=0.5,
            estimated_market_impact=0.5,
            required_edge_cents=4,
        )
        self.assertEqual(result["side"], "NO")
        self.assertEqual(result["conservative_net_edge_cents"], 12)

    def test_15m_does_not_require_future_10m_agreement(self):
        snap = {
            "asset": "BTC",
            "ticker": "TEST-BTC",
            "market_state": "live",
            "seconds_remaining": 850,
            "entry_side": "YES",
            "yes_bid": 49,
            "yes_ask": 50,
            "no_bid": 50,
            "no_ask": 51,
            "yes_ask_qty": 30,
            "estimated_fair_probability": 0.99,
            "confirmation_group_count": 3,
            "q15_prediction_agreement": "UNKNOWN",
            "data_age_seconds": 1,
        }
        with patch.dict(os.environ, {
            "Q15_V9_MIN_CONSERVATIVE_EDGE_CENTS": "4",
            "Q15_V9_CONSERVATIVE_PENALTY_PP": "2",
        }, clear=False):
            engine = CalibratedEdgeEngine()
            result = engine.enrich_all({"BTC": snap}, 1.0, {})["BTC"]
        self.assertNotIn("prediction_conflict", result["calibrated_edge_blockers"])
        self.assertEqual(result["v9_checkpoint"], "15M")
        self.assertEqual(result["calibrated_edge_side"], "YES")
        self.assertIn(result["calibrated_edge_status"], {"READY", "PRICE_NOT_ATTRACTIVE"})

    def test_10m_conflict_is_recorded(self):
        snap = {
            "asset": "ETH", "ticker": "TEST-ETH", "market_state": "live",
            "seconds_remaining": 590, "entry_side": "YES", "yes_bid": 49,
            "yes_ask": 50, "yes_ask_qty": 30, "estimated_fair_probability": 0.99,
            "confirmation_group_count": 3, "q15_prediction_agreement": "CONFLICT",
        }
        engine = CalibratedEdgeEngine()
        result = engine.enrich_all({"ETH": snap}, 1.0, {})["ETH"]
        self.assertIn("prediction_conflict", result["calibrated_edge_blockers"])
        self.assertEqual(result["calibrated_edge_status"], "DIRECTION_UNCERTAIN")

    def test_agreement_and_coverage_are_separate(self):
        result = directional_vote_metrics({
            "yes_votes": 0, "no_votes": 2, "neutral_votes": 8, "total_possible_votes": 10,
        })
        self.assertEqual(result["directional_agreement"], 1.0)
        self.assertEqual(result["evidence_coverage"], 0.2)
        self.assertEqual(result["status"], "INSUFFICIENT_DIRECTIONAL_EVIDENCE")

    def test_outbox_retries_and_marks_sent_only_after_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw = FakeNotifier([False, True])
            with patch.dict(os.environ, {"Q15_V9_OUTBOX_WORKER": "false", "Q15_V9_DISABLE_NETWORK": "false"}, clear=False):
                outbox = ReliableTelegramOutbox(
                    DisabledStore(), raw, sqlite_path=str(Path(temporary) / "outbox.sqlite3")
                )
                accepted = outbox.send("test", idempotency_key="contract:10m:entry:v9")
                self.assertTrue(accepted)
                rows = outbox.rows()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["status"], "FAILED_RETRYABLE")
                outbox._attempt(rows[0]["id"])
                rows = outbox.rows()
                self.assertEqual(rows[0]["status"], "SENT")
                # Duplicate key returns the existing SENT row; no duplicate message.
                outbox.send("test", idempotency_key="contract:10m:entry:v9")
                self.assertEqual(len(outbox.rows()), 1)
                outbox.close()

    def test_oos_refuses_small_sample_and_reports_larger_sample(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reviews.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE ten_minute_predictions (
                    prediction_key TEXT PRIMARY KEY, ticker TEXT, asset TEXT,
                    model_version TEXT, predicted_at TEXT, predicted_side TEXT,
                    side_probability REAL, yes_probability REAL,
                    market_implied_side_probability REAL, executable_ask_cents REAL,
                    point_edge_cents REAL, conservative_edge_cents REAL,
                    features_json TEXT, trajectory_json TEXT, status TEXT,
                    actual_side TEXT, correct INTEGER, resolved_at TEXT,
                    analysis_json TEXT, notified INTEGER
                )
                """
            )
            for index in range(40):
                predicted = "YES" if index % 2 == 0 else "NO"
                actual = predicted if index % 4 != 0 else ("NO" if predicted == "YES" else "YES")
                yes_probability = 0.70 if predicted == "YES" else 0.30
                features = json.dumps({"canonical_economics": {"total_estimated_cost_cents": 1.0}})
                connection.execute(
                    "INSERT INTO ten_minute_predictions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"k{index}", f"t{index}", "BTC", "v9", f"2026-01-{index+1:02d}T00:00:00Z",
                        predicted, 0.70, yes_probability, 0.60, 55.0, 10.0, 8.0,
                        features, "{}", "RESOLVED", actual, int(actual == predicted),
                        f"2026-02-{index+1:02d}T00:00:00Z", "{}", 0,
                    ),
                )
            connection.commit()
            connection.close()
            with patch.dict(os.environ, {"Q15_V9_OOS_MIN_RESOLVED": "30"}, clear=False):
                evaluator = OutOfSampleEvaluator(DisabledStore(), str(path))
                report = evaluator.out_of_sample_report()
            self.assertTrue(report["available"])
            self.assertTrue(report["resolved_outcome_metrics_available"])
            self.assertGreater(report["untouched_test_observations"], 0)
            self.assertIn("brier_score", report["metrics"])


if __name__ == "__main__":
    unittest.main()
