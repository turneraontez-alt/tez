"""Tests for the strict flip-decision engine, net-edge gate, rank-by-skill, the
reset system, and the embedded FLIP CHECK panel output.

The engine is observational and must be statistically honest: leak-free grading
(graded only against settlement), chronological out-of-sample thresholds that are
never tuned on the rows they are judged on, a YES decision only when the interval
threshold is validated to the target precision, one decision per interval per
contract, and a full reset that recalculates from post-reset data only.
"""
from __future__ import annotations

import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from q15_upgrade import checkpoint_v95, flip_decision
from q15_upgrade.ledger_v95 import CHAMPION_WEIGHTS, V95Ledger
from notifications import panels_v95

NOW = 1_700_000_000.0


# --------------------------------------------------------------------------- #
# flip probability — oriented, bounded, manipulation does not force YES
# --------------------------------------------------------------------------- #
class FlipProbabilityTests(unittest.TestCase):
    def test_opposing_manipulation_raises_more_than_supporting(self):
        base = {"prediction_side": "YES", "yes_probability": 0.7, "seconds_remaining": 300}
        opposing = dict(base, flip_risk={"score": 80.0}, manipulation={"direction_after": "NO"})
        supporting = dict(base, flip_risk={"score": 80.0}, manipulation={"direction_after": "YES"})
        p_opp = flip_decision.flip_probability(opposing, "10M")["probability"]
        p_sup = flip_decision.flip_probability(supporting, "10M")["probability"]
        self.assertGreater(p_opp, p_sup)

    def test_high_manipulation_does_not_force_certainty(self):
        # A high manip score with manipulation that SUPPORTS the side must not
        # produce a high flip probability.
        a = {"prediction_side": "NO", "yes_probability": 0.2, "seconds_remaining": 200,
             "flip_risk": {"score": 95.0}, "manipulation": {"direction_after": "NO"}}
        p = flip_decision.flip_probability(a, "7M")["probability"]
        self.assertLess(p, 0.5)

    def test_missing_inputs_are_neutral_and_bounded(self):
        p = flip_decision.flip_probability({"prediction_side": "YES"}, "10M")["probability"]
        self.assertGreaterEqual(p, 0.0)
        self.assertLessEqual(p, 1.0)
        self.assertAlmostEqual(p, 0.5, delta=0.15)


# --------------------------------------------------------------------------- #
# threshold selection — chronological OOS, leak-free, precision-targeted
# --------------------------------------------------------------------------- #
def _cfg(**kw):
    base = dict(enabled=True, target_precision=0.80, train_fraction=0.70,
                min_total=40, min_yes_train=5, min_yes_test=5, time_seconds=600.0)
    base.update(kw)
    return flip_decision.FlipConfig(**base)


class ThresholdSelectionTests(unittest.TestCase):
    def _rows(self, n, high_flips=True, test_high_flips=True, split=0.70):
        rows = []
        cut = int(round(split * n))
        for i in range(n):
            high = (i % 2 == 0)
            prob = 0.85 if high else 0.30
            in_test = i >= cut
            flips = (test_high_flips if in_test else high_flips) if high else False
            rows.append({"created_at": float(i), "flip_probability": prob, "flipped": bool(flips)})
        return rows

    def test_selects_precise_threshold_and_validates(self):
        sel = flip_decision.select_threshold(self._rows(80), _cfg())
        self.assertTrue(sel["validated"])
        self.assertGreater(sel["threshold"], 0.30)
        self.assertLessEqual(sel["threshold"], 0.85)
        self.assertGreaterEqual(sel["test"]["yes_precision"], 0.80)

    def test_not_validated_when_test_fold_fails(self):
        # Train pattern: high prob -> flip. Test pattern: high prob -> NO flip.
        sel = flip_decision.select_threshold(
            self._rows(80, high_flips=True, test_high_flips=False), _cfg())
        self.assertFalse(sel["validated"])
        self.assertGreater(sel["threshold"], 1.0)   # sentinel -> decision stays NO

    def test_insufficient_rows_not_validated(self):
        sel = flip_decision.select_threshold(self._rows(20), _cfg(min_total=40))
        self.assertFalse(sel["validated"])
        self.assertEqual(sel["reason"], "insufficient_rows")


