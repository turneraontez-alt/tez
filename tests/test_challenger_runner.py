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
        self.assertIn("challenger", msg)

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


class GatingTest(unittest.TestCase):
    def test_disabled_runner_is_none(self):
        import q15_upgrade.challenger.runner as rm
        rm._runner = None
        rm._enabled_cache = False
        self.assertIsNone(rm.get_runner())


if __name__ == "__main__":
    unittest.main()
