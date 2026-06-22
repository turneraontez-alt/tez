"""Regression tests for the Shadow-vs-Your-System native ("Your System") grading.

The reported bug: a settled prediction (the 5:30 DOGE) showed as a CORRECT guess in
the challenger comparison report when production had recorded it WRONG. Root cause:
the report re-derived the native side from the RAW champion probability
(``control_prob_yes = raw_yes_probability``), but production's sent ``predicted_side``
comes from the CALIBRATED probability. Raw and calibrated land on opposite sides of
0.5 in ~34% of rows, so a real loss could read as a win.

These tests pin the fix: the native side is graded and ranked from production's
CAPTURED decision (``native_predicted_side`` / ``native_prob_yes`` / ``native_rank``),
matching production exactly, with a documented fallback to the old raw behaviour for
legacy rows that predate decision capture.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade import challenger as ch
from q15_upgrade.challenger.config import ChallengerConfig
from q15_upgrade.challenger.runner import ShadowRunner

MV = "challenger-test"


def _snapshot():
    return {
        "spot": 65000.0, "target": 65050.0, "seconds_remaining": 600,
        "volatility_per_min": 0.004, "data_quality": 0.8,
        "yes_bid": 49.0, "yes_ask": 51.0, "no_bid": 49.0, "no_ask": 51.0,
        "depth_contracts": 50,
        "feature_values": {"momentum": 0.1, "flow": -0.05, "book": 0.0},
    }


def _pred():
    cfg = ChallengerConfig()
    return ch.ShadowPredictor(cfg, ch.make_model(cfg)).predict(_snapshot())


class NativeSideGradingTest(unittest.TestCase):
    def _ledger(self):
        d = tempfile.mkdtemp()
        return ch.ShadowLedger(os.path.join(d, "shadow.sqlite3"))

    def _record_native(self, led, contract, *, control_prob_yes, native_side,
                       native_prob_yes, native_rank, asset="DOGE", checkpoint="15M",
                       close_time=1_700_000_900.0, sent=True):
        """Record one paired row with an explicit production decision, mark it sent."""
        led.record(_pred(), asset=asset, contract=contract, checkpoint=checkpoint,
                   control_prob_yes=control_prob_yes,
                   native_predicted_side=native_side, native_prob_yes=native_prob_yes,
                   native_rank=native_rank, close_time=close_time, model_version=MV)
        if sent:
            led.mark_native_sent(contract, checkpoint, model_version=MV)

    def test_loss_does_not_show_as_correct_the_530_doge(self):
        """The exact reported case: raw says YES, the CALIBRATED sent side was NO,
        official result YES → production graded it WRONG; the report must too."""
        led = self._ledger()
        # raw 0.5215 -> YES ; calibrated 0.4958 -> NO (production sent NO) ; result YES
        self._record_native(led, "KXDOGE15M-26JUN210530-30",
                            control_prob_yes=0.5215, native_side="NO",
                            native_prob_yes=0.4958, native_rank=1)
        led.resolve("KXDOGE15M-26JUN210530-30", "15M", "YES", model_version=MV)

        rbc = led.ranked_by_checkpoint(model_version=MV, top_k=3)
        nat = rbc["by_checkpoint"]["15M"]["native"]["rank1"]
        self.assertEqual((nat["correct"], nat["wrong"]), (0, 1),
                         "native NO vs official YES must be graded WRONG, not correct")

        # The old (buggy) behaviour would have graded raw YES == YES -> correct.
        comp = led.comparison(model_version=MV)
        self.assertEqual(comp["overall"]["current_accuracy"], 0.0)

        end = led.latest_window_end_results(model_version=MV, checkpoints=("15M",))
        doge = next(a for a in end["assets"] if a["asset"] == "DOGE")
        side, correct = doge["checkpoints"]["15M"]["native"]
        self.assertEqual(side, "NO")
        self.assertFalse(correct)
        led.close()

    def test_win_not_shown_as_loss_inverse(self):
        """Inverse: raw says NO but the sent side was YES and the result is YES →
        production graded it CORRECT; the report must agree (no false ✗)."""
        led = self._ledger()
        self._record_native(led, "C-INV", control_prob_yes=0.44, native_side="YES",
                            native_prob_yes=0.56, native_rank=1)
        led.resolve("C-INV", "15M", "YES", model_version=MV)
        rbc = led.ranked_by_checkpoint(model_version=MV, top_k=3)
        nat = rbc["by_checkpoint"]["15M"]["native"]["rank1"]
        self.assertEqual((nat["correct"], nat["wrong"]), (1, 0))
        led.close()

    def test_legacy_row_falls_back_to_raw(self):
        """A row recorded before decision capture (no native_predicted_side) keeps
        the historical behaviour: side derived from control_prob_yes >= 0.5."""
        led = self._ledger()
        led.record(_pred(), asset="DOGE", contract="C-LEGACY", checkpoint="15M",
                   control_prob_yes=0.52, close_time=1_700_000_900.0, model_version=MV)
        led.mark_native_sent("C-LEGACY", "15M", model_version=MV)
        led.resolve("C-LEGACY", "15M", "YES", model_version=MV)
        rbc = led.ranked_by_checkpoint(model_version=MV, top_k=3)
        nat = rbc["by_checkpoint"]["15M"]["native"]["rank1"]
        # raw 0.52 -> YES == official YES -> correct (old behaviour preserved)
        self.assertEqual((nat["correct"], nat["wrong"]), (1, 0))
        led.close()

    def test_native_ranking_follows_production_rank(self):
        """#1/#2/#3 must follow production's own cross-asset rank, not the raw
        probability's decisiveness."""
        led = self._ledger()
        close = 1_700_000_900.0
        # Three assets in one window. Production rank order is BTC(#1), ETH(#2), SOL(#3),
        # but raw-decisiveness order would be the opposite (SOL most decisive on raw).
        self._record_native(led, "C-BTC", asset="BTC", control_prob_yes=0.51,
                            native_side="YES", native_prob_yes=0.90, native_rank=1, close_time=close)
        self._record_native(led, "C-ETH", asset="ETH", control_prob_yes=0.55,
                            native_side="YES", native_prob_yes=0.70, native_rank=2, close_time=close)
        self._record_native(led, "C-SOL", asset="SOL", control_prob_yes=0.95,
                            native_side="YES", native_prob_yes=0.60, native_rank=3, close_time=close)
        for c in ("C-BTC", "C-ETH", "C-SOL"):
            led.resolve(c, "15M", "YES", model_version=MV)
        win = led.latest_window_cases(model_version=MV, top_k=3)
        native = win["checkpoints"]["15M"]["native"]
        self.assertEqual([a for a, _, _ in native], ["BTC", "ETH", "SOL"],
                         "native picks must be ordered by production rank #1..#3")
        led.close()

    def test_scoreboard_control_accuracy_uses_calibrated(self):
        """scoreboard() 'control' accuracy must reflect the champion's CALIBRATED
        output (what it actually predicts), not the raw probability."""
        led = self._ledger()
        # 4 rows where raw is on the WRONG side but calibrated is on the RIGHT side.
        for i in range(4):
            c = f"C-CAL-{i}"
            self._record_native(led, c, asset="DOGE", control_prob_yes=0.45,
                                native_side="YES", native_prob_yes=0.65, native_rank=1,
                                close_time=1_700_000_900.0 + i)
            led.resolve(c, "15M", "YES", model_version=MV)
        sb = led.scoreboard(model_version=MV)
        # calibrated 0.65 -> YES == YES for all 4 -> 100% control accuracy.
        # (Raw 0.45 -> NO would have given 0%.)
        self.assertEqual(sb["control"]["accuracy"], 1.0)
        led.close()

    def test_backfill_regrades_legacy_rows(self):
        """backfill_native_decisions stamps the production decision onto legacy rows
        and flips a mis-graded result to match production."""
        led = self._ledger()
        # Legacy row: raw 0.52 -> YES; resolves YES so it (wrongly) reads correct.
        led.record(_pred(), asset="DOGE", contract="C-BF", checkpoint="15M",
                   control_prob_yes=0.52, close_time=1_700_000_900.0, model_version=MV)
        led.mark_native_sent("C-BF", "15M", model_version=MV)
        led.resolve("C-BF", "15M", "YES", model_version=MV)
        before = led.ranked_by_checkpoint(model_version=MV)["by_checkpoint"]["15M"]["native"]["rank1"]
        self.assertEqual((before["correct"], before["wrong"]), (1, 0))  # buggy raw grade

        # Production actually sent NO (calibrated 0.48). Backfill from a lookup.
        lookup = {("C-BF", "15M"): ("NO", 0.48, 1)}
        stats = led.backfill_native_decisions(lambda c, cp: lookup.get((c, cp)), model_version=MV)
        self.assertEqual(stats["updated"], 1)

        after = led.ranked_by_checkpoint(model_version=MV)["by_checkpoint"]["15M"]["native"]["rank1"]
        self.assertEqual((after["correct"], after["wrong"]), (0, 1),
                         "after backfill the row is graded by the sent side (NO) -> wrong")
        # Idempotent: a second backfill touches nothing (decision already captured).
        again = led.backfill_native_decisions(lambda c, cp: lookup.get((c, cp)), model_version=MV)
        self.assertEqual(again["updated"], 0)
        led.close()

    def test_challenger_side_unchanged(self):
        """The challenger side is still graded on its own calibrated prob — the fix
        must not touch it."""
        led = self._ledger()
        # Use the challenger's own probability path: record without a native decision
        # and confirm the challenger result is independent of native fields.
        led.record(_pred(), asset="DOGE", contract="C-CH", checkpoint="15M",
                   control_prob_yes=0.5, native_predicted_side="NO", native_prob_yes=0.49,
                   native_rank=1, close_time=1_700_000_900.0, model_version=MV)
        led.resolve("C-CH", "15M", "YES", model_version=MV)
        rbc = led.ranked_by_checkpoint(model_version=MV)
        ch_rank1 = rbc["by_checkpoint"]["15M"]["challenger"]["rank1"]
        # The challenger graded exactly one row (correct or wrong, by its own prob).
        self.assertEqual(ch_rank1["correct"] + ch_rank1["wrong"], 1)
        led.close()


