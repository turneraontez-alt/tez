from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)


class TestAdaptive15ConnectionReuse(unittest.TestCase):
    def setUp(self):
        from q15_upgrade.checkpoint_v94_adaptive15 import Adaptive15LearningStore
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Adaptive15LearningStore(os.path.join(self.tmp.name, "a15.sqlite3"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_connect_returns_the_same_persistent_connection(self):
        # The hot path opens a connection several times per asset per cycle;
        # reuse means the file is opened once, not every call.
        c1 = self.store._connect()
        c2 = self.store._connect()
        self.assertIs(c1, c2)
        # close() is a deliberate no-op so `with closing(...)` callers don't
        # tear down the shared connection.
        c1.close()
        self.assertIs(self.store._connect(), c1)

    def test_weights_still_readable_after_reuse(self):
        # Behavior must be unchanged: weights() returns the base schema keys.
        weights = self.store.weights()
        self.assertIsInstance(weights, dict)
        self.assertTrue(weights)
        # A second call (same connection) returns identical values.
        self.assertEqual(weights, self.store.weights())


if __name__ == "__main__":
    unittest.main()
