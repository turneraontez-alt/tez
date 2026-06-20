"""Locks the accuracy / promotion-readiness interpreter.

build_accuracy_report is a pure function of V95Ledger.metrics(); these tests fix
the verdict logic, the calibration-error math, and the skill/edge sign
conventions so the readout stays honest.
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.accuracy_report import build_accuracy_report, compact_summary, _ece


def _metrics(*, primary="10M", min_rows=50, cp_overrides=None, bands=None, overall_resolved=0):
    cp_overrides = cp_overrides or {}
    by_checkpoint = {}
    promotion = {}
    scoreboard_cp = {}
    for cp in ("15M", "10M", "7M"):
        o = cp_overrides.get(cp, {})
        resolved = o.get("resolved", 0)
        by_checkpoint[cp] = {
            "resolved": resolved,
            "accuracy": o.get("accuracy"),
            "champion_brier": o.get("champion_brier"),
            "challenger_brier": o.get("challenger_brier"),
            "baseline_brier": o.get("market_brier"),
        } if resolved else {"resolved": 0}
        if cp in ("15M", "10M"):  # LEARNING_CHECKPOINTS
            promotion[cp] = {
                "candidate": o.get("candidate", False),
                "reason": o.get("reason", "insufficient_resolved_rows"),
                "resolved": resolved,
                "learning_enabled": o.get("learning_enabled", cp == "10M"),
                "alpha": 0.05,
                "vs_champion": {"p_value": o.get("vs_champion_p")},
                "vs_baseline": {"p_value": o.get("vs_market_p")},
            }
        scoreboard_cp[cp] = {"realized_avg_cents": o.get("realized_avg_cents")}
    return {
        "available": True, "model_version": "m1", "minimum_promotion_rows": min_rows,
        "primary_learning_checkpoint": primary,
        "overall": {"resolved": overall_resolved},
        "by_checkpoint": by_checkpoint,
        "promotion_by_checkpoint": promotion,
        "scoreboard": {"by_checkpoint": scoreboard_cp},
        "calibration_bands": bands or [],
    }


class TestAccuracyReport(unittest.TestCase):
    def test_unavailable(self):
        self.assertFalse(build_accuracy_report({"available": False, "error": "x"})["available"])
        self.assertFalse(build_accuracy_report(None)["available"])

    def test_ece_math(self):
        bands = [
            {"band": "50-55%", "count": 10, "mean_predicted": 0.52, "actual_win_rate": 0.50},
            {"band": "60-65%", "count": 30, "mean_predicted": 0.62, "actual_win_rate": 0.70},
        ]
        # weighted: (10/40)*0.02 + (30/40)*0.08 = 0.005 + 0.06 = 0.065
        self.assertEqual(_ece(bands), 0.065)
        self.assertIsNone(_ece([]))

    def test_accumulating_verdict_and_headline(self):
        rep = build_accuracy_report(_metrics(
            overall_resolved=3,
            cp_overrides={"10M": {"resolved": 3, "champion_brier": 0.17,
                                  "challenger_brier": 0.17, "market_brier": 0.16}},
        ))
        b = rep["by_checkpoint"]["10M"]
        self.assertEqual(b["verdict"], "ACCUMULATING")
        self.assertEqual(b["rows_needed_for_promotion"], 47)
        self.assertFalse(b["sample_trustworthy"])
        self.assertIn("3/50", rep["headline"])
        # sign conventions: skill_vs_market = market - champion (>0 beats market)
        self.assertEqual(b["champion_skill_vs_market"], round(0.16 - 0.17, 6))
        self.assertEqual(b["challenger_edge_vs_champion"], round(0.17 - 0.17, 6))

    def test_learning_off_for_disabled_checkpoint(self):
        rep = build_accuracy_report(_metrics(
            cp_overrides={"15M": {"resolved": 80, "learning_enabled": False,
                                  "champion_brier": 0.17, "challenger_brier": 0.17, "market_brier": 0.16}},
        ))
        self.assertEqual(rep["by_checkpoint"]["15M"]["verdict"], "LEARNING_OFF")

    def test_promotion_candidate(self):
        rep = build_accuracy_report(_metrics(
            overall_resolved=60,
            cp_overrides={"10M": {"resolved": 60, "candidate": True,
                                  "reason": "eligible_for_manual_review",
                                  "champion_brier": 0.18, "challenger_brier": 0.15, "market_brier": 0.20,
                                  "vs_champion_p": 0.01, "vs_market_p": 0.02,
                                  "realized_avg_cents": 5.0}},
        ))
        b = rep["by_checkpoint"]["10M"]
        self.assertEqual(b["verdict"], "PROMOTION_CANDIDATE")
        self.assertTrue(b["sample_trustworthy"])
        self.assertGreater(b["challenger_edge_vs_champion"], 0)  # challenger better
        self.assertGreater(b["champion_skill_vs_market"], 0)     # beats market
        self.assertTrue(b["edge_positive"])
        self.assertIn("PROMOTION CANDIDATE", rep["headline"])

    def test_challenger_not_better(self):
        rep = build_accuracy_report(_metrics(
            overall_resolved=60,
            cp_overrides={"10M": {"resolved": 60, "candidate": False,
                                  "reason": "challenger_not_significantly_better",
                                  "champion_brier": 0.16, "challenger_brier": 0.17, "market_brier": 0.18,
                                  "realized_avg_cents": -2.0}},
        ))
        b = rep["by_checkpoint"]["10M"]
        self.assertEqual(b["verdict"], "CHALLENGER_NOT_BETTER")
        self.assertFalse(b["edge_positive"])

    def test_no_data_headline(self):
        rep = build_accuracy_report(_metrics(overall_resolved=0))
        self.assertIn("No resolved predictions yet", rep["headline"])

    def test_compact_summary(self):
        rep = build_accuracy_report(_metrics(
            overall_resolved=3,
            cp_overrides={"10M": {"resolved": 3, "champion_brier": 0.17,
                                  "challenger_brier": 0.17, "market_brier": 0.16}},
        ))
        c = compact_summary(rep)
        self.assertTrue(c["available"])
        self.assertEqual(c["primary_verdict"], "ACCUMULATING")
        self.assertEqual(c["primary_resolved"], 3)
        self.assertEqual(c["headline"], rep["headline"])
        self.assertFalse(compact_summary({"available": False})["available"])


if __name__ == "__main__":
    unittest.main()
