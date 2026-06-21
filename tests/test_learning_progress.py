"""Smoke tests for the read-only learning-progress report tool."""
from __future__ import annotations

import importlib.util
import os
import unittest

# tools/ is a script directory, not a package — load the module by file path so
# the test doesn't force a package layout onto it.
_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tools", "learning_progress.py")
_spec = importlib.util.spec_from_file_location("learning_progress", _PATH)
lp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lp)


class LearningProgressTests(unittest.TestCase):
    def test_collect_never_raises_and_has_both_sections(self):
        report = lp.collect()
        self.assertIn("production_v95", report)
        self.assertIn("shadow_vs_yours", report)
        # Each section always reports availability rather than throwing.
        for sec in report.values():
            self.assertIn("available", sec)

    def test_render_handles_unavailable_sections(self):
        out = lp._render({
            "production_v95": {"available": False, "reason": "no ledger"},
            "shadow_vs_yours": {"available": False, "reason": "disabled"},
        })
        self.assertIn("LEARNING PROGRESS", out)
        self.assertIn("unavailable", out)

    def test_render_formats_populated_report(self):
        out = lp._render({
            "production_v95": {
                "available": True,
                "status": {"unique_predictions": 120, "unique_resolved": 80,
                           "shadow_updates_applied": 5, "last_learning_step_magnitude": 0.01,
                           "last_shadow_update": "2026-06-21T20:00:00Z"},
                "scoreboard": {
                    "overall": {"right": 50, "wrong": 30, "n": 80, "accuracy": 0.625,
                                "realized_total_cents": 340.0},
                    "by_checkpoint": {"10M": {"accuracy": 0.66, "n": 50,
                                              "realized_total_cents": 210.0,
                                              "ci_low": 0.52, "ci_high": 0.78}},
                },
                "accuracy": {"headline": "10M leads; let 15M/7M accumulate."},
            },
            "shadow_vs_yours": {
                "available": True, "model_version": "challenger-v4",
                "scoreboard": {"resolved": 40, "challenger": {"brier": 0.21},
                               "control": {"brier": 0.24},
                               "trading": {"n_trades": 12, "win_rate": 0.58,
                                           "total_hypothetical_pnl_cents": 90.0}},
                "delivery_audit": {"SENT": 30, "DELIVERY_FAILED": 2, "PENDING": 8},
            },
        })
        self.assertIn("predictions recorded : 120", out)
        self.assertIn("62.5%", out)            # overall accuracy
        self.assertIn("challenger-v4", out)
        self.assertIn("SENT=30", out)


if __name__ == "__main__":
    unittest.main()
