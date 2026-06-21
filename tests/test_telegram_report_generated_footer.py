"""The hourly report carries a timezone-aware generation timestamp and stays
within Telegram's limits, without losing the canonical-report marker.

The top header only names the clock hour (':00'); operators asked for the exact
build time so a late or duplicated delivery is unambiguous. This locks that in
and guards the invariants the notifier keys on: the 'Hourly Report —' marker and
a single <pre> panel.
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

import reporting  # noqa: E402
from notifier import _is_performance_report  # noqa: E402


class _Empty:
    """Stand-in collaborator: every attribute is a no-op returning {}."""

    def __getattr__(self, _name):
        def _call(*_a, **_k):
            return {}
        return _call


class _Scalp:
    def record(self):
        return {
            "total": 0, "open": 0, "wins": 0, "losses": 0,
            "win_rate": 0, "total_realized_cents": 0,
        }


def _build():
    r = reporting.HourlyReporter(
        store=_Empty(), notifier=_Empty(), config={},
        perf=_Empty(), learner=_Empty(), scalp=_Scalp(), v95_ledger=None,
    )
    return r.build_report()


class TestGeneratedFooter(unittest.TestCase):
    def test_generated_line_is_timezone_labelled(self):
        line = reporting._generated_line()
        self.assertTrue(line.startswith("Generated "))
        # Always names the zone so the time is never ambiguous.
        self.assertRegex(line, r"(EDT|EST|UTC)")

    def test_report_contains_generation_timestamp(self):
        msg = _build()
        self.assertIn("Generated ", msg)

    def test_report_preserves_canonical_marker(self):
        msg = _build()
        self.assertIn("Hourly Report —", msg)
        self.assertTrue(_is_performance_report(msg))

    def test_report_is_single_pre_panel_within_limit(self):
        msg = _build()
        self.assertEqual(msg.count("<pre>"), 1)
        self.assertEqual(msg.count("</pre>"), 1)
        self.assertLessEqual(len(msg), 4096)

    def test_pct_helper_routes_through_shared_formatter(self):
        self.assertEqual(reporting._pct(0.5), "50%")
        self.assertEqual(reporting._pct(None), "n/a")


if __name__ == "__main__":
    unittest.main()
