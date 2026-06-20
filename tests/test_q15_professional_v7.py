from __future__ import annotations

import os
import tempfile
import unittest

from q15_upgrade.professional_v7 import (
    ProfessionalV7Engine,
    normalize_cents,
    normalize_probability,
    professionalize_telegram_message,
)


def base_snapshot(side="YES", ready=False):
    yes_bid, yes_ask = (60.0, 61.0) if side == "YES" else (39.0, 40.0)
    no_bid, no_ask = (39.0, 40.0) if side == "YES" else (60.0, 61.0)
    return {
        "asset": "ETH",
        "ticker": f"ETH-{side}-TEST",
        "market_state": "live",
        "seconds_remaining": 590,
        "underlying_current": 3500.0,
        "q15_prediction_agreement": "SAME",
        "q15_final_result": side,
        "q15_new_entry_allowed": True,
        "calibrated_edge_side": side,
        "raw_side_probability": 0.82,
        "conservative_side_probability": 0.76,
        "market_implied_side_probability": 0.61,
        "conservative_edge_after_costs_cents": 13.0,
        "point_edge_after_costs_cents": 18.0,
        "calibrated_max_entry_price": 70.0,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "spread": 1.0,
        "orderbook_microstructure": {
            "executable_depth": 20.0,
            "book_persistent": True,
            "flow_skew_side": 0.4,
        },
        "independent_group_count": 3,
        "model_disagreement": "LOW",
        "v5_data_valid": True,
        "decision_state": "ENTRY RECOMMENDED" if ready else "WATCH",
        "calibrated_edge_new_entry_allowed": ready,
        "entry_blockers": [] if ready else ["calibrated_edge_gate"],
        "trade_quality": {"required_edge_cents": 4.0},
    }


class V7Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["Q15_V7_DIAGNOSTIC_JOURNAL_PATH"] = os.path.join(self.tmp.name, "events.jsonl")
        os.environ["Q15_V7_ZERO_TRADE_MIN_DECISIONS"] = "2"
        os.environ["Q15_V7_COMPACT_EMPTY_HOURLY"] = "true"
        self.engine = ProfessionalV7Engine()

    def tearDown(self):
        self.tmp.cleanup()

    def test_units(self):
        self.assertEqual(normalize_cents(0.73), 73.0)
        self.assertEqual(normalize_cents(73), 73.0)
        self.assertAlmostEqual(normalize_probability(4.7), 0.047)
        self.assertEqual(normalize_probability(0.73), 0.73)
        self.assertIsNone(normalize_probability(120))

    def test_synthetic_liveness(self):
        result = self.engine.run_liveness_self_test()
        self.assertTrue(result["all_passed"])
        self.assertTrue(result["yes_case"]["passed"])
        self.assertTrue(result["no_case"]["passed"])
        self.assertTrue(result["negative_edge_case"]["passed"])

    def test_unique_decisions_not_raw_snapshots(self):
        snap = base_snapshot()
        for now in (100.0, 101.0, 102.0):
            self.engine.observe_all({"ETH": snap}, now, {})
        summary = self.engine.summary()
        self.assertEqual(summary["raw_cycles"], 3)
        self.assertEqual(summary["raw_asset_snapshots"], 3)
        self.assertEqual(summary["unique_decisions"], 1)

    def test_yes_and_no_shadow_cases(self):
        yes = base_snapshot("YES")
        no = base_snapshot("NO")
        self.engine.observe_all({"ETH": yes, "BTC": dict(no, asset="BTC", ticker="BTC-NO-TEST")}, 100.0, {})
        summary = self.engine.summary()
        self.assertEqual(summary["shadow_entries"], 2)
        self.assertEqual(summary["production_entries"], 0)
        self.assertEqual(summary["liveness"]["status"], "PRODUCTION_OVERCONSTRAINED_OR_BLOCKED")

    def test_negative_edge_is_rejected(self):
        snap = base_snapshot()
        snap["conservative_edge_after_costs_cents"] = -1.0
        self.engine.observe_all({"ETH": snap}, 100.0, {})
        row = self.engine.summary()["latest_by_asset"]["ETH"]
        self.assertFalse(row["shadow"]["ready"])
        self.assertTrue(any("edge_below_required" in x for x in row["shadow"]["blockers"]))

    def test_state_transition_audit(self):
        snap = base_snapshot()
        snap["decision_state"] = "WATCH"
        self.engine.observe_all({"ETH": snap}, 100.0, {})
        snap2 = dict(snap)
        snap2["decision_state"] = "ENTRY RECOMMENDED"
        snap2["calibrated_edge_new_entry_allowed"] = True
        self.engine.observe_all({"ETH": snap2}, 101.0, {})
        summary = self.engine.summary()
        self.assertTrue(summary["invalid_state_transitions"])
        self.assertEqual(summary["invalid_state_transitions"][-1]["from"], "WATCH")
        self.assertEqual(summary["invalid_state_transitions"][-1]["to"], "ENTRY_RECOMMENDED")

    def test_hourly_report_is_corrected_and_compacted(self):
        self.engine.observe_all({"ETH": base_snapshot()}, 100.0, {})
        text = """📊 Hourly Report — 02:00 UTC
Overall: 7W/7L — 50% (n=14)
Last hour: 0W/0L — n/a (n=0)
Realized: +3.7¢ avg, +52.0¢ total

Factors associated with higher loss risk:
• executable ask: coefficient -0.347
• predicted edge: coefficient +0.183

Learning adjustments active:
• global +0.5pp [SHADOW SUPPRESS]
"""
        out = professionalize_telegram_message(text)
        self.assertIn("Hourly operational status", out)
        self.assertIn("No new resolved contracts", out)
        self.assertIn("Production entries", out)
        self.assertNotIn("Learning adjustments active", out)

    def test_full_hourly_report_labels_coefficients(self):
        os.environ["Q15_V7_COMPACT_EMPTY_HOURLY"] = "false"
        text = """📊 Hourly Report
Last hour: 1W/0L — 100% (n=1)
Factors associated with higher loss risk:
• executable ask: coefficient -0.347
• predicted edge: coefficient +0.183
Learning adjustments active:
• global +0.5pp [SHADOW SUPPRESS]
"""
        out = professionalize_telegram_message(text)
        self.assertIn("Outcome associations", out)
        self.assertIn("lower observed loss risk", out)
        self.assertIn("higher observed loss risk", out)
        self.assertIn("Shadow adjustments under evaluation", out)
        self.assertIn("production unchanged", out)

    def test_production_decision_is_never_overridden(self):
        snap = base_snapshot()
        original_state = snap["decision_state"]
        original_allowed = snap["calibrated_edge_new_entry_allowed"]
        out = self.engine.observe_all({"ETH": snap}, 100.0, {})["ETH"]
        self.assertEqual(out["decision_state"], original_state)
        self.assertEqual(out["calibrated_edge_new_entry_allowed"], original_allowed)
        self.assertTrue(out["q15_v7_shadow_ready"])


if __name__ == "__main__":
    unittest.main()
