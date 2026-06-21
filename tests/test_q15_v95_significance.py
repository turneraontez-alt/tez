"""Promotion significance gate: non-finite statistics are not 'significant'.

A degenerate zero-variance paired sample makes the t-statistic ±inf; a NaN can
arise from other pathological inputs. The two-sided p-value for a non-finite
statistic must be 1.0 (no usable evidence of a difference), never 0.0 — otherwise
the promotion screen could read a degenerate sample as maximally significant.
Promotion stays manual, but the screening gate must not lie.
"""
from __future__ import annotations

import math
import os
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.ledger_v95 import V95Ledger, _two_sided_p, _round_p


def _rows(diff, jitter=0.0, n=40):
    """n paired rows where champion_brier - challenger_brier ~= diff (challenger
    better when diff>0). jitter gives the sample a non-zero variance."""
    out = []
    for i in range(n):
        d = diff + (jitter if i % 2 == 0 else -jitter)
        out.append({"champion_brier": 0.20, "challenger_brier": 0.20 - d})
    return out


class TestTwoSidedP(unittest.TestCase):
    def test_non_finite_is_not_significant(self):
        self.assertEqual(_two_sided_p(math.inf), 1.0)
        self.assertEqual(_two_sided_p(-math.inf), 1.0)
        self.assertEqual(_two_sided_p(math.nan), 1.0)

    def test_zero_statistic_is_p_one(self):
        self.assertAlmostEqual(_two_sided_p(0.0), 1.0, places=6)

    def test_round_p_preserves_strong_results(self):
        # A very strong p-value must NOT flatten to 0.0 (the old fixed 6-dp bug).
        self.assertGreater(_round_p(1e-9), 0.0)
        self.assertGreater(_round_p(3.21e-8), 0.0)
        # Ordinary p-values keep 6-dp behaviour; None and 0 pass through.
        self.assertEqual(_round_p(0.05), 0.05)
        self.assertIsNone(_round_p(None))
        self.assertEqual(_round_p(0.0), 0.0)

    def test_large_finite_statistic_is_significant(self):
        # A genuine, finite separation still reads as significant (regression guard
        # that the non-finite fix did not blunt the normal path).
        self.assertLess(_two_sided_p(5.0), 0.001)

    def test_p_value_is_bounded(self):
        for t in (-10.0, -1.0, 0.0, 1.0, 1.96, 10.0):
            p = _two_sided_p(t)
            self.assertGreaterEqual(p, 0.0)
            self.assertLessEqual(p, 1.0)


class TestPairedBetterTest(unittest.TestCase):
    """The promotion screen must not read a near-degenerate sample (tiny mean,
    numerically tiny variance) as significant, but a genuinely large, consistent
    improvement — even with small variance — is real evidence and stays favored."""

    def test_tiny_improvement_reads_significant_without_floor(self):
        # Documents the hazard: with real floats, near-identical tiny diffs have
        # numerically tiny (not exactly zero) variance, so an economically
        # meaningless +0.0005 Brier reads as highly significant when unguarded.
        os.environ["Q15_V95_PROMOTION_MIN_EFFECT"] = "0.0"
        try:
            res = V95Ledger._paired_better_test(_rows(0.0005, jitter=0.00001), "champion_brier", "challenger_brier")
        finally:
            os.environ.pop("Q15_V95_PROMOTION_MIN_EFFECT", None)
        self.assertTrue(res["favored"])
        self.assertLess(res["p_value"], 0.05)   # the spurious significance

    def test_default_floor_blocks_tiny_improvement(self):
        # The default effect floor rejects the same trivial improvement.
        res = V95Ledger._paired_better_test(_rows(0.0005, jitter=0.00001), "champion_brier", "challenger_brier")
        self.assertFalse(res["favored"])
        self.assertEqual(res["reason"], "effect_below_floor")
        self.assertIsNone(res["p_value"])

    def test_large_consistent_improvement_is_favored(self):
        # A real, material improvement clears the default floor and is favored.
        res = V95Ledger._paired_better_test(_rows(0.05, jitter=0.005), "champion_brier", "challenger_brier")
        self.assertTrue(res["favored"])
        self.assertLess(res["p_value"], 0.05)


class TestNullBrierAggregation(unittest.TestCase):
    """A single NULL scoring column (e.g. a row written under an older schema)
    must not raise from float(None) or collapse the checkpoint group."""

    def _rec(self, led, ticker, side):
        led.record_prediction(
            ticker=ticker, asset="ETH", checkpoint="10M", created_at=time.time(),
            close_time=time.time() + 600, predicted_side=side,
            raw_yes_probability=0.6, calibrated_yes_probability=0.6,
            challenger_yes_probability=0.6, baseline_yes_probability=0.5,
            selected_probability=0.7, conservative_probability=0.65,
            data_quality=0.9, evidence_quality=0.8, trade_quality=0.7,
            trade_decision="ENTRY_RECOMMENDED", regime="NORMAL",
            features={"momentum": 0.3}, contributions={"momentum": 0.2},
            quote={"ask_cents": 50.0}, costs={"total_cents": 2.0}, rank=1,
        )

    def test_null_brier_row_does_not_break_metrics(self):
        os.environ["Q15_V95_10M_SHADOW_LEARNING"] = "true"
        try:
            led = V95Ledger(tempfile.mktemp(suffix=".sqlite3"))
            for i in range(6):
                res = "YES" if i % 2 == 0 else "NO"
                self._rec(led, f"T{i}", res)
                led.resolve_ticker(f"T{i}", res)
            con = sqlite3.connect(led.path)
            con.execute("UPDATE predictions SET champion_brier=NULL, challenger_brier=NULL WHERE ticker='T0'")
            con.commit()
            con.close()
            m = led.metrics()   # must not raise
        finally:
            os.environ.pop("Q15_V95_10M_SHADOW_LEARNING", None)
        self.assertTrue(m["available"])
        self.assertEqual(m["overall"]["resolved"], 6)
        # Averaged over the 5 rows that still carry a value (not collapsed/None).
        self.assertIsInstance(m["overall"]["champion_brier"], float)


if __name__ == "__main__":
    unittest.main()
