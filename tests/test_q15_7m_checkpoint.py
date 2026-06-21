from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.window_focus import FocusSettings, TwoWindowFocusManager
from q15_upgrade.checkpoint_v93 import _format_checkpoint
from q15_upgrade.checkpoint_v94 import format_telegram_message


def full_plan(status: str, side_win_probability: float) -> dict:
    """A complete matched-candidate trade plan (the alert formatter assumes a
    READY/WAIT plan always carries price/TP fields)."""
    return {
        "status": status,
        "side_win_probability": side_win_probability,
        "current_executable_ask": 41.0,
        "ideal_entry_zone": [38.5, 42.0],
        "absolute_maximum_entry_price": 44.0,
        "net_edge_after_costs_cents": 6.2,
        "primary_take_profit": 62.0,
        "stretch_take_profit": 80.0,
        "contract_price_stop_reference": 28.0,
        "reward_risk": 1.8,
        "management_mode": "HOLD_TO_CLOSE_OR_STRETCH_TP",
        "management_explanation": "Strong setup supports holding.",
        "blockers": [],
    }


class EndProbabilityFormatterTest(unittest.TestCase):
    """The 15m / 10m / 7m checkpoint alerts must show the overall end
    probability of YES and NO (which sum to 100%)."""

    def setUp(self):
        self.mgr = TwoWindowFocusManager()

    def test_15m_alert_shows_yes_and_no_end_probability(self):
        row = {
            "asset": "BTC",
            "side": "YES",
            "prediction": {"side_probability": 0.72, "yes_probability": 0.72, "confidence": 0.8},
            "trade_plan": {},
        }
        text = self.mgr._format_checkpoint_alert("15m", row)
        self.assertIn("15M EARLY #1 WATCH", text)
        self.assertIn("Overall end probability — YES: <b>72.0%</b> | NO: <b>28.0%</b>", text)

    def test_10m_alert_shows_yes_and_no_for_no_side(self):
        plan = full_plan("READY", 0.64)
        row = {"asset": "ETH", "side": "NO", "combined_confidence": 0.8, "trade_plan": plan}
        text = self.mgr._format_checkpoint_alert("10m", row)
        self.assertIn("10M FINAL #1 READY", text)
        # side=NO carrying side_win_probability 0.64 => YES end probability 36%.
        self.assertIn("Overall end probability — YES: <b>36.0%</b> | NO: <b>64.0%</b>", text)

    def test_7m_alert_uses_7m_labels_and_end_probability(self):
        plan = full_plan("WAIT_FOR_BETTER_PRICE", 0.61)
        row = {"asset": "SOL", "side": "YES", "combined_confidence": 0.7, "trade_plan": plan}
        text = self.mgr._format_checkpoint_alert("7m", row)
        self.assertIn("7M FINAL #1 — WAIT FOR PRICE", text)
        self.assertNotIn("10M FINAL", text)
        self.assertIn("Overall end probability — YES: <b>61.0%</b> | NO: <b>39.0%</b>", text)

    def test_7m_no_trade_title(self):
        text = self.mgr._format_checkpoint_alert("7m", None, [])
        self.assertIn("7M FINAL CHECK — NO TRADE", text)

    def test_7m_no_trade_mirrors_10m_late_strong_shadow_note(self):
        diagnostics = [{
            "asset": "BTC",
            "candidate_side": "NO",
            "reasons": ["15m uncertain"],
            "late_strong_shadow": {"state": "LATE_STRONG_SHADOW"},
        }]
        seven = self.mgr._format_checkpoint_alert("7m", None, diagnostics)
        ten = self.mgr._format_checkpoint_alert("10m", None, diagnostics)
        self.assertIn("Shadow note:", seven)
        self.assertIn("Shadow note:", ten)


class SevenMinuteNotifyTest(unittest.TestCase):
    """_maybe_notify must emit a distinct, claim-gated 7m alert at ~420s,
    reusing the frozen final (10m) ranking."""

    def test_seven_minute_block_claims_and_sends(self):
        class Store:
            enabled = False

        class Notifier:
            enabled = True

            def __init__(self):
                self.messages = []

            def send(self, msg):
                self.messages.append(msg)
                return True

        notifier = Notifier()
        # Two-window checkpoint alerts are OFF by default now; this test exercises
        # the 7M send path, so enable it explicitly.
        mgr = TwoWindowFocusManager(Store(), notifier, None, None,
                                    FocusSettings(checkpoint_alerts_enabled=True))
        claimed = set()
        mgr._claim = lambda key: claimed.add(key) or True  # type: ignore[assignment]
        mgr._rankings["k"] = {
            "final": [{"asset": "BTC", "side": "YES", "combined_confidence": 0.8,
                       "trade_plan": full_plan("READY", 0.7)}],
            "final_diagnostics": [],
            "early_15m": [],
            "early_diagnostics": [],
        }
        mgr._maybe_notify("k", {"BTC": {"seconds_remaining": 410}}, 0.0)
        self.assertIn("q15-two-window:7m:k", claimed)
        self.assertTrue(any("Overall end probability" in m for m in notifier.messages))


class SevenMinuteRoutingTest(unittest.TestCase):
    def test_v93_recognizes_7m_check(self):
        text = _format_checkpoint("🛑 7M FINAL CHECK — NO TRADE\n1. BTC YES\nRejected: warmup")
        self.assertIn("7M CHECK", text)
        self.assertNotIn("10M CHECK", text)
        self.assertIn("Overall end probability", text)

    def test_v94_routes_7m_through_checkpoint_formatter(self):
        out = format_telegram_message("🛑 7M FINAL CHECK — NO TRADE\n1. BTC YES\nRejected: warmup")
        self.assertIn("7M CHECK", out)


class SevenMinuteSettingsTest(unittest.TestCase):
    def test_settings_default_7m_seconds(self):
        s = FocusSettings()
        self.assertEqual(s.alert_7_at_seconds, 419)
        self.assertGreater(s.alert_10_at_seconds, s.alert_7_at_seconds)


if __name__ == "__main__":
    unittest.main()
