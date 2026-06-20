from __future__ import annotations

import unittest

from q15_upgrade.checkpoint_v93 import (
    analyse_flow,
    analyse_wick_sequence,
    decide_three_gate,
    extract_completed_candles,
)


class WickFlowValueTests(unittest.TestCase):
    def test_three_completed_candles_are_extracted(self) -> None:
        snapshot = {
            "completed_candles": [
                {"open": 100, "high": 102, "low": 98, "close": 99},
                {"open": 99, "high": 101, "low": 95, "close": 100.5},
                {"open": 100.4, "high": 103, "low": 99, "close": 102.5},
            ]
        }
        candles, source, basis = extract_completed_candles(snapshot)
        self.assertEqual(len(candles), 3)
        self.assertEqual(source, "completed_candles")
        self.assertEqual(basis, "source_declares_completed")

    def test_bullish_lower_wick_sequence_supports_yes(self) -> None:
        snapshot = {
            "completed_candles": [
                {"open": 100, "high": 102, "low": 98, "close": 99},
                {"open": 99, "high": 101, "low": 95, "close": 100.5},
                {"open": 100.4, "high": 103, "low": 99, "close": 102.5},
            ]
        }
        result = analyse_wick_sequence(snapshot, "YES")
        self.assertIn(result.status, {"SUPPORTIVE", "STRONG"})
        self.assertTrue(result.setup_pass)
        self.assertTrue(result.rejection_pass)
        self.assertTrue(result.follow_through_pass)
        self.assertFalse(result.opposite_wick_veto)

    def test_bearish_upper_wick_sequence_supports_no(self) -> None:
        snapshot = {
            "completed_candles": [
                {"open": 100, "high": 102, "low": 98, "close": 101},
                {"open": 101, "high": 105, "low": 99, "close": 99.5},
                {"open": 99.6, "high": 101, "low": 96, "close": 97},
            ]
        }
        result = analyse_wick_sequence(snapshot, "NO")
        self.assertIn(result.status, {"SUPPORTIVE", "STRONG"})
        self.assertTrue(result.setup_pass)
        self.assertTrue(result.rejection_pass)
        self.assertTrue(result.follow_through_pass)

    def test_strong_opposite_wick_veto_blocks_yes(self) -> None:
        snapshot = {
            "completed_candles": [
                {"open": 100, "high": 102, "low": 98, "close": 99},
                {"open": 99, "high": 101, "low": 95, "close": 100.5},
                {"open": 101, "high": 106, "low": 100, "close": 100.4},
            ]
        }
        result = analyse_wick_sequence(snapshot, "YES")
        self.assertTrue(result.opposite_wick_veto)
        self.assertEqual(result.status, "OPPOSING")

    def test_flow_requires_two_independent_supportive_groups(self) -> None:
        result = analyse_flow(
            {
                "executed_flow_skew": 0.40,
                "orderbook_persistence": 0.30,
                "spot_momentum": 0.0,
            },
            "YES",
        )
        self.assertEqual(result.status, "SUPPORTIVE")
        self.assertEqual(result.supportive_count, 2)
        self.assertEqual(result.available_count, 3)

    def test_flow_direction_is_side_aware_for_no(self) -> None:
        result = analyse_flow(
            {
                "executed_flow_skew": -0.40,
                "orderbook_persistence": -0.30,
                "spot_momentum": 0.0,
            },
            "NO",
        )
        self.assertEqual(result.status, "SUPPORTIVE")
        self.assertEqual(result.supportive_count, 2)

    def test_all_three_requirements_can_reach_ready(self) -> None:
        decision = decide_three_gate(
            side_valid=True,
            data_status="READY",
            strong_checkpoint_conflict=False,
            value_available=True,
            value_pass=True,
            wick_status="STRONG",
            flow_status="SUPPORTIVE",
        )
        self.assertEqual(decision, "READY")

    def test_positive_value_with_one_confirmation_stays_watch(self) -> None:
        decision = decide_three_gate(
            side_valid=True,
            data_status="READY",
            strong_checkpoint_conflict=False,
            value_available=True,
            value_pass=True,
            wick_status="SUPPORTIVE",
            flow_status="NEUTRAL",
        )
        self.assertEqual(decision, "WATCH")

    def test_negative_value_is_rejected_before_signal_quality(self) -> None:
        decision = decide_three_gate(
            side_valid=True,
            data_status="READY",
            strong_checkpoint_conflict=False,
            value_available=True,
            value_pass=False,
            wick_status="STRONG",
            flow_status="SUPPORTIVE",
        )
        self.assertEqual(decision, "PRICE_NOT_ATTRACTIVE")

    def test_strong_checkpoint_conflict_blocks_entry(self) -> None:
        decision = decide_three_gate(
            side_valid=True,
            data_status="READY",
            strong_checkpoint_conflict=True,
            value_available=True,
            value_pass=True,
            wick_status="STRONG",
            flow_status="SUPPORTIVE",
        )
        self.assertEqual(decision, "CHECKPOINT_CONFLICT")

    def test_stale_data_fails_closed(self) -> None:
        decision = decide_three_gate(
            side_valid=True,
            data_status="DATA_STALE",
            strong_checkpoint_conflict=False,
            value_available=True,
            value_pass=True,
            wick_status="STRONG",
            flow_status="SUPPORTIVE",
        )
        self.assertEqual(decision, "DATA_STALE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
