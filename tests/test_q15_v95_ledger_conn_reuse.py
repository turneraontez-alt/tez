from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)


class TestV95LedgerConnectionReuse(unittest.TestCase):
    def setUp(self):
        from q15_upgrade.ledger_v95 import V95Ledger
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = V95Ledger(os.path.join(self.tmp.name, "ledger.sqlite3"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_connect_returns_the_same_persistent_connection(self):
        # The ledger is read several times per asset per cycle inside
        # v95_analysis; reuse means the file is opened once, not every call.
        c1 = self.ledger._connect()
        c2 = self.ledger._connect()
        self.assertIs(c1, c2)
        # close() is a deliberate no-op so `with closing(...)` callers don't
        # tear down the shared connection.
        c1.close()
        self.assertIs(self.ledger._connect(), c1)

    def test_reads_still_work_after_reuse(self):
        # Behavior must be unchanged: status() and challenger_weights() return
        # the same values across calls on the reused connection.
        status = self.ledger.status()
        self.assertTrue(status.get("available"))
        weights = self.ledger.challenger_weights("10M")
        self.assertIsInstance(weights, dict)
        self.assertTrue(weights)
        self.assertEqual(weights, self.ledger.challenger_weights("10M"))


if __name__ == "__main__":
    unittest.main()
