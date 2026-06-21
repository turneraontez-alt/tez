"""Visibility for dropped/undelivered v9.5 checkpoint alerts.

The review flagged two silent failure modes on the alert path: a
`reserve_notification` that returns no permit (dedup OR a ledger hiccup) and a
send that is handled but produces no message_id (muted / no proof of delivery —
no official record written). Both now emit a throttled WARNING so a real outage
is visible instead of vanishing. `_throttled_warn` is the shared mechanism.
"""
from __future__ import annotations

import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.checkpoint_v94_unified import CheckpointPolicyV94Unified
from q15_upgrade.checkpoint_v95 import CheckpointPolicyV95

# Reuse the established fixtures rather than re-deriving them.
from test_q15_v95 import FakeHub, FakeStore, candles, context, snapshot, NOW  # noqa: E402


class _MutedNotifier:
    """A notifier that accepts the send but never yields a message_id (muted)."""
    def __init__(self):
        self.enabled = True
        self.messages = []
        self.last_message_id = None
    def send(self, text, *a, **k):
        self.messages.append(str(text))
        return True
    def send_with_result(self, text):
        self.messages.append(str(text))
        return {"ok": True, "delivered": False, "muted": True, "message_id": None}


def _make_policy(tmp_path):
    with patch.dict(os.environ, {"Q15_V95_LEDGER_DB": str(tmp_path)}, clear=False):
        with patch.object(CheckpointPolicyV94Unified, "__init__", lambda self, *a, **k: None):
            policy = CheckpointPolicyV95(FakeStore())
    policy.market_data = FakeHub()
    policy._candle_cache = {"BNB": {row["start_time"]: row for row in candles()}}
    policy._latest_context = {"BNB": context()}
    policy._context_lock = threading.RLock()
    policy._candles = lambda asset: list(policy._candle_cache[asset].values())
    return policy


class ThrottledWarnTest(unittest.TestCase):
    def test_logs_once_per_interval_per_key(self):
        with patch.object(CheckpointPolicyV94Unified, "__init__", lambda self, *a, **k: None):
            policy = CheckpointPolicyV95(FakeStore())
        with self.assertLogs("q15_upgrade.checkpoint_v95", level="WARNING") as cm:
            policy._throttled_warn("k", "msg %s", 1, now=100.0, interval=60.0)
            policy._throttled_warn("k", "msg %s", 2, now=110.0, interval=60.0)  # throttled
            policy._throttled_warn("k", "msg %s", 3, now=200.0, interval=60.0)  # interval passed
            policy._throttled_warn("other", "msg %s", 4, now=110.0, interval=60.0)  # other key
        warnings = [r for r in cm.output if "msg" in r]
        self.assertEqual(len(warnings), 3, "second within-interval call must be throttled")


class MutedDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__import__("tempfile").mkdtemp())

    def test_handled_but_not_delivered_warns_and_writes_no_record(self):
        policy = _make_policy(self.tmp / "muted.sqlite3")
        notifier = _MutedNotifier()
        with patch.object(CheckpointPolicyV94Unified, "run_cycle",
                          lambda self, snapshots, *a: snapshots):
            row = snapshot(spot=102.0, target=100.0)
            with self.assertLogs("q15_upgrade.checkpoint_v95", level="WARNING") as cm:
                for offset in range(4):
                    policy.run_cycle({"BNB": dict(row)}, NOW + offset, {}, None, None, notifier)
        # A panel was attempted (muted send recorded the text) ...
        self.assertTrue(any("V9.5 CHECK" in m for m in notifier.messages))
        # ... it warned about the undelivered alert ...
        self.assertTrue(any("handled but not delivered" in r for r in cm.output),
                        "a muted (no message_id) send must surface a warning")
        # ... and wrote NO official record (muted = not official).
        from contextlib import closing
        with closing(policy.ledger._connect()) as conn:
            n = conn.execute("SELECT COUNT(*) FROM sent_predictions").fetchone()[0]
        self.assertEqual(n, 0, "a muted send must not enter the official record")


if __name__ == "__main__":
    unittest.main()
