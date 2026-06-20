from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

import cycle_watchdog as cw


class TestCycleWatchdog(unittest.TestCase):
    def setUp(self):
        cw.reset()

    def tearDown(self):
        cw.reset()
        os.environ.pop("Q15_CYCLE_WATCHDOG_SECONDS", None)

    def test_time_returns_value_and_records_stage(self):
        ct = cw.CycleTimer()
        out = ct.time("x", lambda v: v + 1, 41)
        self.assertEqual(out, 42)
        self.assertIn("x", ct.stages)

    def test_time_reraises_but_still_records(self):
        ct = cw.CycleTimer()
        with self.assertRaises(ValueError):
            ct.time("boom", lambda: (_ for _ in ()).throw(ValueError("x")))
        self.assertIn("boom", ct.stages)

    def test_safe_swallows_exception_but_times(self):
        ct = cw.CycleTimer()
        ct.safe("boom", lambda: (_ for _ in ()).throw(ValueError("nope")))  # must not raise
        self.assertIn("boom", ct.stages)

    def test_commit_flags_slow_and_names_slowest_stage(self):
        os.environ["Q15_CYCLE_WATCHDOG_SECONDS"] = "5"
        ct = cw.CycleTimer()
        ct.stages = {"run_cycle": 6.0, "report": 1.0}
        self.assertTrue(ct.commit(8.0))
        h = cw.health()
        self.assertEqual(h["slowest_stage"], "run_cycle")
        self.assertEqual(h["slowest_stage_seconds"], 6.0)
        self.assertEqual(h["slow_cycle_count"], 1)
        self.assertAlmostEqual(h["unaccounted_seconds"], 1.0, places=3)
        self.assertIsNotNone(h["last_slow_breakdown"])
        self.assertEqual(h["last_slow_breakdown"]["unaccounted"], 1.0)

    def test_fast_cycle_not_flagged(self):
        os.environ["Q15_CYCLE_WATCHDOG_SECONDS"] = "10"
        ct = cw.CycleTimer()
        ct.stages = {"run_cycle": 0.2}
        self.assertFalse(ct.commit(0.5))
        self.assertEqual(cw.health()["slow_cycle_count"], 0)

    def test_worst_stage_seconds_is_high_water_mark(self):
        ct1 = cw.CycleTimer()
        ct1.stages = {"run_cycle": 2.0}
        ct1.commit(2.5)
        ct2 = cw.CycleTimer()
        ct2.stages = {"run_cycle": 1.0}
        ct2.commit(1.5)
        # latest cycle was faster, but the worst-ever should stick at 2.0
        self.assertEqual(cw.health()["worst_stage_seconds"]["run_cycle"], 2.0)
        self.assertEqual(cw.health()["last_stage_seconds"]["run_cycle"], 1.0)


if __name__ == "__main__":
    unittest.main()
