"""Shadow factor lab: per-factor reliability, the promotion gate, and rendering.

All deterministic on synthetic settled rows. The analyzer is read-only and never
applies anything; these tests assert the math + gate, not any live behavior.
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade import factor_lab


def _row(value, result, *, checkpoint="10M", regime="trend", correct=None,
         changed=0, factor="wick", t=0.0):
    return {
        "created_at": t,
        "checkpoint": checkpoint,
        "regime": regime,
        "predicted_side": "YES" if value > 0 else "NO",
        "result": result,
        "changed_before_close": changed,
        "correct": correct,
        "features": {factor: value},
    }


class WilsonTest(unittest.TestCase):
    def test_lower_bound_monotonic_and_bounded(self):
        self.assertEqual(factor_lab._wilson_lower_bound(0, 0), 0.0)
        small = factor_lab._wilson_lower_bound(8, 10)
        big = factor_lab._wilson_lower_bound(80, 100)
        self.assertLess(small, big)            # same rate, more samples -> higher LB
        self.assertGreaterEqual(small, 0.0)
        self.assertLessEqual(big, 1.0)


class FactorReliabilityTest(unittest.TestCase):
    def test_perfectly_aligned_factor_is_reliable(self):
        # Factor's sign always matches the final result -> reliability 1.0.
        rows = [_row(0.6, "YES", t=i) for i in range(30)] + \
               [_row(-0.6, "NO", t=30 + i) for i in range(30)]
        rep = factor_lab.analyze(rows, min_samples=20)
        self.assertTrue(rep["available"])
        wick = next(f for f in rep["factors"] if f["name"] == "wick")
        self.assertEqual(wick["fired"], 60)
        self.assertAlmostEqual(wick["reliability"], 1.0)
        self.assertGreater(wick["prob_shift"], 0.9)   # YES-lean -> YES, NO-lean -> NO

    def test_deadzone_excludes_weak_values(self):
        rows = [_row(0.02, "NO", t=i) for i in range(50)]  # below deadzone -> never fires
        rep = factor_lab.analyze(rows, deadzone=0.05, min_samples=10)
        self.assertEqual(rep["factors"], [])

    def test_misleading_factor_low_reliability(self):
        # Factor leans YES but settles NO most of the time -> reliability ~0.2.
        rows = [_row(0.5, "NO", t=i) for i in range(40)] + \
               [_row(0.5, "YES", t=40 + i) for i in range(10)]
        rep = factor_lab.analyze(rows, min_samples=20)
        wick = next(f for f in rep["factors"] if f["name"] == "wick")
        self.assertAlmostEqual(wick["reliability"], 0.2, places=2)
        self.assertFalse(wick["promotion"]["ready"])


class PromotionGateTest(unittest.TestCase):
    def test_thin_sample_is_not_promotion_ready(self):
        rows = [_row(0.6, "YES", t=i) for i in range(5)]
        rep = factor_lab.analyze(rows, min_samples=40)
        wick = next(f for f in rep["factors"] if f["name"] == "wick")
        self.assertFalse(wick["promotion"]["ready"])
        self.assertTrue(any("insufficient_samples" in r for r in wick["promotion"]["reasons"]))
        self.assertEqual(rep["promotion_ready"], [])

    def test_strong_consistent_factor_is_promotion_ready(self):
        # Reliable across both time halves, enough samples, big shift.
        rows = [_row(0.7, "YES", t=i) for i in range(60)] + \
               [_row(-0.7, "NO", t=60 + i) for i in range(60)]
        rep = factor_lab.analyze(rows, min_samples=40, reliability_threshold=0.55)
        wick = next(f for f in rep["factors"] if f["name"] == "wick")
        self.assertTrue(wick["promotion"]["ready"], wick["promotion"]["reasons"])
        self.assertIn("wick", rep["promotion_ready"])

    def test_oos_inconsistency_blocks_promotion(self):
        # First half reliable, second half a coin flip -> OOS inconsistent.
        first = [_row(0.6, "YES", t=i) for i in range(40)]
        second = ([_row(0.6, "YES", t=40 + i) for i in range(20)] +
                  [_row(0.6, "NO", t=60 + i) for i in range(20)])
        rep = factor_lab.analyze(first + second, min_samples=40, reliability_threshold=0.5)
        wick = next(f for f in rep["factors"] if f["name"] == "wick")
        self.assertFalse(wick["promotion"]["ready"])
        self.assertTrue(any("oos" in r for r in wick["promotion"]["reasons"]))


class CombinationsByIntervalTest(unittest.TestCase):
    def test_combo_carries_per_interval_breakdown(self):
        # Two factors that agree, across all three intervals, settling their way.
        rows = []
        t = 0
        for iv in ("15M", "10M", "7M"):
            for _ in range(20):
                rows.append(_row(0.5, "YES", checkpoint=iv, factor="wick", t=t)); t += 1
            # add the second factor (momentum) to the same rows by merging features
        # rebuild rows with both factors present so they "agree"
        rows = []
        t = 0
        for iv in ("15M", "10M", "7M"):
            for _ in range(20):
                rows.append({
                    "created_at": t, "checkpoint": iv, "regime": "trend",
                    "predicted_side": "YES", "result": "YES",
                    "changed_before_close": 0, "correct": 1,
                    "features": {"wick": 0.5, "momentum": 0.5},
                }); t += 1
        rep = factor_lab.analyze(rows, min_samples=20)
        combo = next(c for c in rep["combinations"]
                     if set(c["factors"]) == {"wick", "momentum"})
        self.assertIn("by_interval", combo)
        for iv in ("15M", "10M", "7M"):
            self.assertIn(iv, combo["by_interval"])
            self.assertEqual(combo["by_interval"][iv]["fired"], 20)
            self.assertAlmostEqual(combo["by_interval"][iv]["reliability"], 1.0)

    def test_render_shows_interval_breakdown_line(self):
        rows = []
        t = 0
        for iv in ("15M", "10M", "7M"):
            for _ in range(20):
                rows.append({
                    "created_at": t, "checkpoint": iv, "regime": "trend",
                    "predicted_side": "YES", "result": "YES",
                    "changed_before_close": 0, "correct": 1,
                    "features": {"wick": 0.5, "momentum": 0.5},
                }); t += 1
        rep = factor_lab.analyze(rows, min_samples=20)
        text = "\n".join(factor_lab.format_report(rep))
        self.assertIn("by interval", text)
        self.assertIn("15M", text)
        self.assertIn("7M", text)


class RenderTest(unittest.TestCase):
    def test_format_report_is_compact_text(self):
        rows = [_row(0.6, "YES", correct=1, t=i) for i in range(30)] + \
               [_row(-0.6, "NO", correct=1, t=30 + i) for i in range(30)]
        rep = factor_lab.analyze(rows, min_samples=20)
        lines = factor_lab.format_report(rep, detail=True)
        text = "\n".join(lines)
        self.assertIn("FACTOR LAB (shadow)", text)
        self.assertIn("wick", text)
        self.assertIn("promotion-ready", text)

    def test_empty_report_renders_safely(self):
        rep = factor_lab.analyze([])
        self.assertFalse(rep["available"])
        lines = factor_lab.format_report(rep)
        self.assertTrue(any("FACTOR LAB" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
