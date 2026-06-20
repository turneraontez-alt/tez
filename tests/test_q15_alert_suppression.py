from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

import notifier as notifier_mod
from notifier import TelegramNotifier, should_suppress_alert


# Final, rendered headers exactly as the user sees them in Telegram.
NONACTIONABLE = [
    "👀 <b>10M V9.5 CHECK — BEST PICKS, NO ENTRY YET</b>\nbody line",
    "🛑 <b>10M FINAL #1 CANDIDATE — NO TRADE — DOGE NO</b>",
    "🛑 <b>10M FINAL CHECK — NO TRADE</b>",
    "⏳ <b>10M FINAL #1 — WAIT FOR PRICE — BTC YES</b>",
    "🕒 <b>15M EARLY #1 WATCH — ETH NO</b>",
    "👀 <b>WATCH — BTC YES</b>",
]

ACTIONABLE = [
    "✅ <b>10M V9.5 CHECK — ENTRY RECOMMENDED</b>\nbody line",
    "🎯 <b>10M FINAL #1 READY — BTC YES</b>",
    "🟢 <b>ENTRY SIGNAL — BTC YES</b>",
    "🟡 <b>PAPER SIGNAL — ETH NO</b>",
    "⚡ <b>DIP — BTC YES</b>\nask 41¢",
    "⚠️ <b>EXIT / INVALIDATED — BTC</b>",
    "📊 <b>Hourly operational status</b>\nNo new resolved contracts this hour.",
    "🚀 Kalshi 15-Minute Monitor started",
]


class TestShouldSuppressAlert(unittest.TestCase):
    def test_balanced_suppresses_nonactionable(self):
        for msg in NONACTIONABLE:
            self.assertTrue(
                should_suppress_alert(msg, level="balanced"),
                f"expected suppression for: {msg!r}",
            )

    def test_balanced_keeps_actionable(self):
        for msg in ACTIONABLE:
            self.assertFalse(
                should_suppress_alert(msg, level="balanced"),
                f"expected delivery for: {msg!r}",
            )

    def test_level_all_disables_suppression(self):
        for msg in NONACTIONABLE:
            self.assertFalse(should_suppress_alert(msg, level="all"))

    def test_alias_levels_disable_suppression(self):
        for alias in ("off", "none", "full", "verbose", "everything"):
            self.assertFalse(should_suppress_alert(NONACTIONABLE[0], level=alias))

    def test_classifies_on_header_only(self):
        # An actionable header must win even if the body mentions "NO TRADE".
        msg = "✅ <b>10M V9.5 CHECK — ENTRY RECOMMENDED</b>\nDecision: NO_TRADE | blocker: spread"
        self.assertFalse(should_suppress_alert(msg, level="balanced"))
        # A non-actionable header must be muted even if the body mentions "DIP".
        msg2 = "👀 <b>10M V9.5 CHECK — BEST PICKS, NO ENTRY YET</b>\nWatch the DIP later"
        self.assertTrue(should_suppress_alert(msg2, level="balanced"))

    def test_empty_message_not_suppressed(self):
        self.assertFalse(should_suppress_alert("", level="balanced"))
        self.assertFalse(should_suppress_alert(None, level="balanced"))

    def test_env_default_is_balanced(self):
        prev = os.environ.pop("Q15_ALERT_LEVEL", None)
        try:
            self.assertTrue(should_suppress_alert(NONACTIONABLE[0]))
        finally:
            if prev is not None:
                os.environ["Q15_ALERT_LEVEL"] = prev


class _Resp:
    status_code = 200
    text = "ok"


class TestSendSuppression(unittest.TestCase):
    def setUp(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
        os.environ["TELEGRAM_CHAT_ID"] = "test-chat"
        os.environ["Q15_ALERT_LEVEL"] = "balanced"
        self.posted = []
        self._orig_post = notifier_mod.requests.post
        notifier_mod.requests.post = lambda *a, **k: (self.posted.append((a, k)) or _Resp())

    def tearDown(self):
        notifier_mod.requests.post = self._orig_post
        os.environ.pop("Q15_ALERT_LEVEL", None)

    def test_nonactionable_is_not_posted_but_reported_delivered(self):
        n = TelegramNotifier()
        self.assertTrue(n.enabled)
        # "V9.5 CHECK" header => augment passthrough (no _LATEST_* needed).
        result = n.send("👀 <b>10M V9.5 CHECK — BEST PICKS, NO ENTRY YET</b>")
        self.assertTrue(result)  # treated as durably handled, no retry
        self.assertEqual(self.posted, [])  # nothing sent to Telegram
        self.assertEqual(n.sent_count, 0)

    def test_actionable_is_posted(self):
        n = TelegramNotifier()
        result = n.send("✅ <b>10M V9.5 CHECK — ENTRY RECOMMENDED</b>")
        self.assertTrue(result)
        self.assertEqual(len(self.posted), 1)
        self.assertEqual(n.sent_count, 1)

    def test_level_all_posts_everything(self):
        os.environ["Q15_ALERT_LEVEL"] = "all"
        n = TelegramNotifier()
        result = n.send("👀 <b>10M V9.5 CHECK — BEST PICKS, NO ENTRY YET</b>")
        self.assertTrue(result)
        self.assertEqual(len(self.posted), 1)


if __name__ == "__main__":
    unittest.main()
