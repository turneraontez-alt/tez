"""Student-t two-sided p-value for the promotion screen.

The review noted the promotion significance test used a normal approximation at
n~50, where Student's t has materially heavier tails (the normal is slightly
anti-conservative). `_two_sided_p` now takes optional degrees of freedom and uses
the exact t-distribution via the regularized incomplete beta; the paired better
test passes df = n - 1. These lock the new behaviour against regression.
"""
from __future__ import annotations

import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.ledger_v95 import V95Ledger, _two_sided_p


class StudentTTwoSidedP(unittest.TestCase):
    def test_non_finite_still_p_one(self):
        for t in (math.inf, -math.inf, math.nan):
            self.assertEqual(_two_sided_p(t, df=10), 1.0)
            self.assertEqual(_two_sided_p(t), 1.0)

    def test_zero_statistic_is_p_one(self):
        self.assertAlmostEqual(_two_sided_p(0.0, df=10), 1.0, places=6)

    def test_t_has_heavier_tails_than_normal(self):
        # For the same statistic, the t-distribution assigns a LARGER (more
        # conservative) two-sided p-value than the normal — most pronounced at
        # small df. This is the whole point of the fix.
        for t in (1.5, 1.96, 2.5, 3.0):
            p_t = _two_sided_p(t, df=10)
            p_norm = _two_sided_p(t)
            self.assertGreater(p_t, p_norm, f"t-tail must exceed normal at t={t}")

    def test_matches_known_t_values(self):
        # Reference two-sided p-values (from standard t tables / scipy):
        #   t=2.228, df=10  -> 0.05    (the classic 95% two-sided critical value)
        #   t=2.086, df=20  -> 0.05
        self.assertAlmostEqual(_two_sided_p(2.228, df=10), 0.05, places=3)
        self.assertAlmostEqual(_two_sided_p(2.086, df=20), 0.05, places=3)

    def test_large_df_converges_to_normal(self):
        # As df -> infinity the t-distribution converges to the normal.
        for t in (0.5, 1.96, 3.0):
            self.assertAlmostEqual(_two_sided_p(t, df=100000), _two_sided_p(t), places=3)

    def test_bounded(self):
        for t in (-10.0, -1.0, 0.0, 1.0, 1.96, 10.0):
            p = _two_sided_p(t, df=7)
            self.assertGreaterEqual(p, 0.0)
            self.assertLessEqual(p, 1.0)


def _rows(diff, jitter, n=30):
    out = []
    for i in range(n):
        d = diff + (jitter if i % 2 == 0 else -jitter)
        out.append({"champion_brier": 0.20, "challenger_brier": 0.20 - d})
    return out


class PairedTestUsesStudentT(unittest.TestCase):
    def test_paired_test_p_value_is_finite_and_uses_df(self):
        # A genuine, material improvement is still favored and significant — the
        # df-aware p-value must not blunt a real effect.
        res = V95Ledger._paired_better_test(_rows(0.05, 0.005), "champion_brier", "challenger_brier")
        self.assertTrue(res["favored"])
        self.assertEqual(res["reason"], "ok")
        self.assertLess(res["p_value"], 0.05)

    def test_paired_p_is_more_conservative_than_normal(self):
        # The reported p-value should equal the Student-t value (>= the normal
        # approximation it replaced) for the same statistic.
        os.environ["Q15_V95_PROMOTION_MIN_EFFECT"] = "0.0"
        try:
            res = V95Ledger._paired_better_test(_rows(0.01, 0.02, n=12),
                                                "champion_brier", "challenger_brier")
        finally:
            os.environ.pop("Q15_V95_PROMOTION_MIN_EFFECT", None)
        if res["t"] is not None:
            # res["t"] is rounded to 4 places; recompute from it to ~4 places.
            normal_p = _two_sided_p(res["t"])
            t_p = _two_sided_p(res["t"], df=12 - 1)
            self.assertAlmostEqual(res["p_value"], t_p, places=4)
            self.assertGreaterEqual(t_p, normal_p)


if __name__ == "__main__":
    unittest.main()
