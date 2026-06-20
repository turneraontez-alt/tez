from __future__ import annotations

import os
import unittest

from q15_upgrade.calibrated_edge import CalibratedEdgeEngine, augment_telegram_message

# NOTE: This module's public contract was replaced by the Q15 V9 edge-proof
# calibrated_edge engine. Assertions below track the V9 contract (status names,
# blocker names, and the "V9 TRADE QUALITY" telegram section). The detailed V9
# economics/outbox behavior is covered in test_q15_v9.py.

V9_STATUSES = {
    "READY",
    "PRICE_NOT_ATTRACTIVE",
    "DIRECTION_UNCERTAIN",
    "INSUFFICIENT_EVIDENCE",
    "INSUFFICIENT_DIRECTIONAL_EVIDENCE",
    "NO_TRADE",
}


class FakeLearner:
    def summary(self):
        return {"metrics": {"effective_n": 100, "brier_score": 0.19, "log_loss": 0.61}}


def base_snapshot():
    return {
        "asset": "ETH",
        "ticker": "ETH-TEST",
        "market_state": "live",
        "seconds_remaining": 590,
        "entry_side": "YES",
        "q15_prediction_agreement": "SAME",
        "q15_final_result": "YES",
        "q15_new_entry_allowed": True,
        "estimated_fair_probability": 0.80,
        "end_prediction_yes_probability": 0.76,
        "yes_bid": 66.0,
        "yes_ask": 67.0,
        "no_bid": 33.0,
        "no_ask": 34.0,
        "spread": 1.0,
        "yes_bid_levels": [[66, 20], [65, 20]],
        "yes_ask_levels": [[67, 20], [68, 20]],
        "taker_yes_volume_15s": 12,
        "taker_no_volume_15s": 4,
        "confirmation_groups": ["price_action", "executed_flow", "order_book"],
        "required_edge": 4.0,
        "model_confidence": 0.95,
        "decision_state": "ENTRY CANDIDATE",
    }


class CalibratedEdgeTests(unittest.TestCase):
    def setUp(self):
        os.environ["Q15_CALIBRATION_MIN_EFFECTIVE_N"] = "50"
        os.environ["Q15_CONSERVATIVE_MIN_EDGE_CENTS"] = "4"
        os.environ["Q15_MIN_EXECUTABLE_DEPTH"] = "5"
        self.engine = CalibratedEdgeEngine(learner=FakeLearner())

    def test_conservative_edge_is_lower_than_point_edge(self):
        snap = base_snapshot()
        out = None
        for now in (100.0, 102.0, 104.0):
            out = self.engine.enrich_all({"ETH": snap}, now, {})["ETH"]
        self.assertLess(out["conservative_edge_after_costs_cents"], out["point_edge_after_costs_cents"])
        self.assertIn(out["calibrated_edge_status"], V9_STATUSES)

    def test_overpriced_contract_is_not_ready(self):
        snap = base_snapshot()
        snap["yes_bid"] = 84.0
        snap["yes_ask"] = 85.0
        out = self.engine.enrich_all({"ETH": snap}, 100.0, {})["ETH"]
        self.assertFalse(out["calibrated_edge_new_entry_allowed"])
        self.assertLess(out["conservative_edge_after_costs_cents"], 0)

    def test_prediction_conflict_blocks_new_entry(self):
        snap = base_snapshot()
        snap["q15_prediction_agreement"] = "CONFLICT"
        snap["q15_final_result"] = "UNCERTAIN"
        out = self.engine.enrich_all({"ETH": snap}, 100.0, {})["ETH"]
        self.assertFalse(out["calibrated_edge_new_entry_allowed"])
        self.assertIn("prediction_conflict", out["calibrated_edge_blockers"])

    def test_existing_confirmed_position_is_not_invalidated_by_edge_flicker(self):
        snap = base_snapshot()
        snap.update({"decision_state": "ENTRY CONFIRMED", "entry_age_seconds": 15, "yes_bid": 84, "yes_ask": 85})
        out = self.engine.enrich_all({"ETH": snap}, 100.0, {})["ETH"]
        # V9 keeps the held position's state intact on a transient overpriced flicker
        # (it does not tear the confirmed entry down), even though a fresh entry at the
        # flickered price is no longer justified.
        self.assertEqual(out["decision_state"], "ENTRY CONFIRMED")
        self.assertIn("calibrated_edge_entry_allowed", out)

    def test_preview_is_available_to_same_cycle_telegram(self):
        snap = base_snapshot()
        self.engine.preview_all({"ETH": snap}, 99.0, {})
        message = augment_telegram_message("🛑 15M EARLY CHECK — ETH YES")
        self.assertIn("V9 TRADE QUALITY", message)

    def test_alert_augmentation_adds_trade_quality(self):
        snap = base_snapshot()
        self.engine.enrich_all({"ETH": snap}, 100.0, {})
        message = augment_telegram_message(
            "🛑 10M FINAL CHECK — NO TRADE\n1. ETH YES\nRejected: waiting_for_next_candle_follow_through, edge_below_required_8.0c"
        )
        self.assertIn("V9 TRADE QUALITY", message)
        self.assertIn("Main blocker:", message)
        self.assertIn("Read-only monitor", message)

    def test_augmented_section_is_safe_for_telegram_html(self):
        import q15_upgrade.calibrated_edge as ce
        record = {
            "asset": "ETH",
            "side": "YES",
            "status": "NO_TRADE <x>",
            "blockers_human": ["spread & depth > limit", "edge < required"],
            "management": "HOLD & WAIT",
            "model_disagreement": "high <flag>",
            "calibration": {"calibration_status": "warming & <n>", "effective_n": 12},
            "microstructure": {},
            "ideal_entry_zone_cents": [10.0, 20.0],
        }
        with ce._LATEST_LOCK:
            ce._LATEST_BY_ASSET["ETH"] = record
        message = augment_telegram_message("🛑 10M FINAL CHECK — NO TRADE\n1. ETH YES")
        self.assertIn("V9 TRADE QUALITY", message)
        # Intended markup preserved
        self.assertIn("<b>", message)
        # No raw special operators leak into the appended section that could break parsing
        section = message.split("V9 TRADE QUALITY", 1)[1]
        self.assertNotIn(" & ", section)
        self.assertNotIn(" > ", section)
        self.assertNotIn("< ", section)


if __name__ == "__main__":
    unittest.main()
