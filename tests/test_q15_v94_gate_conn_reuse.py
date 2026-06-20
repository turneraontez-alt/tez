from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

NOW = 1_800_000_000.0


class TestV94GateConnReuse(unittest.TestCase):
    def setUp(self):
        from q15_upgrade.checkpoint_v94_unified import TelegramNotificationGate
        self.tmp = tempfile.TemporaryDirectory()
        self.gate = TelegramNotificationGate(os.path.join(self.tmp.name, "gate.sqlite3"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_connect_is_reused(self):
        c1 = self.gate._connect()
        c2 = self.gate._connect()
        self.assertIs(c1, c2)
        # close() is a deliberate no-op so `with closing(...)` callers don't tear
        # down the shared connection.
        c1.close()
        self.assertIs(self.gate._connect(), c1)

    def test_acquire_and_suppress_still_work_after_reuse(self):
        # Behavior unchanged: first acquire grants a permit, a repeat for the
        # same event key is suppressed (BEGIN IMMEDIATE transactions still work
        # on the reused connection).
        permit = self.gate.acquire(
            event_key="10M:1800000600", checkpoint="10M", close_bucket=1800000600,
            fingerprint="watch", has_entry=False, now=NOW,
        )
        self.assertIsNotNone(permit)
        self.gate.complete(permit, success=True, now=NOW)
        self.assertIsNone(self.gate.acquire(
            event_key="10M:1800000600", checkpoint="10M", close_bucket=1800000600,
            fingerprint="watch-again", has_entry=False, now=NOW + 1,
        ))


if __name__ == "__main__":
    unittest.main()
