"""Tests for the Shadow-vs-Yours delivery-accounting fix.

The bug: Your System's native picks were marked DELIVERY_FAILED the instant the
*synchronous* outbox attempt didn't return delivered, even though the async
background worker actually delivered the report (which is why the owner kept
receiving reports while the record showed 0 sent / N failed).

The fix: tag the pick PENDING under the official report's idempotency key, and
reconcile the true outcome from the outbox's status — SENT (sync OR worker) ->
credited, DEAD_LETTER -> failed, otherwise still pending.
"""
from __future__ import annotations

import os
import tempfile
import unittest

from q15_upgrade.challenger.config import ChallengerConfig
from q15_upgrade.challenger.runner import ShadowRunner


def _cfg(tmp):
    return ChallengerConfig().with_overrides(
        enabled=True, db_path=os.path.join(tmp, "shadow.sqlite3"),
        refit_every=5, min_train_rows=20, model_version="challenger-test",
    )


class OutboxStatusByKeyTests(unittest.TestCase):
    def _outbox(self, tmp):
        from notifications.outbox_v9 import ReliableTelegramOutbox

        class _Raw:
            token = "x"
            chat_id = "y"
            enabled = True
            sent_count = 0
            last_sent_at = None
            last_error = None
            def __init__(self):
                self.delivers = True
            def send_with_result(self, text):
                if self.delivers:
                    return {"ok": True, "delivered": True, "muted": False, "message_id": 11}
                return {"ok": False, "delivered": False, "muted": False, "message_id": None}

        # Disable the background worker so the test controls delivery timing.
        os.environ["Q15_V9_OUTBOX_WORKER"] = "false"
        raw = _Raw()
        box = ReliableTelegramOutbox(
            store=None, raw_notifier=raw,
            sqlite_path=os.path.join(tmp, "outbox.sqlite3"),
        )
        return box, raw

    def test_status_by_key_tracks_delivery(self):
        tmp = tempfile.mkdtemp()
        box, raw = self._outbox(tmp)
        self.assertIsNone(box.status_by_key("missing-key"))
        res = box.send_with_result("hello", idempotency_key="k-sent")
        self.assertTrue(res["delivered"])
        self.assertEqual(box.status_by_key("k-sent"), "SENT")

    def test_status_by_key_failed_then_pending(self):
        tmp = tempfile.mkdtemp()
        box, raw = self._outbox(tmp)
        raw.delivers = False
        box.send_with_result("hello", idempotency_key="k-fail")
        # First failed attempt -> retryable, not yet dead.
        self.assertIn(box.status_by_key("k-fail"), ("FAILED_RETRYABLE", "PENDING", "DEAD_LETTER"))


class ReconcileTests(unittest.TestCase):
    def _runner_with_pending(self, tmp, key="v95-official:10M:1700000600"):
        r = ShadowRunner(_cfg(tmp))
        # Generated pick (background row), as record_prediction -> observe would create.
        r.observe(ticker="KXBTC-T", asset="BTC", checkpoint="10M", created_at=1_700_000_000,
                  close_time=1_700_000_600, control_prob_yes=0.6,
                  features={"momentum": 0.1}, quote={"yes_ask_cents": 40})
        # The synchronous send "failed" -> tag PENDING under the report key.
        self.assertTrue(r.mark_native_pending("KXBTC-T", "10M", key))
        return r, key

    def test_worker_delivery_is_credited_sent(self):
        tmp = tempfile.mkdtemp()
        r, key = self._runner_with_pending(tmp)
        # Before reconcile: not counted as sent.
        self.assertEqual(r.delivery_audit()["SENT"], 0)
        # The background worker later delivered the report (outbox row -> SENT).
        out = r.reconcile_native_delivery(lambda k: "SENT" if k == key else None)
        self.assertEqual(out["promoted"], 1)
        self.assertEqual(r.delivery_audit()["SENT"], 1)

    def test_dead_letter_is_marked_failed(self):
        tmp = tempfile.mkdtemp()
        r, key = self._runner_with_pending(tmp)
        out = r.reconcile_native_delivery(lambda k: "DEAD_LETTER")
        self.assertEqual(out["failed"], 1)
        audit = r.delivery_audit()
        self.assertEqual(audit["SENT"], 0)
        self.assertEqual(audit["DELIVERY_FAILED"], 1)

    def test_transient_status_leaves_pending(self):
        tmp = tempfile.mkdtemp()
        r, key = self._runner_with_pending(tmp)
        out = r.reconcile_native_delivery(lambda k: "FAILED_RETRYABLE")
        self.assertEqual(out, {"promoted": 0, "failed": 0})
        # Still pending and still reconcilable on a later cycle when it delivers.
        out2 = r.reconcile_native_delivery(lambda k: "SENT")
        self.assertEqual(out2["promoted"], 1)

    def test_failed_then_delivered_is_recovered(self):
        # A pick marked DELIVERY_FAILED on an earlier dead-letter must still be
        # promoted if the same key later reports SENT (defensive recovery).
        tmp = tempfile.mkdtemp()
        r, key = self._runner_with_pending(tmp)
        r.reconcile_native_delivery(lambda k: "DEAD_LETTER")
        self.assertEqual(r.delivery_audit()["DELIVERY_FAILED"], 1)
        r.reconcile_native_delivery(lambda k: "SENT")
        self.assertEqual(r.delivery_audit()["SENT"], 1)

    def test_sent_row_not_downgraded(self):
        tmp = tempfile.mkdtemp()
        r, key = self._runner_with_pending(tmp)
        r.mark_native_sent("KXBTC-T", "10M")  # a real synchronous delivery
        # A spurious dead-letter lookup must not revoke a real SENT.
        r.reconcile_native_delivery(lambda k: "DEAD_LETTER")
        self.assertEqual(r.delivery_audit()["SENT"], 1)


if __name__ == "__main__":
    unittest.main()