# --------------------------------------------------------------------------- #
# ledger: record (one per interval), grade on settlement, reset, post-reset only
# --------------------------------------------------------------------------- #
class FlipLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _ledger(self):
        return V95Ledger(Path(self.temp.name) / f"flip_{id(self)}.sqlite3")

    def test_one_decision_per_contract_interval(self):
        led = self._ledger()
        led.record_flip_decision(contract="KX-A", checkpoint="10M", asset="BTC",
                                 predicted_side="YES", flip_probability=0.6, threshold=0.5,
                                 decision="YES", validated=True, created_at=NOW, close_time=NOW + 600)
        # second write for same (contract, interval) is ignored
        led.record_flip_decision(contract="KX-A", checkpoint="10M", asset="BTC",
                                 predicted_side="NO", flip_probability=0.1, threshold=0.5,
                                 decision="NO", validated=True, created_at=NOW + 5, close_time=NOW + 600)
        rows = led.flip_decision_rows("10M", resolved_only=False)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision"], "YES")  # first write wins

    def test_grading_uses_settlement_only(self):
        led = self._ledger()
        # Pick predicted YES; a "flip" means it settles NO.
        led.record_flip_decision(contract="KX-A", checkpoint="10M", asset="BTC",
                                 predicted_side="YES", flip_probability=0.9, threshold=0.5,
                                 decision="YES", validated=True, created_at=NOW, close_time=NOW + 600)
        led.record_flip_decision(contract="KX-B", checkpoint="10M", asset="ETH",
                                 predicted_side="YES", flip_probability=0.1, threshold=0.5,
                                 decision="NO", validated=True, created_at=NOW, close_time=NOW + 600)
        led.resolve_ticker("KX-A", "NO", NOW + 700)   # YES -> NO = a true flip; decision YES correct
        led.resolve_ticker("KX-B", "YES", NOW + 700)  # YES -> YES = no flip; decision NO correct
        rows = {r["asset"]: r for r in led.flip_decision_rows("10M", resolved_only=True)}
        self.assertTrue(rows["BTC"]["flipped"])
        self.assertTrue(rows["BTC"]["correct"])
        self.assertFalse(rows["ETH"]["flipped"])
        self.assertTrue(rows["ETH"]["correct"])

    def test_reset_archives_and_excludes_pre_reset(self):
        led = self._ledger()
        led.record_flip_decision(contract="OLD", checkpoint="10M", asset="BTC",
                                 predicted_side="YES", flip_probability=0.6, threshold=0.5,
                                 decision="YES", validated=True, created_at=NOW, close_time=NOW + 600)
        led.resolve_ticker("OLD", "NO", NOW + 700)
        self.assertEqual(len(led.flip_decision_rows("10M", resolved_only=True)), 1)
        res = led.reset_flip_data(now=NOW + 1000)
        self.assertTrue(res["reset"])
        self.assertEqual(res["archived"], 1)
        # active table is now empty (post-reset only); archive holds the old row
        self.assertEqual(led.flip_decision_rows("10M", resolved_only=False, post_reset_only=True), [])
        status = led.flip_reset_status()
        self.assertEqual(status["active_decisions"], 0)
        self.assertEqual(status["archived_decisions"], 1)
        self.assertEqual(status["reset_at"], NOW + 1000)
        # a fresh post-reset decision is collected from here forward
        led.record_flip_decision(contract="NEW", checkpoint="10M", asset="ETH",
                                 predicted_side="YES", flip_probability=0.6, threshold=0.5,
                                 decision="YES", validated=True, created_at=NOW + 2000, close_time=NOW + 2600)
        self.assertEqual(len(led.flip_decision_rows("10M", resolved_only=False)), 1)

    def test_report_shape(self):
        led = self._ledger()
        rep = led.flip_decision_report()
        self.assertIn("by_interval", rep)
        for cp in ("15M", "10M", "7M"):
            self.assertIn(cp, rep["by_interval"])
        self.assertIn("all_intervals_validated", rep)


