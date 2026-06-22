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
    _manipulation_score,
    _manipulation_divergence_threshold_bps,
    _collect_manipulation_tells,
    _panel_manipulation,
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


class TestManipulationScoreFormula(unittest.TestCase):
    """The 0..1 score = fraction-of-tells + absorption bonus, capped at 1.0."""

    def test_no_tells_scores_zero(self):
        self.assertEqual(_manipulation_score([]), 0.0)

    def test_pin_only_is_one_third(self):
        self.assertEqual(_manipulation_score(["PIN"]), round(1 / 3.0, 4))

    def test_absorption_only_adds_bonus(self):
        # 1/3 + 0.34 = 0.6733...
        self.assertEqual(_manipulation_score(["ABSORPTION"]), 0.6733)

    def test_two_non_absorption_tells(self):
        self.assertEqual(_manipulation_score(["PIN", "DIVERGENCE"]), 0.6667)

    def test_score_is_capped_at_one(self):
        # 3/3 + 0.34 = 1.34 -> capped to 1.0.
        self.assertEqual(_manipulation_score(["ABSORPTION", "PIN", "DIVERGENCE"]), 1.0)
        # ABSORPTION + PIN = 2/3 + 0.34 = 1.0067 -> capped to 1.0.
        self.assertEqual(_manipulation_score(["ABSORPTION", "PIN"]), 1.0)

    def test_score_deterministic_and_pure(self):
        # Same input -> same output, no dependence on call order or state.
        first = _manipulation_score(["DIVERGENCE"])
        _ = _manipulation_score(["ABSORPTION", "PIN"])
        self.assertEqual(_manipulation_score(["DIVERGENCE"]), first)


class TestDivergenceThreshold(unittest.TestCase):
    """Threshold boundary, Coinbase/OKX disagreement, and asset-specific config."""

    def tearDown(self):
        for k in list(os.environ):
            if k.startswith("Q15_V95_MANIPULATION_DIVERGENCE_BPS"):
                os.environ.pop(k, None)

    def test_default_band_is_35_bps(self):
        self.assertEqual(_manipulation_divergence_threshold_bps(None), 35.0)
        self.assertEqual(_manipulation_divergence_threshold_bps("BTC"), 35.0)

    def test_at_threshold_flags_just_below_does_not(self):
        # Exact boundary (>=) flags; one bps below does not.
        at = _manipulation_signal({"name": "NORMAL"}, {}, {"divergence_bps": 35.0})
        below = _manipulation_signal({"name": "NORMAL"}, {}, {"divergence_bps": 34.999})
        self.assertTrue(at["suspected"])
        self.assertFalse(below["suspected"])

    def test_coinbase_okx_disagreement_flags_divergence(self):
        # A wide cross-venue spread is the DIVERGENCE tell.
        sig = _manipulation_signal({"name": "NORMAL"}, {}, {"divergence_bps": 80.0})
        self.assertEqual(sig["reasons"], ["DIVERGENCE"])

    def test_per_asset_override_widens_band(self):
        os.environ["Q15_V95_MANIPULATION_DIVERGENCE_BPS_DOGE"] = "100"
        self.assertEqual(_manipulation_divergence_threshold_bps("DOGE"), 100.0)
        # BTC (no override) keeps the global default.
        self.assertEqual(_manipulation_divergence_threshold_bps("BTC"), 35.0)
        # 80 bps flags BTC but NOT DOGE (its band is 100).
        self.assertTrue(_manipulation_signal({"name": "NORMAL"}, {}, {"divergence_bps": 80}, asset="BTC")["suspected"])
        self.assertFalse(_manipulation_signal({"name": "NORMAL"}, {}, {"divergence_bps": 80}, asset="DOGE")["suspected"])

    def test_per_asset_override_is_case_insensitive(self):
        os.environ["Q15_V95_MANIPULATION_DIVERGENCE_BPS_DOGE"] = "100"
        self.assertEqual(_manipulation_divergence_threshold_bps("doge"), 100.0)

    def test_global_override_applies_when_no_asset_override(self):
        os.environ["Q15_V95_MANIPULATION_DIVERGENCE_BPS"] = "50"
        self.assertEqual(_manipulation_divergence_threshold_bps("ETH"), 50.0)


