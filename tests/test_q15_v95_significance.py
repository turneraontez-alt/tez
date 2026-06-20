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
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.ledger_v95 import _two_sided_p


class TestTwoSidedP(unittest.TestCase):
    def test_non_finite_is_not_significant(self):
        self.assertEqual(_two_sided_p(math.inf), 1.0)
        self.assertEqual(_two_sided_p(-math.inf), 1.0)
        self.assertEqual(_two_sided_p(math.nan), 1.0)

    def test_zero_statistic_is_p_one(self):
        self.assertAlmostEqual(_two_sided_p(0.0), 1.0, places=6)

    def test_large_finite_statistic_is_significant(self):
        # A genuine, finite separation still reads as significant (regression guard
        # that the non-finite fix did not blunt the normal path).
        self.assertLess(_two_sided_p(5.0), 0.001)

    def test_p_value_is_bounded(self):
        for t in (-10.0, -1.0, 0.0, 1.0, 1.96, 10.0):
            p = _two_sided_p(t)
            self.assertGreaterEqual(p, 0.0)
            self.assertLessEqual(p, 1.0)


if __name__ == "__main__":
    unittest.main()
