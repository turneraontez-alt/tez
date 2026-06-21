"""Flip-risk monitoring: scoring, learning, thresholds, alert gating, performance.

The flip-risk overlay measures whether the frozen V9.5 prediction is at risk of
flipping. It must NEVER change the prediction — these tests assert it is purely
observational, that missing data lowers confidence (not the score), that the
learned thresholds come from real resolved-contract history with a sample gate,
and that the alert state machine honours persistence / evidence / hysteresis /
cooldown so a single wick can't spam alerts.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade import flip_risk as fr
from q15_upgrade.ledger_v95 import V95Ledger


# ----------------------------------------------------------------- scoring
class TestRiskScoring(unittest.TestCase):
    def _analysis(self, side="NO", **over):
        a = {
            "prediction_side": side,
            "feature_values": {"wick": 0.7, "book": 0.5, "flow": 0.4, "momentum": 0.6},
            "feature_quality": {"wick": 0.8, "book": 0.7, "flow": 0.6, "momentum": 0.7},
            "feature_details": {
                "absorption": {"available": True, "absorbed": True, "flow": 0.5},
                "exchange_consensus": {"source_count": 3, "divergence_bps": 50},
                "threshold_interaction": {"available": True, "crossings": 6, "failed_breakdown": True},
            },
            "regime": {"name": "THRESHOLD_PIN"}, "quote": {"spread_cents": 8, "ask_depth": 5},
            "market_implied_yes_probability": 0.45, "selected_probability": 0.62,
        }
        a.update(over)
        return a

    def test_full_evidence_high_score_full_confidence(self):
        r = fr.compute_risk(self._analysis())
        self.assertGreater(r.score, 50)
        self.assertEqual(round(r.confidence), 100)  # all categories present
        self.assertGreaterEqual(len(r.evidence_categories), 2)
        self.assertEqual(r.direction_monitored, "NO → YES")

    def test_missing_data_lowers_confidence_not_score(self):
        # Only a strong wick available -> high score but low confidence.
        sparse = {"prediction_side": "NO", "feature_values": {"wick": 0.9},
                  "feature_quality": {"wick": 0.9}, "feature_details": {}, "regime": {}, "quote": {}}
        r = fr.compute_risk(sparse)
        self.assertGreater(r.score, 80)          # the present evidence is strong
        self.assertLess(r.confidence, 30)        # but coverage is thin
        self.assertEqual(r.evidence_categories, ["wicks"])

    def test_no_data_is_zero(self):
        r = fr.compute_risk({"prediction_side": "NO", "feature_values": {}, "feature_quality": {},
                             "feature_details": {}, "regime": {}, "quote": {}})
        self.assertEqual(r.score, 0.0)
        self.assertEqual(r.confidence, 0.0)

    def test_same_direction_pressure_is_not_risk(self):
        # Bullish wick under a YES prediction does not threaten a YES->NO flip.
        a = {"prediction_side": "YES", "feature_values": {"wick": 0.9}, "feature_quality": {"wick": 0.9},
             "feature_details": {}, "regime": {}, "quote": {}}
        self.assertEqual(fr.compute_risk(a).components["wicks"], 0.0)


# ----------------------------------------------------------------- buckets/threshold
class TestBucketsAndThresholds(unittest.TestCase):
    def test_bucket_label(self):
        self.assertEqual(fr.bucket_label(0), "0-9")
        self.assertEqual(fr.bucket_label(76), "70-79")
        self.assertEqual(fr.bucket_label(90), "90-100")
        self.assertEqual(fr.bucket_label(100), "90-100")

    def test_threshold_prefers_learned_when_enough_samples(self):
        asset = {"threshold": 78.0, "samples": 40}
        overall = {"threshold": 85.0, "samples": 200}
        res = fr.resolve_threshold(asset, overall, asset="BTC", checkpoint="10M", direction="NO → YES")
        self.assertEqual(res.threshold, 78.0)
        self.assertEqual(res.status, "Learned")

    def test_threshold_falls_back_to_overall_below_minimum(self):
        asset = {"threshold": 78.0, "samples": 8}     # below 30
        overall = {"threshold": 85.0, "samples": 200}
        res = fr.resolve_threshold(asset, overall, asset="BTC", checkpoint="10M", direction="NO → YES")
        self.assertEqual(res.threshold, 85.0)
        self.assertEqual(res.status, "Preliminary")
        self.assertIn("Overall", res.source)

    def test_threshold_default_when_no_history(self):
        res = fr.resolve_threshold(None, None, asset="BTC", checkpoint="10M", direction="NO → YES")
        self.assertEqual(res.status, "Preliminary")
        self.assertEqual(res.threshold, 80.0)  # conservative default, not loosened

    def test_flip_probability_from_bucket(self):
        buckets = {"70-79": {"flip_rate": 0.34}, "80-89": {"flip_rate": 0.46}}
        self.assertEqual(fr.estimate_flip_probability(76, buckets), 0.34)
        self.assertIsNone(fr.estimate_flip_probability(20, buckets))
        self.assertIsNone(fr.estimate_flip_probability(76, None))


# ----------------------------------------------------------------- alert gating
class TestAlertStateMachine(unittest.TestCase):
    def _risk(self, score, cats=("wicks", "spot_momentum"), conf=80):
        return fr.RiskAssessment(score=score, confidence=conf, evidence_categories=list(cats),
                                 primary_reason="x", direction_monitored="NO → YES")

    def _thr(self, status="Learned"):
        return fr.ThresholdResolution(80.0, "BTC 10M NO → YES", status, 40, 30)

    def _run(self, risk, n, prior=None, t0=100.0, flip_prob=0.5, flip_prob_lower=0.85):
        # Default flip_prob_lower clears the hit-rate gate so these tests exercise
        # the persistence/evidence/hysteresis machine in isolation.
        d = None
        for i in range(n):
            d, prior = fr.evaluate_alert(risk=risk, threshold=self._thr(), flip_probability=flip_prob,
                                         prior=prior, now=t0 + i, flip_prob_lower=flip_prob_lower)
        return d, prior

    def test_requires_persistence(self):
        d1, st = self._run(self._risk(85), 1)
        self.assertFalse(d1.should_send)
        d3, st = self._run(self._risk(85), 3)
        self.assertTrue(d3.should_send)

    def test_single_evidence_category_never_alerts(self):
        d, _ = self._run(self._risk(95, cats=("wicks",)), 5)
        self.assertFalse(d.should_send)
        self.assertEqual(d.reason, "evidence_categories")

    def test_below_threshold_is_watch_or_normal(self):
        d, _ = self._run(self._risk(75), 5)  # threshold 80, watch margin 10
        self.assertEqual(d.state, fr.WATCH)
        self.assertFalse(d.should_send)
        d2, _ = self._run(self._risk(50), 5)
        self.assertEqual(d2.state, fr.NORMAL)

    def test_dedup_after_first_fire(self):
        _, st = self._run(self._risk(85), 3)
        d, _ = fr.evaluate_alert(risk=self._risk(85), threshold=self._thr(),
                                 flip_probability=0.5, prior=st, now=500.0, flip_prob_lower=0.85)
        self.assertFalse(d.should_send)
        self.assertEqual(d.reason, "deduplicated")

    def test_hysteresis_reset_then_rearm(self):
        _, st = self._run(self._risk(85), 3)
        # Drop below threshold - hysteresis (80-10=70) resets the latch.
        d, st = fr.evaluate_alert(risk=self._risk(65), threshold=self._thr(),
                                  flip_probability=0.5, prior=st, now=600.0, flip_prob_lower=0.85)
        self.assertFalse(st["latched"])
        d2, _ = self._run(self._risk(85), 3, prior=st, t0=700.0)
        self.assertTrue(d2.should_send)  # re-armed

    def test_confidence_gate(self):
        d, _ = self._run(self._risk(85, conf=10), 5)  # below min_confidence 40
        self.assertFalse(d.should_send)
        self.assertEqual(d.reason, "confidence")

    def test_hitrate_gate_blocks_until_reliable(self):
        # No bucket history (flip_prob_lower=None) -> dormant on "hitrate".
        d, _ = self._run(self._risk(85), 5, flip_prob_lower=None)
        self.assertFalse(d.should_send)
        self.assertEqual(d.reason, "hitrate")
        # A point-estimate that is high but with a low Wilson bound still blocks.
        d2, _ = self._run(self._risk(85), 5, flip_prob_lower=0.55)
        self.assertFalse(d2.should_send)
        self.assertEqual(d2.reason, "hitrate")
        # Lower bound clears 70% -> fires once persistence is met (3 obs).
        d3, _ = self._run(self._risk(85), 3, flip_prob_lower=0.72)
        self.assertTrue(d3.should_send)


class TestHitRateReliability(unittest.TestCase):
    def test_wilson_lower_bound_grows_with_samples(self):
        # Same 80% point rate, more samples -> tighter, higher lower bound.
        small = fr.wilson_lower_bound(4, 5)
        large = fr.wilson_lower_bound(80, 100)
        self.assertLess(small, large)
        self.assertGreater(large, 0.70)   # 80/100 is reliably >70%
        self.assertLess(small, 0.70)      # 4/5 is not
        self.assertIsNone(fr.wilson_lower_bound(0, 0))

    def test_bucket_reliability_reads_counts(self):
        buckets = {"80-89": {"n": 100, "flips": 80, "flip_rate": 0.8}}
        lower, n = fr.bucket_flip_reliability(85, buckets)
        self.assertEqual(n, 100)
        self.assertGreater(lower, 0.70)
        self.assertEqual(fr.bucket_flip_reliability(85, None), (None, 0))
        self.assertEqual(fr.bucket_flip_reliability(20, buckets), (None, 0))


# ----------------------------------------------------------------- ledger learning
def _rec(led, ticker, cp, side, score, asset="BTC"):
    led.record_prediction(
        ticker=ticker, asset=asset, checkpoint=cp, created_at=time.time(), close_time=time.time() + 600,
        predicted_side=side, raw_yes_probability=0.6, calibrated_yes_probability=0.6,
        challenger_yes_probability=0.6, baseline_yes_probability=0.6, selected_probability=0.6,
        conservative_probability=0.6, data_quality=0.7, evidence_quality=0.7, trade_quality=0.6,
        trade_decision="WATCH_PRICE", regime="NORMAL", features={}, contributions={},
        quote={"ask_cents": 50}, rank=1, flip_risk_score=score, flip_risk_confidence=80, flip_evidence_count=3)


class TestLedgerFlipLearning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.led = V95Ledger(os.path.join(self.tmp.name, "l.sqlite3"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_flip_observations_are_point_in_time(self):
        # t1: 15M NO@85 -> 10M YES  (a NO->YES flip; score 85 precedes it at 15M)
        _rec(self.led, "t1", "15M", "NO", 85)
        _rec(self.led, "t1", "10M", "YES", 40)
        self.led.resolve_ticker("t1", "YES", time.time() + 700)
        # t2: 15M NO@20 -> 10M NO (no flip)
        _rec(self.led, "t2", "15M", "NO", 20)
        _rec(self.led, "t2", "10M", "NO", 15)
        self.led.resolve_ticker("t2", "NO", time.time() + 700)

        d = self.led.flip_stats()["by_checkpoint"]["15M"]["NO → YES"]["overall"]
        self.assertEqual(d["samples"], 2)
        self.assertEqual(d["flips"], 1)
        self.assertEqual(d["avg_score_before_flip"], 85.0)
        self.assertEqual(d["buckets"]["80-89"]["flip_rate"], 1.0)
        self.assertEqual(d["buckets"]["20-29"]["flip_rate"], 0.0)

    def test_directions_kept_separate(self):
        _rec(self.led, "y1", "15M", "YES", 80)
        _rec(self.led, "y1", "10M", "NO", 30)  # YES->NO flip
        self.led.resolve_ticker("y1", "NO", time.time() + 700)
        fs = self.led.flip_stats()["by_checkpoint"]["15M"]
        self.assertEqual(fs["YES → NO"]["overall"]["flips"], 1)
        self.assertEqual(fs["NO → YES"]["overall"]["samples"], 0)

    def test_frozen_prediction_lookup(self):
        _rec(self.led, "fz", "15M", "NO", 70)
        fp = self.led.frozen_prediction("fz", "15M")
        self.assertEqual(fp["side"], "NO")
        self.assertEqual(fp["flip_risk_score"], 70.0)
        self.assertIsNone(self.led.frozen_prediction("fz", "10M"))

    def test_warning_performance_precision_and_detection(self):
        # A flip that we warned about (true positive) + a flip we missed.
        _rec(self.led, "w1", "15M", "NO", 88)
        _rec(self.led, "w1", "10M", "YES", 40)
        self.led.resolve_ticker("w1", "YES", time.time() + 700)
        self.led.record_flip_warning(asset="BTC", checkpoint="15M", ticker="w1", direction="NO → YES",
                                     risk_score=88, flip_probability=0.5, confidence=80, now=time.time())
        # A warning that did NOT flip (false positive).
        _rec(self.led, "w2", "15M", "NO", 84)
        _rec(self.led, "w2", "10M", "NO", 50)
        self.led.resolve_ticker("w2", "NO", time.time() + 700)
        self.led.record_flip_warning(asset="BTC", checkpoint="15M", ticker="w2", direction="NO → YES",
                                     risk_score=84, flip_probability=0.5, confidence=80, now=time.time())
        # A flip with no warning (missed).
        _rec(self.led, "w3", "15M", "YES", 30)
        _rec(self.led, "w3", "10M", "NO", 30)
        self.led.resolve_ticker("w3", "NO", time.time() + 700)

        self.led.reconcile_flip_warnings()
        perf = self.led.flip_warning_performance()["overall"]
        self.assertEqual(perf["alerts"], 2)
        self.assertEqual(perf["correct"], 1)
        self.assertEqual(perf["false"], 1)
        self.assertEqual(perf["precision"], 0.5)
        self.assertEqual(perf["actual_flips"], 2)   # w1 (NO->YES) and w3 (YES->NO)
        self.assertEqual(perf["detected"], 1)        # only w1 was warned
        self.assertEqual(perf["missed"], 1)

    def test_warning_dedup(self):
        first = self.led.record_flip_warning(asset="BTC", checkpoint="10M", ticker="d1",
                                             direction="NO → YES", risk_score=85, flip_probability=0.5,
                                             confidence=80, now=time.time())
        second = self.led.record_flip_warning(asset="BTC", checkpoint="10M", ticker="d1",
                                              direction="NO → YES", risk_score=90, flip_probability=0.6,
                                              confidence=82, now=time.time())
        self.assertTrue(first)
        self.assertFalse(second)  # one row per (ticker, checkpoint, direction)


# ----------------------------------------------------------------- formatting
class TestFormatting(unittest.TestCase):
    def _risk(self):
        return fr.RiskAssessment(score=86, confidence=84, evidence_categories=["wicks", "prediction_divergence"],
                                 primary_reason="rejection wick", direction_monitored="NO → YES")

    def test_dashboard_block_fields(self):
        thr = fr.ThresholdResolution(82.0, "BTC 10M NO → YES", "Learned", 40, 30)
        block = "\n".join(fr.dashboard_block(risk=self._risk(), threshold=thr, flip_probability=0.34,
                                             state=fr.WATCH, persistence=2))
        for needle in ["MANIPULATION / FLIP RISK", "Risk score: 86%", "Risk confidence: 84%",
                       "Learned flip threshold: 82%", "Estimated flip probability: 34%",
                       "Direction monitored: NO → YES", "Evidence categories: 2",
                       "Threshold source: BTC 10M NO → YES", "Status: WATCH"]:
            self.assertIn(needle, block)

    def test_high_flip_risk_message(self):
        thr = fr.ThresholdResolution(82.0, "BTC 10M NO → YES", "Learned", 40, 30)
        msg = fr.format_high_flip_risk(asset="BTC", checkpoint="10M", current_side="NO", risk=self._risk(),
                                       threshold=thr, flip_probability=0.48, persistence=3)
        self.assertIn("HIGH FLIP RISK — BTC 10M", msg)
        self.assertIn("Possible flip: NO → YES", msg)
        self.assertIn("not a confirmed flip", msg)

    def test_confirmed_flip_message(self):
        msg = fr.format_confirmed_flip(asset="BTC", checkpoint="10M", previous_side="NO", new_side="YES",
                                       risk_before=88, flip_prob_before=0.53, evidence=["wicks", "volume"])
        self.assertIn("CONFIRMED PREDICTION FLIP — BTC 10M", msg)
        self.assertIn("Previous prediction: NO", msg)
        self.assertIn("New prediction: YES", msg)


class TestPushedAccountingAndSlots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.led = V95Ledger(os.path.join(self.tmp.name, "l.sqlite3"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_pushed_vs_background_kept_separate(self):
        # Two pushed (one right, one wrong) and one background (right).
        _rec(self.led, "p1", "10M", "YES", 20); self.led.mark_pushed("p1", "10M")
        self.led.resolve_ticker("p1", "YES", time.time() + 700)        # pushed correct
        _rec(self.led, "p2", "10M", "YES", 20); self.led.mark_pushed("p2", "10M")
        self.led.resolve_ticker("p2", "NO", time.time() + 700)         # pushed wrong
        _rec(self.led, "b1", "10M", "YES", 20)                          # background
        self.led.resolve_ticker("b1", "YES", time.time() + 700)        # background correct

        sb = self.led.scoreboard()
        pushed = sb["by_pushed"]["pushed"]
        bg = sb["by_pushed"]["background"]
        self.assertEqual((pushed["right"], pushed["wrong"], pushed["n"]), (1, 1, 2))
        self.assertEqual(pushed["accuracy"], 0.5)
        # Background's correct one must NOT leak into the pushed record.
        self.assertEqual((bg["right"], bg["n"]), (1, 1))
        self.assertEqual(sb["pushed_by_checkpoint"]["10M"]["n"], 2)
        # status() exposes the compact pushed record the live alert renders.
        self.assertEqual(self.led.status()["pushed_by_checkpoint"]["10M"]["accuracy"], 0.5)

    def test_one_active_slot_per_timeframe(self):
        now = 1000.0
        # First 10M push claims the slot until its contract closes at now+600.
        self.assertFalse(self.led.pushed_slot_blocks("10M", "c1", now))
        self.assertTrue(self.led.claim_pushed_slot("10M", "c1", now + 600, now))
        # A different contract is blocked while c1 is still open.
        self.assertTrue(self.led.pushed_slot_blocks("10M", "c2", now + 10))
        self.assertFalse(self.led.claim_pushed_slot("10M", "c2", now + 600, now + 10))
        # The same contract is never blocked by itself (re-confirm is fine).
        self.assertFalse(self.led.pushed_slot_blocks("10M", "c1", now + 10))
        # A different timeframe has its own independent slot.
        self.assertFalse(self.led.pushed_slot_blocks("15M", "c2", now + 10))
        # Once c1 closes, the slot frees and c2 can take it.
        self.assertFalse(self.led.pushed_slot_blocks("10M", "c2", now + 601))
        self.assertIsNone(self.led.active_pushed_slot("10M", now + 601))
        self.assertTrue(self.led.claim_pushed_slot("10M", "c2", now + 1200, now + 601))


if __name__ == "__main__":
    unittest.main()
