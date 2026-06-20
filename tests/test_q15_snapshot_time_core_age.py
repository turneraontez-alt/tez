"""Locks the snapshot_time fix for false AVOID_INVALID_DATA.

The v9.5 canonical build derives "core age" via `_first_value`, which walks the
whole snapshot for any timestamp-ish key. Without an authoritative `snapshot_time`
it could latch onto an unrelated stale nested timestamp and trip
`stale_core_snapshot` (→ AVOID_INVALID_DATA) even when spot/orderbook are fresh.

These tests assert:
1. A fresh `snapshot_time` takes precedence over a stale nested timestamp, so the
   canonical snapshot is core_valid (no stale_core_snapshot).
2. Removing `snapshot_time` and leaving only a stale nested timestamp reproduces
   the bug (stale_core_snapshot fires) — proving the walk was the cause.
3. A genuinely stale `snapshot_time` still trips stale_core_snapshot — the safety
   check is preserved, not disabled.
4. apply_v95_policy surfaces `q15_v9_5_main_blocker`.
"""
from __future__ import annotations

import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.checkpoint_v95 import build_canonical_snapshot, apply_v95_policy


def _fresh_snapshot(now: float) -> dict:
    return {
        "asset": "BTC",
        "ticker": "KXBTC-X",
        "underlying_current": 63678.18,
        "target_value": 63695.91,
        "seconds_remaining": 845,
        "yes_bid": 40, "yes_ask": 42,
        "underlying_candles_5s": [
            {"open": 63670.0 + i, "high": 63680.0 + i, "low": 63660.0 + i,
             "close": 63678.0 + i, "start_time": now - (5 * (3 - i)) - 5,
             "end_time": now - (5 * (3 - i))}
            for i in range(4)
        ],
    }


def _build(snapshot: dict, now: float):
    return build_canonical_snapshot(
        snapshot, asset="BTC", checkpoint="15M", now=now,
        cached_candles=None, context=None, public=None,
    )


class TestSnapshotTimeCoreAge(unittest.TestCase):
    def test_fresh_snapshot_time_beats_stale_nested_timestamp(self):
        now = time.time()
        snap = _fresh_snapshot(now)
        snap["snapshot_time"] = now
        # A stale nested timestamp the walk would otherwise pick up.
        snap["some_subsystem"] = {"timestamp": now - 6000}
        canonical = _build(snap, now)
        self.assertNotIn("stale_core_snapshot", canonical.core_errors)
        self.assertTrue(canonical.core_valid, msg=f"errors={canonical.core_errors}")

    def test_bug_reproduced_without_snapshot_time(self):
        now = time.time()
        snap = _fresh_snapshot(now)
        # No snapshot_time; only a stale nested timestamp -> the old walk behaviour
        # latches onto it and trips stale_core_snapshot.
        snap["some_subsystem"] = {"timestamp": now - 6000}
        canonical = _build(snap, now)
        self.assertIn("stale_core_snapshot", canonical.core_errors)

    def test_genuinely_stale_snapshot_time_still_trips(self):
        now = time.time()
        snap = _fresh_snapshot(now)
        snap["snapshot_time"] = now - 6000  # snapshot really wasn't rebuilt
        canonical = _build(snap, now)
        self.assertIn("stale_core_snapshot", canonical.core_errors)

    def test_apply_policy_surfaces_main_blocker(self):
        snap: dict = {}
        apply_v95_policy(snap, {"trade_decision": "AVOID_INVALID_DATA",
                                "main_blocker": "stale_core_snapshot",
                                "prediction_available": False})
        self.assertEqual(snap["q15_v9_5_main_blocker"], "stale_core_snapshot")
        self.assertEqual(snap["q15_v9_5_trade_decision"], "AVOID_INVALID_DATA")


if __name__ == "__main__":
    unittest.main()
