"""R2 + R3 — YES-edge knobs (both default-OFF, frozen-champion-safe).

R2  Q15_V95_YES_DECISION_THRESHOLD_15M — a 15M-scoped calibrated-YES decision
    threshold. Default 0.5 reproduces the frozen symmetric cut exactly; the whole
    YES deficit lives at 15M, so the knob only moves that checkpoint.

R3  Q15_V95_CHALLENGER_YES_ASSIST — an observational marker for the 10M/7M
    BNB/HYPE/XRP/DOGE pocket where the out-of-sample challenger's YES recall beats
    the NO-leaning champion. It never changes the side or the alert.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.checkpoint_v95 import (
    _challenger_yes_assist,
    _yes_decision_threshold,
)


class YesDecisionThresholdTest(unittest.TestCase):
    def test_default_is_symmetric_half_at_every_checkpoint(self):
        # Default 0.5 = byte-identical to the frozen `>= 0.5` cut.
        for cp in ("15M", "10M", "7M", "13M", "unknown", None):
            self.assertEqual(_yes_decision_threshold(cp), 0.5)

    def test_override_applies_to_15m_only(self):
        with mock.patch.dict(os.environ,
                             {"Q15_V95_YES_DECISION_THRESHOLD_15M": "0.45"}):
            self.assertEqual(_yes_decision_threshold("15M"), 0.45)
            # 10M / 7M are deliberately untouched by the 15M-scoped knob.
            self.assertEqual(_yes_decision_threshold("10M"), 0.5)
            self.assertEqual(_yes_decision_threshold("7M"), 0.5)

    def test_override_is_clamped_to_safe_band(self):
        with mock.patch.dict(os.environ,
                             {"Q15_V95_YES_DECISION_THRESHOLD_15M": "0.05"}):
            self.assertEqual(_yes_decision_threshold("15M"), 0.40)  # low clamp
        with mock.patch.dict(os.environ,
                             {"Q15_V95_YES_DECISION_THRESHOLD_15M": "0.95"}):
            self.assertEqual(_yes_decision_threshold("15M"), 0.60)  # high clamp

    def test_lowering_threshold_flips_a_borderline_15m_call_to_yes(self):
        # A calibrated_yes of 0.47 is NO under the symmetric cut but YES once the
        # 15M threshold is relaxed to 0.45 — this is the recall lever in action.
        calibrated_yes = 0.47
        self.assertEqual(
            "YES" if calibrated_yes >= _yes_decision_threshold("15M") else "NO",
            "NO")
        with mock.patch.dict(os.environ,
                             {"Q15_V95_YES_DECISION_THRESHOLD_15M": "0.45"}):
            self.assertEqual(
                "YES" if calibrated_yes >= _yes_decision_threshold("15M") else "NO",
                "YES")


class ChallengerYesAssistTest(unittest.TestCase):
    def test_inactive_by_default_even_in_an_eligible_cell(self):
        self.assertEqual(
            _challenger_yes_assist("BNB", "10M", "NO", 0.72),
            {"active": False})

    def _enabled(self):
        return mock.patch.dict(os.environ,
                               {"Q15_V95_CHALLENGER_YES_ASSIST": "true"})

    def test_active_in_the_validated_pocket(self):
        with self._enabled():
            out = _challenger_yes_assist("hype", "7M", "NO", 0.66)
        self.assertTrue(out["active"])
        self.assertEqual(out["challenger_side"], "YES")
        self.assertEqual(out["champion_side"], "NO")
        self.assertEqual(out["asset"], "HYPE")
        self.assertEqual(out["checkpoint"], "7M")
        self.assertEqual(out["challenger_yes_probability"], 0.66)

    def test_inactive_outside_the_pocket(self):
        with self._enabled():
            # Wrong asset (champion wins on BTC).
            self.assertFalse(_challenger_yes_assist("BTC", "10M", "NO", 0.8)["active"])
            # Wrong checkpoint (challenger loses at 15M).
            self.assertFalse(_challenger_yes_assist("BNB", "15M", "NO", 0.8)["active"])
            # Champion already says YES — no disagreement to flag.
            self.assertFalse(_challenger_yes_assist("BNB", "10M", "YES", 0.8)["active"])
            # Challenger does not actually lean YES.
            self.assertFalse(_challenger_yes_assist("BNB", "10M", "NO", 0.49)["active"])


if __name__ == "__main__":
    unittest.main()
