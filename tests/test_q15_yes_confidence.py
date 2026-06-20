from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.window_focus import FocusSettings, TwoWindowFocusManager


class Store:
    enabled = False


class Notifier:
    enabled = True

    def send(self, msg):
        return True


def make_manager(settings=None):
    return TwoWindowFocusManager(Store(), Notifier(), None, None, settings or FocusSettings())


ALIGNED = ["market:aligned", "distance:aligned", "model:aligned"]


def pred(side="YES", consensus=0.98, side_prob=0.80, decisive=0.60, tags=None):
    return {
        "side": side,
        "consensus": consensus,
        "side_probability": side_prob,
        "decisiveness": decisive,
        "feature_tags": ALIGNED if tags is None else tags,
    }


class YesConfidenceTest(unittest.TestCase):
    def test_strong_when_signature_matches(self):
        mgr = make_manager()
        yc = mgr._yes_confidence(pred())
        self.assertIsNotNone(yc)
        self.assertEqual(yc["label"], "STRONG")
        self.assertEqual(yc["reasons"], [])

    def test_weak_when_consensus_low(self):
        mgr = make_manager()
        yc = mgr._yes_confidence(pred(consensus=0.90))
        self.assertEqual(yc["label"], "WEAK")
        self.assertTrue(any("consensus" in r for r in yc["reasons"]))

    def test_weak_when_distance_alignment_missing(self):
        # Distance alignment is the dominant differentiator for YES (~91% vs ~50%).
        mgr = make_manager()
        yc = mgr._yes_confidence(pred(tags=["market:aligned", "model:aligned"]))
        self.assertEqual(yc["label"], "WEAK")
        self.assertTrue(any("alignment" in r for r in yc["reasons"]))

    def test_weak_when_side_prob_low(self):
        mgr = make_manager()
        yc = mgr._yes_confidence(pred(side_prob=0.50))
        self.assertEqual(yc["label"], "WEAK")

    def test_alignment_not_required_when_disabled(self):
        mgr = make_manager(FocusSettings(yes_confidence_require_alignment=False))
        yc = mgr._yes_confidence(pred(tags=["side:YES"]))
        self.assertEqual(yc["label"], "STRONG")

    def test_none_for_no_side(self):
        mgr = make_manager()
        self.assertIsNone(mgr._yes_confidence(pred(side="NO")))

    def test_none_for_uncertain_side(self):
        mgr = make_manager()
        self.assertIsNone(mgr._yes_confidence(pred(side="UNCERTAIN")))

    def test_none_when_disabled(self):
        mgr = make_manager(FocusSettings(yes_confidence_enabled=False))
        self.assertIsNone(mgr._yes_confidence(pred()))

    def test_none_for_non_mapping(self):
        mgr = make_manager()
        self.assertIsNone(mgr._yes_confidence(None))

    def test_alert_includes_strong_line_for_yes(self):
        mgr = make_manager()
        mgr._cycles["BTC-T"] = {"predictions": {"10m": pred()}}
        row = {"asset": "BTC", "side": "YES", "ticker": "BTC-T", "trade_plan": {}}
        msg = mgr._format_checkpoint_alert("10m", row)
        self.assertIn("YES conviction: <b>STRONG</b>", msg)

    def test_alert_weak_line_escapes_reason_text(self):
        mgr = make_manager()
        mgr._cycles["BTC-T"] = {"predictions": {"10m": pred(consensus=0.90)}}
        row = {"asset": "BTC", "side": "YES", "ticker": "BTC-T", "trade_plan": {}}
        msg = mgr._format_checkpoint_alert("10m", row)
        self.assertIn("YES conviction: <b>WEAK</b>", msg)
        # The reason "consensus 90% < 95%" must be HTML-escaped (< -> &lt;).
        self.assertIn("&lt;", msg)
        self.assertNotIn("% < ", msg)

    def test_alert_omits_line_for_no(self):
        mgr = make_manager()
        mgr._cycles["BTC-T"] = {"predictions": {"10m": pred(side="NO")}}
        row = {"asset": "BTC", "side": "NO", "ticker": "BTC-T", "trade_plan": {}}
        msg = mgr._format_checkpoint_alert("10m", row)
        self.assertNotIn("YES conviction", msg)


if __name__ == "__main__":
    unittest.main()