class TestManipulationEdgeCases(unittest.TestCase):
    """Missing/stale data, extreme moves, malformed inputs, duplicate calls."""

    def test_missing_price_data_is_clean(self):
        # No absorption, empty exchange, benign regime -> nothing flagged.
        sig = _manipulation_signal({"name": "NORMAL"}, {"available": False}, {})
        self.assertFalse(sig["suspected"])
        self.assertEqual(sig, {"suspected": False, "reasons": [], "lean": None, "score": 0.0})

    def test_none_inputs_do_not_crash(self):
        sig = _manipulation_signal(None, None, None)
        self.assertFalse(sig["suspected"])

    def test_non_numeric_divergence_treated_as_zero(self):
        # Stale/garbage feed value must not flag and must not raise.
        sig = _manipulation_signal({"name": "NORMAL"}, {}, {"divergence_bps": "n/a"})
        self.assertFalse(sig["suspected"])

    def test_negative_divergence_does_not_flag(self):
        sig = _manipulation_signal({"name": "NORMAL"}, {}, {"divergence_bps": -120.0})
        self.assertFalse(sig["suspected"])

    def test_extreme_divergence_caps_score(self):
        sig = _manipulation_signal({"name": "NORMAL"}, {}, {"divergence_bps": 1_000_000.0})
        self.assertTrue(sig["suspected"])
        self.assertEqual(sig["reasons"], ["DIVERGENCE"])
        self.assertLessEqual(sig["score"], 1.0)

    def test_absorbed_but_not_available_is_ignored(self):
        # absorbed without available must not flag (guards malformed details).
        sig = _manipulation_signal({"name": "NORMAL"}, {"available": False, "absorbed": True}, {})
        self.assertFalse(sig["suspected"])

    def test_duplicate_calls_are_stable(self):
        regime = {"name": "THRESHOLD_PIN"}
        absorb = _absorbed(0.6)
        exch = {"divergence_bps": 50}
        outputs = [_manipulation_signal(regime, absorb, exch) for _ in range(5)]
        self.assertTrue(all(o == outputs[0] for o in outputs))

    def test_input_mappings_not_mutated(self):
        regime = {"name": "THRESHOLD_PIN"}
        absorb = _absorbed(0.6)
        exch = {"divergence_bps": 50}
        snap = (dict(regime), dict(absorb), dict(exch))
        _manipulation_signal(regime, absorb, exch)
        self.assertEqual((regime, absorb, exch), snap)


class TestCollectTells(unittest.TestCase):
    """Tell ordering and the absorption directional lean."""

    def test_tells_returned_in_fixed_order(self):
        tells, lean = _collect_manipulation_tells(
            "THRESHOLD_PIN", _absorbed(0.6), {"divergence_bps": 99}, 35.0)
        self.assertEqual(tells, ["ABSORPTION", "PIN", "DIVERGENCE"])
        self.assertEqual(lean, "NO")

    def test_lean_only_from_absorption(self):
        tells, lean = _collect_manipulation_tells("THRESHOLD_PIN", {"available": False}, {}, 35.0)
        self.assertEqual(tells, ["PIN"])
        self.assertIsNone(lean)


class TestPanelUnitScaling(unittest.TestCase):
    """The panel risk number must be 0..100 whether it comes from flip-risk or
    the manipulation-score fallback (regression for the prior unit mismatch)."""

    def test_flip_risk_score_used_directly(self):
        analysis = {
            "flip_risk": {"score": 72.0, "primary_reason": "order-book imbalance"},
            "manipulation": {"suspected": True, "reasons": ["ABSORPTION"], "lean": "NO", "score": 0.67},
        }
        panel = _panel_manipulation(analysis)
        self.assertEqual(panel["risk"], 72.0)
        self.assertEqual(panel["level"], "HIGH")

    def test_manipulation_fallback_rescaled_to_0_100(self):
        # No flip-risk score -> fall back to manip score (0..1) rescaled to 0..100.
        analysis = {
            "manipulation": {"suspected": True, "reasons": ["ABSORPTION"], "lean": "NO", "score": 0.67},
        }
        panel = _panel_manipulation(analysis)
        self.assertEqual(panel["risk"], 67.0)        # not 0.67
        self.assertEqual(panel["level"], "HIGH")     # 67 >= 60

    def test_low_manipulation_fallback_level(self):
        analysis = {
            "manipulation": {"suspected": True, "reasons": ["PIN"], "lean": None, "score": 0.3333},
        }
        panel = _panel_manipulation(analysis)
        self.assertEqual(panel["risk"], round(0.3333 * 100.0, 4))
        self.assertEqual(panel["level"], "LOW")      # 33.33 < 35

    def test_nothing_flagged_returns_none(self):
        analysis = {"manipulation": {"suspected": False, "reasons": [], "lean": None, "score": 0.0}}
        self.assertIsNone(_panel_manipulation(analysis))


