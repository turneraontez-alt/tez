"""Ranked official-report panel + report-frequency lock (prompt sections 1 & 2).

Covers: the 0-100 shadow ENTRY SCORE formula, per-pick extraction, the Top-3
panel formatter (all fields, missing rank -> em-dash, suppression markers), and
the ledger's one-report-per-(interval, window) lock.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade import panels_v95
from q15_upgrade.checkpoint_v95 import (
    _build_ranked_picks, _entry_score, _extract_pick,
)


def _analysis(*, side="YES", yes=0.72, sel=0.72, edge=6.0, ideal=44.0, ask=43.0,
              wick=0.30, momentum=0.20, flow=0.10, decision="ENTRY_RECOMMENDED",
              available=True, flip_score=None, flip_dir=None, manip_score=0.0,
              cal_rows=None):
    return {
        "prediction_available": available,
        "prediction_side": side,
        "yes_probability": yes,
        "selected_probability": sel,
        "net_edge_cents": edge,
        "ideal_entry_cents": ideal,
        "trade_decision": decision,
        "feature_values": {"wick": wick, "momentum": momentum, "flow": flow},
        "quote": {"ask_cents": ask},
        "manipulation": {"suspected": False, "reasons": [], "score": manip_score},
        "flip_risk": ({} if flip_score is None else
                      {"score": flip_score, "direction_monitored": flip_dir}),
        "calibration": ({} if cal_rows is None else {"rows": cal_rows}),
    }


class EntryScoreTest(unittest.TestCase):
    def test_none_when_unavailable(self):
        self.assertIsNone(_entry_score(_analysis(available=False)))
        self.assertIsNone(_entry_score({"prediction_available": True}))  # no selected prob

    def test_bounded_0_100(self):
        s = _entry_score(_analysis(sel=1.0, edge=100.0, wick=1.0, momentum=1.0))
        self.assertLessEqual(s, 100.0)
        self.assertGreaterEqual(s, 0.0)

    def test_deterministic_weighted_components(self):
        # dir-conf=1 (sel=1) ->30, edge=clamp(10/10)=1 ->25, wick=0, mom=0,
        # manip=1 (no suspicion) ->10  => 65
        a = _analysis(sel=1.0, edge=10.0, wick=0.0, momentum=0.0)
        self.assertAlmostEqual(_entry_score(a), 65.0, places=3)
        # stronger wick + momentum lifts it
        b = _analysis(sel=1.0, edge=10.0, wick=1.0, momentum=1.0)
        self.assertAlmostEqual(_entry_score(b), 100.0, places=3)

    def test_negative_edge_floors_edge_component(self):
        a = _analysis(sel=0.5, edge=-5.0, wick=0.0, momentum=0.0)  # dir-conf 0, edge 0
        self.assertAlmostEqual(_entry_score(a), 10.0, places=3)  # only manip term


class ExtractPickTest(unittest.TestCase):
    def test_all_fields_yes(self):
        p = _extract_pick(1, "BTC", _analysis(side="YES", yes=0.72))
        self.assertEqual(p["rank"], 1)
        self.assertEqual(p["asset"], "BTC")
        self.assertEqual(p["side"], "YES")
        self.assertAlmostEqual(p["confidence"], 0.72)
        self.assertAlmostEqual(p["yes_prob"], 0.72)
        self.assertAlmostEqual(p["no_prob"], 0.28)
        self.assertIsNotNone(p["entry_score"])
        self.assertEqual(p["price_cents"], 43.0)
        self.assertEqual(p["max_cents"], 44.0)
        self.assertEqual(p["edge_cents"], 6.0)
        self.assertTrue(p["is_entry"])
        self.assertIn("supportive", p["wick_status"])

    def test_confidence_oriented_to_no_side(self):
        p = _extract_pick(2, "SOL", _analysis(side="NO", yes=0.30))
        self.assertAlmostEqual(p["confidence"], 0.70)  # confidence on the NO side
        self.assertFalse(p["is_entry"] is None)


class BuildRankedPicksTest(unittest.TestCase):
    def test_top_3_in_ranking_order_skips_unavailable(self):
        analyses = {
            "BTC": _analysis(side="YES"),
            "SOL": _analysis(side="NO", decision="PREDICTION_ONLY"),
            "XRP": _analysis(available=False),       # skipped
            "ETH": _analysis(side="YES", decision="WATCH_PRICE"),
        }
        ranking = [{"asset": a} for a in ("BTC", "SOL", "XRP", "ETH")]
        picks = _build_ranked_picks(analyses, ranking, top_k=3)
        self.assertEqual([p["asset"] for p in picks], ["BTC", "SOL", "ETH"])
        self.assertEqual([p["rank"] for p in picks], [1, 2, 3])

    def test_fewer_than_three_left_short(self):
        analyses = {"BTC": _analysis()}
        picks = _build_ranked_picks(analyses, [{"asset": "BTC"}], top_k=3)
        self.assertEqual(len(picks), 1)


class RankedPanelFormatTest(unittest.TestCase):
    def test_compact_picks_and_decision_block(self):
        picks = [
            _extract_pick(1, "SOL", _analysis(side="NO", yes=0.28, decision="WATCH_PRICE",
                                              manip_score=0.43, cal_rows=42)),
            _extract_pick(2, "BTC", _analysis(side="YES", yes=0.66, decision="ENTRY_RECOMMENDED")),
            _extract_pick(3, "XRP", _analysis(side="NO", yes=0.39, decision="PREDICTION_ONLY")),
        ]
        msg = panels_v95.build_ranked_checkpoint_panel(checkpoint="10M", picks=picks)
        # marker preservation (suppression + per-checkpoint level key)
        self.assertIn("V9.5 CHECK", msg)
        self.assertIn("10M CHECK", msg)
        self.assertIn("ENTRY RECOMMENDED", msg)  # BTC is an entry -> actionable
        self.assertEqual(msg.count("<pre>"), 1)
        # three compact ranked lines: medal · asset · side · settle confidence
        for medal in ("🥇", "🥈", "🥉"):
            self.assertIn(medal, msg)
        self.assertIn("🥇 SOL NO — 72%", msg)   # confidence is on the predicted (NO) side
        self.assertIn("🥈 BTC YES — 66%", msg)
        # one decision block keyed to the headline (#1 = SOL)
        for label in ("Flip risk:", "Manipulation:", "Entry:", "Best entry:",
                      "Main reason:", "Sample:"):
            self.assertIn(label, msg)
        self.assertIn("Entry: WAIT", msg)        # SOL decision WATCH_PRICE -> WAIT
        self.assertIn("Manipulation: 43%", msg)
        self.assertIn("Best entry: ≤44¢", msg)
        self.assertIn("Sample: 42", msg)
        # the verbose per-pick dump is gone
        for gone in ("P(Yes)", "Entry score:", "Wick/PA:", "Flow/Mom:", "Decision:"):
            self.assertNotIn(gone, msg)

    def test_flip_arrow_only_when_high_and_opposite(self):
        # low flip risk -> bare percent, no arrow
        low = [_extract_pick(1, "BTC", _analysis(side="YES", decision="PREDICTION_ONLY",
                                                 flip_score=20.0, flip_dir="YES → NO"))]
        self.assertIn("Flip risk: 20%", panels_v95.build_ranked_checkpoint_panel(
            checkpoint="15M", picks=low))
        self.assertNotIn("→", panels_v95.build_ranked_checkpoint_panel(checkpoint="15M", picks=low))
        # high flip risk toward the opposite side -> arrow shows the genuine target
        high = [_extract_pick(1, "BTC", _analysis(side="YES", decision="PREDICTION_ONLY",
                                                  flip_score=64.0, flip_dir="YES → NO"))]
        msg = panels_v95.build_ranked_checkpoint_panel(checkpoint="15M", picks=high)
        self.assertIn("Flip risk: 64% → NO", msg)

    def test_manipulation_wait_flag_when_high(self):
        picks = [_extract_pick(1, "BTC", _analysis(side="YES", decision="WATCH_PRICE",
                                                   manip_score=0.71))]
        msg = panels_v95.build_ranked_checkpoint_panel(checkpoint="7M", picks=picks)
        self.assertIn("Manipulation: 71% — WAIT", msg)

    def test_missing_rank_renders_dash_not_invented(self):
        picks = [_extract_pick(1, "BTC", _analysis(decision="PREDICTION_ONLY"))]
        msg = panels_v95.build_ranked_checkpoint_panel(checkpoint="7M", picks=picks)
        self.assertIn("🥈 —", msg)
        self.assertIn("🥉 —", msg)

    def test_no_entry_marker_when_nothing_recommended(self):
        picks = [_extract_pick(1, "BTC", _analysis(decision="PREDICTION_ONLY"))]
        msg = panels_v95.build_ranked_checkpoint_panel(checkpoint="15M", picks=picks)
        self.assertIn("NO ENTRY YET", msg)
        self.assertNotIn("ENTRY RECOMMENDED", msg)
        self.assertIn("Entry: SKIP", msg)        # PREDICTION_ONLY -> SKIP

    def test_sample_dash_when_no_calibration(self):
        picks = [_extract_pick(1, "BTC", _analysis(decision="PREDICTION_ONLY"))]
        msg = panels_v95.build_ranked_checkpoint_panel(checkpoint="15M", picks=picks)
        self.assertIn("Sample: —", msg)


class ReportLockTest(unittest.TestCase):
    def _ledger(self):
        from q15_upgrade.ledger_v95 import V95Ledger
        tmp = tempfile.mkdtemp()
        os.environ["Q15_V95_LEDGER_DB"] = os.path.join(tmp, "lock.sqlite3")
        return V95Ledger()

    def test_one_report_per_interval_window(self):
        led = self._ledger()
        close = 1_700_000_900.0
        now = close - 300
        self.assertFalse(led.report_locked("10M", close, now))
        self.assertTrue(led.lock_official_report("10M", close, now, message_id=11))
        self.assertTrue(led.report_locked("10M", close, now))
        # second claim for the same window is rejected (locked -> no resend)
        self.assertFalse(led.lock_official_report("10M", close, now, message_id=22))

    def test_unlock_allows_retry(self):
        led = self._ledger()
        close, now = 1_700_000_900.0, 1_700_000_600.0
        self.assertTrue(led.lock_official_report("7M", close, now, message_id=None))
        led.unlock_official_report("7M", close, now)
        self.assertFalse(led.report_locked("7M", close, now))
        self.assertTrue(led.lock_official_report("7M", close, now, message_id=5))  # re-claimable

    def test_different_windows_and_intervals_independent(self):
        led = self._ledger()
        now = 1_700_000_600.0
        self.assertTrue(led.lock_official_report("10M", 1_700_000_900.0, now, message_id=1))
        # different interval, same window -> independent
        self.assertTrue(led.lock_official_report("15M", 1_700_000_900.0, now, message_id=2))
        # same interval, next 15-min window -> independent
        self.assertTrue(led.lock_official_report("10M", 1_700_001_800.0, now, message_id=3))


if __name__ == "__main__":
    unittest.main()
