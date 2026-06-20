from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

import cycle_watchdog as cw

_ENV = [
    "Q15_WATCHDOG_ALERT_ENABLED",
    "Q15_WATCHDOG_ALERT_SECONDS",
    "Q15_WATCHDOG_ALERT_WARMUP_SECONDS",
    "Q15_WATCHDOG_ALERT_COOLDOWN_SECONDS",
]


class TestWatchdogPager(unittest.TestCase):
    def setUp(self):
        cw.reset()
        for k in _ENV:
            os.environ.pop(k, None)

    def tearDown(self):
        cw.reset()
        for k in _ENV:
            os.environ.pop(k, None)

    def _cycle(self, secs, stage="run_cycle"):
        ct = cw.CycleTimer()
        ct.stages = {stage: max(0.0, secs - 1.0)}
        ct.commit(secs)

    def test_pages_on_real_stall(self):
        self._cycle(54.0)
        msg = cw.alert_message(now=1000.0, uptime_seconds=300.0)
        self.assertIsNotNone(msg)
        self.assertIn("run_cycle", msg)
        self.assertIn("stall", msg.lower())
        self.assertIn("54s", msg)

    def test_no_page_for_fast_cycle(self):
        self._cycle(2.0)  # below the 20s default alert threshold
        self.assertIsNone(cw.alert_message(now=1000.0, uptime_seconds=300.0))

    def test_no_page_during_warmup(self):
        self._cycle(54.0)
        self.assertIsNone(cw.alert_message(now=1000.0, uptime_seconds=10.0))

    def test_cooldown_suppresses_repeats_then_pages_again(self):
        self._cycle(54.0)
        self.assertIsNotNone(cw.alert_message(now=1000.0, uptime_seconds=300.0))
        # Within the 600s cooldown -> suppressed even though still stalling.
        self._cycle(50.0)
        self.assertIsNone(cw.alert_message(now=1060.0, uptime_seconds=360.0))
        # After the cooldown -> pages again.
        self._cycle(50.0)
        self.assertIsNotNone(cw.alert_message(now=1700.0, uptime_seconds=1000.0))

    def test_disabled_flag(self):
        os.environ["Q15_WATCHDOG_ALERT_ENABLED"] = "false"
        self._cycle(54.0)
        self.assertIsNone(cw.alert_message(now=1000.0, uptime_seconds=300.0))

    def test_custom_threshold(self):
        os.environ["Q15_WATCHDOG_ALERT_SECONDS"] = "40"
        self._cycle(30.0)  # over the 20s default, but under the custom 40s
        self.assertIsNone(cw.alert_message(now=1000.0, uptime_seconds=300.0))


if __name__ == "__main__":
    unittest.main()