# --------------------------------------------------------------------------- #
# net-edge gate (default OFF) + rank-by-skill (default OFF)
# --------------------------------------------------------------------------- #
class GateAndRankTests(unittest.TestCase):
    def test_net_edge_gate_off_by_default(self):
        a = {"trade_decision": "ENTRY_RECOMMENDED", "net_edge_cents": -3.0}
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("Q15_V95_NET_EDGE_GATE_ENABLED", None)
            self.assertTrue(checkpoint_v95._is_actionable_entry(a))

    def test_net_edge_gate_blocks_thin_edge_when_on(self):
        thin = {"trade_decision": "ENTRY_RECOMMENDED", "net_edge_cents": -3.0}
        fat = {"trade_decision": "ENTRY_RECOMMENDED", "net_edge_cents": 5.0}
        with mock.patch.dict(os.environ, {"Q15_V95_NET_EDGE_GATE_ENABLED": "1",
                                          "Q15_V95_NET_EDGE_GATE_MIN_CENTS": "1.0"}):
            self.assertFalse(checkpoint_v95._is_actionable_entry(thin))
            self.assertTrue(checkpoint_v95._is_actionable_entry(fat))

    def test_rank_by_skill_promotes_high_grade_over_high_edge(self):
        analyses = {
            "BTC": {"prediction_side": "YES", "trade_decision": "WATCH_PRICE",
                    "confidence_grade": "D", "selected_probability": 0.55,
                    "net_edge_cents": 9.0, "conservative_probability": 0.55, "data_quality": 0.9},
            "ETH": {"prediction_side": "NO", "trade_decision": "WATCH_PRICE",
                    "confidence_grade": "A", "selected_probability": 0.88,
                    "net_edge_cents": 1.0, "conservative_probability": 0.85, "data_quality": 0.9},
        }
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("Q15_V95_RANK_BY_SKILL", None)
            default_rank = checkpoint_v95.rank_analyses(analyses)
        self.assertEqual(default_rank[0]["asset"], "BTC")  # net-edge first by default
        with mock.patch.dict(os.environ, {"Q15_V95_RANK_BY_SKILL": "1"}):
            skill_rank = checkpoint_v95.rank_analyses(analyses)
        self.assertEqual(skill_rank[0]["asset"], "ETH")    # A-grade now #1


# --------------------------------------------------------------------------- #
# panel output — strict FLIP CHECK only; no loose flip/manip lines
# --------------------------------------------------------------------------- #
class PanelTests(unittest.TestCase):
    def _pick(self, **kw):
        base = dict(rank=1, asset="BTC", side="YES", confidence=0.72, is_entry=False,
                    entry_label="SKIP", best_entry_max=55, main_reason="x", sample=120,
                    flip_decision="NO", flip_decision_probability=0.41, flip_decision_threshold=0.62)
        base.update(kw)
        return base

    def test_flip_check_block_rendered(self):
        panel = panels_v95.build_ranked_checkpoint_panel(checkpoint="10M", picks=[self._pick()])
        self.assertIn("10M FLIP CHECK", panel)
        self.assertIn("Decision: NO", panel)
        self.assertIn("Flip Probability: 41%", panel)
        self.assertIn("Required Threshold: 62%", panel)
        # the loose legacy lines are gone (manipulation math hidden)
        self.assertNotIn("Flip risk:", panel)
        self.assertNotIn("Manipulation:", panel)

    def test_unvalidated_threshold_renders_dash(self):
        panel = panels_v95.build_ranked_checkpoint_panel(
            checkpoint="7M", picks=[self._pick(flip_decision_threshold=1.01)])
        self.assertIn("Required Threshold: —", panel)

    def test_decision_yes_rendered(self):
        panel = panels_v95.build_ranked_checkpoint_panel(
            checkpoint="15M", picks=[self._pick(flip_decision="YES", flip_decision_probability=0.7,
                                                flip_decision_threshold=0.62)])
        self.assertIn("Decision: YES", panel)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
