"""Notifier.send_with_result must report exactly what happened to a message so the
official record can prove a real delivery (not a mute) and capture the Telegram
message_id. send() stays a bool wrapper for every existing caller."""
import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

import notifications.notifier as notif


class _Resp:
    def __init__(self, status_code=200, message_id=4242):
        self.status_code = status_code
        self._mid = message_id
        self.text = "ok"

    def json(self):
        return {"ok": True, "result": {"message_id": self._mid}}


def _enabled_notifier():
    n = notif.TelegramNotifier()
    n.enabled = True
    n.token = "t"
    n.chat_id = "c"
    return n


class SendWithResultTest(unittest.TestCase):
    def test_delivered_captures_message_id(self):
        n = _enabled_notifier()
        with patch.object(notif, "should_suppress_alert", return_value=False), \
             patch.object(notif, "augment_telegram_message", side_effect=lambda t: t), \
             patch.object(notif, "professionalize_telegram_message", side_effect=lambda t: t), \
             patch.object(notif.requests, "post", return_value=_Resp(200, 4242)):
            res = n.send_with_result("hi")
        self.assertEqual(res, {"ok": True, "delivered": True, "muted": False, "message_id": 4242})
        self.assertEqual(n.last_message_id, 4242)

    def test_muted_is_handled_but_not_delivered(self):
        n = _enabled_notifier()
        with patch.object(notif, "should_suppress_alert", return_value=True):
            res = n.send_with_result("hi")
        self.assertTrue(res["ok"])          # handled -> no retry
        self.assertFalse(res["delivered"])  # but NOT an official delivery
        self.assertTrue(res["muted"])
        self.assertIsNone(res["message_id"])
        self.assertIsNone(n.last_message_id)

    def test_failed_send(self):
        n = _enabled_notifier()
        with patch.object(notif, "should_suppress_alert", return_value=False), \
             patch.object(notif, "augment_telegram_message", side_effect=lambda t: t), \
             patch.object(notif, "professionalize_telegram_message", side_effect=lambda t: t), \
             patch.object(notif.requests, "post", return_value=_Resp(400)):
            res = n.send_with_result("hi")
        self.assertEqual(res["ok"], False)
        self.assertFalse(res["delivered"])
        self.assertIsNone(res["message_id"])

    def test_disabled(self):
        n = notif.TelegramNotifier()
        n.enabled = False
        res = n.send_with_result("hi")
        self.assertEqual(res, {"ok": False, "delivered": False, "muted": False, "message_id": None})

    def test_send_bool_wrapper_matches_ok(self):
        n = _enabled_notifier()
        with patch.object(notif, "should_suppress_alert", return_value=True):
            self.assertIs(n.send("hi"), True)          # muted -> handled -> True
        with patch.object(notif, "should_suppress_alert", return_value=False), \
             patch.object(notif, "augment_telegram_message", side_effect=lambda t: t), \
             patch.object(notif, "professionalize_telegram_message", side_effect=lambda t: t), \
             patch.object(notif.requests, "post", return_value=_Resp(500)):
            self.assertIs(n.send("hi"), False)         # failed -> False


if __name__ == "__main__":
    unittest.main()
