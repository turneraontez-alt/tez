from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)


class TestV94ContextCacheConnReuse(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Another test module sets CACHE_PERSIST=false at import; set both env
        # vars explicitly so this test is order-independent.
        self._prev = {k: os.environ.get(k) for k in ("Q15_V94_CACHE_DB_PATH", "Q15_V94_CACHE_PERSIST")}
        os.environ["Q15_V94_CACHE_DB_PATH"] = os.path.join(self.tmp.name, "ctx.sqlite3")
        os.environ["Q15_V94_CACHE_PERSIST"] = "true"
        from q15_upgrade.checkpoint_v94 import CheckpointPolicyV94
        self._cls = CheckpointPolicyV94
        self.policy = CheckpointPolicyV94(None)

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def test_cache_connection_is_reused(self):
        # _persist_candles runs per asset every cycle; reuse means the file is
        # opened once, not every call.
        c1 = self.policy._cache_connection()
        c2 = self.policy._cache_connection()
        self.assertIs(c1, c2)
        # close() is a deliberate no-op so `with conn:` callers don't tear down
        # the shared connection.
        c1.close()
        self.assertIs(self.policy._cache_connection(), c1)

    def test_persist_and_load_roundtrip_unchanged(self):
        # Behavior must be unchanged: a persisted candle is readable by a fresh
        # instance pointed at the same file (the reused connection still commits).
        now = 1_000_000.0
        candle = {
            "start_time": now - 60.0, "end_time": now,
            "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
            "volume": 3.0, "trade_count": 4.0,
        }
        self.policy._persist_candles("BTC", [candle], now)
        other = self._cls(None)
        other._load_persistent_cache("BTC", now)
        cached = other._candles("BTC")
        self.assertTrue(any(abs(c["end_time"] - now) < 1e-6 for c in cached))


if __name__ == "__main__":
    unittest.main()