class NativeDecisionPlumbingTest(unittest.TestCase):
    def test_observe_persists_native_decision(self):
        """The runner.observe -> ledger.record path carries the production decision
        end to end, and the report grades by it."""
        tmp = tempfile.mkdtemp()
        cfg = ChallengerConfig().with_overrides(
            enabled=True, db_path=os.path.join(tmp, "shadow.sqlite3"),
            model_version=MV)
        r = ShadowRunner(cfg)
        r.observe(ticker="KXDOGE15M-X", asset="DOGE", checkpoint="15M",
                  created_at=1_700_000_000.0, close_time=1_700_000_900.0,
                  control_prob_yes=0.5215,            # raw -> YES
                  native_predicted_side="NO", native_prob_yes=0.4958, native_rank=1,
                  features={"momentum": 0.1, "flow": -0.05, "book": 0.0},
                  quote={"yes_bid_cents": 49, "yes_ask_cents": 51,
                         "no_bid_cents": 49, "no_ask_cents": 51})
        r.mark_native_sent("KXDOGE15M-X", "15M")
        r.resolve("KXDOGE15M-X", "15M", "YES", resolved_at=1_700_000_900.0)
        rbc = r.ledger.ranked_by_checkpoint(model_version=MV, top_k=3)
        nat = rbc["by_checkpoint"]["15M"]["native"]["rank1"]
        self.assertEqual((nat["correct"], nat["wrong"]), (0, 1))


if __name__ == "__main__":
    unittest.main()
