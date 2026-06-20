"""Per-interval performance metrics + 10M learning priority.

Covers the item-4 additions: precision/recall (YES & NO), FPR/FNR, by-grade
breakdown, prediction-stability (change-rate), the configurable 10M sample-weight
boost, and the new schema columns (grade / original side / changed_before_close).
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.ledger_v95 import V95Ledger


def _mk_ledger():
    return V95Ledger(tempfile.mktemp(suffix=".sqlite3"))


def _record(led, ticker, cp, side, rank=1, grade="A", asset="ETH"):
    return led.record_prediction(
        ticker=ticker, asset=asset, checkpoint=cp, created_at=time.time(),
        close_time=time.time() + 600, predicted_side=side,
        raw_yes_probability=0.7, calibrated_yes_probability=0.7, challenger_yes_probability=0.7,
        baseline_yes_probability=0.6, selected_probability=0.7, conservative_probability=0.65,
        data_quality=0.8, evidence_quality=0.7, trade_quality=0.7,
        trade_decision="ENTRY_RECOMMENDED", regime="NORMAL",
        features={"momentum": 0.3}, contributions={"momentum": 0.2}, quote={"ask_cents": 50},
        rank=rank, confidence_grade=grade,
    )


class TestClassificationMetrics(unittest.TestCase):
    def test_precision_recall_and_error_rates(self):
        led = _mk_ledger()
        # YES preds: 2 correct (TP), 1 wrong (FP). NO preds: 1 correct (TN), 1 wrong (FN).
        _record(led, "y1", "10M", "YES"); led.resolve_ticker("y1", "YES")  # TP
        _record(led, "y2", "10M", "YES"); led.resolve_ticker("y2", "YES")  # TP
        _record(led, "y3", "10M", "YES"); led.resolve_ticker("y3", "NO")   # FP
        _record(led, "n1", "10M", "NO");  led.resolve_ticker("n1", "NO")   # TN
        _record(led, "n2", "10M", "NO");  led.resolve_ticker("n2", "YES")  # FN
        c = led.scoreboard()["by_checkpoint"]["10M"]["classification"]
        self.assertEqual((c["tp"], c["fp"], c["tn"], c["fn"]), (2, 1, 1, 1))
        self.assertAlmostEqual(c["precision_yes"], 2 / 3, places=3)   # TP/(TP+FP)
        self.assertAlmostEqual(c["recall_yes"], 2 / 3, places=3)      # TP/(TP+FN)
        self.assertAlmostEqual(c["precision_no"], 1 / 2, places=3)    # TN/(TN+FN)
        self.assertAlmostEqual(c["recall_no"], 1 / 2, places=3)       # TN/(TN+FP)
        self.assertAlmostEqual(c["false_positive_rate"], 1 / 2, places=3)  # FP/(FP+TN)
        self.assertAlmostEqual(c["false_negative_rate"], 1 / 3, places=3)  # FN/(FN+TP)

    def test_by_grade_breakdown(self):
        led = _mk_ledger()
        _record(led, "a1", "10M", "YES", grade="A"); led.resolve_ticker("a1", "YES")
        _record(led, "c1", "10M", "YES", grade="C"); led.resolve_ticker("c1", "NO")
        by_grade = led.scoreboard()["by_checkpoint"]["10M"]["by_grade"]
        self.assertEqual(by_grade["A"]["accuracy"], 1.0)
        self.assertEqual(by_grade["C"]["accuracy"], 0.0)
        self.assertNotIn("B", by_grade)  # no B rows -> omitted

    def test_priority_interval_surfaced(self):
        led = _mk_ledger()
        self.assertEqual(led.scoreboard()["priority_interval"], "10M")


class TestPredictionStability(unittest.TestCase):
    def test_change_rate_flags_side_drift_without_mutating_grade(self):
        led = _mk_ledger()
        _record(led, "drift", "10M", "YES")
        # Live side flips to NO before close -> flagged, but graded side stays YES.
        self.assertTrue(led.note_prediction_revision(ticker="drift", checkpoint="10M", current_side="NO"))
        # Idempotent: a second flip call does not re-flag.
        self.assertFalse(led.note_prediction_revision(ticker="drift", checkpoint="10M", current_side="NO"))
        led.resolve_ticker("drift", "YES")  # original YES was correct
        cp = led.scoreboard()["by_checkpoint"]["10M"]
        self.assertEqual(cp["right"], 1)               # graded on the locked YES
        self.assertEqual(cp["stability"]["changed"], 1)
        self.assertEqual(cp["stability"]["change_rate"], 1.0)

    def test_same_side_is_not_flagged(self):
        led = _mk_ledger()
        _record(led, "steady", "10M", "YES")
        self.assertFalse(led.note_prediction_revision(ticker="steady", checkpoint="10M", current_side="YES"))
        led.resolve_ticker("steady", "YES")
        self.assertEqual(led.scoreboard()["by_checkpoint"]["10M"]["stability"]["change_rate"], 0.0)


class TestTenMinutePriorityWeight(unittest.TestCase):
    def test_primary_weight_is_configurable_and_defaults_high(self):
        led = _mk_ledger()
        self.assertGreaterEqual(led.primary_learning_weight, 1.25)

    def test_env_override(self):
        os.environ["Q15_V95_PRIMARY_LEARNING_WEIGHT"] = "1.5"
        try:
            self.assertAlmostEqual(_mk_ledger().primary_learning_weight, 1.5, places=6)
        finally:
            del os.environ["Q15_V95_PRIMARY_LEARNING_WEIGHT"]


class TestSchemaMigration(unittest.TestCase):
    def test_new_columns_present_and_reopen_safe(self):
        path = tempfile.mktemp(suffix=".sqlite3")
        led = V95Ledger(path)
        _record(led, "m1", "10M", "YES", grade="B")
        import sqlite3
        con = sqlite3.connect(path)
        cols = {r[1] for r in con.execute("PRAGMA table_info(predictions)")}
        con.close()
        self.assertTrue({"confidence_grade", "original_predicted_side", "changed_before_close"} <= cols)
        # Reopen must be a no-op migration.
        V95Ledger(path)


if __name__ == "__main__":
    unittest.main()
