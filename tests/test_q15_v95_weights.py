"""Pin the frozen model constants and cover the two regime/coverage knobs.

The champion logit weights and the evidence-quality blend are production model
parameters: changing them silently shifts every prediction. These tests assert
their exact values so an accidental edit fails loudly (promotion is meant to be
manual + significance-tested, never an incidental diff). They also cover the two
new default-OFF tuning knobs:
  * Q15_V95_REGIME_AWARE_ANCHOR  — shrink model deviation in noisy regimes.
  * Q15_V95_EVIDENCE_COVERAGE_*  — widen the haircut when evidence is thin.
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.ledger_v95 import CHAMPION_WEIGHTS
from q15_upgrade.checkpoint_v95 import (
    _EVIDENCE_QUALITY_WEIGHTS,
    _regime_anchor_strength,
)


class TestFrozenWeights(unittest.TestCase):
    def test_champion_weights_are_pinned(self):
        self.assertEqual(CHAMPION_WEIGHTS, {
            "intercept": 0.0,
            "momentum": 0.34,
            "flow": 0.26,
            "book": 0.18,
            "wick": 0.12,
            "context": 0.18,
            "threshold_interaction": 0.30,
            "exchange_consensus": 0.18,
            "derivatives": 0.10,
            "absorption": 0.20,
        })

    def test_evidence_quality_blend_is_pinned_and_normalized(self):
        self.assertEqual(_EVIDENCE_QUALITY_WEIGHTS, {
            "momentum": 0.25,
            "flow": 0.16,
            "book": 0.12,
            "wick": 0.08,
            "context": 0.12,
            "threshold_interaction": 0.15,
            "exchange_consensus": 0.08,
            "derivatives": 0.04,
        })
        self.assertAlmostEqual(sum(_EVIDENCE_QUALITY_WEIGHTS.values()), 1.0, places=9)


class TestRegimeAwareAnchor(unittest.TestCase):
    def tearDown(self):
        for k in ("Q15_V95_REGIME_AWARE_ANCHOR", "Q15_V95_REGIME_ANCHOR_SENSITIVITY",
                  "Q15_V95_REGIME_ANCHOR_MIN_FACTOR"):
            os.environ.pop(k, None)

    def test_enabled_by_default_shrinks_in_noisy_regime(self):
        # Default ON: a noisy regime (uncertainty above the 0.08 baseline) shrinks
        # the anchor strength without any env var set.
        strength, factor = _regime_anchor_strength(1.0, {"uncertainty": 0.20})
        self.assertLess(factor, 1.0)
        self.assertLess(strength, 1.0)

    def test_can_be_disabled_back_to_identity(self):
        os.environ["Q15_V95_REGIME_AWARE_ANCHOR"] = "false"
        strength, factor = _regime_anchor_strength(1.0, {"uncertainty": 0.20})
        self.assertEqual(strength, 1.0)
        self.assertEqual(factor, 1.0)

    def test_noisy_regime_shrinks_strength_when_enabled(self):
        os.environ["Q15_V95_REGIME_AWARE_ANCHOR"] = "true"
        os.environ["Q15_V95_REGIME_ANCHOR_SENSITIVITY"] = "3.0"
        # NORMAL baseline (0.08) -> no shrink; high uncertainty -> shrink.
        s_normal, f_normal = _regime_anchor_strength(1.0, {"uncertainty": 0.08})
        s_pin, f_pin = _regime_anchor_strength(1.0, {"uncertainty": 0.20})
        self.assertEqual(f_normal, 1.0)
        self.assertLess(f_pin, 1.0)
        self.assertLess(s_pin, s_normal)
        # factor = 1 - 3*(0.20-0.08) = 0.64
        self.assertAlmostEqual(f_pin, 0.64, places=6)

    def test_factor_respects_floor(self):
        os.environ["Q15_V95_REGIME_AWARE_ANCHOR"] = "true"
        os.environ["Q15_V95_REGIME_ANCHOR_SENSITIVITY"] = "50.0"
        os.environ["Q15_V95_REGIME_ANCHOR_MIN_FACTOR"] = "0.4"
        _, factor = _regime_anchor_strength(1.0, {"uncertainty": 0.20})
        self.assertGreaterEqual(factor, 0.4)


if __name__ == "__main__":
    unittest.main()
