from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from q15_upgrade.checkpoint_v91 import _LATEST, _LATEST_LOCK
from q15_upgrade.checkpoint_v92 import (
    CheckpointPolicyV92,
    _format_checkpoint,
    _format_hourly,
    _rolling_state,
)


def rolling(yes: int, no: int, neutral: int = 0) -> dict:
    directional = yes + no
    total = yes + no + neutral
    selected = "YES" if yes > no else "NO" if no > yes else "UNCERTAIN"
    return {
        "yes_votes": yes,
        "no_votes": no,
        "neutral_votes": neutral,
        "directional_votes": directional,
        "total_possible_votes": total,
        "directional_agreement": max(yes, no) / directional if directional else 0.0,
        "evidence_coverage": directional / 5.0,
        "selected_side": selected,
    }


class V92PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.policy = CheckpointPolicyV92(
            store=None,
            sqlite_path=Path(self.temp.name) / "state.sqlite3",
        )

    def tearDown(self) -> None:
        with _LATEST_LOCK:
            _LATEST.clear()
        self.temp.cleanup()

    def test_two_of_five_is_watch_not_ready(self) -> None:
        state = _rolling_state(
            rolling(2, 0, 0),
            watch_min_votes=2,
            ready_min_votes=4,
            minimum_agreement=0.72,
        )
        self.assertEqual(state, "WATCH")

    def test_four_of_five_is_direction_valid(self) -> None:
        state = _rolling_state(
            rolling(4, 0, 0),
            watch_min_votes=2,
            ready_min_votes=4,
            minimum_agreement=0.72,
        )
        self.assertEqual(state, "DIRECTION_VALID")

    def test_missing_15m_is_penalty_not_veto(self) -> None:
        relation = self.policy._relationship(
            None,
            current_side="YES",
            current_p_yes=0.80,
            current_evidence_state="DIRECTION_VALID",
        )
        self.assertEqual(relation.relation, "MISSING_15M")
        self.assertFalse(relation.blocks_entry)
        self.assertGreater(relation.penalty_cents, 0.0)

    def test_weak_conflict_is_penalty_not_veto(self) -> None:
        relation = self.policy._relationship(
            {
                "side": "NO",
                "p_yes": 0.40,
                "rolling": rolling(0, 4, 0),
            },
            current_side="YES",
            current_p_yes=0.80,
            current_evidence_state="DIRECTION_VALID",
        )
        self.assertEqual(relation.relation, "WEAK_CONFLICT")
        self.assertFalse(relation.blocks_entry)
        self.assertGreater(relation.penalty_cents, 0.0)

    def test_strong_conflict_blocks(self) -> None:
        relation = self.policy._relationship(
            {
                "side": "NO",
                "p_yes": 0.20,
                "rolling": rolling(0, 4, 0),
            },
            current_side="YES",
            current_p_yes=0.80,
            current_evidence_state="DIRECTION_VALID",
        )
        self.assertEqual(relation.relation, "STRONG_CONFLICT")
        self.assertTrue(relation.blocks_entry)

    def test_missing_prior_can_still_reach_ready(self) -> None:
        snapshots = {
            "BTC": {
                "ticker": "BTC-TEST",
                "seconds_remaining": 600,
                "calibrated_edge_side": "YES",
                "calibrated_edge_status": "READY",
                "q15_v91_frozen_side": "YES",
                "q15_v91_checkpoint": "10M",
                "q15_v91_contract_key": "BTC-TEST",
                "q15_v92_evidence_state": "DIRECTION_VALID",
                "q15_v92_checkpoint_relation": "MISSING_15M",
                "q15_v92_sequence_penalty_cents": 2.0,
                "q15_v92_sequence_blocks_entry": False,
                "canonical_economics": {
                    "conservative_net_edge_cents": 12.0,
                },
                "trade_quality": {"required_edge_cents": 8.0},
                "v9_calibrated_p_yes": 0.80,
            }
        }
        result = self.policy.finalize_all(snapshots, 1000.0)["BTC"]
        self.assertEqual(result["q15_v92_decision"], "READY")
        self.assertTrue(result["q15_v92_entry_allowed"])
        self.assertEqual(result["q15_v92_adjusted_required_edge_cents"], 10.0)

    def test_strong_conflict_rejects_even_with_edge(self) -> None:
        snapshots = {
            "ETH": {
                "ticker": "ETH-TEST",
                "seconds_remaining": 600,
                "calibrated_edge_side": "YES",
                "calibrated_edge_status": "READY",
                "q15_v91_frozen_side": "YES",
                "q15_v91_checkpoint": "10M",
                "q15_v91_contract_key": "ETH-TEST",
                "q15_v92_evidence_state": "DIRECTION_VALID",
                "q15_v92_checkpoint_relation": "STRONG_CONFLICT",
                "q15_v92_sequence_penalty_cents": 0.0,
                "q15_v92_sequence_blocks_entry": True,
                "canonical_economics": {
                    "conservative_net_edge_cents": 20.0,
                },
                "trade_quality": {"required_edge_cents": 8.0},
                "v9_calibrated_p_yes": 0.80,
            }
        }
        result = self.policy.finalize_all(snapshots, 1000.0)["ETH"]
        self.assertEqual(result["q15_v92_decision"], "CHECKPOINT_CONFLICT")
        self.assertFalse(result["q15_v92_entry_allowed"])

    def test_15m_does_not_require_10m(self) -> None:
        result = self.policy.pre_enrich_all(
            {
                "BTC": {
                    "ticker": "BTC-15M",
                    "seconds_remaining": 900,
                    "v9_calibrated_p_yes": 0.82,
                }
            },
            1200.0,
        )["BTC"]
        self.assertEqual(result["q15_v92_checkpoint_relation"], "EARLY_ONLY")
        self.assertFalse(result["q15_v92_sequence_blocks_entry"])

    def test_checkpoint_formatter_uses_watch_header(self) -> None:
        with _LATEST_LOCK:
            _LATEST["BTC"] = {
                "q15_v91_checkpoint": "15M",
                "q15_v91_frozen_side": "YES",
                "calibrated_edge_side": "YES",
                "v9_calibrated_p_yes": 0.82,
                "calibrated_side_probability": 0.82,
                "conservative_side_probability": 0.74,
                "canonical_economics": {
                    "executable_entry_ask_cents": 60.0,
                    "total_estimated_cost_cents": 2.0,
                    "conservative_net_edge_cents": 12.0,
                },
                "q15_v92_conservative_edge_cents": 12.0,
                "q15_v92_base_required_edge_cents": 8.0,
                "q15_v92_adjusted_required_edge_cents": 8.0,
                "q15_v91_rolling": rolling(2, 0, 0),
                "q15_v92_evidence_state": "WATCH",
                "q15_v92_checkpoint_relation": "EARLY_ONLY",
                "q15_v92_decision": "WATCH",
                "q15_v92_blockers": ["WATCH"],
            }
        text = _format_checkpoint(
            "🛑 15M EARLY CHECK — NO TRADE\n1. BTC YES\nRejected: warmup"
        )
        self.assertIn("WATCH, NO ENTRY YET", text)
        self.assertIn("Directional observations: 2/5", text)
        self.assertEqual(text.count("no future 10M prediction is required"), 1)

    def test_hourly_formatter_removes_coefficients_and_scopes_entries(self) -> None:
        text = _format_hourly(
            "📊 Hourly operational status\n"
            "No new resolved contracts this hour.\n"
            "Overall: 7W/7L — 50% (resolved n=14; unresolved records=15409)\n"
            "Realized: +3.7¢ avg, +52.0¢ total\n"
            "• raw win probability: coefficient -0.294 — associated with lower observed loss risk\n"
            "Unique contracts: 21 | unique checkpoint decisions: 49\n"
            "Production entries: 3 | shadow-ready: 0\n"
            "Trade-liveness: LIVE_ENTRY_PATH_OBSERVED\n"
        )
        self.assertIn("Current V9.2 live flow", text)
        self.assertNotIn("coefficient -0.294", text)
        self.assertIn("Historical / mixed-version results", text)
        self.assertIn("15M/10M agreement is no longer a universal veto", text)


if __name__ == "__main__":
    unittest.main()
