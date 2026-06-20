from __future__ import annotations

import unittest

from q15_upgrade.window_focus import TwoWindowFocusManager


class AlertHtmlEscapingTest(unittest.TestCase):
    """Telegram checkpoint alerts are sent with parse_mode=HTML. Rejection /
    diagnostic reason strings contain '<', '>', and '&' (e.g. "consensus 64% <
    72%"). If those are not escaped, Telegram rejects the whole message with
    HTTP 400 'can't parse entities' and the 15m/10m check is never delivered.
    These tests lock in escaping while preserving the intended <b>/<i> markup.
    """

    def setUp(self):
        self.mgr = TwoWindowFocusManager()

    def test_no_trade_reason_with_less_than_is_escaped(self):
        diagnostics = [{
            "asset": "BTC",
            "candidate_side": "NO",
            "side_probability": 0.53,
            "reasons": ["consensus 64% < 72%", "side estimate 53.0% < 58%"],
        }]
        text = self.mgr._format_checkpoint_alert("10m", None, diagnostics)
        self.assertIn("&lt;", text)
        # A raw "% < " would be parsed by Telegram as the start of a tag.
        self.assertNotIn("% < ", text)

    def test_reason_ampersand_and_greater_than_escaped(self):
        diagnostics = [{
            "asset": "ETH",
            "candidate_side": "YES",
            "reasons": ["spread & depth > limit"],
        }]
        text = self.mgr._format_checkpoint_alert("15m", None, diagnostics)
        self.assertIn("&amp;", text)
        self.assertIn("&gt;", text)
        self.assertNotIn(" & ", text)
        self.assertNotIn(" > ", text)

    def test_intended_markup_is_preserved(self):
        diagnostics = [{"asset": "SOL", "candidate_side": "NO", "reasons": ["x"]}]
        text = self.mgr._format_checkpoint_alert("10m", None, diagnostics)
        self.assertIn("<b>", text)
        self.assertIn("</b>", text)
        self.assertIn("<i>", text)
        self.assertIn("</i>", text)

    def test_ready_plan_message_has_no_unescaped_operators(self):
        plan = {
            "status": "READY",
            "side_win_probability": 0.64,
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
        row = {"asset": "ETH", "side": "YES", "combined_confidence": 0.81, "trade_plan": plan}
        text = self.mgr._format_checkpoint_alert("10m", row)
        self.assertIn("10M FINAL #1 READY", text)
        self.assertNotIn(" < ", text)


if __name__ == "__main__":
    unittest.main()