class TestTrackingDisabledScore(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("Q15_V95_MANIPULATION_TRACKING", None)

    def test_disabled_returns_clean_zero_score(self):
        os.environ["Q15_V95_MANIPULATION_TRACKING"] = "0"
        sig = _manipulation_signal({"name": "THRESHOLD_PIN"}, _absorbed(0.6), {"divergence_bps": 99})
        self.assertEqual(sig, {"suspected": False, "reasons": [], "lean": None, "score": 0.0})


class TestPinTellTightening(unittest.TestCase):
    """The optional, default-OFF stricter distance band for the PIN tell.

    The THRESHOLD_PIN regime (frozen champion) is untouched; only the read-only
    PIN tell may be narrowed, and only when the operator opts in.
    """

    def tearDown(self):
        os.environ.pop("Q15_V95_MANIPULATION_PIN_MAX_DISTANCE_SIGMA", None)

    def test_default_is_byte_identical(self):
        # No knob set: PIN fires for THRESHOLD_PIN regardless of distance_sigma.
        sig = _manipulation_signal(
            {"name": "THRESHOLD_PIN", "distance_sigma": 0.24}, {"available": False}, {})
        self.assertIn("PIN", sig["reasons"])

    def test_knob_suppresses_pin_outside_band(self):
        os.environ["Q15_V95_MANIPULATION_PIN_MAX_DISTANCE_SIGMA"] = "0.15"
        # distance_sigma above the stricter band -> PIN tell suppressed (regime
        # still THRESHOLD_PIN, but the observational flag does not fire).
        sig = _manipulation_signal(
            {"name": "THRESHOLD_PIN", "distance_sigma": 0.22}, {"available": False}, {})
        self.assertNotIn("PIN", sig["reasons"])
        self.assertFalse(sig["suspected"])

    def test_knob_keeps_pin_inside_band(self):
        os.environ["Q15_V95_MANIPULATION_PIN_MAX_DISTANCE_SIGMA"] = "0.15"
        sig = _manipulation_signal(
            {"name": "THRESHOLD_PIN", "distance_sigma": 0.05}, {"available": False}, {})
        self.assertIn("PIN", sig["reasons"])

    def test_missing_measurement_keeps_pin(self):
        # Knob set but no distance_sigma available: never silently drop the tell.
        os.environ["Q15_V95_MANIPULATION_PIN_MAX_DISTANCE_SIGMA"] = "0.15"
        sig = _manipulation_signal({"name": "THRESHOLD_PIN"}, {"available": False}, {})
        self.assertIn("PIN", sig["reasons"])

    def test_invalid_knob_is_ignored(self):
        os.environ["Q15_V95_MANIPULATION_PIN_MAX_DISTANCE_SIGMA"] = "not-a-number"
        sig = _manipulation_signal(
            {"name": "THRESHOLD_PIN", "distance_sigma": 0.9}, {"available": False}, {})
        self.assertIn("PIN", sig["reasons"])  # unparseable -> no tightening


class TestManipulationScoreboardDimensions(unittest.TestCase):
    """The added by_checkpoint / by_side / by_reason_isolated breakdowns."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.led = V95Ledger(os.path.join(self.tmp.name, "l.sqlite3"))

    def tearDown(self):
        self.tmp.cleanup()

    def _rec(self, tkr, checkpoint, side, result, manip, reason):
        self.led.record_prediction(
            ticker=tkr, asset="BTC", checkpoint=checkpoint, created_at=1000.0, close_time=2000.0,
            predicted_side=side, raw_yes_probability=0.6, calibrated_yes_probability=0.6,
            challenger_yes_probability=0.6, baseline_yes_probability=0.6, selected_probability=0.65,
            conservative_probability=0.6, data_quality=0.7, evidence_quality=0.7, trade_quality=0.6,
            trade_decision="WATCH_PRICE", regime="NORMAL", features={}, contributions={},
            quote={"ask_cents": 50}, rank=1, costs={"total_cents": 2}, confidence_grade="B",
            manipulation_suspected=manip, manipulation_reason=reason)
        self.led.resolve_ticker(tkr, result, 2100.0)

    def test_isolated_reason_excludes_combined_rows(self):
        # ABSORPTION-only (right) vs ABSORPTION+PIN combined (wrong): the isolated
        # bucket must hold only the single-tell row, while by_reason holds both.
        self._rec("AA", "10M", "YES", "YES", True, "ABSORPTION")
        self._rec("BB", "10M", "YES", "NO", True, "PIN,ABSORPTION")
        bm = self.led.scoreboard()["by_manipulation"]
        self.assertEqual(bm["by_reason"]["ABSORPTION"]["n"], 2)        # both rows
        self.assertEqual(bm["by_reason_isolated"]["ABSORPTION"]["n"], 1)  # only AA
        self.assertEqual(bm["by_reason_isolated"]["ABSORPTION"]["right"], 1)
        # The combined row is not isolated under PIN either.
        self.assertNotIn("PIN", bm["by_reason_isolated"])

    def test_by_checkpoint_and_by_side_split(self):
        self._rec("A1", "7M", "NO", "NO", True, "PIN")    # 7M, NO, suspected, right
        self._rec("A2", "7M", "NO", "YES", False, None)   # 7M, NO, clean, wrong
        self._rec("A3", "15M", "YES", "YES", True, "PIN") # 15M, YES, suspected, right
        bm = self.led.scoreboard()["by_manipulation"]
        self.assertEqual(bm["by_checkpoint"]["7M"]["suspected"]["n"], 1)
        self.assertEqual(bm["by_checkpoint"]["7M"]["clean"]["n"], 1)
        self.assertEqual(bm["by_checkpoint"]["15M"]["suspected"]["right"], 1)
        self.assertEqual(bm["by_side"]["NO"]["suspected"]["n"], 1)
        self.assertEqual(bm["by_side"]["YES"]["suspected"]["n"], 1)
        self.assertEqual(bm["by_side"]["NO"]["clean"]["n"], 1)

    def test_by_reason_side_crosses_tell_and_side(self):
        # ABSORPTION-confirmed NO (the hypothesised signal) vs bare-PIN YES (noise).
        self._rec("B1", "10M", "NO", "NO", True, "ABSORPTION")        # absorption, NO, right
        self._rec("B2", "10M", "NO", "NO", True, "ABSORPTION,PIN")    # absorption, NO, right
        self._rec("B3", "10M", "YES", "NO", True, "PIN")             # pin_only, YES, wrong
        bm = self.led.scoreboard()["by_manipulation"]["by_reason_side"]
        # absorption bucket = any row carrying ABSORPTION (B1, B2), both NO + right
        self.assertEqual(bm["absorption"]["NO"]["n"], 2)
        self.assertEqual(bm["absorption"]["NO"]["right"], 2)
        self.assertEqual(bm["absorption"]["YES"]["n"], 0)
        # pin_only bucket = rows whose ONLY tell is PIN (B3); the PIN in B2 is excluded
        self.assertEqual(bm["pin_only"]["YES"]["n"], 1)
        self.assertEqual(bm["pin_only"]["YES"]["right"], 0)
        self.assertEqual(bm["pin_only"]["NO"]["n"], 0)

    def test_persistence_counts_only_earlier_checkpoints(self):
        # Contract P flagged at 15M then 10M: the 10M flag persisted (prior 15M flag).
        self._rec("P", "15M", "NO", "NO", True, "PIN")   # first checkpoint -> fresh
        self._rec("P", "10M", "NO", "NO", True, "PIN")   # prior 15M flagged -> persistent
        # Contract Q flagged only at 7M: no earlier flag -> fresh (no look-ahead to
        # later checkpoints, and there are no earlier ones).
        self._rec("Q", "7M", "YES", "NO", True, "PIN")
        bp = self.led.scoreboard()["by_manipulation"]["by_persistence"]
        self.assertEqual(bp["persistent_flag"]["n"], 1)  # only P@10M
        self.assertEqual(bp["fresh_flag"]["n"], 2)       # P@15M and Q@7M
        # Interval-controlled view (the honest one): P@10M is persistent within 10M;
        # P@15M is fresh within 15M; Q@7M is fresh within 7M.
        self.assertEqual(bp["by_checkpoint"]["10M"]["persistent"]["n"], 1)
        self.assertEqual(bp["by_checkpoint"]["10M"]["fresh"]["n"], 0)
        self.assertEqual(bp["by_checkpoint"]["15M"]["fresh"]["n"], 1)
        self.assertEqual(bp["by_checkpoint"]["7M"]["fresh"]["n"], 1)


if __name__ == "__main__":
    unittest.main()
