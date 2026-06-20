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


def pred(side="NO", consensus=0.99, side_prob=0.81, decisive=0.63, tags=None):
    return {
        "side": side,
        "consensus": consensus,
        "side_probability": side_prob,
        "decisiveness": decisive,
        "feature_tags": ALIGNED if tags is None else tags,
    }


class NoConfidenceTest(unittest.TestCase):
    def test_strong_when_signature_matches(self):
        mgr = make_manager()
        nc = mgr._no_confidence(pred())
        self.assertIsNotNone(nc)
        self.assertEqual(nc["label"], "STRONG")
        self.assertEqual(nc["reasons"], [])

    def test_weak_when_consensus_low(self):
        mgr = make_manager()
        nc = mgr._no_confidence(pred(consensus=0.83))
        self.assertEqual(nc["label"], "WEAK")
        self.assertTrue(any("consensus" in r for r in nc["reasons"]))

    def test_weak_when_decisiveness_low(self):
        # Mirrors the single historical losing NO (decisiveness 0.21).
        mgr = make_manager()
        nc = mgr._no_confidence(pred(consensus=0.83, side_prob=0.60, decisive=0.21))
        self.assertEqual(nc["label"], "WEAK")

    def test_weak_when_alignment_missing(self):
        mgr = make_manager()
        nc = mgr._no_confidence(pred(tags=["market:aligned", "side:NO"]))
        self.assertEqual(nc["label"], "WEAK")
        self.assertTrue(any("alignment" in r for r in nc["reasons"]))

    def test_alignment_not_required_when_disabled(self):
        mgr = make_manager(FocusSettings(no_confidence_require_alignment=False))
        nc = mgr._no_confidence(pred(tags=["side:NO"]))
        self.assertEqual(nc["label"], "STRONG")

    def test_none_for_yes_side(self):
        mgr = make_manager()
        self.assertIsNone(mgr._no_confidence(pred(side="YES")))

    def test_none_for_uncertain_side(self):
        mgr = make_manager()
        self.assertIsNone(mgr._no_confidence(pred(side="UNCERTAIN")))

    def test_none_when_disabled(self):
        mgr = make_manager(FocusSettings(no_confidence_enabled=False))
        self.assertIsNone(mgr._no_confidence(pred()))

    def test_none_for_non_mapping(self):
        mgr = make_manager()
        self.assertIsNone(mgr._no_confidence(None))

    def test_alert_includes_strong_line_for_no(self):
        mgr = make_manager()
        mgr._cycles["BTC-T"] = {"predictions": {"10m": pred()}}
        row = {"asset": "BTC", "side": "NO", "ticker": "BTC-T", "trade_plan": {}}
        msg = mgr._format_checkpoint_alert("10m", row)
        self.assertIn("NO conviction: <b>STRONG</b>", msg)

    def test_alert_weak_line_escapes_reason_text(self):
        mgr = make_manager()
        mgr._cycles["BTC-T"] = {"predictions": {"10m": pred(consensus=0.83)}}
        row = {"asset": "BTC", "side": "NO", "ticker": "BTC-T", "trade_plan": {}}
        msg = mgr._format_checkpoint_alert("10m", row)
        self.assertIn("NO conviction: <b>WEAK</b>", msg)
        # The reason "consensus 83% < 97%" must be HTML-escaped (< -> &lt;).
        self.assertIn("&lt;", msg)
        self.assertNotIn("% < ", msg)

    def test_alert_omits_line_for_yes(self):
        mgr = make_manager()
        mgr._cycles["BTC-T"] = {"predictions": {"10m": pred(side="YES")}}
        row = {"asset": "BTC", "side": "YES", "ticker": "BTC-T", "trade_plan": {}}
        msg = mgr._format_checkpoint_alert("10m", row)
        self.assertNotIn("NO conviction", msg)


if __name__ == "__main__":
    unittest.main()
