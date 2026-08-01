from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from notifications.reporting import HourlyReporter


class _Config:
    hourly_report_enabled = True


class _Store:
    def __init__(self):
        self.claims = []

    def claim_event(self, key):
        self.claims.append(key)
        return True


class _Notifier:
    enabled = True

    def __init__(self):
        self.messages = []
        self.sent = threading.Event()

    def send(self, message):
        self.messages.append(message)
        self.sent.set()


class HourlyReportAsyncTest(unittest.TestCase):
    def test_report_build_and_send_do_not_block_refresh_caller(self):
        store = _Store()
        notifier = _Notifier()
        reporter = HourlyReporter(store, notifier, _Config(), None, None, None)
        reporter._last_hour = "previous"

        def slow_report():
            time.sleep(0.15)
            return "report"

        with mock.patch.object(reporter, "build_report", side_effect=slow_report):
            started = time.perf_counter()
            reporter.maybe_send(time.time())
            elapsed = time.perf_counter() - started
            self.assertLess(elapsed, 0.08)
            self.assertTrue(notifier.sent.wait(1.0))

        self.assertEqual(notifier.messages, ["report"])
        self.assertEqual(len(store.claims), 1)


if __name__ == "__main__":
    unittest.main()
