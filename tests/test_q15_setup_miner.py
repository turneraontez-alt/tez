from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade import setup_miner
from q15_upgrade.setup_miner import (
    Condition, Setup, MineConfig, mine, wilson_lower_bound, binom_sf, build_report_lines,
)


def _row(t, *, result, regime=None, **features):
    return {"created_at": float(t), "features": dict(features), "regime": regime, "result": result}


class StatsTest(unittest.TestCase):
    def test_wilson_perfect_small_sample_is_not_one(self):
        # 8/8 must NOT report as a guarantee; lower bound well below 100%.
        lb = wilson_lower_bound(8, 8)
        self.assertLess(lb, 0.75)
        self.assertGreater(lb, 0.5)

    def test_binom_sf_symmetry_and_bounds(self):
        self.assertAlmostEqual(binom_sf(0, 10, 0.5), 1.0)
        self.assertAlmostEqual(binom_sf(10, 10, 0.5), 0.5 ** 10)
        self.assertEqual(binom_sf(11, 10, 0.5), 0.0)


class ConditionTest(unittest.TestCase):
    def test_missing_feature_is_undecidable(self):
        c = Condition("momentum", "ge", 0.0)
        self.assertIsNone(c.test({}, None))           # absent -> None, not False
        self.assertTrue(c.test({"momentum": 0.1}, None))
        self.assertFalse(c.test({"momentum": -0.1}, None))

    def test_setup_undecidable_when_any_condition_missing(self):
        s = Setup((Condition("a", "ge", 0.0), Condition("b", "lt", 0.0)))
        self.assertIsNone(s.matches({"a": 1.0}, None))  # b missing
        self.assertTrue(s.matches({"a": 1.0, "b": -1.0}, None))
        self.assertFalse(s.matches({"a": -1.0, "b": -1.0}, None))


class MineGuardTest(unittest.TestCase):
    def _cfg(self, **kw):
        base = dict(min_support_train=10, min_support_test=5, max_conditions=2,
                    train_fraction=0.7, min_wilson_lb=0.6)
        base.update(kw)
        return MineConfig(**base)

    def test_insufficient_data_is_reported_not_guessed(self):
        rows = [_row(i, result="YES", momentum=0.1) for i in range(8)]
        rep = mine(rows, self._cfg())
        self.assertEqual(rep.hypotheses_tested, 0)
        self.assertIsNotNone(rep.note)
        self.assertIn("insufficient data", rep.note)

    def test_genuine_signal_validates_out_of_sample(self):
        # Construct a real, stable rule: momentum>=0 -> YES, momentum<0 -> NO,
        # holding in BOTH the early (train) and late (test) slices.
        rows = []
        t = 0
        for _ in range(40):
            rows.append(_row(t, result="YES", momentum=0.3, flow=0.0)); t += 1
        for _ in range(40):
            rows.append(_row(t, result="NO", momentum=-0.3, flow=0.0)); t += 1
        # interleave so the chronological split has both classes in train & test
        rows.sort(key=lambda r: (r["features"]["momentum"] < 0, r["created_at"]))
        for i, r in enumerate(rows):
            r["created_at"] = float(i)
        rep = mine(rows, self._cfg())
        validated = rep.validated(self._cfg())
        labels = {f.setup.label() for f in validated}
        self.assertTrue(any("momentum" in l for l in labels))

    def test_noise_does_not_validate(self):
        # Random-ish 50/50 outcome independent of the feature: nothing should pass
        # the out-of-sample gate after multiple-testing correction.
        rows = []
        for i in range(120):
            rows.append(_row(i, result="YES" if i % 2 == 0 else "NO",
                             momentum=0.1 if i % 3 == 0 else -0.1,
                             flow=0.2 if i % 5 == 0 else -0.2))
        cfg = self._cfg()
        rep = mine(rows, cfg)
        self.assertEqual(rep.validated(cfg), [])

    def test_in_sample_perfect_but_test_fails_is_flagged_not_trusted(self):
        # A setup that is 100% in train but flips in test must NOT be 'validated'.
        rows = []
        t = 0
        # train slice (first 70%): regime PIN always YES
        for _ in range(40):
            rows.append(_row(t, result="YES", regime="PIN", momentum=0.1)); t += 1
        for _ in range(20):
            rows.append(_row(t, result="NO", regime="CALM", momentum=-0.1)); t += 1
        # test slice (last 30%): regime PIN now mostly NO -> rule breaks
        for _ in range(25):
            rows.append(_row(t, result="NO", regime="PIN", momentum=0.1)); t += 1
        cfg = self._cfg()
        rep = mine(rows, cfg)
        pin = [f for f in rep.findings if "regime=PIN" in f.setup.label()]
        self.assertTrue(pin)
        f = pin[0]
        self.assertEqual(f.train_accuracy, 1.0)            # perfect in-sample
        self.assertFalse(f.validated_out_of_sample(cfg))   # but NOT validated
        text = "\n".join(build_report_lines(rep, cfg))
        self.assertIn("did NOT carry over", text)

    def test_multiple_testing_correction_applied(self):
        # Features must vary in sign or they form no discriminating condition.
        rows = [_row(i, result="YES" if i % 2 else "NO",
                     a=0.1 if i % 2 else -0.1, b=-0.1 if i % 3 else 0.1,
                     c=0.2 if i % 4 else -0.2) for i in range(120)]
        cfg = self._cfg()
        rep = mine(rows, cfg)
        # alpha is divided by the number of hypotheses actually tested.
        self.assertGreater(rep.hypotheses_tested, 0)
        self.assertAlmostEqual(rep.alpha_adjusted, cfg.alpha / rep.hypotheses_tested)


if __name__ == "__main__":
    unittest.main()
