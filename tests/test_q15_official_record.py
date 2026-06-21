"""Phase 1 foundation for the OFFICIAL RECORD rule.

Only a prediction that was actually DELIVERED to the owner before the contract
closed may enter the official win/loss record. These lock the two primitives:

1. ``Notifier.send_with_result`` reports delivered/muted/failed + the Telegram
   message_id, so a mute (handled but not sent) is never mistaken for a delivery.
2. ``V95Ledger.record_sent_prediction`` is an immutable, sent-before-close gate,
   and ``official_scoreboard`` grades each stored call against the settled result
   without ever editing the stored prediction — split by interval / YES-NO /
   entry / manipulation, with Wilson-backed low-sample flags.
"""
import tempfile
import time
import unittest
from contextlib import closing

from q15_upgrade.ledger_v95 import V95Ledger


def _mk_ledger():
    led = V95Ledger(tempfile.mktemp(suffix=".sqlite3"))
    led._cache_enabled = True
    return led


def _resolve(led, ticker, checkpoint, side, official, *, asset="ETH", raw=0.6):
    led.record_prediction(
        ticker=ticker, asset=asset, checkpoint=checkpoint, created_at=time.time(),
        close_time=time.time() + 600, predicted_side=side,
        raw_yes_probability=raw, calibrated_yes_probability=raw,
        challenger_yes_probability=raw, baseline_yes_probability=0.5,
        selected_probability=raw, conservative_probability=max(0.01, raw - 0.05),
        data_quality=0.8, evidence_quality=0.7, trade_quality=0.7,
        trade_decision="ENTRY_RECOMMENDED", regime="NORMAL",
        features={"momentum": 0.3}, contributions={"momentum": 0.2},
        quote={"ask_cents": 50}, rank=1,
    )
    led.resolve_ticker(ticker, official)


def _send(led, ticker, interval, side, *, record_type="interval", mid=111,
          sent_at=1000.0, close_time=2000.0, manip=None, entry_decision=None):
    return led.record_sent_prediction(
        contract_id=ticker, asset="ETH", interval=interval, record_type=record_type,
        predicted_side=side, probability=0.7, manipulation_probability=manip,
        entry_decision=entry_decision, sent_at=sent_at, close_time=close_time,
        message_id=mid,
    )


class RecordSentPredictionGateTest(unittest.TestCase):
    def test_rejects_when_no_message_id(self):
        led = _mk_ledger()
        self.assertFalse(_send(led, "C1", "10M", "YES", mid=None))

    def test_rejects_when_sent_at_or_after_close(self):
        led = _mk_ledger()
        self.assertFalse(_send(led, "C1", "10M", "YES", sent_at=2000.0, close_time=2000.0))
        self.assertFalse(_send(led, "C1", "10M", "YES", sent_at=2500.0, close_time=2000.0))

    def test_accepts_when_delivered_before_close(self):
        led = _mk_ledger()
        self.assertTrue(_send(led, "C1", "10M", "YES", sent_at=1000.0, close_time=2000.0))

    def test_is_immutable_insert_only(self):
        led = _mk_ledger()
        self.assertTrue(_send(led, "C1", "10M", "YES", mid=5))
        # Same (contract, interval, type, message_id) is ignored, never overwritten.
        self.assertTrue(_send(led, "C1", "10M", "NO", mid=5))  # method returns True (insert OR ignore)
        with closing(led._connect()) as conn:
            rows = list(conn.execute(
                "SELECT predicted_side FROM sent_predictions WHERE contract_id='C1'"))
        self.assertEqual([str(r["predicted_side"]) for r in rows], ["YES"])  # original kept


class OfficialScoreboardTest(unittest.TestCase):
    def test_only_sent_and_settled_rows_count(self):
        led = _mk_ledger()
        # Three resolved contracts; only two were sent (delivered).
        _resolve(led, "A", "10M", "YES", "YES")          # sent, correct
        _resolve(led, "B", "10M", "NO", "YES")           # sent, wrong
        _resolve(led, "C", "10M", "YES", "YES")          # analyzed, NOT sent
        _send(led, "A", "10M", "YES", mid=1)
        _send(led, "B", "10M", "NO", mid=2)
        board = led.official_scoreboard()
        total = board["10M"]["total"]
        self.assertEqual(total["n"], 2, "unsent C must not appear in the official record")
        self.assertEqual((total["right"], total["wrong"]), (1, 1))

    def test_yes_no_split_and_grading(self):
        led = _mk_ledger()
        _resolve(led, "Y1", "7M", "YES", "YES")   # YES correct
        _resolve(led, "Y2", "7M", "YES", "NO")    # YES wrong
        _resolve(led, "N1", "7M", "NO", "NO")     # NO correct
        for t, s, m in (("Y1", "YES", 1), ("Y2", "YES", 2), ("N1", "NO", 3)):
            _send(led, t, "7M", s, mid=m)
        board = led.official_scoreboard()["7M"]
        self.assertEqual((board["yes"]["right"], board["yes"]["wrong"]), (1, 1))
        self.assertEqual((board["no"]["right"], board["no"]["wrong"]), (1, 0))
        self.assertEqual(board["total"]["n"], 3)

    def test_entry_and_interval_are_separate_categories(self):
        led = _mk_ledger()
        _resolve(led, "E1", "10M", "YES", "YES")
        # Same contract recorded both as an interval call and an entry delivery.
        _send(led, "E1", "10M", "YES", record_type="interval", mid=1)
        _send(led, "E1", "10M", "YES", record_type="entry", mid=2,
              entry_decision="ENTRY_RECOMMENDED")
        board = led.official_scoreboard()
        self.assertEqual(board["10M"]["total"]["n"], 1)
        self.assertEqual(board["entry"]["total"]["n"], 1)

    def test_low_sample_flagged(self):
        led = _mk_ledger()
        _resolve(led, "A", "10M", "YES", "YES")
        _send(led, "A", "10M", "YES", mid=1)
        board = led.official_scoreboard()
        self.assertTrue(board["10M"]["total"]["low_n"], "a 1-sample bucket must flag low_n")

    def test_manipulation_record_graded_against_outcome(self):
        led = _mk_ledger()
        # Manipulation call predicts post-event direction; grade vs the settlement.
        _resolve(led, "M1", "10M", "YES", "YES")
        _resolve(led, "M2", "10M", "NO", "YES")
        _send(led, "M1", "10M", "YES", record_type="manipulation", mid=1, manip=0.68)
        _send(led, "M2", "10M", "NO", record_type="manipulation", mid=2, manip=0.71)
        manip = led.official_scoreboard()["manipulation"]
        self.assertEqual((manip["right"], manip["wrong"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
