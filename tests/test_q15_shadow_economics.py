from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade import shadow_economics as se


class GateTest(unittest.TestCase):
    def test_control_replicates_production_price_gate(self):
        # net_edge = 0.62*100 - 55 - 0 = 7 >= 6 (10M required) -> enter.
        d = se.control_gate(0.62, 55.0, 0.0, 6.0)
        self.assertTrue(d.enter)
        self.assertAlmostEqual(d.net_edge_cents, 7.0)
        # 0.62*100 - 58 - 0 = 4 < 6 -> no enter.
        self.assertFalse(se.control_gate(0.62, 58.0, 0.0, 6.0).enter)

    def test_no_executable_ask_blocks_both(self):
        self.assertFalse(se.control_gate(0.7, None, 0.0, 6.0).enter)
        self.assertFalse(se.shadow_gate(0.7, None, 0.0, 6.0, se.EconConfig()).enter)

    def test_shadow_stricter_extra_costs_trim_marginal_trade(self):
        cfg = se.EconConfig(slippage_cents=0.5, adverse_selection_cents=0.25,
                            no_trade_zone_cents=1.0, risk_aversion_k=0.0)
        # net_edge live = 6.0 exactly (enters live). Shadow charges +0.75 -> 5.25,
        # still >= 6? No: 5.25 < 6 -> shadow declines this marginal trade.
        live = se.control_gate(0.61, 55.0, 0.0, 6.0)   # 61-55 = 6.0
        shadow = se.shadow_gate(0.61, 55.0, 0.0, 6.0, cfg)
        self.assertTrue(live.enter)
        self.assertFalse(shadow.enter)
        self.assertEqual(shadow.reason, "edge_below_required")

    def test_shadow_risk_adjustment_penalises_low_confidence(self):
        # Big raw edge but low confidence (high sigma) can fail the risk-adj gate.
        cfg = se.EconConfig(slippage_cents=0.0, adverse_selection_cents=0.0,
                            no_trade_zone_cents=0.0, risk_aversion_k=0.20)
        # prob 0.55 -> sigma ~49.7 -> penalty ~9.9; net_edge = 55-45 = 10; risk_adj ~0.1
        d_low = se.shadow_gate(0.55, 45.0, 0.0, 6.0, cfg)
        # prob 0.75 -> sigma ~43.3 -> penalty ~8.66; net_edge = 75-65 = 10; risk_adj ~1.3
        d_high = se.shadow_gate(0.75, 65.0, 0.0, 6.0, cfg)
        self.assertGreater(d_high.risk_adjusted_ev, d_low.risk_adjusted_ev)

    def test_no_trade_zone_floor(self):
        cfg = se.EconConfig(slippage_cents=0.0, adverse_selection_cents=0.0,
                            no_trade_zone_cents=5.0, risk_aversion_k=0.0)
        # net_edge 3 >= required 2 but below the 5c floor -> blocked.
        d = se.shadow_gate(0.53, 50.0, 0.0, 2.0, cfg)
        self.assertFalse(d.enter)
        self.assertEqual(d.reason, "inside_no_trade_zone")


class CompareTest(unittest.TestCase):
    def _rows(self):
        # Three settled 10M trades (required edge 6c). Build so the live gate enters
        # all three but the shadow declines the marginal loser.
        return [
            # strong winner: live & shadow both enter, +44c
            {"checkpoint": "10M", "prob": 0.70, "ask_cents": 55.0, "cost_cents": 0.0,
             "correct": True, "realized_cents": 45.0},
            # strong-ish winner: both enter, +43c
            {"checkpoint": "10M", "prob": 0.68, "ask_cents": 56.0, "cost_cents": 0.0,
             "correct": True, "realized_cents": 44.0},
            # marginal loser: live enters (edge exactly 6), shadow declines -> live -56c
            {"checkpoint": "10M", "prob": 0.62, "ask_cents": 56.0, "cost_cents": 0.0,
             "correct": False, "realized_cents": -56.0},
        ]

    def test_shadow_skips_marginal_loser_and_beats_live(self):
        cfg = se.EconConfig(slippage_cents=0.5, adverse_selection_cents=0.25,
                            no_trade_zone_cents=1.0, risk_aversion_k=0.0)
        cmp = se.compare(self._rows(), cfg)
        self.assertEqual(cmp.rows_considered, 3)
        self.assertEqual(cmp.control.n_entries, 3)     # live entered all
        self.assertEqual(cmp.shadow.n_entries, 2)      # shadow skipped the marginal loser
        # Shadow avoided the -56c loss, so it is ahead on P&L despite its extra costs.
        self.assertGreater(cmp.pnl_delta_cents, 0)
        # Shadow P&L is charged the extra modelled cents per entry (no free lunch).
        self.assertAlmostEqual(cmp.shadow.total_pnl_cents, (45.0 - 0.75) + (44.0 - 0.75))

    def test_rows_missing_inputs_are_skipped(self):
        rows = [
            {"checkpoint": "10M", "prob": 0.7, "ask_cents": None, "cost_cents": 0.0,
             "correct": True, "realized_cents": 40.0},  # no ask -> skipped
            {"checkpoint": "10M", "prob": 0.7, "ask_cents": 55.0, "cost_cents": 0.0,
             "correct": True, "realized_cents": None},   # no realized -> skipped
        ]
        cmp = se.compare(rows, se.EconConfig())
        self.assertEqual(cmp.rows_considered, 0)

    def test_report_lines_render(self):
        cmp = se.compare(self._rows(), se.EconConfig())
        text = "\n".join(se.build_report_lines(cmp))
        self.assertIn("ENTRY-ECONOMICS SHADOW", text)
        self.assertIn("Live", text)
        self.assertIn("Shadow", text)
        self.assertIn("read-only", text.lower())


if __name__ == "__main__":
    unittest.main()
