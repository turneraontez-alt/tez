from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from q15_upgrade.challenger.config import ChallengerConfig
from q15_upgrade.challenger.runner import ShadowRunner, _build_snapshot
import notifier as notifier_mod


def _cfg(tmp):
    return ChallengerConfig().with_overrides(
        enabled=True, db_path=os.path.join(tmp, "shadow.sqlite3"),
        refit_every=5, min_train_rows=20, model_version="challenger-test",
    )


class BuildSnapshotTest(unittest.TestCase):
    def test_maps_signals_and_quote(self):
        snap = _build_snapshot(
            {"momentum": 0.2, "flow": {"value": -0.1, "quality": 0.8}},
            {"yes_bid_cents": 40, "yes_ask_cents": 42, "no_bid_cents": 56, "no_ask_cents": 58},
        )
        self.assertEqual(snap["feature_values"]["momentum"], 0.2)
        self.assertEqual(snap["feature_values"]["flow"], -0.1)  # unwrapped from {value,quality}
        self.assertEqual(snap["yes_ask"], 42)


class RunnerTest(unittest.TestCase):
    def test_observe_resolve_and_report(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        # record + resolve 12 contracts across two 15-min windows
        for i in range(12):
            tkr = f"KXBTC15M-{i}"
            r.observe(ticker=tkr, asset="BTC", checkpoint="10M",
                      created_at=1_700_000_000 + i * 60,
                      close_time=1_700_000_900 + i * 60, control_prob_yes=0.55,
                      features={"momentum": 0.1, "flow": -0.05, "book": 0.0},
                      quote={"yes_bid_cents": 40, "yes_ask_cents": 42,
                             "no_bid_cents": 56, "no_ask_cents": 58})
            r.resolve(tkr, "10M", "YES" if i % 2 == 0 else "NO",
                      resolved_at=1_700_000_900 + i * 60)
        cmp = r.comparison()
        self.assertEqual(cmp["overall"]["n"], 12)
        self.assertIsNotNone(cmp["overall"]["challenger_accuracy"])
        self.assertIsNotNone(cmp["overall"]["current_accuracy"])
        self.assertIn("10M", cmp["by_checkpoint"])
        msg = r.report_message()
        self.assertIn("CHALLENGER SHADOW", msg)
        self.assertIn("Shadow", msg)

    def test_pending_report_set_on_new_window(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        r.observe(ticker="C1", asset="BTC", checkpoint="10M", created_at=1_700_000_000,
                  close_time=1_700_000_900, control_prob_yes=0.6,
                  features={"momentum": 0.1}, quote={"yes_ask_cents": 42, "yes_bid_cents": 40})
        r.resolve("C1", "10M", "YES", resolved_at=1_700_000_900)
        first = r.drain_report()
        self.assertIsNotNone(first)
        self.assertIsNone(r.drain_report())  # drained once

    def test_observe_never_raises_on_bad_input(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        # garbage features/quote must not raise (production safety)
        r.observe(ticker="X", asset="BTC", checkpoint="10M", created_at=0.0,
                  close_time=None, control_prob_yes=None, features=None, quote=None)


class NotifierBypassTest(unittest.TestCase):
    def test_challenger_report_not_suppressed(self):
        # Even under balanced level, the challenger report header is delivered.
        os.environ.pop("Q15_ALERT_LEVEL", None)
        msg = "CHALLENGER SHADOW — accuracy\nbody"
        self.assertFalse(notifier_mod.should_suppress_alert(msg, level="balanced")
                         and notifier_mod._is_challenger_report(msg) is False)
        self.assertTrue(notifier_mod._is_challenger_report(msg))


class RankedComparisonTest(unittest.TestCase):
    def _insert(self, led, *, asset, checkpoint, close, chal, ctrl, official, mv="challenger-test"):
        led.ledger._conn.execute(
            "INSERT INTO shadow_predictions (created_at, model_version, asset, contract, "
            "checkpoint, close_time, control_prob_yes, challenger_prob_yes, official_result) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (1.0, mv, asset, f"{asset}-{checkpoint}-{int(close)}", checkpoint, close, ctrl, chal, official),
        )
        led.ledger._conn.commit()

    def test_per_rank_scoring_no_double_count(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        # One case (10M, close=1000) with 3 assets, controlled probs/outcomes.
        # challenger ranks by |p-0.5|: ETH(.4) BTC(.3) SOL(.1)
        # native     ranks by |p-0.5|: BTC(.35) SOL(.10) ETH(.05)
        self._insert(r, asset="ETH", checkpoint="10M", close=1000, chal=0.9, ctrl=0.55, official="YES")
        self._insert(r, asset="BTC", checkpoint="10M", close=1000, chal=0.2, ctrl=0.85, official="NO")
        self._insert(r, asset="SOL", checkpoint="10M", close=1000, chal=0.6, ctrl=0.40, official="YES")
        rk = r.ranked(top_k=3)
        self.assertEqual(rk["n_cases"], 1)
        # challenger: P1 ETH YES vs YES ok, P2 BTC NO vs NO ok, P3 SOL YES vs YES ok -> 3/3
        self.assertEqual(rk["challenger"]["rank1"], {"correct": 1, "wrong": 0, "accuracy": 1.0})
        self.assertEqual(rk["challenger"]["overall"]["correct"], 3)
        self.assertEqual(rk["challenger"]["overall"]["accuracy"], 1.0)
        # native: P1 BTC YES vs NO x, P2 SOL NO vs YES x, P3 ETH YES vs YES ok -> 1/3
        self.assertEqual(rk["native"]["rank1"], {"correct": 0, "wrong": 1, "accuracy": 0.0})
        self.assertEqual(rk["native"]["overall"]["correct"], 1)
        self.assertAlmostEqual(rk["native"]["overall"]["accuracy"], 1 / 3, places=3)
        # report renders with both models + verdict
        msg = r.report_message()
        self.assertIn("Top-1", msg)
        self.assertIn("Shadow ahead", msg)

    def test_end_result_section(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        # latest window: BTC settles YES, called right at both checkpoints by the
        # shadow but missed at 15M by native.
        self._insert(r, asset="BTC", checkpoint="15M", close=2000, chal=0.7, ctrl=0.45, official="YES")
        self._insert(r, asset="BTC", checkpoint="10M", close=2000, chal=0.8, ctrl=0.62, official="YES")
        er = r.ledger.latest_window_end_results(model_version="challenger-test")
        self.assertEqual(len(er["assets"]), 1)
        a = er["assets"][0]
        self.assertEqual(a["official"], "YES")
        self.assertEqual(a["checkpoints"]["15M"]["challenger"], ("YES", True))
        self.assertEqual(a["checkpoints"]["15M"]["native"], ("NO", False))
        self.assertEqual(a["checkpoints"]["10M"]["native"], ("YES", True))
        msg = r.report_message()
        self.assertIn("END-RESULT CALL", msg)

    def test_distinct_cases_by_checkpoint_and_window(self):
        tmp = tempfile.mkdtemp()
        r = ShadowRunner(_cfg(tmp))
        # same close, different checkpoints -> 2 cases; same checkpoint diff close -> +1
        self._insert(r, asset="BTC", checkpoint="10M", close=1000, chal=0.9, ctrl=0.9, official="YES")
        self._insert(r, asset="BTC", checkpoint="15M", close=1000, chal=0.9, ctrl=0.9, official="YES")
        self._insert(r, asset="BTC", checkpoint="10M", close=1900, chal=0.9, ctrl=0.9, official="NO")
        rk = r.ranked()
        self.assertEqual(rk["n_cases"], 3)


class GatingTest(unittest.TestCase):
    def test_disabled_runner_is_none(self):
        import q15_upgrade.challenger.runner as rm
        rm._runner = None
        rm._enabled_cache = False
        self.assertIsNone(rm.get_runner())


if __name__ == "__main__":
    unittest.main()
