"""Two alert fixes: the one-follow-up-per-interval check, and the BEST ENTRY
top/detail consistency guard.

The follow-up is a single post-entry re-check (hold / take-profit / avoid /
exit) armed only when an entry was recommended, fired exactly once per contract
and interval. The best-entry guard ensures the asset shown at the top of the
alert is always rank #1 of the detailed ranking — never a different, merely
higher-confidence asset.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade import checkpoint_v95 as cp
from q15_upgrade.ledger_v95 import V95Ledger


def _a(side="NO", entry=True, prob=0.72, edge=8.0, decision="ENTRY_RECOMMENDED",
       cons=0.7, dq=0.8, flip=0.0, ideal=58.0):
    return {
        "prediction_side": side, "entry_allowed": entry, "prediction_available": True,
        "selected_probability": prob, "conservative_probability": cons, "data_quality": dq,
        "trade_decision": decision, "net_edge_cents": edge, "ideal_entry_cents": ideal,
        "yes_probability": prob if side == "YES" else 1 - prob,
        "no_probability": 1 - prob if side == "YES" else prob,
        "confidence_grade": "A", "regime": {"name": "NORMAL"},
        "quote": {"ask_cents": 55}, "flip_risk": {"score": flip},
    }


class TestBestEntryConsistency(unittest.TestCase):
    def test_best_entry_is_rank_one_qualifier_not_highest_confidence(self):
        # BNB has higher confidence but BTC has the stronger executable trade score
        # (bigger net edge) and is the recommended entry -> BTC must be best entry.
        analyses = {
            "BTC": _a(side="NO", prob=0.724, edge=8.6, ideal=58.0),
            "BNB": _a(side="YES", prob=0.965, edge=2.0, ideal=83.0),
        }
        ranking = cp.rank_analyses(analyses)
        self.assertEqual(ranking[0]["asset"], "BTC")        # rank #1 by trade score
        best = cp._best_entry(analyses, ranking)
        self.assertEqual(best[0], "BTC")
        self.assertTrue(cp._best_entry_consistent(analyses, ranking))
        msg = build = cp.build_v95_message("10M", analyses, ranking, {})
        self.assertIn("🏆 BEST ENTRY — BTC NO", msg)
        # The detail table lists BTC first too (same ranking, one source of truth).
        self.assertLess(msg.index("BTC"), msg.index("BNB"))

    def test_only_recommended_entries_qualify(self):
        analyses = {
            "BTC": _a(side="NO", entry=False, decision="WATCH_PRICE", edge=1.0),
            "ETH": _a(side="YES", entry=True, decision="ENTRY_RECOMMENDED", edge=5.0),
        }
        ranking = cp.rank_analyses(analyses)
        best = cp._best_entry(analyses, ranking)
        self.assertEqual(best[0], "ETH")               # the only qualifying entry
        self.assertTrue(cp._best_entry_consistent(analyses, ranking))

    def test_no_entry_shows_no_entry_recommended(self):
        analyses = {"BTC": _a(entry=False, decision="WATCH_CONFIDENCE")}
        ranking = cp.rank_analyses(analyses)
        self.assertIsNone(cp._best_entry(analyses, ranking))
        self.assertTrue(cp._best_entry_consistent(analyses, ranking))  # vacuously
        msg = cp.build_v95_message("15M", analyses, ranking, {})
        self.assertIn("NO ENTRY RECOMMENDED", msg)
        self.assertNotIn("🏆 BEST ENTRY", msg)

    def test_followup_remaining_flag_renders(self):
        analyses = {"BTC": _a(side="NO")}
        ranking = cp.rank_analyses(analyses)
        yes = cp.build_v95_message("10M", analyses, ranking, {}, followup_remaining=True)
        no = cp.build_v95_message("10M", analyses, ranking, {}, followup_remaining=False)
        self.assertIn("Follow-up check remaining: YES", yes)
        self.assertIn("Follow-up check remaining: NO", no)


class TestFollowupVerdict(unittest.TestCase):
    def test_side_change_is_reversal(self):
        tag, advice = cp._followup_verdict("NO", _a(side="YES"))
        self.assertEqual(tag, "REVERSAL")
        self.assertIn("SIDE CHANGED", advice)

    def test_entry_gone_is_exit(self):
        tag, advice = cp._followup_verdict("NO", _a(side="NO", entry=False, decision="WATCH_PRICE"))
        self.assertEqual(tag, "EXIT")
        self.assertIn("no longer valid", advice)

    def test_still_valid_is_hold(self):
        tag, advice = cp._followup_verdict("NO", _a(side="NO", entry=True, flip=0.0))
        self.assertEqual(tag, "HOLD")
        self.assertIn("HOLD", advice)

    def test_high_flip_risk_suggests_take_profit(self):
        tag, advice = cp._followup_verdict("NO", _a(side="NO", entry=True, flip=90.0))
        self.assertEqual(tag, "HOLD")
        self.assertIn("taking profit", advice)

    def test_missing_contract_is_exit(self):
        tag, advice = cp._followup_verdict("NO", None)
        self.assertEqual(tag, "EXIT")

    def test_message_header_is_actionable(self):
        msg = cp.build_followup_message("10M", "BTC", "NO", _a(side="NO"))
        self.assertIn("FOLLOW-UP — BTC 10M", msg)
        self.assertIn("Recommended side: NO", msg)
        # The header carries an actionable marker so the notifier won't mute it.
        import notifier
        self.assertFalse(notifier.should_suppress_alert(msg, level="balanced"))


class TestFollowupLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.led = V95Ledger(os.path.join(self.tmp.name, "l.sqlite3"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_followup_armed_and_consumed_once(self):
        now = 1000.0
        self.assertTrue(self.led.arm_entry_followup(
            ticker="t1", checkpoint="10M", asset="BTC", side="NO", now=now, delay=120))
        # Re-arming the same contract+interval is a no-op (one follow-up only).
        self.assertFalse(self.led.arm_entry_followup(
            ticker="t1", checkpoint="10M", asset="BTC", side="NO", now=now + 5, delay=120))
        # Not due yet.
        self.assertEqual(self.led.due_followups(now + 60), [])
        self.assertFalse(self.led.followup_already_sent("t1", "10M"))
        # Due after the delay; fire and consume.
        due = self.led.due_followups(now + 121)
        self.assertEqual([d["ticker"] for d in due], ["t1"])
        self.assertTrue(self.led.mark_followup_sent("t1", "10M", now + 121))
        self.assertTrue(self.led.followup_already_sent("t1", "10M"))
        # Never fires again.
        self.assertEqual(self.led.due_followups(now + 200), [])
        self.assertFalse(self.led.mark_followup_sent("t1", "10M", now + 200))

    def test_intervals_tracked_independently(self):
        now = 1000.0
        self.led.arm_entry_followup(ticker="t1", checkpoint="15M", asset="BTC", side="NO", now=now, delay=120)
        self.led.arm_entry_followup(ticker="t1", checkpoint="10M", asset="BTC", side="NO", now=now, delay=120)
        # Sending the 15M follow-up must not consume the 10M one.
        self.led.mark_followup_sent("t1", "15M", now + 121)
        self.assertTrue(self.led.followup_already_sent("t1", "15M"))
        self.assertFalse(self.led.followup_already_sent("t1", "10M"))
        self.assertEqual([d["checkpoint"] for d in self.led.due_followups(now + 121)], ["10M"])


if __name__ == "__main__":
    unittest.main()
