"""Suspected price-manipulation tracking (read-only).

Covers the composite "big spenders pushing the price" signal built from the
engine's existing tells (strike pin / order-wall absorption / cross-exchange
divergence), its recording on the ledger, the suspected-vs-clean scoreboard
breakdown, the live alert tag, and the snapshot stamping. The signal NEVER
changes the prediction — these assert it is observational only.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.checkpoint_v95 import (
    _manipulation_signal,
    _manipulation_phrase,
    apply_v95_policy,
    build_v95_message,
)
from q15_upgrade.ledger_v95 import V95Ledger


def _absorbed(flow):
    return {"available": True, "absorbed": True, "flow": flow, "momentum": 0.0}


class TestManipulationSignal(unittest.TestCase):
    def tearDown(self):
        for k in ("Q15_V95_MANIPULATION_TRACKING", "Q15_V95_MANIPULATION_MIN_SIGNALS",
                  "Q15_V95_MANIPULATION_DIVERGENCE_BPS"):
            os.environ.pop(k, None)

    def test_absorption_flags_with_directional_lean(self):
        # Positive aggressive flow eaten without price moving -> leans NO (bearish).
        sig = _manipulation_signal({"name": "NORMAL"}, _absorbed(0.6), {})
        self.assertTrue(sig["suspected"])
        self.assertEqual(sig["reasons"], ["ABSORPTION"])
        self.assertEqual(sig["lean"], "NO")
        # Negative flow failing to push price down leans YES.
        sig_y = _manipulation_signal({"name": "NORMAL"}, _absorbed(-0.6), {})
        self.assertEqual(sig_y["lean"], "YES")

    def test_pin_regime_flags(self):
        sig = _manipulation_signal({"name": "THRESHOLD_PIN"}, {"available": False}, {})
        self.assertTrue(sig["suspected"])
        self.assertIn("PIN", sig["reasons"])
        self.assertIsNone(sig["lean"])  # pin is non-directional

    def test_divergence_flags_by_regime_or_bps(self):
        self.assertTrue(_manipulation_signal({"name": "EXCHANGE_DIVERGENCE"}, {}, {})["suspected"])
        self.assertTrue(_manipulation_signal({"name": "NORMAL"}, {}, {"divergence_bps": 50})["suspected"])
        self.assertFalse(_manipulation_signal({"name": "NORMAL"}, {}, {"divergence_bps": 10})["suspected"])

    def test_clean_regime_not_flagged(self):
        sig = _manipulation_signal({"name": "TREND"}, {"available": False}, {"divergence_bps": 5})
        self.assertFalse(sig["suspected"])
        self.assertEqual(sig["reasons"], [])
        self.assertEqual(sig["score"], 0.0)

    def test_min_signals_gate_requires_agreement(self):
        os.environ["Q15_V95_MANIPULATION_MIN_SIGNALS"] = "2"
        one = _manipulation_signal({"name": "THRESHOLD_PIN"}, {"available": False}, {})
        self.assertFalse(one["suspected"])  # only PIN -> needs 2
        two = _manipulation_signal({"name": "THRESHOLD_PIN"}, _absorbed(0.6), {})
        self.assertTrue(two["suspected"])  # PIN + ABSORPTION

    def test_tracking_can_be_disabled(self):
        os.environ["Q15_V95_MANIPULATION_TRACKING"] = "false"
        sig = _manipulation_signal({"name": "THRESHOLD_PIN"}, _absorbed(0.6), {"divergence_bps": 99})
        self.assertFalse(sig["suspected"])
        self.assertEqual(sig["reasons"], [])

    def test_phrase_is_human_readable(self):
        sig = _manipulation_signal({"name": "THRESHOLD_PIN"}, _absorbed(0.6), {})
        phrase = _manipulation_phrase(sig)
        self.assertIn("absorption", phrase)
        self.assertIn("pin", phrase)
        self.assertIn("NO", phrase)  # the lean


class TestSnapshotStamping(unittest.TestCase):
    def test_apply_policy_stamps_manipulation_fields(self):
        analysis = {
            "prediction_available": True, "prediction_side": "YES",
            "regime": {"name": "THRESHOLD_PIN"},
            "manipulation": {"suspected": True, "reasons": ["PIN", "ABSORPTION"], "lean": "NO", "score": 0.6},
        }
        snap = apply_v95_policy({}, analysis)
        self.assertTrue(snap["q15_v9_5_manipulation_suspected"])
        self.assertEqual(snap["q15_v9_5_manipulation_reason"], "PIN,ABSORPTION")
        self.assertEqual(snap["q15_v9_5_manipulation_lean"], "NO")

    def test_clean_stamps_false_none(self):
        analysis = {"prediction_available": True, "prediction_side": "YES",
                    "manipulation": {"suspected": False, "reasons": [], "lean": None, "score": 0.0}}
        snap = apply_v95_policy({}, analysis)
        self.assertFalse(snap["q15_v9_5_manipulation_suspected"])
        self.assertIsNone(snap["q15_v9_5_manipulation_reason"])


class TestAlertTag(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("Q15_V95_MANIPULATION_ALERT_TAG", None)

    def _analyses(self, manip):
        return {"BTC": {
            "prediction_available": True, "prediction_side": "YES", "selected_probability": 0.66,
            "yes_probability": 0.66, "no_probability": 0.34, "confidence_grade": "B",
            "net_edge_cents": 3.0, "entry_allowed": False, "required_edge_cents": 6.0,
            "market_implied_yes_probability": 0.6, "quote": {"ask_cents": 55}, "manipulation": manip,
        }}

    def test_watch_block_present_when_suspected(self):
        manip = {"suspected": True, "reasons": ["ABSORPTION"], "lean": "NO", "score": 0.67}
        msg = build_v95_message("10M", self._analyses(manip), [{"asset": "BTC", "ticker": "T"}], {})
        self.assertIn("Manipulation watch", msg)
        self.assertIn("may flip → NO", msg)
        # Invariants preserved.
        self.assertIn("V9.5 CHECK", msg)
        self.assertIn("NO ENTRY YET", msg)

    def test_no_block_when_clean(self):
        manip = {"suspected": False, "reasons": [], "lean": None, "score": 0.0}
        msg = build_v95_message("10M", self._analyses(manip), [{"asset": "BTC", "ticker": "T"}], {})
        self.assertNotIn("Manipulation watch", msg)

    def test_tag_can_be_disabled(self):
        os.environ["Q15_V95_MANIPULATION_ALERT_TAG"] = "false"
        manip = {"suspected": True, "reasons": ["PIN"], "lean": None, "score": 0.33}
        msg = build_v95_message("10M", self._analyses(manip), [{"asset": "BTC", "ticker": "T"}], {})
        self.assertNotIn("Manipulation watch", msg)


class TestScoreboardBreakdown(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.led = V95Ledger(os.path.join(self.tmp.name, "l.sqlite3"))

    def tearDown(self):
        self.tmp.cleanup()

    def _rec(self, tkr, side, result, manip, reason):
        self.led.record_prediction(
            ticker=tkr, asset="BTC", checkpoint="10M", created_at=1000.0, close_time=2000.0,
            predicted_side=side, raw_yes_probability=0.6, calibrated_yes_probability=0.6,
            challenger_yes_probability=0.6, baseline_yes_probability=0.6, selected_probability=0.65,
            conservative_probability=0.6, data_quality=0.7, evidence_quality=0.7, trade_quality=0.6,
            trade_decision="WATCH_PRICE", regime="NORMAL", features={}, contributions={},
            quote={"ask_cents": 50}, rank=1, costs={"total_cents": 2}, confidence_grade="B",
            manipulation_suspected=manip, manipulation_reason=reason)
        self.led.resolve_ticker(tkr, result, 2100.0)

    def test_suspected_vs_clean_and_by_reason(self):
        self._rec("AA", "YES", "NO", True, "ABSORPTION")          # suspected, wrong
        self._rec("BB", "YES", "YES", True, "PIN,ABSORPTION")     # suspected, right (both tells)
        self._rec("CC", "YES", "YES", False, None)               # clean, right
        self._rec("DD", "NO", "NO", False, None)                 # clean, right
        bm = self.led.scoreboard()["by_manipulation"]
        self.assertEqual((bm["suspected"]["right"], bm["suspected"]["wrong"]), (1, 1))
        self.assertEqual((bm["clean"]["right"], bm["clean"]["wrong"]), (2, 0))
        # A row with two reasons counts under each tell.
        self.assertEqual(bm["by_reason"]["ABSORPTION"]["n"], 2)
        self.assertEqual(bm["by_reason"]["PIN"]["n"], 1)


if __name__ == "__main__":
    unittest.main()
