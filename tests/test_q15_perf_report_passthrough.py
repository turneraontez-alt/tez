from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

import notifications.notifier as notifier_mod
from notifications.notifier import _is_performance_report

# The canonical hourly performance report exactly as reporting.HourlyReporter
# builds it: the em-dash header plus the per-segment breakdown.
PERF_REPORT = """📊 <b>Hourly Report — 02:00 UTC</b>
Overall: 7W/7L — 50% (n=14, 2 pending)
• Real alerts: 3W/2L — 60% (n=5)
• Paper (untraded): 4W/5L — 44% (n=9)
Last hour: 1W/0L — 100% (n=1)
Realized: +3.7¢ avg, +52.0¢ total

⚡ Scalps: 2W/2L — 50% (n=4, +8.0¢, 1 open)

<i>Model-based estimate, not financial advice. Read-only monitor — no orders are placed.</i>"""


class TestPerformanceReportPassthrough(unittest.TestCase):
    def test_detects_canonical_report(self):
        self.assertTrue(_is_performance_report(PERF_REPORT))

    def test_does_not_match_checkpoint_or_reformatted(self):
        self.assertFalse(_is_performance_report("🛑 10M CHECK — NO TRADE"))
        self.assertFalse(
            _is_performance_report("📊 <b>Hourly Operational Status</b>\nbody")
        )

    def test_send_preserves_every_stat_line(self):
        """The reformatter chain must not strip the per-segment stats: the
        report is delivered to Telegram exactly as built."""
        captured = {}

        class FakeResp:
            status_code = 200
            text = "ok"

        class FakeRequests:
            @staticmethod
            def post(url, json=None, timeout=None):
                captured["text"] = json["text"]
                return FakeResp()

        n = notifier_mod.TelegramNotifier()
        n.token = "tok"
        n.chat_id = "chat"
        n.enabled = True
        original = notifier_mod.requests
        notifier_mod.requests = FakeRequests()
        try:
            self.assertTrue(n.send(PERF_REPORT))
        finally:
            notifier_mod.requests = original

        out = captured["text"]
        for needle in (
            "Real alerts: 3W/2L",
            "Paper (untraded): 4W/5L",
            "Last hour: 1W/0L — 100%",
            "Realized: +3.7¢ avg, +52.0¢ total",
            "⚡ Scalps: 2W/2L",
        ):
            self.assertIn(needle, out, f"missing stat line: {needle!r}")
        # The lossy reformatter signature must be absent.
        self.assertNotIn("Operational Status", out)
        self.assertNotIn("three-gate flow", out)


if __name__ == "__main__":
    unittest.main()
