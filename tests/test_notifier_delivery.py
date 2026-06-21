"""Telegram delivery robustness: timeouts, network errors, rate limits, bad
responses — and that none of them crash the caller or leak the bot token.

The notifier deliberately distinguishes "handled" (delivered OR muted -> the
outbox marks it done, no retry) from "failed" (ok=False -> the caller may retry).
These tests pin that contract for the failure modes the task calls out, using a
fake `requests` so nothing touches the network or wall clock.
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

import notifier as notifier_mod  # noqa: E402

# An actionable header so suppression never mutes these (we are testing the
# transport, not the verbosity gate).
MSG = "✅ <b>10M V9.5 CHECK · ENTRY RECOMMENDED</b>\nbody"


def _notifier():
    n = notifier_mod.TelegramNotifier()
    n.token = "secret-bot-token"
    n.chat_id = "chat"
    n.enabled = True
    return n


class _FakeResp:
    def __init__(self, status_code, payload=None, text="ok"):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _patch_requests:
    """Context manager swapping notifier.requests for a fake post()."""

    def __init__(self, post):
        self._post = post

    def __enter__(self):
        self._orig = notifier_mod.requests

        class _R:
            @staticmethod
            def post(url, json=None, timeout=None):
                return self._post(url, json, timeout)

        # Surface the real exception types the code catches.
        _R.RequestException = self._orig.RequestException
        notifier_mod.requests = _R
        return self

    def __exit__(self, *exc):
        notifier_mod.requests = self._orig
        return False


class TestDelivery(unittest.TestCase):
    def test_success_extracts_message_id(self):
        def post(url, body, timeout):
            self.assertEqual(timeout, 8)  # bounded, never blocks the loop forever
            self.assertEqual(body["parse_mode"], "HTML")
            return _FakeResp(200, {"result": {"message_id": 4242}})

        n = _notifier()
        with _patch_requests(post):
            res = n.send_with_result(MSG)
        self.assertTrue(res["ok"] and res["delivered"])
        self.assertEqual(res["message_id"], 4242)
        self.assertEqual(n.last_message_id, 4242)

    def test_rate_limited_429_is_failure_not_silent_drop(self):
        # A 429/5xx returns ok=False so the caller's retry path engages; it is
        # explicitly NOT marked delivered or muted.
        def post(url, body, timeout):
            return _FakeResp(429, text="Too Many Requests: retry after 5")

        n = _notifier()
        with _patch_requests(post):
            res = n.send_with_result(MSG)
        self.assertFalse(res["ok"])
        self.assertFalse(res["delivered"])
        self.assertFalse(res["muted"])
        self.assertIsNone(res["message_id"])

    def test_network_timeout_is_caught_and_reported(self):
        import requests

        def post(url, body, timeout):
            raise requests.exceptions.Timeout("timed out")

        n = _notifier()
        with _patch_requests(post):
            res = n.send_with_result(MSG)
        self.assertFalse(res["ok"])
        self.assertIsNone(res["message_id"])

    def test_invalid_json_body_does_not_crash(self):
        # 200 but an unparseable body -> still "delivered", message_id None.
        def post(url, body, timeout):
            return _FakeResp(200, payload=None)

        n = _notifier()
        with _patch_requests(post):
            res = n.send_with_result(MSG)
        self.assertTrue(res["delivered"])
        self.assertIsNone(res["message_id"])

    def test_token_never_leaks_into_last_error(self):
        import requests

        def post(url, body, timeout):
            raise requests.exceptions.ConnectionError(
                "failed to reach https://api.telegram.org/botsecret-bot-token/sendMessage"
            )

        n = _notifier()
        with _patch_requests(post):
            n.send_with_result(MSG)
        self.assertIsNotNone(n.last_error)
        self.assertNotIn("secret-bot-token", n.last_error)
        self.assertIn("***", n.last_error)

    def test_disabled_notifier_reports_unhandled(self):
        n = notifier_mod.TelegramNotifier()
        n.enabled = False
        res = n.send_with_result(MSG)
        self.assertFalse(res["ok"])
        self.assertFalse(res["delivered"])


class TestSuppressionDedup(unittest.TestCase):
    def test_routine_watch_is_muted_under_balanced(self):
        # A non-actionable header is muted (handled, not delivered) so routine
        # checks do not spam the chat — the existing dedup policy.
        muted = notifier_mod.should_suppress_alert(
            "👀 <b>10M V9.5 CHECK · NO ENTRY YET</b>\nbody", level="balanced"
        )
        self.assertTrue(muted)

    def test_actionable_entry_always_delivered(self):
        self.assertFalse(
            notifier_mod.should_suppress_alert(
                "✅ <b>10M V9.5 CHECK · ENTRY RECOMMENDED</b>", level="balanced"
            )
        )


if __name__ == "__main__":
    unittest.main()
