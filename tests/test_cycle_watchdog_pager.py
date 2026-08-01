from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

import cycle_watchdog as cw

_ENV = [
    "Q15_WATCHDOG_ALERT_ENABLED",
    "Q15_WATCHDOG_ALERT_SECONDS",
    "Q15_WATCHDOG_ALERT_WARMUP_SECONDS",
    "Q15_WATCHDOG_ALERT_COOLDOWN_SECONDS",
    "Q15_FEED_WATCHDOG_ENABLED",
    "Q15_FEED_WATCHDOG_STALE_SECONDS",
    "Q15_FEED_WATCHDOG_THRESHOLDS",
    "Q15_FEED_WATCHDOG_GRACE_SECONDS",
    "Q15_FEED_WATCHDOG_COOLDOWN_SECONDS",
    "Q15_HEARTBEAT_ENABLED",
    "Q15_HEARTBEAT_PATH",
    "Q15_HEARTBEAT_COOLDOWN_PATH",
    "Q15_HEARTBEAT_STALE_SECONDS",
    "Q15_HEARTBEAT_PAGER_COOLDOWN_SECONDS",
    "Q15_HEARTBEAT_EXIT_ALERT_ENABLED",
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

    def test_feed_watchdog_pages_once_after_grace(self):
        cw.observe_feed_ages({"coinbase_adv_l2": 301.0}, now=1000.0)
        self.assertIsNone(cw.feed_alert_message(now=1000.0))

        cw.observe_feed_ages({"coinbase_adv_l2": 900.0}, now=1601.0)
        msg = cw.feed_alert_message(now=1601.0)
        self.assertIsNotNone(msg)
        self.assertIn("Q15 FEED WATCHDOG", msg)
        self.assertIn("coinbase_adv_l2", msg)
        self.assertNotIn("V9.5 CHECK", msg)
        self.assertNotIn("ENTRY RECOMMENDED", msg)
        self.assertNotIn("NO ENTRY YET", msg)

        cw.observe_feed_ages({"coinbase_adv_l2": 1200.0}, now=2601.0)
        self.assertIsNone(cw.feed_alert_message(now=2601.0))

    def test_feed_watchdog_recovery_resets_episode(self):
        cw.observe_feed_ages({"coinbase_adv_l2": 900.0}, now=1000.0)
        cw.observe_feed_ages({"coinbase_adv_l2": 901.0}, now=1601.0)
        self.assertIsNotNone(cw.feed_alert_message(now=1601.0))

        cw.observe_feed_ages({"coinbase_adv_l2": 10.0}, now=1700.0)
        health = cw.feed_health()["feeds"]["coinbase_adv_l2"]
        self.assertFalse(health["stale"])

        os.environ["Q15_FEED_WATCHDOG_COOLDOWN_SECONDS"] = "30"
        cw.observe_feed_ages({"coinbase_adv_l2": 901.0}, now=2000.0)
        cw.observe_feed_ages({"coinbase_adv_l2": 902.0}, now=2601.0)
        self.assertIsNotNone(cw.feed_alert_message(now=2601.0))

    def test_feed_watchdog_uses_per_feed_thresholds(self):
        os.environ["Q15_FEED_WATCHDOG_STALE_SECONDS"] = "300"
        os.environ["Q15_FEED_WATCHDOG_THRESHOLDS"] = (
            "kalshi_ws=5,coinbase_adv_l2=20,bad,ignored=nan"
        )

        health = cw.observe_feed_ages(
            {"kalshi_ws": 6.0, "coinbase_adv_l2": 19.0, "other": 250.0},
            now=1000.0,
        )["feeds"]

        self.assertTrue(health["kalshi_ws"]["stale"])
        self.assertEqual(health["kalshi_ws"]["stale_seconds"], 5.0)
        self.assertFalse(health["coinbase_adv_l2"]["stale"])
        self.assertEqual(health["coinbase_adv_l2"]["stale_seconds"], 20.0)
        self.assertFalse(health["other"]["stale"])
        self.assertEqual(health["other"]["stale_seconds"], 300.0)

    def test_heartbeat_file_reports_age_and_staleness(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["Q15_HEARTBEAT_PATH"] = os.path.join(td, "heartbeat.json")
            os.environ["Q15_HEARTBEAT_COOLDOWN_PATH"] = os.path.join(td, "cooldown.json")
            os.environ["Q15_HEARTBEAT_STALE_SECONDS"] = "120"

            cw.write_heartbeat(now=1000.0, cycle=7, status="cycle_start")

            fresh = cw.heartbeat_status(now=1010.0)
            self.assertTrue(fresh["exists"])
            self.assertEqual(fresh["cycle"], 7)
            self.assertEqual(fresh["age_seconds"], 10.0)
            self.assertFalse(fresh["stale"])

            stale = cw.heartbeat_status(now=1121.0)
            self.assertTrue(stale["stale"])
            self.assertEqual(stale["stale_seconds"], 120.0)

    def test_heartbeat_supervisor_persists_cooldown_across_reset(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["Q15_HEARTBEAT_PATH"] = os.path.join(td, "heartbeat.json")
            os.environ["Q15_HEARTBEAT_COOLDOWN_PATH"] = os.path.join(td, "cooldown.json")
            os.environ["Q15_HEARTBEAT_STALE_SECONDS"] = "120"
            os.environ["Q15_HEARTBEAT_PAGER_COOLDOWN_SECONDS"] = "600"
            sent = []

            cw.write_heartbeat(now=1000.0, cycle=1)
            msg = cw.heartbeat_supervisor_tick(now=1121.0, send_fn=sent.append)
            self.assertIsNotNone(msg)
            self.assertEqual(sent, [msg])
            self.assertIn("Q15 HEARTBEAT WATCHDOG", msg)
            self.assertNotIn("V9.5 CHECK", msg)
            self.assertNotIn("ENTRY RECOMMENDED", msg)
            self.assertNotIn("NO ENTRY YET", msg)

            cw.reset()  # simulates an in-memory restart; cooldown file remains.
            self.assertIsNone(cw.heartbeat_supervisor_tick(now=1200.0, send_fn=sent.append))
            self.assertIsNotNone(cw.heartbeat_supervisor_tick(now=1722.0, send_fn=sent.append))
            self.assertEqual(len(sent), 2)

    def test_heartbeat_cooldown_write_failure_uses_memory(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["Q15_HEARTBEAT_PATH"] = os.path.join(td, "heartbeat.json")
            os.environ["Q15_HEARTBEAT_COOLDOWN_PATH"] = os.path.join(td, "cooldown.json")
            os.environ["Q15_HEARTBEAT_STALE_SECONDS"] = "120"
            os.environ["Q15_HEARTBEAT_PAGER_COOLDOWN_SECONDS"] = "600"
            sent = []

            cw.write_heartbeat(now=1000.0, cycle=1)
            with patch.object(cw, "_write_json_atomic", side_effect=OSError("readonly")):
                msg = cw.heartbeat_supervisor_tick(now=1121.0, send_fn=sent.append)
                self.assertIsNotNone(msg)
                self.assertIsNone(cw.heartbeat_supervisor_tick(now=1122.0, send_fn=sent.append))
                self.assertIsNotNone(cw.heartbeat_supervisor_tick(now=1722.0, send_fn=sent.append))

            self.assertEqual(len(sent), 2)

    def test_process_exit_alert_is_persistently_rate_limited(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["Q15_HEARTBEAT_PATH"] = os.path.join(td, "heartbeat.json")
            os.environ["Q15_HEARTBEAT_COOLDOWN_PATH"] = os.path.join(td, "cooldown.json")
            os.environ["Q15_HEARTBEAT_PAGER_COOLDOWN_SECONDS"] = "600"
            sent = []

            msg = cw.send_process_exit_alert(now=1000.0, send_fn=sent.append)
            self.assertIsNotNone(msg)
            self.assertEqual(sent, [msg])
            self.assertIn("Q15 PROCESS EXIT", msg)
            self.assertNotIn("V9.5 CHECK", msg)

            cw.reset()
            self.assertIsNone(cw.send_process_exit_alert(now=1200.0, send_fn=sent.append))


if __name__ == "__main__":
    unittest.main()
