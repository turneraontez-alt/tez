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
    # Startup placeholder emitted before the first v9.5 cycle populates globals;
    # re-opens on every restart, carries no decision -> mute under balanced.
    "🟡 Q15 V9.5 STARTUP — canonical analysis is not ready; legacy report suppressed. Check /api/q15-v9-5/diagnostics.",
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


class TestPerCheckpointAlertLevel(unittest.TestCase):
    """Q15_ALERT_LEVEL_10M / _7M / _15M override the global level per checkpoint."""

    # Routine "NO ENTRY YET" checks per checkpoint (muted under global balanced).
    CHECK_10M = "👀 <b>10M V9.5 CHECK · NO ENTRY YET</b>\nbody"
    CHECK_7M = "👀 <b>7M V9.5 CHECK · NO ENTRY YET</b>\nbody"
    CHECK_15M = "👀 <b>15M V9.5 CHECK · NO ENTRY YET</b>\nbody"

    def setUp(self):
        # Clean slate: global balanced, no per-checkpoint overrides.
        self._saved = {k: os.environ.pop(k, None) for k in (
            "Q15_ALERT_LEVEL", "Q15_ALERT_LEVEL_10M",
            "Q15_ALERT_LEVEL_7M", "Q15_ALERT_LEVEL_15M",
        )}
        os.environ["Q15_ALERT_LEVEL"] = "balanced"

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_default_inherits_global_balanced(self):
        # No override set -> 10m/7m checks stay muted exactly as before.
        self.assertTrue(should_suppress_alert(self.CHECK_10M))
        self.assertTrue(should_suppress_alert(self.CHECK_7M))
        self.assertTrue(should_suppress_alert(self.CHECK_15M))

    def test_10m_override_delivers_only_10m(self):
        os.environ["Q15_ALERT_LEVEL_10M"] = "all"
        self.assertFalse(should_suppress_alert(self.CHECK_10M))  # delivered
        self.assertTrue(should_suppress_alert(self.CHECK_7M))    # still muted
        self.assertTrue(should_suppress_alert(self.CHECK_15M))   # still muted

    def test_7m_override_delivers_only_7m(self):
        os.environ["Q15_ALERT_LEVEL_7M"] = "all"
        self.assertFalse(should_suppress_alert(self.CHECK_7M))
        self.assertTrue(should_suppress_alert(self.CHECK_10M))
        self.assertTrue(should_suppress_alert(self.CHECK_15M))

    def test_10m_and_7m_overrides_together(self):
        os.environ["Q15_ALERT_LEVEL_10M"] = "all"
        os.environ["Q15_ALERT_LEVEL_7M"] = "all"
        self.assertFalse(should_suppress_alert(self.CHECK_10M))
        self.assertFalse(should_suppress_alert(self.CHECK_7M))
        self.assertTrue(should_suppress_alert(self.CHECK_15M))  # 15m untouched

    def test_override_can_also_mute_a_checkpoint(self):
        # Global says deliver-all, but a per-checkpoint override re-mutes 15m.
        os.environ["Q15_ALERT_LEVEL"] = "all"
        os.environ["Q15_ALERT_LEVEL_15M"] = "balanced"
        self.assertFalse(should_suppress_alert(self.CHECK_10M))  # global all
        self.assertTrue(should_suppress_alert(self.CHECK_15M))   # re-muted

    def test_override_never_overrides_actionable_header(self):
        # An ENTRY header is delivered regardless; muting an override can't drop it.
        os.environ["Q15_ALERT_LEVEL_10M"] = "balanced"
        self.assertFalse(should_suppress_alert("✅ <b>10M V9.5 CHECK · ENTRY RECOMMENDED</b>"))

    def test_non_checkpoint_alert_ignores_overrides(self):
        # A dip alert carries no checkpoint label -> keeps the global level.
        os.environ["Q15_ALERT_LEVEL_10M"] = "balanced"
        os.environ["Q15_ALERT_LEVEL"] = "all"
        self.assertFalse(should_suppress_alert("⚡ <b>DIP — BTC YES</b>\nask 41¢"))

    def test_explicit_level_arg_bypasses_overrides(self):
        # Passing level= explicitly is honoured as-is (used by callers/tests).
        os.environ["Q15_ALERT_LEVEL_10M"] = "all"
        self.assertTrue(should_suppress_alert(self.CHECK_10M, level="balanced"))


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


class TestOfficialIntervalReportAlwaysDelivers(unittest.TestCase):
    """The official per-interval report (the 'TOP 3 PICKS' ranked check) must be
    delivered every interval — even with NO ENTRY — so the visible record and the
    Shadow-vs-Yours comparison fill. Routine checks stay muted as before."""

    # An official NO-ENTRY report carries both 'TOP 3 PICKS' and 'NO ENTRY YET'.
    OFFICIAL = "🔎 <b>V9.5 CHECK — 15M · TOP 3 PICKS · NO ENTRY YET</b>\nbody"
    ROUTINE = "👀 <b>15M V9.5 CHECK · NO ENTRY YET</b>\nbody"

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in (
            "Q15_ALERT_LEVEL", "Q15_V95_RANKED_REPORT_ALWAYS_DELIVER",
        )}
        os.environ["Q15_ALERT_LEVEL"] = "balanced"

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_official_report_delivers_under_balanced(self):
        self.assertFalse(should_suppress_alert(self.OFFICIAL))   # delivered
        self.assertTrue(should_suppress_alert(self.ROUTINE))     # routine still muted

    def test_flag_off_restores_muting(self):
        os.environ["Q15_V95_RANKED_REPORT_ALWAYS_DELIVER"] = "false"
        self.assertTrue(should_suppress_alert(self.OFFICIAL))     # muted again (rollback)

    def test_entry_official_report_still_delivers(self):
        msg = "🔎 <b>V9.5 CHECK — 10M · TOP 3 PICKS · ENTRY RECOMMENDED</b>\nbody"
        self.assertFalse(should_suppress_alert(msg))


if __name__ == "__main__":
    unittest.main()
