"""Locks the candle-path efficiency optimizations as behaviour-identical:

1. _normalize_key memoization returns the same string as the plain regex.
2. _candles() returns independent copies (mutating a returned candle must not
   touch the shared cache) — proving dict() is a safe stand-in for deepcopy on
   the flat float candle rows.
"""
from __future__ import annotations

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)


def _plain_normalize_key(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")


class TestCandlePathEfficiency(unittest.TestCase):
    def test_normalize_key_memo_matches_plain(self):
        from q15_upgrade.checkpoint_v94_unified import _normalize_key
        for sample in (
            "Q15_V91_Contract-Key", "underlying_candles_5s", "OHLCV", "", "a..b--c",
            "q15_v9.5_rank", 5, 3.14, "MixedCASE_123",
        ):
            self.assertEqual(_normalize_key(sample), _plain_normalize_key(sample))
        # second call on a repeated key is a cache hit (the whole point).
        before = _normalize_key.cache_info().hits
        _normalize_key("underlying_candles_5s")
        self.assertGreater(_normalize_key.cache_info().hits, before)

    def test_candles_returns_isolated_copies(self):
        from q15_upgrade.checkpoint_v94 import CheckpointPolicyV94
        policy = CheckpointPolicyV94(None)
        original = {
            "start_time": 100.0, "end_time": 105.0, "open": 1.0, "high": 2.0,
            "low": 0.5, "close": 1.5, "volume": 3.0, "trade_count": 4.0,
        }
        with policy._context_lock:
            policy._candle_cache["BTC"] = {100.0: dict(original)}
        out = policy._candles("BTC")
        self.assertEqual(out, [original])
        # Mutating the returned candle must not corrupt the shared cache.
        out[0]["close"] = 999.0
        self.assertEqual(policy._candle_cache["BTC"][100.0]["close"], 1.5)


if __name__ == "__main__":
    unittest.main()
