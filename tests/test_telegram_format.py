"""Unit tests for the shared Telegram presentation layer (telegram_format.py).

These verify the formatting *contract* every Telegram builder relies on:
  * percentages, prices, cents and countdowns use consistent precision;
  * missing / NaN / wrong-typed inputs degrade to an explicit placeholder
    instead of raising or rendering "None";
  * freshness and divergence treat a missing signal as NOT trusted;
  * timestamps are timezone-aware and labelled, using an injected clock so the
    test never depends on wall-clock time;
  * dynamic text is HTML-escaped;
  * over-long messages are clamped under Telegram's 4096-char limit.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

import telegram_format as tf  # noqa: E402


class TestNumericFormatting(unittest.TestCase):
    def test_format_pct_fraction_to_percent(self):
        self.assertEqual(tf.format_pct(0.625, 1), "62.5%")
        self.assertEqual(tf.format_pct(0.5), "50%")
        self.assertEqual(tf.format_pct(1.0), "100%")

    def test_format_pct_missing_is_placeholder_not_crash(self):
        self.assertEqual(tf.format_pct(None), "n/a")
        self.assertEqual(tf.format_pct(float("nan")), "n/a")
        self.assertEqual(tf.format_pct("0.5"), "n/a")
        # bool must not be treated as a number (isinstance(True, int) is True).
        self.assertEqual(tf.format_pct(True), "n/a")

    def test_format_cents_signed_and_unsigned(self):
        self.assertEqual(tf.format_cents(53), "53¢")
        self.assertEqual(tf.format_cents(1.8, 1, signed=True), "+1.8¢")
        self.assertEqual(tf.format_cents(-2.0, 1, signed=True), "-2.0¢")
        self.assertEqual(tf.format_cents(None), "–")

    def test_format_price_precision_by_magnitude(self):
        self.assertEqual(tf.format_price(63250.5), "$63,250.50")
        self.assertEqual(tf.format_price(0.6231), "$0.6231")  # sub-$10 -> 4dp
        self.assertEqual(tf.format_price(None), "–")

    def test_format_countdown_and_age(self):
        self.assertEqual(tf.format_countdown(540), "9m 00s")
        self.assertEqual(tf.format_countdown(65), "1m 05s")
        self.assertEqual(tf.format_countdown(3725), "1h 02m")
        self.assertEqual(tf.format_countdown(-5), "0m 00s")  # clamps to zero
        self.assertEqual(tf.format_countdown(None), "–")
        self.assertEqual(tf.format_age(3), "3s")
        self.assertEqual(tf.format_age(125), "2m 05s")
        self.assertEqual(tf.format_age(None), "?")


class TestFreshness(unittest.TestCase):
    def test_fresh_aging_stale_thresholds(self):
        label, stale = tf.freshness(3)
        self.assertIn("fresh", label)
        self.assertFalse(stale)
        label, stale = tf.freshness(8)
        self.assertIn("aging", label)
        self.assertFalse(stale)
        label, stale = tf.freshness(20)
        self.assertIn("STALE", label)
        self.assertTrue(stale)

    def test_missing_age_is_not_trusted(self):
        # An absent freshness signal must never read as silently fresh.
        label, stale = tf.freshness(None)
        self.assertEqual(label, "unknown")
        self.assertTrue(stale)


class TestDivergence(unittest.TestCase):
    def test_agreement_and_divergence_in_bps(self):
        text, diverged = tf.divergence(63250, 63270)
        self.assertIn("agree", text)
        self.assertFalse(diverged)
        text, diverged = tf.divergence(63250, 63600)
        self.assertIn("diverge", text)
        self.assertTrue(diverged)

    def test_custom_labels_and_threshold(self):
        text, _ = tf.divergence(100, 101, label_a="CB", label_b="OK")
        self.assertTrue(text.startswith("CB/OK"))

    def test_half_up_feed_never_false_alarms(self):
        # Missing or non-positive prices -> n/a, not a divergence alarm.
        self.assertEqual(tf.divergence(None, 100), ("n/a", False))
        self.assertEqual(tf.divergence(100, 0), ("n/a", False))
        self.assertEqual(tf.divergence(-1, 100), ("n/a", False))


class TestTimestamps(unittest.TestCase):
    def test_timestamp_is_timezone_labelled(self):
        # A fixed UTC instant rendered in US Eastern, with the zone name present.
        epoch = 1719000000  # 2024-06-21 20:00:00 UTC
        out = tf.format_timestamp(epoch)
        self.assertRegex(out, r"\d{2}:\d{2}:\d{2} [A-Z]{2,4}$")

    def test_naive_datetime_assumed_utc(self):
        naive = datetime(2024, 6, 21, 20, 0, 0)  # no tzinfo
        aware = datetime(2024, 6, 21, 20, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(tf.format_timestamp(naive), tf.format_timestamp(aware))

    def test_now_label_uses_injected_clock(self):
        fixed = datetime(2024, 6, 21, 20, 0, 0, tzinfo=timezone.utc)
        out = tf.now_label(clock=lambda: fixed)
        self.assertEqual(out, tf.format_timestamp(fixed))

    def test_with_date_and_12h(self):
        epoch = 1719000000
        self.assertTrue(tf.format_timestamp(epoch, with_date=True).startswith("2024-06-21 "))
        self.assertIn("PM", tf.format_timestamp(epoch, clock_24h=False))

    def test_missing_timestamp_placeholder(self):
        self.assertEqual(tf.format_timestamp(None), "n/a")


class TestEscapingAndLength(unittest.TestCase):
    def test_escape_html(self):
        self.assertEqual(tf.escape_html("<b>&\"x'"), "&lt;b&gt;&amp;&quot;x&#x27;")
        self.assertEqual(tf.escape_html(None), "")

    def test_clamp_for_telegram(self):
        long = "x" * 5000
        out = tf.clamp_for_telegram(long)
        self.assertLessEqual(len(out), 4096)
        self.assertTrue(out.endswith("(truncated)"))
        # A short message is returned untouched.
        self.assertEqual(tf.clamp_for_telegram("short"), "short")


if __name__ == "__main__":
    unittest.main()
