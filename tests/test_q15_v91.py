from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from q15_upgrade.checkpoint_v91 import CheckpointPolicyV91, format_telegram_message


class DisabledStore:
    enabled = False


def snapshot(asset="DOGE", ticker="DOGE-TEST", seconds=900, p_yes=0.15):
    side = "NO" if p_yes < 0.5 else "YES"
    ask = 60.0
    return {
        "asset": asset,
        "ticker": ticker,
        "seconds_remaining": seconds,
        "v9_calibrated_p_yes": p_yes,
        "calibrated_edge_side": side,
        "calibrated_edge_status": "READY",
        "calibrated_side_probability": max(p_yes, 1-p_yes),
        "conservative_side_probability": max(p_yes, 1-p_yes)-0.05,
        "canonical_economics": {
            "executable_entry_ask_cents": ask,
            "total_estimated_cost_cents": 2.0,
            "conservative_net_edge_cents": (max(p_yes, 1-p_yes)-0.05)*100-ask-2,
            "maximum_entry_price_cents": (max(p_yes, 1-p_yes)-0.05)*100-2-4,
        },
        "trade_quality": {"required_edge_cents": 4.0},
    }


class V91Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.sqlite3"
        os.environ["Q15_V91_OBSERVATION_BUCKET_SECONDS"] = "10"
        os.environ["Q15_V91_ROLLING_WINDOW"] = "5"
        os.environ["Q15_V91_MIN_DIRECTIONAL_VOTES"] = "3"
        os.environ["Q15_V91_MIN_DIRECTIONAL_AGREEMENT"] = "0.72"
        self.policy = CheckpointPolicyV91(DisabledStore(), self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def collect(self, snap, start=100.0, n=5):
        out = None
        for i in range(n):
            out = self.policy.pre_enrich_all({snap["asset"]: snap}, start + i*10.0)[snap["asset"]]
        return out

    def test_15m_does_not_require_future_10m(self):
        snap = snapshot(seconds=900, p_yes=0.15)
        enriched = self.collect(snap)
        self.assertEqual(enriched["q15_prediction_agreement"], "EARLY_ONLY")
        post = self.policy.finalize_all({"DOGE": enriched}, 150.0)["DOGE"]
        self.assertEqual(post["q15_v91_decision"], "READY")
        self.assertTrue(post["q15_v91_entry_allowed"])

    def test_10m_same_prediction(self):
        fifteen = self.collect(snapshot(seconds=900, p_yes=0.15, ticker="SAME"), 100, 5)
        self.policy.finalize_all({"DOGE": fifteen}, 150)
        ten = self.collect(snapshot(seconds=590, p_yes=0.12, ticker="SAME"), 200, 5)
        self.assertEqual(ten["q15_prediction_agreement"], "SAME")
        self.assertEqual(ten["q15_final_result"], "NO")

    def test_10m_conflict(self):
        self.collect(snapshot(seconds=900, p_yes=0.15, ticker="CONFLICT"), 100, 5)
        ten = self.collect(snapshot(seconds=590, p_yes=0.85, ticker="CONFLICT"), 200, 5)
        self.assertEqual(ten["q15_prediction_agreement"], "CONFLICT")
        post = self.policy.finalize_all({"DOGE": ten}, 250)["DOGE"]
        self.assertFalse(post["q15_v91_entry_allowed"])

    def test_missing_prior_is_not_called_consensus_failure(self):
        ten = self.collect(snapshot(seconds=590, p_yes=0.15, ticker="MISSING"), 200, 5)
        self.assertEqual(ten["q15_prediction_agreement"], "MISSING_PRIOR_CHECKPOINT")

    def test_agreement_and_coverage_are_separate(self):
        snap = snapshot(seconds=900, p_yes=0.5, ticker="VOTES")
        # 8-neutral example is tested with a 10 observation window.
        os.environ["Q15_V91_ROLLING_WINDOW"] = "10"
        policy = CheckpointPolicyV91(DisabledStore(), Path(self.tmp.name)/"votes.sqlite3")
        for i in range(8):
            policy.pre_enrich_all({"DOGE": snap}, 100+i*10)
        no_snap = snapshot(seconds=900, p_yes=0.10, ticker="VOTES")
        result = None
        for i in range(2):
            result = policy.pre_enrich_all({"DOGE": no_snap}, 200+i*10)["DOGE"]
        rolling = result["q15_v91_rolling"]
        self.assertEqual(rolling["no_votes"], 2)
        self.assertEqual(rolling["neutral_votes"], 8)
        self.assertAlmostEqual(rolling["directional_agreement"], 1.0)
        self.assertAlmostEqual(rolling["evidence_coverage"], 0.2)
        self.assertEqual(rolling["status"], "INSUFFICIENT_DIRECTIONAL_EVIDENCE")

    def test_persistence_survives_new_instance(self):
        self.collect(snapshot(seconds=900, p_yes=0.15, ticker="PERSIST"), 100, 5)
        new_policy = CheckpointPolicyV91(DisabledStore(), self.db)
        ten = new_policy.pre_enrich_all({"DOGE": snapshot(seconds=590, p_yes=0.12, ticker="PERSIST")}, 200)["DOGE"]
        self.assertEqual(ten["q15_prediction_agreement"], "SAME")

    def test_negative_edge_rejected(self):
        snap = snapshot(seconds=900, p_yes=0.15, ticker="NEG")
        snap["calibrated_edge_status"] = "PRICE_NOT_ATTRACTIVE"
        snap["canonical_economics"]["conservative_net_edge_cents"] = -5.0
        enriched = self.collect(snap)
        post = self.policy.finalize_all({"DOGE": enriched}, 150)["DOGE"]
        self.assertEqual(post["q15_v91_decision"], "PRICE_NOT_ATTRACTIVE")
        self.assertFalse(post["q15_v91_entry_allowed"])

    def test_unified_formatter_removes_old_sections_and_duplicate_disclaimer(self):
        snap = snapshot(seconds=590, p_yes=0.15, ticker="FORMAT")
        self.collect(snapshot(seconds=900, p_yes=0.15, ticker="FORMAT"), 100, 5)
        enriched = self.collect(snap, 200, 5)
        final = self.policy.finalize_all({"DOGE": enriched}, 250)["DOGE"]
        msg = (
            "🛑 10M FINAL CHECK — NO TRADE\nClosest candidates:\n"
            "1. DOGE NO — side estimate 85.0%\nRejected: old reason\n"
            "Probabilistic paper estimate only; it can be wrong. Read-only monitor — no orders are placed.\n\n"
            "PROFESSIONAL TRADE QUALITY — TOP CANDIDATES\nold duplicate\n"
            "V9 TRADE QUALITY\nold second duplicate"
        )
        out = format_telegram_message(msg)
        self.assertNotIn("PROFESSIONAL TRADE QUALITY", out)
        self.assertNotIn("V9 TRADE QUALITY", out)
        self.assertEqual(out.count("Probabilistic paper estimate only"), 1)
        self.assertIn("15M comparison: SAME", out)
        self.assertNotIn("READY result did not override", out)
        self.assertIn("DOGE NO", out)

    def test_hourly_coefficients_split_and_shadow_labeled(self):
        msg = (
            "📊 Hourly Report — 02:00 UTC\nOverall: 7W/7L — 50% (n=14, 15376 pending)\n"
            "Factors associated with higher loss risk:\n"
            "• executable ask: coefficient -0.347\n"
            "• predicted edge: coefficient +0.183\n"
            "Learning adjustments active:\n• vol:high +1.9pp [SHADOW SUPPRESS]"
        )
        out = format_telegram_message(msg)
        self.assertIn("higher observed loss risk", out)
        self.assertIn("lower observed loss risk", out)
        self.assertIn("not applied to production alerts", out)
        self.assertIn("unresolved records=15376", out)
        self.assertEqual(out.count("Probabilistic paper estimate only"), 1)


if __name__ == "__main__":
    unittest.main()
